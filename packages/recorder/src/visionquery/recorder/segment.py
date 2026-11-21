"""A Segment on disk, from its first Fragment until it is finished, and after a crash (ADR 0017).

A Segment is one file of a Camera's Footage, in one epoch-aligned window (`window`). Its files,
under the camera's directory, are named for the day and the window it starts in (UTC) and the
id fixed when it opened:

- `<YYYY-MM-DD>/<HHMMSS>-<id>.mp4`    the init, then every Fragment, as ffmpeg wrote them
- `<YYYY-MM-DD>/<HHMMSS>-<id>.frags`  the Fragment index: one JSON line per Fragment, saying
                                      where it ends in the file, when it arrived, how long it
                                      lasts, and (on the first) when the Segment begins, the
                                      window it was cut to, and by how much the clock stepped
                                      just before it, if it did (`stepped_by`)
- `<YYYY-MM-DD>/<HHMMSS>-<id>.closed` written once, when the Segment is finished: why, over
                                      what span, how many Fragments, whether its times
                                      stand on a synchronised NTP reading, and `stepped_by`

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

While a Segment is written, an empty note named for it lies in `<camera>/.open/`, made before
anything else of it and removed once it is finished. After a crash, `recover` reads the notes
(never walking the Segments there ever were, so a start costs what is unfinished, not what is
kept), truncates each Segment they name to its last whole Fragment and finishes it `recovered`,
with the end it really reached, and writes that down so a second start reaches the same answer.
Each is recovered by itself: one that cannot be is logged, counted and moved to
`<footage>/.quarantine/`, never stopping the rest, and files no note names are never touched.

Adapted from phyOS's Photon recorder (`adapters.photon.segment`), without signing or shipping.
"""

import contextlib
import json
import logging
import math
import os
import re
import shutil
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast, get_args
from uuid import UUID, uuid4

from visionquery.recorder.mp4 import Fragment, Init, rebase

__all__ = [
    "SLACK",
    "Reason",
    "Segment",
    "SegmentFile",
    "recover",
    "segments",
    "span_problem",
    "window",
]

#: Why a Segment was finished: at its window's end; when the camera's stream went away; when
#: the recorder stopped recording the camera (told to stop, or the camera was removed); or, by
#: the recorder after a crash, from what it found on disk.
type Reason = Literal["rolled", "stream_ended", "stopped", "recovered"]

_log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Where a camera's directory notes each Segment being written, one empty file named for it
#: (`<day>_<HHMMSS>-<id>`), so that a start finds what a crash left unfinished without walking
#: every Segment there ever was.
_OPEN = ".open"
#: Where `recover` moves a Segment it cannot recover, under the directory it recovers, so that
#: no later start meets it again and nothing is deleted that might still be read.
_QUARANTINE = ".quarantine"
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_STEM = re.compile(r"\d{6}-[0-9a-f]{32}")

#: How far a Segment's times may lie outside its window, besides its last Fragment's own length
#: (a Fragment that began inside the window ends any time after it, however long a keyframe
#: interval the camera has): a stall before the Segment's first Fragment, within what a
#: connection tolerates (ten seconds), and a slew. A Segment further out than this stands on a
#: clock that stepped, or on a bug: it is finished all the same, `verified: false`, with an
#: ERROR logged, because refusing it would stop the camera recording over a fact about its time.
SLACK = timedelta(seconds=30)


def span_problem(
    time_from: datetime,
    time_to: datetime,
    *,
    last_seconds: float,
    window: tuple[datetime, datetime] | None,
    slack: timedelta = SLACK,
) -> str | None:
    """Why a Segment's span cannot be true, or None: the one predicate `close` and `recover`
    both judge by. A Segment may end after its window by its own last Fragment's length plus
    `slack`, and begin before it by `slack`."""
    if time_to < time_from:
        return f"it would end ({time_to}) before it begins ({time_from})"
    if window is not None:
        start, end = window
        if time_from < start - slack or time_to > end + timedelta(seconds=last_seconds) + slack:
            return f"it spans {time_from} to {time_to}, outside its window {start} to {end}"
    return None


def _judged(
    path: Path,
    time_from: datetime,
    time_to: datetime,
    *,
    last_seconds: float,
    window: tuple[datetime, datetime] | None,
    slack: timedelta,
    verified: bool,
) -> bool:
    """Whether the Segment's times can still be vouched for. A span no clock could have recorded
    is logged as an ERROR and never raised: it would stop the camera recording."""
    problem = span_problem(
        time_from, time_to, last_seconds=last_seconds, window=window, slack=slack
    )
    if problem is None:
        return verified
    _log.error(
        "Segment %s is kept, but not verified: %s",
        path.name,
        problem,
        extra={"segment": str(path)},
    )
    return False


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
    #: By how many seconds the clock stepped just before it began, when it did (the Segment
    #: before it, on the old time, may overlap it or leave a gap), so that the index can tell
    #: that overlap or gap from a fault or an outage; None when it did not.
    stepped_by: float | None = None


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
        self,
        path: Path,
        segment_id: UUID,
        not_before: datetime | None,
        begins_at: datetime | None,
        verified: bool,
        window: tuple[datetime, datetime] | None,
        slack: timedelta,
        marker: Path,
        stepped_by: float | None,
    ) -> None:
        self.path = path
        self._marker = marker
        self.segment_id = segment_id
        self._not_before = not_before
        self._begins_at = begins_at
        self._verified = verified
        self._window = window
        self._slack = slack
        self._stepped_by = stepped_by
        self._last_seconds = 0.0
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
        begins_at: datetime | None = None,
        window_end: datetime | None = None,
        slack: timedelta = SLACK,
        stepped_by: float | None = None,
    ) -> SegmentFile:
        """Fix the Segment's identity and write its init in `directory`, the camera's.

        `window_start` names the file. `not_before` is the earliest instant the Segment can
        honestly begin: when its connection opened, or when the Segment before it on that
        connection ended. `begins_at`, for a Segment whose media runs on from the one before it,
        is exactly when it begins: where that one ended. `verified` says whether everything the
        Segment's beginning rests on stood on NTP, and each Fragment appended can only take it
        away. `window_end`, when given, is the end of the window `window_start` begins: the
        Segment is then finished `verified: false`, and an ERROR logged, if its times lie outside
        the window by more than its last Fragment's length and `slack`. `stepped_by` is the
        shift, in seconds, of a clock that stepped just before the Segment began, and is written
        down with it.
        """
        start = window_start.astimezone(UTC)
        day = directory / start.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        segment_id = uuid4()
        stem = f"{start.strftime('%H%M%S')}-{segment_id.hex}"
        # Noted before anything else is written, so no Segment can exist that a start would
        # not find.
        marker = directory / _OPEN / f"{day.name}_{stem}"
        marker.parent.mkdir(exist_ok=True)
        marker.touch(exist_ok=False)
        _fsync_dir(marker.parent)
        segment = cls(
            day / f"{stem}.mp4",
            segment_id,
            not_before,
            begins_at,
            verified,
            None if window_end is None else (start, window_end),
            slack,
            marker,
            stepped_by,
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

    @property
    def closed(self) -> bool:
        """Whether no file of the Segment is held open any more."""
        return self._data.closed and self._lines.closed

    @property
    def verified(self) -> bool:
        """Whether every time the Segment rests on so far stood on NTP."""
        return self._verified

    def append(self, fragment: Fragment, *, arrived_at: datetime, verified: bool = True) -> None:
        """Write one Fragment, then the index line that vouches for it; kept once this returns.

        `verified` says whether `arrived_at` stood on NTP. A Segment is verified only if every
        time it holds was, so a Fragment that was not makes it not, from its line on."""
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
        self._verified = self._verified and verified
        if self._time_from is None:
            begins = arrived_at - timedelta(seconds=fragment.seconds)
            if self._not_before is not None:
                begins = max(begins, self._not_before)
            if self._begins_at is not None:
                begins = self._begins_at
            # Never after its own end: a pin a little past the arrival (a slew within the
            # tolerance) gives way to the arrival.
            line["from"] = min(begins, arrived_at).isoformat()
            line["verified"] = self._verified
            if self._window is not None:
                line["window"] = [value.isoformat() for value in self._window]
            if self._stepped_by is not None:
                line["stepped_by"] = self._stepped_by
        elif not verified:
            line["verified"] = False
        # The one check `recover` also runs: a line it would refuse is never written.
        _check_line(line, first=self._time_from is None, number=self._fragments + 1)
        self._lines.write(json.dumps(line, separators=(",", ":")) + "\n")
        self._lines.flush()
        os.fsync(self._lines.fileno())
        if self._time_from is None:
            self._time_from = datetime.fromisoformat(line["from"])
        self._time_to = arrived_at
        self._last_seconds = fragment.seconds
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
            self._forget()
            return None
        assert self._time_from is not None
        assert self._time_to is not None
        verified = _judged(
            self.path,
            self._time_from,
            self._time_to,
            last_seconds=self._last_seconds,
            window=self._window,
            slack=self._slack,
            verified=self._verified,
        )
        segment = Segment(
            segment_id=self.segment_id,
            path=self.path,
            time_from=self._time_from,
            time_to=self._time_to,
            reason=reason,
            fragments=self._fragments,
            verified=verified,
            stepped_by=self._stepped_by,
        )
        _mark(segment)
        self._forget()
        return segment

    def abandon(self) -> Segment | None:
        """Give up on the Segment because writing it failed (the disk is full, a file vanished):
        close its handles and finish it `recovered`, from the Fragments its index vouches for,
        as a start after a crash would. What cannot be finished now is left for that start."""
        for handle in (self._data, self._lines):
            with contextlib.suppress(OSError):
                handle.close()
        try:
            done = _recover(self.path)
        except Exception as error:
            _log.error(
                "Segment %s could not be finished after a failure to write it; the next start "
                "will find it: %s: %s",
                self.path,
                type(error).__name__,
                error,
                extra={"segment": str(self.path)},
            )
            return None
        self._forget()
        return done

    def _forget(self) -> None:
        """The Segment is finished (or nothing of it remains): it is no longer being written."""
        self._marker.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            _fsync_dir(self._marker.parent)


def _mark(segment: Segment) -> None:
    done: dict[str, Any] = {
        "reason": segment.reason,
        "time_from": segment.time_from.isoformat(),
        "time_to": segment.time_to.isoformat(),
        "fragments": segment.fragments,
        "verified": segment.verified,
    }
    if segment.stepped_by is not None:
        done["stepped_by"] = segment.stepped_by
    _write_atomically(segment.path.with_suffix(".closed"), json.dumps(done))


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
        stepped_by=raw.get("stepped_by"),
    )


def _init_size(data: Path) -> int:
    """Where the first Fragment begins: the init's size, read from the boxes' own headers."""
    at = 0
    with data.open("rb") as file:
        while header := file.read(8):
            if len(header) < 8:
                break
            size, kind = struct.unpack(">I4s", header)
            if kind == b"moof":
                return at
            if size == 1:
                (size,) = struct.unpack(">Q", file.read(8).ljust(8, b"\0"))
            if size < 8:
                break
            at += size
            file.seek(at)
    return at  # no Fragment yet: everything is init


def _instant(raw: object) -> datetime:
    value = datetime.fromisoformat(str(raw))
    if value.tzinfo is None:
        raise ValueError(f"{raw!r} has no time zone")
    return value


def _timed(entry: dict[str, Any], key: str) -> datetime:
    return _instant(entry[key])


def _finite(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _check_line(entry: dict[str, Any], *, first: bool, number: int) -> None:
    """Raise ValueError unless `entry` is an index line the writer could have written.

    The one check of a line, run by `SegmentFile.append` before it writes the line and by
    `_validate` on every line `recover` reads, so recovery cannot refuse what the writer wrote.
    `seconds` may be zero (a Fragment can state no duration) but never negative or not finite.
    Only the first line carries `from`, `window` and `stepped_by`, and `verified` is a bool
    there and only ever `false` after it."""
    end, seconds = entry["end"], entry["seconds"]
    if not isinstance(end, int) or isinstance(end, bool):
        raise ValueError(f"line {number}: end {end!r} is not a whole number")
    if not _finite(seconds) or seconds < 0:
        raise ValueError(f"line {number}: a Fragment of {seconds!r} seconds")
    arrived = _timed(entry, "arrived")
    if first:
        verified = entry.get("verified")
        if not isinstance(verified, bool):
            raise ValueError(f"line {number}: verified {verified!r} is not a bool")
        begins = _timed(entry, "from")
        if begins > arrived:
            raise ValueError(f"the Segment begins ({begins}) after its first Fragment arrived")
        if "stepped_by" in entry and not _finite(entry["stepped_by"]):
            raise ValueError(f"line {number}: stepped_by {entry['stepped_by']!r} is not a number")
    else:
        if "verified" in entry and entry["verified"] is not False:
            raise ValueError(f"line {number}: verified {entry['verified']!r} after the first line")
        for key in ("from", "window", "stepped_by"):
            if key in entry:
                raise ValueError(f"line {number}: {key} belongs to the first line")
    if "window" in entry:
        window: object = entry["window"]
        if not isinstance(window, list) or len(cast("list[object]", window)) != 2:
            raise ValueError(f"window {window!r} is not a start and an end")
        pair = cast("list[object]", window)
        start, end_of_window = (_instant(pair[0]), _instant(pair[1]))
        # Whether the Segment lies inside it is `span_problem`'s to judge, not damage: a stall
        # before its first Fragment puts the Segment outside its window and it is still whole.
        if not start < end_of_window:
            raise ValueError(f"window {start} to {end_of_window} ends before it begins")


def _pair_ends(data: Path, init_size: int) -> list[int]:
    """Where each whole `moof` and `mdat` pair of the media ends, from the init on.

    A Fragment is exactly such a pair, so these are the only places an index may say a
    Fragment ends. The walk stops at the first box that is not the next of the pair, or that
    the file does not hold whole: what follows is a tail a crash tore."""
    ends: list[int] = []
    size_on_disk = data.stat().st_size
    at, moof_seen = init_size, False
    with data.open("rb") as file:
        while at + 8 <= size_on_disk:
            file.seek(at)
            size, kind = struct.unpack(">I4s", file.read(8))
            header = 8
            if size == 1:
                (size,) = struct.unpack(">Q", file.read(8).ljust(8, b"\0"))
                header = 16
            if size < header or at + size > size_on_disk:
                break
            if kind == b"moof" and not moof_seen:
                moof_seen = True
            elif kind == b"mdat" and moof_seen:
                moof_seen = False
                ends.append(at + size)
            else:
                break
            at += size
    return ends


def _validate(lines: list[dict[str, Any]], *, data: Path, init_size: int) -> None:
    """Raise ValueError unless the index says only what the writer could have written.

    A writer fsyncs a Fragment's bytes and then its line, in order, so each line's `end` is the
    end of the next whole `moof` and `mdat` pair of the media, its arrivals never go back, and
    each line passes `_check_line`. An index that says otherwise is damaged, and nothing it says
    can be trusted: `recover` quarantines it, with the media untouched, rather than cut the
    file at an `end` or publish a span it names. Only bytes after the last `end` named are a
    torn tail that `recover` may cut."""
    ends = _pair_ends(data, init_size) if lines else []
    if len(lines) > len(ends):
        raise ValueError(f"{len(lines)} lines vouch for {len(ends)} whole Fragments")
    previous_arrived: datetime | None = None
    for number, entry in enumerate(lines, 1):
        _check_line(entry, first=number == 1, number=number)
        if entry["end"] != ends[number - 1]:
            raise ValueError(
                f"line {number}: end {entry['end']} is not where Fragment {number} ends "
                f"({ends[number - 1]})"
            )
        arrived = _timed(entry, "arrived")
        if previous_arrived is not None and arrived < previous_arrived:
            raise ValueError(f"line {number}: arrived {arrived} before {previous_arrived}")
        previous_arrived = arrived


def _recover(path: Path) -> Segment | None:
    """Truncate an unfinished Segment to its last vouched-for Fragment and finish it."""
    data, index, closed = _files(path)
    # What an interrupted atomic write left, by names that are this Segment's own.
    for partial in (index, closed):
        partial.with_suffix(partial.suffix + ".tmp").unlink(missing_ok=True)
    lines: list[dict[str, Any]] = []
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines(keepends=True):
            if not line.endswith("\n"):
                break  # a line the crash interrupted vouches for nothing
            lines.append(json.loads(line))
    if not data.exists() or data.stat().st_size == 0:
        lines = []  # no media: there is nothing a line could vouch for
    _validate(lines, data=data, init_size=_init_size(data) if lines else 0)
    if not lines:
        for file in _files(path):
            file.unlink(missing_ok=True)
        _fsync_dir(path.parent)
        return None

    first = lines[0]
    time_from = datetime.fromisoformat(first["from"])
    time_to = datetime.fromisoformat(lines[-1]["arrived"])
    recorded_window = first.get("window")
    verified = _judged(
        path,
        time_from,
        time_to,
        last_seconds=lines[-1]["seconds"],
        window=None
        if recorded_window is None
        else (
            datetime.fromisoformat(recorded_window[0]),
            datetime.fromisoformat(recorded_window[1]),
        ),
        slack=SLACK,
        verified=first["verified"] and all(entry.get("verified", True) for entry in lines),
    )
    segment = Segment(
        segment_id=_segment_id(path),
        path=path,
        time_from=time_from,
        time_to=time_to,
        reason="recovered",
        fragments=len(lines),
        verified=verified,
        stepped_by=first.get("stepped_by"),
    )
    with data.open("r+b") as file:
        file.truncate(lines[-1]["end"])
        os.fsync(file.fileno())
    # The index loses any line that named bytes now cut off, so it matches the file.
    _write_atomically(
        index, "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in lines)
    )
    _mark(segment)
    return segment


def _visible(directory: Path) -> bool:
    return not any(part.startswith(".") for part in directory.parts)


def _segment_files(directory: Path) -> list[Path]:
    """Every Segment's media under `directory`, the quarantine and the notes excepted."""
    if not directory.exists():
        return []
    return [
        path for path in directory.rglob("*.mp4") if _visible(path.relative_to(directory).parent)
    ]


def _open_markers(directory: Path) -> list[Path]:
    """The notes of Segments being written: in `directory` itself, as a camera's directory has
    them, and in each directory beneath it, as the Footage directory's cameras do."""
    notes: list[Path] = []
    candidates = [directory]
    with contextlib.suppress(FileNotFoundError):
        candidates += sorted(
            child
            for child in directory.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        )
    for candidate in candidates:
        with contextlib.suppress(FileNotFoundError):
            notes += sorted((candidate / _OPEN).iterdir())
    return notes


def _segment_of(marker: Path) -> Path:
    """The media a note names, refusing any name that is not a Segment's."""
    day, _, stem = marker.name.partition("_")
    if not (_DAY.fullmatch(day) and _STEM.fullmatch(stem)):
        raise ValueError(f"{marker.name!r} is not the name of a Segment's note")
    return marker.parent.parent / day / f"{stem}.mp4"


def _quarantine(directory: Path, marker: Path, path: Path | None) -> None:
    """Move what is left of a Segment, and its note, under `directory`'s quarantine."""
    here = directory / _QUARANTINE
    moving = [marker] if path is None else [marker, *_files(path)]
    for source in moving:
        if not source.exists():
            continue
        target = here / source.parent.relative_to(directory) / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, target)
    with contextlib.suppress(OSError):
        _fsync_dir(marker.parent)


def recover(
    directory: Path, *, on_quarantine: Callable[[Path, Exception], None] | None = None
) -> list[Segment]:
    """Finish every Segment under `directory` that a crash left unfinished, oldest first.

    Only while nothing is writing under `directory`: a Segment being written is unfinished too.
    Each Segment is recovered by itself: one that cannot be (a damaged index, a name that is not
    a Segment's, a file that cannot be read) is logged as an ERROR naming it, told to
    `on_quarantine`, and moved to `<directory>/.quarantine/`, where no later start meets it, and
    never stops the others, or any camera from recording.
    """
    found: list[Segment] = []
    for marker in _open_markers(directory):
        path: Path | None = None
        try:
            path = _segment_of(marker)
            if not path.with_suffix(".closed").exists() and (segment := _recover(path)):
                found.append(segment)
            marker.unlink(missing_ok=True)
        except Exception as error:
            named = marker if path is None else path
            _log.error(
                "Segment %s could not be recovered and is moved to the quarantine, where it is "
                "left as it is: %s: %s",
                named,
                type(error).__name__,
                error,
                extra={"segment": str(named)},
            )
            moved = True
            try:
                _quarantine(directory, marker, path)
            except OSError as failure:
                moved = False
                _log.error(
                    "Segment %s could not be moved to the quarantine either, and stays where "
                    "it is (%s); the next start tries again",
                    named,
                    failure.strerror or type(failure).__name__,
                    extra={"segment": str(named)},
                )
            if moved and on_quarantine is not None:
                on_quarantine(named, error)
    return sorted(found, key=lambda segment: segment.time_from)


def segments(directory: Path) -> list[Segment]:
    """Every finished Segment under `directory`, oldest first."""
    found = [
        _read_closed(path)
        for path in _segment_files(directory)
        if path.with_suffix(".closed").exists()
    ]
    return sorted(found, key=lambda segment: segment.time_from)
