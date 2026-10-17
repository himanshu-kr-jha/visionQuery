"""A Segment on disk, from its first Fragment until it is finished, and after a crash.

A Segment is one file of a Camera's Footage, in one epoch-aligned window (`window`). Its files,
under the camera's directory, are named for the day and the window it starts in (UTC) and the
id fixed when it opened:

- `<YYYY-MM-DD>/<HHMMSS>-<id>.mp4`    the init, then every Fragment, as ffmpeg wrote them
- `<YYYY-MM-DD>/<HHMMSS>-<id>.frags`  the Fragment index: one JSON line per Fragment, saying
                                      where it ends in the file, when it arrived, how long it
                                      lasts, and (on the first) when the Segment begins
- `<YYYY-MM-DD>/<HHMMSS>-<id>.closed` written once, when the Segment is finished: why, over
                                      what span, how many Fragments, and whether its times
                                      stand on a synchronised NTP reading

**The order of writes is the guarantee.** A Fragment's bytes are fsynced before its index line
is, so every line names bytes that exist, and a crash costs at most the Fragment being written.
The file is only ever appended to and, after a crash, truncated back to the last Fragment a line
vouches for, so it always ends on a whole Fragment and plays.

**Times are the timebase's, at arrival** (`visionquery.recorder.timebase`), never the file's.
A Fragment is written when the keyframe after it arrives, so it arrives at its end. A Segment
ends at its last Fragment's arrival and begins at its first Fragment's arrival less that
Fragment's own length, read from the media, but never before its connection opened or the
Segment before it ended (`not_before`). A stall in the stream therefore stays inside the span
rather than being squeezed out of it, and the span never claims more than was received.

**The only byte that changes is each Fragment's decode time** (`mp4.rebase`), so that every
file's own timeline starts at zero: copy-only, never a transcode.

After a crash, `recover` finds every Segment that was never finished, truncates it to its last
whole Fragment and finishes it `recovered`, with the end it really reached, and writes that down
so a second start reaches the same answer.

Adapted from phyOS's Photon recorder (`adapters.photon.segment`), without signing or shipping.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast, get_args
from uuid import UUID, uuid4

from visionquery.recorder.mp4 import Fragment, Init, rebase

__all__ = ["Reason", "Segment", "SegmentFile", "recover", "segments", "window"]

#: Why a Segment was finished: at its window's end; when the camera's stream went away; when
#: the recorder stopped recording the camera (told to stop, or the camera was removed); or, by
#: the recorder after a crash, from what it found on disk.
type Reason = Literal["rolled", "stream_ended", "stopped", "recovered"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def window(at: datetime, length: timedelta) -> tuple[datetime, datetime]:
    """The window of `length` holding `at`, aligned to the epoch, in UTC.

    Aligned to the epoch rather than to when recording began, so every camera's Segments cut at
    the same instants without coordinating, and a restart does not shift them.
    """
    step = round(length.total_seconds() * 1_000_000)
    elapsed = round((at - _EPOCH).total_seconds() * 1_000_000)
    start = _EPOCH + timedelta(microseconds=(elapsed // step) * step)
    return start, start + length


@dataclass(frozen=True)
class Segment:
    """A finished Segment: no longer written, and never written again."""

    segment_id: UUID
    path: Path
    time_from: datetime
    time_to: datetime
    reason: Reason
    fragments: int
    #: Whether its times stand on a synchronised NTP reading, rather than the bare wall clock.
    verified: bool


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomically(path: Path, text: str) -> None:
    partial = path.with_suffix(path.suffix + ".tmp")
    with partial.open("w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    partial.replace(path)
    _fsync_dir(path.parent)


def _files(path: Path) -> tuple[Path, Path, Path]:
    return path, path.with_suffix(".frags"), path.with_suffix(".closed")


class SegmentFile:
    """One Segment being written."""

    def __init__(
        self, path: Path, segment_id: UUID, not_before: datetime | None, verified: bool
    ) -> None:
        self.path = path
        self.segment_id = segment_id
        self._not_before = not_before
        self._verified = verified
        self._fragments = 0
        self._time_from: datetime | None = None
        self._time_to: datetime | None = None
        self._base: dict[int, int] | None = None
        self._data = path.open("ab")
        self._lines = path.with_suffix(".frags").open("a", encoding="utf-8")

    @classmethod
    def open(
        cls,
        directory: Path,
        init: Init,
        *,
        window_start: datetime,
        not_before: datetime | None,
        verified: bool,
    ) -> SegmentFile:
        """Fix the Segment's identity and write its init in `directory`, the camera's.

        `window_start` names the file. `not_before` is the earliest instant the Segment can
        honestly begin: when its connection opened, or when the Segment before it on that
        connection ended. `verified` says whether the timebase stood on NTP when it opened.
        """
        start = window_start.astimezone(UTC)
        day = directory / start.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        segment_id = uuid4()
        segment = cls(
            day / f"{start.strftime('%H%M%S')}-{segment_id.hex}.mp4",
            segment_id,
            not_before,
            verified,
        )
        segment._data.write(init.data)
        segment._data.flush()
        os.fsync(segment._data.fileno())
        _fsync_dir(day)
        _fsync_dir(directory)
        return segment

    @property
    def fragments(self) -> int:
        return self._fragments

    def append(self, fragment: Fragment, *, arrived_at: datetime) -> None:
        """Write one Fragment, then the index line that vouches for it; kept once this returns."""
        if self._base is None:
            self._base = dict(fragment.starts)
        fragment = rebase(fragment, self._base)
        self._data.write(fragment.data)
        self._data.flush()
        os.fsync(self._data.fileno())
        line: dict[str, Any] = {
            "end": self._data.tell(),
            "arrived": arrived_at.isoformat(),
            "seconds": fragment.seconds,
        }
        if self._time_from is None:
            begins = arrived_at - timedelta(seconds=fragment.seconds)
            if self._not_before is not None:
                begins = max(begins, self._not_before)
            line["from"] = begins.isoformat()
            line["verified"] = self._verified
        self._lines.write(json.dumps(line, separators=(",", ":")) + "\n")
        self._lines.flush()
        os.fsync(self._lines.fileno())
        if self._time_from is None:
            self._time_from = datetime.fromisoformat(line["from"])
        self._time_to = arrived_at
        self._fragments += 1

    def close(self, reason: Reason) -> Segment | None:
        """Stop writing and say why. A Segment with no Fragment in it is not Footage."""
        if reason not in get_args(Reason.__value__):
            raise ValueError(f"{reason!r} is not in the closed set of reasons a Segment ends")
        self._data.close()
        self._lines.close()
        if self._fragments == 0:
            for file in _files(self.path):
                file.unlink(missing_ok=True)
            return None
        assert self._time_from is not None
        assert self._time_to is not None
        segment = Segment(
            segment_id=self.segment_id,
            path=self.path,
            time_from=self._time_from,
            time_to=self._time_to,
            reason=reason,
            fragments=self._fragments,
            verified=self._verified,
        )
        _mark(segment)
        return segment


def _mark(segment: Segment) -> None:
    _write_atomically(
        segment.path.with_suffix(".closed"),
        json.dumps(
            {
                "reason": segment.reason,
                "time_from": segment.time_from.isoformat(),
                "time_to": segment.time_to.isoformat(),
                "fragments": segment.fragments,
                "verified": segment.verified,
            }
        ),
    )


def _segment_id(path: Path) -> UUID:
    return UUID(path.stem.rpartition("-")[2])


def _read_closed(path: Path) -> Segment:
    raw = json.loads(path.with_suffix(".closed").read_text(encoding="utf-8"))
    return Segment(
        segment_id=_segment_id(path),
        path=path,
        time_from=datetime.fromisoformat(raw["time_from"]),
        time_to=datetime.fromisoformat(raw["time_to"]),
        reason=cast(Reason, raw["reason"]),
        fragments=raw["fragments"],
        verified=raw["verified"],
    )


def _recover(path: Path) -> Segment | None:
    """Truncate an unfinished Segment to its last vouched-for Fragment and finish it."""
    data, index, _ = _files(path)
    lines: list[dict[str, Any]] = []
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines(keepends=True):
            if not line.endswith("\n"):
                break  # a line the crash interrupted vouches for nothing
            lines.append(json.loads(line))
    size = data.stat().st_size
    lines = [entry for entry in lines if entry["end"] <= size]
    if not lines:
        for file in _files(path):
            file.unlink(missing_ok=True)
        _fsync_dir(path.parent)
        return None

    with data.open("r+b") as file:
        file.truncate(lines[-1]["end"])
        os.fsync(file.fileno())
    # The index loses any line that named bytes now cut off, so it matches the file.
    _write_atomically(
        index, "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in lines)
    )
    first = lines[0]
    segment = Segment(
        segment_id=_segment_id(path),
        path=path,
        time_from=datetime.fromisoformat(first["from"]),
        time_to=datetime.fromisoformat(lines[-1]["arrived"]),
        reason="recovered",
        fragments=len(lines),
        verified=first["verified"],
    )
    _mark(segment)
    return segment


def _segment_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return list(directory.rglob("*.mp4"))


def recover(directory: Path) -> list[Segment]:
    """Finish every Segment under `directory` that a crash left unfinished, oldest first.

    Only while nothing is writing under `directory`: a Segment being written is unfinished too.
    """
    found: list[Segment] = []
    for path in _segment_files(directory):
        if not path.with_suffix(".closed").exists() and (segment := _recover(path)) is not None:
            found.append(segment)
    return sorted(found, key=lambda segment: segment.time_from)


def segments(directory: Path) -> list[Segment]:
    """Every finished Segment under `directory`, oldest first."""
    found = [
        _read_closed(path)
        for path in _segment_files(directory)
        if path.with_suffix(".closed").exists()
    ]
    return sorted(found, key=lambda segment: segment.time_from)
