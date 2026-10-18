"""Every finished Segment, indexed in the memory, so that any instant resolves to a file.

The recorder's cameras tell the `Indexer` of each Segment they finish (`finished`), and it
indexes them in the memory's segment index (`visionquery.memory.footage`), one at a time and in
the order they finished, on a connection of its own. Recording never waits on it: a camera
only queues the Segment, and the Footage is already safe on disk.

**What the memory cannot take now, it takes later.** While the memory cannot be reached, or
fails, the Segment at the head of the queue is tried again after a wait that doubles from
`first_wait` to `last_wait`, and the ones behind it wait their turn; that the index cannot be
reached is logged once, as a WARNING, and that it can again once, as an INFO. Because the queue
is never reordered, a camera's Segments are indexed in time order, so everything of a camera's
that finished before the last one indexed was either indexed or refused.

**What the memory refuses, it always will** (`SegmentRefusedError`: a Segment overlapping
another of its camera's, of a camera the registry never had, or that cannot be Footage), so
such a Segment is logged once, as an ERROR naming its file and why, counted, and passed over:
one bad Segment never holds up the rest. So is a Segment whose indexing failed in a way nobody
foresaw, which is logged with the class of what failed, never its text.

**On start, it catches up from disk before anything else**: the queue is lost with the process,
and the recorder may have crashed with Segments finished and not indexed, or recovered
(`segment.recover`). For each camera's directory it asks the index for the camera's last
Segment and indexes every Segment on disk that begins no earlier, reading only the days from
the one before that Segment's on; indexing again changes nothing, so going over what is already
indexed is harmless. A directory whose name is not a camera id is not a camera's Footage, and
is left alone.

**On stop**, it goes on until the catch-up is done and the queue is empty, or the memory
fails, whichever is first; what is left is caught up on at the next start.
"""

import asyncio
import collections
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from visionquery.memory.connection import Connection, ConnectionFailedError, connect
from visionquery.memory.footage import (
    IndexedSegment,
    SegmentIndex,
    SegmentIndexError,
    SegmentRefusedError,
)
from visionquery.memory.settings import DatabaseSettings
from visionquery.recorder import metrics
from visionquery.recorder.camera import Backoff
from visionquery.recorder.segment import Segment, segments

__all__ = ["Indexer"]

_log = logging.getLogger(__name__)

#: Bounds on talking to the database, so that one that stops answering cannot hold the indexer.
_CONNECT_TIMEOUT = 5
_STATEMENT_TIMEOUT_MS = 5000

#: What became of one attempt to index a Segment: done (indexed now or before), refused for
#: good, or failed for now, to be tried again.
type _Outcome = Literal["done", "refused", "failed"]


class _MemoryFailedError(Exception):
    """The memory could not be reached or failed; what to say about it, without driver text."""


class Indexer:
    """Indexes the Segments under `footage` that the recorder finishes, and those it missed."""

    def __init__(
        self,
        footage: Path,
        database: DatabaseSettings,
        *,
        first_wait: float = 1.0,
        last_wait: float = 30.0,
    ) -> None:
        self._footage = footage
        self._database = database
        self._backoff = Backoff(first_wait, last_wait)
        self._pending: collections.deque[tuple[int, Segment]] = collections.deque()
        self._caught_up = False
        self._stopping = False
        #: Set when there may be work, or when asked to stop.
        self._wake = asyncio.Event()
        #: Set only when asked to stop; cuts a wait after a failure short.
        self._stopped = asyncio.Event()
        self._connection: Connection | None = None
        #: What the memory last failed with; None while it works, and before the first attempt.
        self._failing: str | None = None
        metrics.INDEX_PENDING.set(0)
        metrics.INDEX_UP.set(0)

    def finished(self, camera_id: int, segment: Segment) -> None:
        """Queue a Segment camera `camera_id` finished, to be indexed. Called in the event loop."""
        self._pending.append((camera_id, segment))
        metrics.INDEX_PENDING.set(len(self._pending))
        self._wake.set()

    def stop(self) -> None:
        """Ask `run` to return once the work it has is done, or the memory fails."""
        self._stopping = True
        self._stopped.set()
        self._wake.set()

    async def run(self) -> None:
        """Catch up from disk, then index each Segment as it is finished, until stopped."""
        try:
            while True:
                try:
                    if not self._caught_up:
                        worked = await asyncio.to_thread(self._catch_up)
                    elif self._pending:
                        worked = await self._index_next()
                    elif self._stopping:
                        return
                    else:
                        self._wake.clear()
                        await self._wake.wait()
                        continue
                except Exception as error:
                    # Nothing foreseen raises here; whatever did is said once, by its class
                    # alone, and the work is tried again like any failure, so indexing never
                    # stops for good while the recorder runs.
                    await asyncio.to_thread(self._close)
                    self._failed(f"indexing failed unexpectedly ({type(error).__name__})")
                    worked = False
                if worked:
                    self._backoff.reset()
                elif self._stopping:
                    return
                else:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopped.wait(), timeout=self._backoff.next())
        finally:
            await asyncio.to_thread(self._close)

    async def _index_next(self) -> bool:
        """Try the Segment at the head of the queue; False when the memory failed."""
        camera_id, segment = self._pending[0]
        outcome = await asyncio.to_thread(self._attempt, camera_id, segment)
        if outcome == "failed":
            return False
        self._pending.popleft()
        metrics.INDEX_PENDING.set(len(self._pending))
        return True

    def _catch_up(self) -> bool:
        """Index what is on disk and not indexed; False when the memory failed."""
        try:
            index = self._index()
            for directory in _camera_directories(self._footage):
                camera_id = int(directory.name)
                latest = _read(index.latest, camera_id)
                since = None if latest is None else latest.time_from
                found = segments(
                    directory, since=_day_before(since), unreadable=_unreadable(camera_id)
                )
                for segment in found:
                    if since is not None and segment.time_from < since:
                        continue
                    if self._attempt(camera_id, segment, index) == "failed":
                        return False
        except _MemoryFailedError as error:
            self._close()
            self._failed(str(error))
            return False
        self._reached()
        self._caught_up = True
        return True

    def _attempt(
        self, camera_id: int, segment: Segment, index: SegmentIndex | None = None
    ) -> _Outcome:
        """Index one Segment, saying what became of it; logs and counts it either way."""
        label = str(camera_id)
        said = {"camera_id": camera_id, "segment": str(segment.path)}
        try:
            index = index or self._index()
            entry = self._entry(camera_id, segment)
            try:
                added = index.index(entry)
            except SegmentIndexError as error:
                self._close()
                raise _MemoryFailedError(str(error)) from None
        except _MemoryFailedError as error:
            self._failed(str(error))
            return "failed"
        except SegmentRefusedError as error:
            metrics.SEGMENTS_REFUSED.labels(label).inc()
            _log.error(
                "camera %s: Segment %s is not indexed, and will not be: %s",
                camera_id,
                segment.path.name,
                error,
                extra=said,
            )
            return "refused"
        except Exception as error:
            # A long-running service goes on; the class alone, since its text might hold
            # anything, the driver's included.
            metrics.SEGMENTS_REFUSED.labels(label).inc()
            _log.error(
                "camera %s: Segment %s is not indexed: indexing it failed (%s)",
                camera_id,
                segment.path.name,
                type(error).__name__,
                extra=said,
            )
            return "refused"
        self._reached()
        if added:
            metrics.SEGMENTS_INDEXED.labels(label).inc()
        return "done"

    def _entry(self, camera_id: int, segment: Segment) -> IndexedSegment:
        return IndexedSegment(
            camera_id=camera_id,
            segment_id=segment.segment_id,
            time_from=segment.time_from,
            time_to=segment.time_to,
            path=PurePosixPath(segment.path.relative_to(self._footage).as_posix()),
            reason=segment.reason,
            fragments=segment.fragments,
            verified=segment.verified,
        )

    def _index(self) -> SegmentIndex:
        """The index, on a connection kept between Segments."""
        if self._connection is None or self._connection.closed:
            try:
                self._connection = connect(
                    self._database.url.get_secret_value(),
                    _CONNECT_TIMEOUT,
                    statement_timeout_ms=_STATEMENT_TIMEOUT_MS,
                )
            except ConnectionFailedError as error:
                raise _MemoryFailedError(
                    f"the database could not be reached: {error.category} ({error.cause})"
                ) from None
        return SegmentIndex(self._connection)

    def _failed(self, problem: str) -> None:
        metrics.INDEX_UP.set(0)
        if self._failing is None:
            _log.warning(
                "Segments cannot be indexed in the segment index for now, and are tried again "
                "with backoff; Footage goes on being recorded, and what is finished meanwhile "
                "is indexed when it can be: %s",
                problem,
            )
        self._failing = problem

    def _reached(self) -> None:
        metrics.INDEX_UP.set(1)
        if self._failing is not None:
            _log.info("indexing Segments in the segment index again; indexing what was finished")
            self._failing = None

    def _close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def _read[T](read: Callable[[int], T], camera_id: int) -> T:
    """One read of the index, with its failure as the memory failing."""
    try:
        return read(camera_id)
    except SegmentIndexError as error:
        raise _MemoryFailedError(str(error)) from None


def _unreadable(camera_id: int) -> Callable[[Path, Exception], None]:
    """What to do with a finished Segment of camera `camera_id` whose record cannot be read:
    say so, count it, and go on with the rest."""

    def unreadable(path: Path, error: Exception) -> None:
        metrics.SEGMENTS_REFUSED.labels(str(camera_id)).inc()
        _log.error(
            "camera %s: Segment %s is not indexed: its .closed record could not be read (%s)",
            camera_id,
            path.name,
            type(error).__name__,
            extra={"camera_id": camera_id, "segment": str(path)},
        )

    return unreadable


def _camera_directories(footage: Path) -> list[Path]:
    """Each camera's directory under `footage`, named for its id; nothing else there (the lock,
    a file system's `lost+found`) is a camera's Footage."""
    if not footage.is_dir():
        return []
    return sorted(
        (path for path in footage.iterdir() if path.is_dir() and path.name.isdigit()),
        key=lambda path: int(path.name),
    )


def _day_before(since: datetime | None) -> date | None:
    """The first day whose directory can hold a Segment beginning at `since` or later: a
    Segment is filed under the day (UTC) its first Fragment begins in, which may be a moment
    either side of when the Segment begins, across midnight."""
    return None if since is None else since.astimezone(UTC).date() - timedelta(days=1)
