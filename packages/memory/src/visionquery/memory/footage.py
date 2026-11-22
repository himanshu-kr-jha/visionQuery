"""The Footage segment index: every finished Segment, by camera and the span of time it covers.

The recorder writes Footage to disk as Segments (ADR 0017) and indexes each one here once it is
finished, so that any instant on any camera resolves to the Segment holding it and how far into
it the instant is (`SegmentIndex.find`), or to nothing when no Segment holds it: the camera was
away, the recorder was down, or the Footage was never recorded.

**A Segment is keyed on its span, never on its file name.** Its times are the recorder's, on its
NTP-anchored clock, and it covers `[time_from, time_to)`: its start and not its end, where the
next one on the same connection begins. A Segment is at most `MAX_SPAN` long, which bounds
every lookup to two days' partitions.

**Two Segments of a camera may overlap, and that is a fact, never hidden.** A clock that steps
back, within the recorder's run (the newer Segment then has `stepped_by`) or across a restart
(it has none), gives a camera two Segments claiming the same instants. Both are Footage, so
both are indexed, and each stretch two of them share is recorded as an `Overlap`, which
`overlaps` returns for a camera and a window and an Answer states as it states a Gap. An
instant inside one resolves to the verified Segment when exactly one of those holding it is
verified, and otherwise to the newer: more generally, to the newest of the verified ones, else
the newest. Newer is indexed later, which for one camera is recorded later, because the
recorder indexes in the order it records. `index` records Overlaps under a per-camera advisory
lock, so two indexers at once cannot miss each other's.

**Indexing is idempotent.** Indexing a Segment already indexed, with the same facts, changes
nothing; with other facts it is refused (`SegmentConflictError`), because a Segment is finished
once and never changes, so two accounts of one cannot both be true and the index does not
guess which is. A Segment whose camera the registry never had is refused too, and so is one
that cannot be Footage at all (`InvalidSegmentError`), before the database sees it; whatever
those checks miss and the table refuses (a constraint, a value out of range) is an
`InvalidSegmentError` too. Every refusal is a `SegmentRefusedError`, which indexing again will
not change; a `SegmentIndexError` is the database failing, which it may.

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
import math
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
    "Overlap",
    "Reason",
    "SegmentConflictError",
    "SegmentIndex",
    "SegmentIndexError",
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

#: The ids a camera can have, and the most Fragments a Segment can hold: Postgres `integer`s.
_INTEGER = range(-(2**31), 2**31)

#: The smallest step of time the database keeps: an instant is the window one tick long.
_TICK = timedelta(microseconds=1)

#: The first key of the advisory locks `index` takes, one per camera (the second key).
_LOCK_SPACE = int.from_bytes(b"vqsg", signed=True)


class FootageError(Exception):
    """Something the segment index refused or could not do."""


class SegmentRefusedError(FootageError):
    """A Segment the index will not hold, however often it is offered."""


class InvalidSegmentError(SegmentRefusedError, ValueError):
    """A Segment that cannot be Footage: its span, path, Fragments or reason are wrong."""


class SegmentConflictError(SegmentRefusedError):
    """A Segment already indexed with other facts: a Segment never changes once finished."""

    def __init__(self, segment: IndexedSegment, indexed: IndexedSegment) -> None:
        self.segment = segment
        self.indexed = indexed
        differ = [
            name
            for name in (
                "camera_id", "time_from", "time_to", "path", "reason", "fragments", "verified",
                "stepped_by",
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
    #: By how many seconds the clock stepped just before it began, when the recorder saw it
    #: step (ADR 0017); None when it did not.
    stepped_by: float | None = None


@dataclass(frozen=True)
class Overlap:
    """A stretch of a camera's time that two of its Segments both hold, `[time_from, time_to)`:
    the clock stepped back between them, within the recorder's run or across a restart."""

    camera_id: int
    time_from: datetime
    time_to: datetime
    #: The Segment indexed first, and the one indexed after it: the recorder indexes a camera's
    #: Segments in the order it records them.
    older: IndexedSegment
    newer: IndexedSegment


@dataclass(frozen=True)
class Instant:
    """Where an instant of a camera's Footage is: the Segment holding it, and how long after
    the Segment began it is; and when other Segments hold it too, the Overlaps it lies in."""

    segment: IndexedSegment
    offset: timedelta
    overlaps: tuple[Overlap, ...] = ()


_COLUMNS: LiteralString = (
    "camera_id, segment_id, time_from, time_to, path, reason, fragments, verified, stepped_by"
)
#: `_COLUMNS` of the older and of the newer Segment of an Overlap, as `overlaps` joins them.
_OLDER: LiteralString = (
    "older.camera_id, older.segment_id, older.time_from, older.time_to, older.path,"
    " older.reason, older.fragments, older.verified, older.stepped_by"
)
_NEWER: LiteralString = (
    "newer.camera_id, newer.segment_id, newer.time_from, newer.time_to, newer.path,"
    " newer.reason, newer.fragments, newer.verified, newer.stepped_by"
)
_WIDTH = 9


def _segment(row: tuple[Any, ...]) -> IndexedSegment:
    camera_id, segment_id, time_from, time_to, path, reason, fragments, verified, stepped_by = row
    return IndexedSegment(
        camera_id,
        segment_id,
        time_from,
        time_to,
        PurePosixPath(path),
        reason,
        fragments,
        verified,
        stepped_by,
    )


def _overlap(row: tuple[Any, ...]) -> Overlap:
    camera_id, time_from, time_to = row[:3]
    older, newer = row[3 : 3 + _WIDTH], row[3 + _WIDTH :]
    return Overlap(camera_id, time_from, time_to, _segment(older), _segment(newer))


def _aware(at: datetime, what: str) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise InvalidSegmentError(f"{what} has no time zone; Footage is timed in UTC")


def _utf_8(text: str) -> bool:
    """Whether `text` is UTF-8 text, as a file name Python read off disk need not be."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check(segment: IndexedSegment) -> None:
    """Refuse a Segment that cannot be Footage, saying why, before the database sees it."""
    if segment.camera_id not in _INTEGER:
        raise UnknownCameraError(segment.camera_id)
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
    if segment.fragments not in _INTEGER:
        raise InvalidSegmentError(
            f"Segment {segment.segment_id} holds more Fragments than any Segment can"
        )
    if segment.reason not in get_args(Reason.__value__):
        raise InvalidSegmentError(
            f"{segment.reason!r} is not in the closed set of reasons a Segment ends"
        )
    stepped_by: object = segment.stepped_by
    if stepped_by is not None and (
        not isinstance(stepped_by, int | float)
        or isinstance(stepped_by, bool)
        or not math.isfinite(stepped_by)
        or stepped_by == 0
    ):
        raise InvalidSegmentError(
            f"Segment {segment.segment_id} says the clock stepped by {stepped_by!r} seconds "
            "before it, which no step is"
        )
    path = segment.path
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.suffix != ".mp4"
        or path.name == ".mp4"
        or "\x00" in str(path)
        or not _utf_8(str(path))
    ):
        raise InvalidSegmentError(
            f"Segment {segment.segment_id}'s path must be its .mp4 file relative to the "
            f"Footage directory, and {str(path)!r} is not"
        )


class SegmentIndex:
    """Indexes finished Segments, finds the one an instant resolves to, and says where two of a
    camera's overlap."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def index(self, segment: IndexedSegment) -> bool:
        """Index a finished Segment: True when it is newly indexed, False when it already was.

        Each stretch it shares with a Segment of its camera already indexed is recorded as an
        `Overlap`, with it as the newer. Raises a `SegmentRefusedError` for a Segment the index
        will never hold, and `SegmentIndexError` when the database failed.
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
                    " ORDER BY indexed_order",
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
                else:
                    self._insert(segment, overlapping=held)
                    return True
        except psycopg.errors.ForeignKeyViolation:
            refused = UnknownCameraError(segment.camera_id)
        except (psycopg.errors.IntegrityError, psycopg.errors.DataError) as error:
            # A rule of the table's that `_check` missed: no retry changes what the Segment is.
            refused = InvalidSegmentError(
                f"the database refused Segment {segment.segment_id} ({type(error).__name__})"
            )
        except psycopg.Error as error:
            failed = type(error).__name__
        # Raised here, outside the `except`, so the driver's error is not even its context.
        if refused is not None:
            raise refused
        raise SegmentIndexError(f"the segment index's database failed: {failed}")

    def _insert(self, segment: IndexedSegment, *, overlapping: list[IndexedSegment]) -> None:
        self._connection.execute(
            f"INSERT INTO segments ({_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                segment.camera_id,
                segment.segment_id,
                segment.time_from,
                segment.time_to,
                str(segment.path),
                segment.reason,
                segment.fragments,
                segment.verified,
                segment.stepped_by,
            ),
        )
        for held in overlapping:
            self._connection.execute(
                "INSERT INTO segment_overlaps (camera_id, time_from, time_to, older_id, newer_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    segment.camera_id,
                    max(held.time_from, segment.time_from),
                    min(held.time_to, segment.time_to),
                    held.segment_id,
                    segment.segment_id,
                ),
            )

    def find(self, camera_id: int, at: datetime) -> Instant | None:
        """The Segment of camera `camera_id` that the instant `at` resolves to, how long after
        it began `at` is, and the Overlaps `at` lies in; None when no Segment holds it.

        Of several Segments holding `at`, the newest verified one, else the newest: of two, the
        verified one if exactly one is, otherwise the newer."""
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("the instant has no time zone; Footage is timed in UTC")
        if camera_id not in _INTEGER:
            return None
        rows = self._run(
            f"SELECT {_COLUMNS} FROM segments"
            " WHERE camera_id = %(camera)s AND time_from <= %(at)s AND time_to > %(at)s"
            " AND time_from > %(at)s - %(span)s"
            " ORDER BY verified DESC, indexed_order DESC",
            {"camera": camera_id, "at": at, "span": MAX_SPAN},
        )
        if not rows:
            return None
        segment = _segment(rows[0])
        overlaps = () if len(rows) == 1 else tuple(self.overlaps(camera_id, at, at + _TICK))
        return Instant(segment, at - segment.time_from, overlaps)

    def overlaps(self, camera_id: int, time_from: datetime, time_to: datetime) -> list[Overlap]:
        """Every Overlap of camera `camera_id` that shares an instant with
        `[time_from, time_to)`, by when it begins."""
        if any(t.tzinfo is None or t.utcoffset() is None for t in (time_from, time_to)):
            raise ValueError("the window has no time zone; Footage is timed in UTC")
        if camera_id not in _INTEGER:
            return []
        # Each of an Overlap's Segments holds its start, so began at most `MAX_SPAN` before it.
        rows = self._run(
            f"SELECT o.camera_id, o.time_from, o.time_to, {_OLDER}, {_NEWER}"
            " FROM segment_overlaps o"
            " JOIN segments older ON older.segment_id = o.older_id"
            "     AND older.camera_id = o.camera_id AND older.time_from <= o.time_from"
            "     AND older.time_from > o.time_from - %(span)s"
            " JOIN segments newer ON newer.segment_id = o.newer_id"
            "     AND newer.camera_id = o.camera_id AND newer.time_from <= o.time_from"
            "     AND newer.time_from > o.time_from - %(span)s"
            " WHERE o.camera_id = %(camera)s AND o.time_from < %(to)s AND o.time_to > %(from)s"
            " AND o.time_from > %(from)s - %(span)s"
            " ORDER BY o.time_from, newer.indexed_order, older.indexed_order",
            {"camera": camera_id, "from": time_from, "to": time_to, "span": MAX_SPAN},
        )
        return [_overlap(row) for row in rows]

    def segment_ids(self, camera_id: int) -> frozenset[UUID]:
        """The id of every Segment of camera `camera_id` indexed, whenever it begins."""
        if camera_id not in _INTEGER:
            return frozenset()
        rows = self._run(
            "SELECT segment_id FROM segments WHERE camera_id = %(camera)s",
            {"camera": camera_id},
        )
        return frozenset(row[0] for row in rows)

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
