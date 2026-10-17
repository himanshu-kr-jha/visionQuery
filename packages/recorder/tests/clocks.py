"""Clocks moved by hand, for the recorder's tests (imported by name, like boxes.py)."""

from datetime import UTC, datetime

from visionquery.recorder.timebase import Reading, TimeBase

__all__ = ["T0", "Clocks"]

T0 = datetime(2026, 10, 6, 10, 0, tzinfo=UTC).timestamp()


class Clocks:
    """A wall clock, a monotonic clock and a time source, each moved by hand."""

    def __init__(self, *, synchronised: bool = True, wall: float = T0) -> None:
        self.wall = wall
        self.mono = 1000.0
        self.reading = Reading(source="ntp", synchronised=synchronised, est_error_us=250)

    def tick(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def timebase(self) -> TimeBase:
        return TimeBase(wall=lambda: self.wall, mono=lambda: self.mono, source=lambda: self.reading)
