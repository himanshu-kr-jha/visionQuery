"""The time every Segment is stamped with: NTP's reading, carried forward by the monotonic clock.

Timestamps come from this timebase, never from when a frame happened to be processed or a file
written. The wall clock is whatever whoever holds the machine set it to, and it can be stepped
under a running process, so the recorder reads its time source when a Segment opens and carries
that reading forward with the monotonic clock, which nobody can step.

The time source is NTP, through the kernel: when the kernel says its clock is synchronised, the
wall clock at that instant *is* NTP's reading (a container shares its host's clock, so the
host's NTP daemon disciplines it). When it is not synchronised, the last good reading carried
forward is still better than the wall, and with no good reading ever, the wall clock is used and
said to be unverified. The Segment is kept either way, because discarding real Footage to avoid
stating an uncertainty that can simply be stated is the worse error.

**A reading carried forward stops counting as verified after `verified_for`** (two hours by
default: a Segment lasts at most an hour, and is opened on a fresh reading whenever NTP is
there, so a reading that old means NTP has been gone for a Segment or more). A host that lost
NTP a month ago still has a monotonic clock, but a month of its drift is not something to vouch
for.

**Anchoring again can move the time, but only a step does** (whoever set the clock, or NTP
finding it far off, as at boot). NTP slews the monotonic clock exactly as it slews the wall
clock, so the difference between them, which is what anchoring measures, stays the same through
any slew. `anchor()` says by how much the time moved, and a shift beyond `STEP_TOLERANCE` is a
discontinuity that the Segmenter must not bridge (`visionquery.recorder.camera`).

Why arrival on this clock, and not the cameras' own timestamps: ADR 0017. Adapted from phyOS's
Photon recorder (`adapters.photon.timebase`).
"""

import ctypes
import ctypes.util
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

__all__ = ["STEP_TOLERANCE", "ClockState", "Reading", "TimeBase", "read_kernel_clock"]

#: A shift of the time by more than this when the timebase is anchored again is a step. Only
#: steps move the shift, since a slew moves the monotonic and the wall clock alike; 250 ms sits
#: below the 0.4 s at which systemd-timesyncd steps instead of slewing, and above the noise of
#: reading two clocks one after the other.
STEP_TOLERANCE = timedelta(milliseconds=250)
#: How long after its last synchronised reading the timebase still counts as verified.
VERIFIED_FOR = timedelta(hours=2)

type Source = Literal["ntp", "wall"]


@dataclass(frozen=True)
class Reading:
    """What the time source said, at one instant."""

    source: Source
    synchronised: bool
    est_error_us: int | None = None
    max_error_us: int | None = None


@dataclass(frozen=True)
class ClockState:
    """What the timebase stands on, for metrics and logs."""

    #: `ntp` when the anchor in use is a synchronised NTP reading, `wall` when it is the bare
    #: wall clock.
    source: Source
    verified: bool
    #: The wall clock less the timebase: how far a timestamp from the wall would be off.
    wall_offset_seconds: float
    anchored_seconds_ago: float
    est_error_us: int | None
    max_error_us: int | None


#: `adjtimex` returns TIME_ERROR when the kernel's clock is not synchronised.
_TIME_ERROR = 5
#: `struct timex`, with room to spare: modes, then offset, freq, maxerror, esterror as longs.
_TIMEX_SIZE = 512


def read_kernel_clock() -> Reading:
    """Ask the kernel whether NTP disciplines its clock, and how well.

    `adjtimex` with no modes set reads and changes nothing, and needs no privilege. Off Linux,
    or where the call is refused, the answer is that nothing is known, which is the wall clock.
    """
    if sys.platform != "linux":
        return Reading(source="wall", synchronised=False)
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        buf = ctypes.create_string_buffer(_TIMEX_SIZE)
        state: int = libc.adjtimex(buf)
    except OSError, AttributeError:
        return Reading(source="wall", synchronised=False)
    if state < 0:
        return Reading(source="wall", synchronised=False)
    long = ctypes.sizeof(ctypes.c_long)
    maxerror = ctypes.c_long.from_buffer(buf, long * 3).value
    esterror = ctypes.c_long.from_buffer(buf, long * 4).value
    return Reading(
        source="ntp",
        synchronised=state != _TIME_ERROR,
        est_error_us=esterror,
        max_error_us=maxerror,
    )


@dataclass(frozen=True)
class _Anchor:
    wall: float
    mono: float
    reading: Reading
    verified: bool


class TimeBase:
    """A time source reading, carried forward by the monotonic clock."""

    def __init__(
        self,
        *,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        source: Callable[[], Reading] = read_kernel_clock,
        verified_for: timedelta = VERIFIED_FOR,
    ) -> None:
        self._verified_for = verified_for.total_seconds()
        self._wall = wall
        self._mono = mono
        self._source = source
        self._anchor: _Anchor | None = None

    def anchor(self) -> timedelta:
        """Read the wall, the monotonic clock and the time source together; how far that moved
        the time (nothing the first time).

        Called when a Segment opens. A synchronised source replaces the anchor; an
        unsynchronised one keeps the last good anchor, or, with none, falls back to the wall
        clock and marks it unverified.
        """
        before = self._anchor
        reading = self._source()
        wall, mono = self._wall(), self._mono()
        if reading.synchronised:
            self._anchor = _Anchor(wall, mono, reading, verified=True)
        elif self._anchor is None or not self._anchor.verified:
            self._anchor = _Anchor(wall, mono, Reading("wall", False), verified=False)
        if before is None:
            return timedelta(0)
        after = self._anchor
        return timedelta(seconds=(after.wall - after.mono) - (before.wall - before.mono))

    def now(self) -> datetime:
        """The time now, on this timebase; anchors first when nothing has."""
        anchor = self._anchored()
        carried = anchor.wall + (self._mono() - anchor.mono)
        return datetime.fromtimestamp(carried, UTC)

    @property
    def verified(self) -> bool:
        """Whether the anchor in use is a synchronised NTP reading, taken within `verified_for`."""
        return self._verified(self._anchored())

    def _verified(self, anchor: _Anchor) -> bool:
        return anchor.verified and self._mono() - anchor.mono <= self._verified_for

    def state(self) -> ClockState:
        anchor = self._anchored()
        mono = self._mono()
        carried = anchor.wall + (mono - anchor.mono)
        return ClockState(
            source=anchor.reading.source,
            verified=self._verified(anchor),
            wall_offset_seconds=self._wall() - carried,
            anchored_seconds_ago=mono - anchor.mono,
            est_error_us=anchor.reading.est_error_us,
            max_error_us=anchor.reading.max_error_us,
        )

    def _anchored(self) -> _Anchor:
        if self._anchor is None:
            self.anchor()
        assert self._anchor is not None
        return self._anchor
