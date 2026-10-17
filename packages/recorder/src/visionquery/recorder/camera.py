"""One camera, from its stream in MediaMTX to finished Segments on disk.

**Segment boundaries are the clock's, not the stream's.** A Segment covers one epoch-aligned
window (`segment.window`), so every camera cuts its Footage at the same instants without
coordinating. Copy-only Footage can be cut only at a keyframe, so a Segment rolls at the first
Fragment that starts at or after its window's end: each Segment begins at the first keyframe in
its window and ends with the Fragment that holds the boundary.

**The timebase is anchored again when a Segment opens** and carried forward by the monotonic
clock (`visionquery.recorder.timebase`); an arrival is read from it the moment its Fragment is
read, never from when the file was written.

Adapted from phyOS's Photon recorder (`adapters.photon.camera`), without signing or shipping.
"""

from datetime import datetime, timedelta
from pathlib import Path

from visionquery.recorder.mp4 import Fragment, Init
from visionquery.recorder.segment import Reason, Segment, SegmentFile, window
from visionquery.recorder.timebase import TimeBase

__all__ = ["Segmenter"]


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
            begins = arrived_at - timedelta(seconds=fragment.seconds)
            if self._not_before is not None:
                begins = max(begins, self._not_before)
            start, self._ends_at = window(begins, self._length)
            self._segment = SegmentFile.open(
                self._directory,
                self._init,
                window_start=start,
                not_before=self._not_before,
                verified=self._timebase.verified,
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
