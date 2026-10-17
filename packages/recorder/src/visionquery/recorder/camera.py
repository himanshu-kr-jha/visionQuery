"""One camera, from its stream in MediaMTX to finished Segments on disk.

**Segment boundaries are the clock's, not the stream's.** A Segment covers one epoch-aligned
window (`segment.window`), so every camera cuts its Footage at the same instants without
coordinating. Copy-only Footage can be cut only at a keyframe, so a Segment rolls at the first
Fragment that starts at or after its window's end: each Segment begins at the first keyframe in
its window and ends with the Fragment that holds the boundary.

**The timebase is anchored again when a Segment opens** and carried forward by the monotonic
clock (`visionquery.recorder.timebase`); an arrival is read from it the moment its Fragment is
read, never from when the file was written.

**A dropped stream is a finished Segment and a wait, never a stopped camera.** When the camera's
stream ends (it stopped publishing, MediaMTX restarted, or it went silent for `stall_after`),
its Segment is finished `stream_ended` and the recorder connects again after a wait that doubles
from `first_wait` to `last_wait` and starts again from `first_wait` once video arrives. The time
the camera was away is the gap between two Segments, stated rather than filled. That it was lost
is logged once, as a WARNING, however many attempts fail, and that it is back once, as an INFO.

**Three ways a Segment ends here**: `rolled` at its window's end, `stream_ended` when the stream
went away, `stopped` when the recorder was told to stop recording the camera. A crash is not a
stop: a cancelled recording finishes nothing, and the next start recovers what is on disk.

Adapted from phyOS's Photon recorder (`adapters.photon.camera`), without signing or shipping.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from visionquery.memory.cameras import Camera
from visionquery.recorder import metrics
from visionquery.recorder.ffmpeg import Recording, Source
from visionquery.recorder.mp4 import Fragment, Init, MalformedError
from visionquery.recorder.segment import Reason, Segment, SegmentFile, window
from visionquery.recorder.timebase import TimeBase

__all__ = ["Backoff", "Segmenter", "Timing", "record_camera"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Timing:
    """How long Segments are, and how the recorder waits on a camera that went away."""

    #: The window every Segment is cut to, aligned to the epoch.
    segment: timedelta = timedelta(minutes=5)
    #: Seconds to wait before connecting again, doubling from the first to the last.
    first_wait: float = 1.0
    last_wait: float = 10.0
    #: Seconds of silence on a connection before it is given up on.
    stall_after: float = 10.0


class Backoff:
    """Waits that double from `first` to `last`, and start again from `first` when reset."""

    def __init__(self, first: float, last: float) -> None:
        self._first = first
        self._last = last
        self._next = first

    def next(self) -> float:
        wait = self._next
        self._next = min(self._next * 2, self._last)
        return wait

    def reset(self) -> None:
        self._next = self._first


class Segmenter:
    """Cuts one connection's Fragments into Segments in `directory`, the camera's.

    Its calls write to disk and fsync, so an event loop runs them in a thread.
    """

    def __init__(
        self,
        directory: Path,
        init: Init,
        timebase: TimeBase,
        length: timedelta,
        *,
        not_before: datetime | None,
    ) -> None:
        self._directory = directory
        self._init = init
        self._timebase = timebase
        self._length = length
        #: Nothing on this connection can have been received before it opened, and nothing in a
        #: Segment before the Segment ahead of it ended.
        self._not_before = not_before
        self._segment: SegmentFile | None = None
        self._ends_at: datetime | None = None

    def add(self, fragment: Fragment, *, arrived_at: datetime) -> Segment | None:
        """Write `fragment`, which arrived at `arrived_at` on the timebase; the Segment it
        rolled over, finished, if it began the next one."""
        rolled: Segment | None = None
        begins = arrived_at - timedelta(seconds=fragment.seconds)
        if self._segment is not None and self._ends_at is not None and begins >= self._ends_at:
            rolled = self.finish("rolled")
        if self._segment is None:
            arrived_at += self._timebase.anchor()
            if rolled is None:
                begins = arrived_at - timedelta(seconds=fragment.seconds)
                if self._not_before is not None:
                    begins = max(begins, self._not_before)
            # A rolled Segment's media runs on from the last one's: it begins where that one
            # ended, and is filed under the window its first Fragment begins in.
            start, self._ends_at = window(begins, self._length)
            self._segment = SegmentFile.open(
                self._directory,
                self._init,
                window_start=start,
                not_before=self._not_before,
                verified=self._timebase.verified,
                begins_at=None if rolled is None else rolled.time_to,
            )
        self._segment.append(fragment, arrived_at=arrived_at)
        return rolled

    def finish(self, reason: Reason) -> Segment | None:
        """Finish the Segment being written, if any, saying why."""
        segment, self._segment, self._ends_at = self._segment, None, None
        if segment is None:
            return None
        done = segment.close(reason)
        if done is not None:
            self._not_before = done.time_to
        return done


def _said(camera: Camera) -> dict[str, Any]:
    """What every line logged about a camera carries; never its URL (ADR 0014)."""
    return {"camera": camera.name, "camera_id": camera.id, "path": camera.path}


class _Outage:
    """Says once that a camera's video stopped arriving and once that it is back."""

    def __init__(self, camera: Camera) -> None:
        self._camera = camera
        self._since: float | None = None
        self._started = False

    def lost(self, why: str | None) -> None:
        metrics.RECEIVING.labels(str(self._camera.id)).set(0)
        if self._since is not None:
            return
        self._since = asyncio.get_running_loop().time()
        _log.warning(
            "%s (%s): no video %s (%s); trying again with backoff",
            self._camera.name,
            self._camera.path,
            "since it was last received" if self._started else "yet",
            why or "the stream ended",
            extra=_said(self._camera),
        )

    def back(self) -> None:
        metrics.RECEIVING.labels(str(self._camera.id)).set(1)
        if self._since is not None:
            away = asyncio.get_running_loop().time() - self._since
            _log.info(
                "%s (%s): receiving video %safter %.1f s",
                self._camera.name,
                self._camera.path,
                "again " if self._started else "",
                away,
                extra={**_said(self._camera), "away_seconds": round(away, 1)},
            )
        elif not self._started:
            _log.info(
                "%s (%s): recording",
                self._camera.name,
                self._camera.path,
                extra=_said(self._camera),
            )
        self._since = None
        self._started = True


async def record_camera(
    camera: Camera,
    source: Source,
    directory: Path,
    timing: Timing,
    stop: asyncio.Event,
    *,
    timebase: TimeBase | None = None,
) -> None:
    """Record `camera`'s stream from `source` into Segments in `directory`, until `stop` is set.

    A clean stop finishes the Segment being written `stopped`; a cancellation leaves it on disk
    unfinished, as a crash would, for `segment.recover` to finish on the next start.
    """
    timebase = timebase or TimeBase()
    backoff = Backoff(timing.first_wait, timing.last_wait)
    outage = _Outage(camera)
    try:
        while not stop.is_set():
            received, why = await _connect_once(
                camera, source, directory, timing, timebase, stop, outage
            )
            if stop.is_set():
                break
            if received:
                backoff.reset()
            outage.lost(why)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff.next())
    finally:
        metrics.RECEIVING.labels(str(camera.id)).set(0)


async def _connect_once(
    camera: Camera,
    source: Source,
    directory: Path,
    timing: Timing,
    timebase: TimeBase,
    stop: asyncio.Event,
    outage: _Outage,
) -> tuple[bool, str | None]:
    """One connection to the camera's stream, until it ends: whether video arrived, and why
    it ended."""
    label = str(camera.id)
    metrics.CONNECTIONS.labels(label).inc()
    not_before = timebase.now()
    segmenter: Segmenter | None = None
    received = False
    why: str | None = None
    async with Recording(source) as recording:
        stopper = asyncio.create_task(_stop_when(stop, recording))
        try:
            async for item in recording:
                if isinstance(item, Init):
                    if segmenter is None:
                        segmenter = Segmenter(
                            directory, item, timebase, timing.segment, not_before=not_before
                        )
                    continue
                arrived = timebase.now()
                assert segmenter is not None
                rolled = await asyncio.to_thread(segmenter.add, item, arrived_at=arrived)
                if not received:
                    received = True
                    outage.back()
                    metrics.CLOCK_VERIFIED.set(int(timebase.verified))
                metrics.FRAGMENTS.labels(label).inc()
                metrics.BYTES.labels(label).inc(len(item.data))
                if rolled is not None:
                    _finished(camera, rolled, timebase)
        except MalformedError as error:
            why = f"the stream could not be read: {error}"
        finally:
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
    # Reached only when the recording ended or was stopped; a cancellation leaves the Segment
    # being written on disk for the next start to recover.
    if segmenter is not None:
        reason: Reason = "stopped" if stop.is_set() else "stream_ended"
        done = await asyncio.to_thread(segmenter.finish, reason)
        if done is not None:
            _finished(camera, done, timebase)
    return received, why or recording.ended_because


async def _stop_when(stop: asyncio.Event, recording: Recording) -> None:
    await stop.wait()
    await recording.stop()


def _finished(camera: Camera, segment: Segment, timebase: TimeBase) -> None:
    metrics.SEGMENTS.labels(str(camera.id), segment.reason).inc()
    metrics.WALL_OFFSET.set(timebase.state().wall_offset_seconds)
    _log.info(
        "%s (%s): Segment %s finished %s, %s to %s",
        camera.name,
        camera.path,
        segment.path.name,
        segment.reason,
        segment.time_from.isoformat(),
        segment.time_to.isoformat(),
        extra={
            **_said(camera),
            "segment": str(segment.path),
            "time_from": segment.time_from.isoformat(),
            "time_to": segment.time_to.isoformat(),
            "reason": segment.reason,
            "fragments": segment.fragments,
            "verified": segment.verified,
        },
    )
