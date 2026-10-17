"""The recorder's clock: NTP's reading, carried forward by the monotonic clock.

The wall clock can be stepped under a running process, by NTP or by whoever holds the machine,
so a Segment's times are the time source's reading taken when the Segment opens, carried
forward by a clock nobody can step. Each Segment says whether that reading was NTP's.
"""

import sys
from datetime import UTC, datetime

import pytest
from visionquery.recorder.timebase import Reading, TimeBase, read_kernel_clock

T0 = datetime(2026, 10, 6, 10, 0, tzinfo=UTC).timestamp()


class Clocks:
    """A wall clock, a monotonic clock and a time source, each moved by hand."""

    def __init__(self, *, synchronised: bool = True) -> None:
        self.wall = T0
        self.mono = 1000.0
        self.reading = Reading(source="ntp", synchronised=synchronised, est_error_us=250)

    def tick(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def timebase(self) -> TimeBase:
        return TimeBase(wall=lambda: self.wall, mono=lambda: self.mono, source=lambda: self.reading)


def test_time_is_the_anchor_carried_forward_by_the_monotonic_clock() -> None:
    clocks = Clocks()
    timebase = clocks.timebase()
    timebase.anchor()

    clocks.tick(90)

    assert timebase.now() == datetime.fromtimestamp(T0 + 90, UTC)
    assert timebase.now().tzinfo is UTC


def test_a_wall_clock_stepped_under_a_running_segment_moves_nothing() -> None:
    """Somebody sets the clock an hour forward mid-Segment; the Segment's times do not jump."""
    clocks = Clocks()
    timebase = clocks.timebase()
    timebase.anchor()

    clocks.tick(10)
    clocks.wall += 3600

    assert timebase.now() == datetime.fromtimestamp(T0 + 10, UTC)


def test_the_wall_clocks_offset_is_stated() -> None:
    clocks = Clocks()
    timebase = clocks.timebase()
    timebase.anchor()

    clocks.tick(10)
    clocks.wall += 2.5

    state = timebase.state()
    assert state.source == "ntp"
    assert state.verified
    assert state.wall_offset_seconds == pytest.approx(2.5)
    assert state.est_error_us == 250


def test_each_anchor_takes_a_fresh_reading_while_ntp_is_synchronised() -> None:
    clocks = Clocks()
    timebase = clocks.timebase()
    timebase.anchor()
    clocks.tick(10)
    clocks.wall += 2.0  # NTP stepped the wall clock to the truth

    timebase.anchor()

    assert timebase.now() == datetime.fromtimestamp(T0 + 12, UTC)
    assert timebase.state().wall_offset_seconds == pytest.approx(0.0)


def test_a_source_that_loses_sync_keeps_the_last_good_anchor() -> None:
    """NTP unreachable for a while: the last reading carried forward beats the bare wall."""
    clocks = Clocks()
    timebase = clocks.timebase()
    timebase.anchor()
    clocks.tick(600)
    clocks.wall += 7.0
    clocks.reading = Reading(source="ntp", synchronised=False)

    timebase.anchor()

    assert timebase.now() == datetime.fromtimestamp(T0 + 600, UTC)
    state = timebase.state()
    assert state.verified
    assert state.anchored_seconds_ago == pytest.approx(600.0)


def test_with_no_good_reading_ever_the_wall_clock_is_used_and_said_to_be_unverified() -> None:
    """A wrong time is worse than none only when it looks like a right one."""
    clocks = Clocks(synchronised=False)
    timebase = clocks.timebase()

    timebase.anchor()
    clocks.tick(5)

    assert timebase.now() == datetime.fromtimestamp(T0 + 5, UTC)
    state = timebase.state()
    assert state.source == "wall"
    assert not state.verified


def test_now_before_any_anchor_takes_one() -> None:
    clocks = Clocks()

    assert clocks.timebase().now() == datetime.fromtimestamp(T0, UTC)


@pytest.mark.skipif(sys.platform != "linux", reason="adjtimex is Linux's")
def test_the_kernel_says_whether_ntp_disciplines_its_clock() -> None:
    reading = read_kernel_clock()

    assert reading.source == "ntp"
    assert isinstance(reading.synchronised, bool)
    assert reading.max_error_us is not None
    assert reading.max_error_us >= 0
