"""The Footage segment index: every finished Segment, by camera and the span of time it covers.

The recorder writes Footage to disk as Segments (ADR 0017) and indexes each one here once it is
finished, so that any instant on any camera resolves to the Segment holding it and how far into
it the instant is (`SegmentIndex.find`), or to nothing when no Segment holds it: the camera was
away, the recorder was down, or the Footage was never recorded.

**A Segment is keyed on its span, never on its file name.** Its times are the recorder's, on its
NTP-anchored clock, and it covers `[time_from, time_to)`: its start and not its end, where the
next one on the same connection begins. One camera's Segments never overlap, so an instant is
held by at most one. TimescaleDB holds no exclusion constraint, so `index` checks it under a
per-camera advisory lock, and two indexers at once cannot both add Segments that overlap. A
Segment is at most `MAX_SPAN` long, which bounds every lookup to two days' partitions.

**Indexing is idempotent.** Indexing a Segment already indexed, with the same facts, changes
nothing; with other facts it is refused (`SegmentConflictError`), because a Segment is finished
once and never changes, so two accounts of one cannot both be true and the index does not
guess which is. A Segment that overlaps another of its camera's, or whose camera the registry
never had, is refused too, and so is one that cannot be Footage at all (`InvalidSegmentError`),
before the database sees it. Every refusal is a `SegmentRefusedError`, which indexing again
will not change; a `SegmentIndexError` is the database failing, which it may.

**The offset is on the Deployment's clock**: how long after the Segment began the instant is.
Within the file it is the same but where the stream stalled, which only the Segment's Fragment
index (`.frags`, beside the file) can tell; playback maps it through that.

`path` is relative to the Footage directory, never absolute, because each process mounts that
directory where it likes. The index holds no Footage, only where it is, so it holds no Visual
Data (ADR 0002) and nothing secret.

The index works on the connection it is given, as the camera registry does: outside a
transaction each operation commits on its own; inside one the caller opened, each runs in a
savepoint and is kept or rolled back with the caller's transaction.
"""

import builtins
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Literal, LiteralString, get_args
from uuid import UUID

import psycopg

__all__ = [
    "MAX_SPAN",
    "FootageError",
    "IndexedSegment",
    "Instant",
    "InvalidSegmentError",
    "Reason",
    "SegmentConflictError",
    "SegmentIndex",
    "SegmentIndexError",
    "SegmentOverlapError",
    "SegmentRefusedError",
    "UnknownCameraError",
]

#: Why a Segment was finished: at its window's end; when the camera's stream went away; when
#: the recorder stopped recording the camera; or, by the recorder after a crash, from what it
#: found on disk.
type Reason = Literal["rolled", "stream_ended", "stopped", "recovered"]

#: The longest Segment the index accepts. A Segment's window is at most an hour, so only a
#: Segment whose times are wrong is longer; the bound keeps every lookup to two days' partitions.
MAX_SPAN = timedelta(days=1)

#: The first key of the advisory locks `index` takes, one per camera (the second key).
_LOCK_SPACE = int.from_bytes(b"vqsg", signed=True)


class FootageError(Exception):
    """Something the segment index refused or could not do."""


class SegmentRefusedError(FootageError):
    """A Segment the index will not hold, however often it is offered."""


class InvalidSegmentError(SegmentRefusedError, ValueError):
    """A Segment that cannot be Footage: its span, path, Fragments or reason are wrong."""


class SegmentOverlapError(SegmentRefusedError):
    """A Segment that overlaps another of its camera's, which holds the instants it would."""

    def __init__(self, segment: IndexedSegment, holder: IndexedSegment) -> None:
        self.segment = segment
        self.holder = holder
        super().__init__(
            f"Segment {segment.segment_id} of camera {segment.camera_id} "
            f"({segment.time_from.isoformat()} to {segment.time_to.isoformat()}) overlaps "
            f"Segment {holder.segment_id} ({holder.time_from.isoformat()} to "
            f"{holder.time_to.isoformat()}), which is indexed"
        )


class SegmentConflictError(SegmentRefusedError):
    """A Segment already indexed with other facts: a Segment never changes once finished."""

    def __init__(self, segment: IndexedSegment, indexed: IndexedSegment) -> None:
        self.segment = segment
        self.indexed = indexed
        differ = [
            name
            for name in (
                "camera_id", "time_from", "time_to", "path", "reason", "fragments", "verified",
            )
            if getattr(segment, name) != getattr(indexed, name)
        ]  # fmt: skip
        super().__init__(
            f"Segment {segment.segment_id} is already indexed, with another "
            f"{', '.join(differ)}; the one indexed is kept"
        )


class UnknownCameraError(SegmentRefusedError, LookupError):
    """A Segment of a camera the registry never had."""

    def __init__(self, camera_id: int) -> None:
        self.camera_id = camera_id
        super().__init__(f"there is no camera {camera_id}, so its Footage cannot be indexed")


class SegmentIndexError(FootageError):
    """The database failed; named by the class of the driver's error, never by its text."""


@dataclass(frozen=True)
class IndexedSegment:
    """One finished Segment of a camera's Footage, as the index holds it."""

    camera_id: int
    #: Fixed when the recorder opened the Segment, and part of its file's name.
    segment_id: UUID
    #: When it begins and ends, on the recorder's NTP-anchored clock; it holds
    #: `[time_from, time_to)`.
    time_from: datetime
    time_to: datetime
    #: Its file, relative to the Footage directory.
    path: PurePosixPath
    reason: Reason
    #: How many Fragments it holds.
    fragments: int
    #: Whether its times stand on a synchronised NTP reading, rather than the bare wall clock.
    verified: bool


@dataclass(frozen=True)
class Instant:
    """Where an instant of a camera's Footage is: the Segment holding it, and how long after
    the Segment began it is."""

    segment: IndexedSegment
    offset: timedelta


_COLUMNS: LiteralString = (
    "camera_id, segment_id, time_from, time_to, path, reason, fragments, verified"
)


def _segment(row: tuple[Any, ...]) -> IndexedSegment:
    camera_id, segment_id, time_from, time_to, path, reason, fragments, verified = row
    return IndexedSegment(
        camera_id, segment_id, time_from, time_to, PurePosixPath(path), reason, fragments, verified
    )


def _aware(at: datetime, what: str) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise InvalidSegmentError(f"{what} has no time zone; Footage is timed in UTC")


def _check(segment: IndexedSegment) -> None:
    """Refuse a Segment that cannot be Footage, saying why, before the database sees it."""
    _aware(segment.time_from, "the Segment's start")
    _aware(segment.time_to, "the Segment's end")
    if segment.time_to <= segment.time_from:
        raise InvalidSegmentError(
            f"Segment {segment.segment_id} ends ({segment.time_to.isoformat()}) no later than "
            f"it begins ({segment.time_from.isoformat()})"
        )
    if segment.time_to - segment.time_from > MAX_SPAN:
        raise InvalidSegmentError(
            f"Segment {segment.segment_id} is longer than {MAX_SPAN}, which no Segment is"
        )
    if segment.fragments < 1:
        raise InvalidSegmentError(f"Segment {segment.segment_id} holds no Fragment")
    if segment.reason not in get_args(Reason.__value__):
        raise InvalidSegmentError(
            f"{segment.reason!r} is not in the closed set of reasons a Segment ends"
        )
    path = segment.path
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.suffix != ".mp4"
        or path.name == ".mp4"
    ):
        raise InvalidSegmentError(
            f"Segment {segment.segment_id}'s path must be its .mp4 file relative to the "
            f"Footage directory, and {str(path)!r} is not"
        )


class SegmentIndex:
    """Indexes finished Segments and finds the one holding an instant."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def index(self, segment: IndexedSegment) -> bool:
        """Index a finished Segment: True when it is newly indexed, False when it already was.

        Raises a `SegmentRefusedError` for a Segment the index will never hold, and
        `SegmentIndexError` when the database failed.
        """
        _check(segment)
        failed: str | None = None
        refused: SegmentRefusedError | None = None
        try:
            with self._connection.transaction():
                self._connection.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK_SPACE, segment.camera_id)
                )
                found = self._connection.execute(
                    f"SELECT {_COLUMNS} FROM segments"
                    " WHERE segment_id = %(id)s"
                    " OR (camera_id = %(camera)s AND time_from < %(to)s AND time_to > %(from)s"
                    "     AND time_from > %(from)s - %(span)s)"
                    " ORDER BY time_from",
                    {
                        "id": segment.segment_id,
                        "camera": segment.camera_id,
                        "from": segment.time_from,
                        "to": segment.time_to,
                        "span": MAX_SPAN,
                    },
                ).fetchall()
                held = [_segment(row) for row in found]
                if same := [s for s in held if s.segment_id == segment.segment_id]:
                    if same[0] == segment:
                        return False
                    refused = SegmentConflictError(segment, same[0])
                elif held:
                    refused = SegmentOverlapError(segment, held[0])
                else:
                    self._connection.execute(
                        f"INSERT INTO segments ({_COLUMNS})"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            segment.camera_id,
                            segment.segment_id,
                            segment.time_from,
                            segment.time_to,
                            str(segment.path),
                            segment.reason,
                            segment.fragments,
                            segment.verified,
                        ),
                    )
                    return True
        except psycopg.errors.ForeignKeyViolation:
            refused = UnknownCameraError(segment.camera_id)
        except psycopg.Error as error:
            failed = type(error).__name__
        # Raised here, outside the `except`, so the driver's error is not even its context.
        if refused is not None:
            raise refused
        raise SegmentIndexError(f"the segment index's database failed: {failed}")

    def find(self, camera_id: int, at: datetime) -> Instant | None:
        """The Segment of camera `camera_id` holding the instant `at`, and how long after it
        began `at` is; None when no Segment holds it."""
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("the instant has no time zone; Footage is timed in UTC")
        rows = self._run(
            f"SELECT {_COLUMNS} FROM segments"
            " WHERE camera_id = %(camera)s AND time_from <= %(at)s AND time_to > %(at)s"
            " AND time_from > %(at)s - %(span)s"
            " ORDER BY time_from DESC LIMIT 1",
            {"camera": camera_id, "at": at, "span": MAX_SPAN},
        )
        if not rows:
            return None
        segment = _segment(rows[0])
        return Instant(segment, at - segment.time_from)

    def latest(self, camera_id: int) -> IndexedSegment | None:
        """The last Segment of camera `camera_id` indexed, by when it begins; None if none is."""
        rows = self._run(
            f"SELECT {_COLUMNS} FROM segments WHERE camera_id = %(camera)s"
            " ORDER BY time_from DESC LIMIT 1",
            {"camera": camera_id},
        )
        return _segment(rows[0]) if rows else None

    def _run(
        self, query: LiteralString, parameters: dict[str, object]
    ) -> builtins.list[tuple[Any, ...]]:
        """The rows of one read, or `SegmentIndexError` naming only the class of what failed."""
        failed: str
        try:
            with self._connection.transaction():
                return self._connection.execute(query, parameters).fetchall()
        except psycopg.Error as error:
            failed = type(error).__name__
        raise SegmentIndexError(f"the segment index's database failed: {failed}")
