"""Cutting one connection's Fragments into Segments, on epoch-aligned boundaries, in the
timebase's time.

The Fragments are hand-built and their arrivals told by a clock moved by hand, so a boundary
falls exactly where a test puts it. A Segment rolls at the first Fragment that starts at or
after its window's end: copy-only Footage can be cut only at a keyframe, so each Segment begins
at the first keyframe in its window, and ends with the Fragment that holds the boundary.
"""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from boxes import fragments, parsed_init
from clocks import Clocks
from visionquery.recorder.camera import Segmenter
from visionquery.recorder.segment import Segment, recover, segments
from visionquery.recorder.timebase import Reading

FIVE_MINUTES = timedelta(minutes=5)


def at(text: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-06T{text}+00:00")


def record(
    directory: Path, clocks: Clocks, length: timedelta, seconds: float, *, start: datetime
) -> tuple[list[Segment], Segment | None]:
    """Two-second Fragments arriving from `start` for `seconds`, then the stream ends."""
    clocks.wall = start.timestamp()
    timebase = clocks.timebase()
    segmenter = Segmenter(directory, parsed_init(), timebase, length, not_before=timebase.now())
    rolled: list[Segment] = []
    for frag in fragments(int(seconds // 2)):
        clocks.tick(2)
        done = segmenter.add(frag, arrived_at=timebase.now())
        if done is not None:
            rolled.append(done)
    return rolled, segmenter.finish("stream_ended")


def test_a_segment_rolls_at_the_first_keyframe_past_an_epoch_aligned_boundary(
    tmp_path: Path,
) -> None:
    """Recording begins at 10:03:52, so the first Segment fills the rest of the 10:00 window."""
    rolled, last = record(tmp_path, Clocks(), FIVE_MINUTES, 200, start=at("10:03:52"))

    (first,) = rolled
    assert last is not None
    assert (first.time_from, first.reason) == (at("10:03:52"), "rolled")
    # The Fragment arriving at 10:05:00 started at 10:04:58, inside the first window.
    assert first.time_to == at("10:05:00")
    assert last.time_from == at("10:05:00")
    assert last.time_to == at("10:07:12")
    assert [segment.path.name[:6] for segment in (first, last)] == ["100000", "100500"]


def test_every_window_after_the_first_is_one_whole_segment(tmp_path: Path) -> None:
    rolled, last = record(tmp_path, Clocks(), FIVE_MINUTES, 17 * 60, start=at("10:03:52"))

    assert [(segment.time_from, segment.time_to) for segment in rolled] == [
        (at("10:03:52"), at("10:05:00")),
        (at("10:05:00"), at("10:10:00")),
        (at("10:10:00"), at("10:15:00")),
        (at("10:15:00"), at("10:20:00")),
    ]
    assert last is not None
    assert last.time_from == at("10:20:00")


def test_the_segment_length_is_configurable(tmp_path: Path) -> None:
    rolled, last = record(tmp_path, Clocks(), timedelta(seconds=30), 70, start=at("10:00:10"))

    assert [segment.time_from for segment in rolled] == [at("10:00:10"), at("10:00:30")]
    assert [segment.time_to for segment in rolled] == [at("10:00:30"), at("10:01:00")]
    assert last is not None
    assert (last.time_from, last.time_to) == (at("10:01:00"), at("10:01:20"))


def test_segments_tile_the_recording_without_overlap_or_gap(tmp_path: Path) -> None:
    rolled, last = record(tmp_path, Clocks(), timedelta(seconds=30), 180, start=at("10:00:07"))
    assert last is not None

    found = segments(tmp_path)

    assert found == [*rolled, last]
    assert all(a.time_to == b.time_from for a, b in pairwise(found))


def test_segment_times_are_the_timebases_never_the_files(tmp_path: Path) -> None:
    """The timebase says it is 2001; the files are written now. The Segments say 2001."""
    start = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)
    rolled, last = record(tmp_path, Clocks(wall=start.timestamp()), FIVE_MINUTES, 10, start=start)

    assert rolled == []
    assert last is not None
    assert (last.time_from, last.time_to) == (start, start + timedelta(seconds=10))
    written = datetime.fromtimestamp(last.path.stat().st_mtime, UTC)
    assert written.year >= 2026
    (found,) = segments(tmp_path)
    assert (found.time_from, found.time_to) == (last.time_from, last.time_to)


def test_a_wall_clock_stepped_mid_segment_does_not_move_its_times(tmp_path: Path) -> None:
    clocks = Clocks()
    timebase = clocks.timebase()
    segmenter = Segmenter(tmp_path, parsed_init(), timebase, FIVE_MINUTES, not_before=None)
    for n, frag in enumerate(fragments(5)):
        clocks.tick(2)
        if n == 2:
            clocks.wall += 3600  # somebody sets the clock an hour forward
        segmenter.add(frag, arrived_at=timebase.now())

    done = segmenter.finish("stream_ended")

    assert done is not None
    assert done.time_to - done.time_from == timedelta(seconds=10)


def test_a_segment_opens_on_a_fresh_ntp_reading(tmp_path: Path) -> None:
    """The timebase is anchored again when a Segment opens; an arrival measured just before is
    moved onto the new anchor with it."""
    clocks = Clocks()
    timebase = clocks.timebase()
    segmenter = Segmenter(tmp_path, parsed_init(), timebase, timedelta(seconds=10), not_before=None)
    parts = fragments(8)
    for frag in parts[:5]:
        clocks.tick(2)
        segmenter.add(frag, arrived_at=timebase.now())
    clocks.tick(2)
    clocks.wall += 0.5  # NTP corrected the wall clock while the first Segment was written
    arrived = timebase.now()

    rolled = segmenter.add(parts[5], arrived_at=arrived)

    assert rolled is not None
    done = segmenter.finish("stopped")
    assert done is not None
    assert done.time_to == arrived + timedelta(seconds=0.5)


def test_whether_ntp_stood_behind_the_times_is_kept(tmp_path: Path) -> None:
    clocks = Clocks(synchronised=False)
    timebase = clocks.timebase()
    segmenter = Segmenter(tmp_path, parsed_init(), timebase, FIVE_MINUTES, not_before=None)
    clocks.tick(2)
    segmenter.add(fragments(1)[0], arrived_at=timebase.now())

    done = segmenter.finish("stream_ended")

    assert done is not None
    assert not done.verified


def test_a_connection_that_sent_no_fragment_leaves_nothing(tmp_path: Path) -> None:
    clocks = Clocks()
    segmenter = Segmenter(tmp_path, parsed_init(), clocks.timebase(), FIVE_MINUTES, not_before=None)

    assert segmenter.finish("stream_ended") is None
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


def test_a_rolled_segment_begins_exactly_where_the_one_before_it_ended(tmp_path: Path) -> None:
    """Its media runs on from the last one's, so no instant falls between them, however the
    clock was slewed when the new Segment's anchor was taken or late a Fragment arrived; and it
    is still filed under the window it begins."""
    clocks = Clocks()
    timebase = clocks.timebase()
    length = timedelta(seconds=10)
    segmenter = Segmenter(tmp_path, parsed_init(), timebase, length, not_before=None)
    rolled: list[Segment] = []
    for n, frag in enumerate(fragments(15)):
        clocks.tick(2)
        clocks.wall += 0.000_25  # NTP slewing the wall clock, a little every second
        late = timedelta(milliseconds=3 if n % 2 else 0)
        done = segmenter.add(frag, arrived_at=timebase.now() + late)
        if done is not None:
            rolled.append(done)
    last = segmenter.finish("stopped")
    assert last is not None

    found = [*rolled, last]

    assert len(found) == 3
    assert all(a.time_to == b.time_from for a, b in pairwise(found))
    assert [segment.path.name[:6] for segment in found] == ["100000", "100010", "100020"]


# --- the clock stepped when the timebase is anchored again -------------------------------------
#
# The README's "no NTP yet, recording anyway" case, then NTP syncs and steps the wall clock: the
# normal sequence on a host that has just booted. A Segment must never claim time that was not
# recorded, nor have a negative length, nor stand verified on a timeline it was not measured on.


def stepped(
    directory: Path, step: float, *, at_fragment: int, length: timedelta = timedelta(seconds=10)
) -> tuple[list[Segment], list[timedelta]]:
    """Two-second Fragments from 10:00:00 on a clock that has no NTP, until, just before the
    Fragment `at_fragment` arrives, NTP syncs and steps the wall clock by `step` seconds. The
    first Segment ends at the fifth Fragment, so `at_fragment=5` is a roll and `0` is the first
    Fragment of a connection. The Segments, and the steps the segmenter noticed."""
    clocks = Clocks(synchronised=False)
    timebase = clocks.timebase()
    steps: list[timedelta] = []
    segmenter = Segmenter(
        directory, parsed_init(), timebase, length, not_before=timebase.now(), on_step=steps.append
    )
    found: list[Segment] = []
    for n, frag in enumerate(fragments(8)):
        clocks.tick(2)
        if n == at_fragment:
            clocks.reading = Reading(source="ntp", synchronised=True, est_error_us=250)
            clocks.wall += step
        done = segmenter.add(frag, arrived_at=timebase.now())
        if done is not None:
            found.append(done)
    last = segmenter.finish("stream_ended")
    assert last is not None
    return [*found, last], steps


@pytest.mark.parametrize("step", [3600.0, -3600.0, 45.0, -45.0, 0.5])
def test_a_clock_stepped_at_a_roll_starts_the_next_segment_on_the_new_timeline(
    tmp_path: Path, step: float
) -> None:
    found, steps = stepped(tmp_path, step, at_fragment=5)

    first, second = found
    jump = timedelta(seconds=step)
    assert steps == [jump]
    # The second Segment is what was received after the step: three Fragments, the first of them
    # ending at 10:00:12 on the old timeline, so at 10:00:12 + step on the new, and lasting two
    # seconds; the span is the six seconds received, wherever the clock then was.
    assert second.time_from == at("10:00:10") + jump
    assert second.time_to == at("10:00:16") + jump
    assert second.time_to >= second.time_from
    begun = second.time_from
    window_start = begun.replace(second=begun.second // 10 * 10, microsecond=0)
    assert second.path.name[:6] == window_start.strftime("%H%M%S"), "filed where it really is"
    # The first stands where it was measured, on the clock that had no NTP.
    assert (first.time_from, first.time_to) == (at("10:00:00"), at("10:00:10"))
    assert not first.verified
    assert second.verified, "all of it was measured on the synchronised reading"


@pytest.mark.parametrize("step", [3600.0, -3600.0, 45.0, -45.0])
def test_a_clock_stepped_before_a_connections_first_fragment_does_not_pin_it_to_the_old_one(
    tmp_path: Path, step: float
) -> None:
    """The connection opened at 10:00:00, on the old timeline; its first Fragment arrives after
    the step. Nothing about the old time bounds it."""
    (only,), steps = stepped(tmp_path, step, at_fragment=0, length=timedelta(minutes=5))

    jump = timedelta(seconds=step)
    assert steps == [jump]
    assert only.time_from == at("10:00:00") + jump
    assert only.time_to == at("10:00:16") + jump
    assert only.verified


def test_a_slew_within_the_tolerance_is_not_a_step(tmp_path: Path) -> None:
    """NTP slews the clock by up to 500 ppm: 150 ms in a five-minute Segment. That keeps the
    Segments joined, exactly as before, and is not reported."""
    found, steps = stepped(tmp_path, 0.1, at_fragment=5)

    first, second = found
    assert steps == []
    assert second.time_from == first.time_to


def test_a_segment_pinned_to_a_time_nothing_vouched_for_is_not_verified(tmp_path: Path) -> None:
    """A slew too small to be a step joins the new Segment to the end of one measured with no
    NTP: its beginning is that unverified time, so it is not verified, though its anchor is.
    The one after it begins on a time NTP did vouch for, and is."""
    clocks = Clocks(synchronised=False)
    timebase = clocks.timebase()
    segmenter = Segmenter(
        tmp_path, parsed_init(), timebase, timedelta(seconds=10), not_before=timebase.now()
    )
    found: list[Segment] = []
    for n, frag in enumerate(fragments(14)):
        clocks.tick(2)
        if n == 5:
            clocks.reading = Reading(source="ntp", synchronised=True)
            clocks.wall += 0.01
        done = segmenter.add(frag, arrived_at=timebase.now())
        if done is not None:
            found.append(done)
    last = segmenter.finish("stopped")
    assert last is not None

    first, second, third = [*found, last]

    assert [s.verified for s in (first, second, third)] == [False, False, True]
    assert second.time_from == first.time_to


def test_a_connection_pinned_to_an_unverified_connect_time_is_not_verified(tmp_path: Path) -> None:
    clocks = Clocks(synchronised=False)
    timebase = clocks.timebase()
    not_before = timebase.now()
    clocks.tick(1)
    clocks.reading = Reading(source="ntp", synchronised=True)
    segmenter = Segmenter(tmp_path, parsed_init(), timebase, FIVE_MINUTES, not_before=not_before)
    clocks.tick(1)  # the first Fragment lasts two seconds, so it begins before the connection
    segmenter.add(fragments(1)[0], arrived_at=timebase.now())

    done = segmenter.finish("stopped")

    assert done is not None
    assert done.time_from == not_before
    assert not done.verified


@pytest.mark.parametrize("step", [3600.0, -3600.0, 45.0, -45.0])
def test_the_segment_after_a_step_says_by_how_much_the_clock_stepped(
    tmp_path: Path, step: float
) -> None:
    """The index must tell a step's overlap or gap from a fault or an outage: the Segment that
    begins on the new time carries the shift, in its index and in what says it is finished."""
    found, _ = stepped(tmp_path, step, at_fragment=5)

    first, second = found
    assert first.stepped_by is None
    assert second.stepped_by == step
    index = [
        json.loads(line) for line in second.path.with_suffix(".frags").read_text().splitlines()
    ]
    assert index[0]["stepped_by"] == step
    assert all("stepped_by" not in line for line in index[1:])
    assert json.loads(second.path.with_suffix(".closed").read_text())["stepped_by"] == step
    assert "stepped_by" not in json.loads(first.path.with_suffix(".closed").read_text())
    assert {segment.path: segment.stepped_by for segment in segments(tmp_path)} == {
        first.path: None,
        second.path: step,
    }


def test_a_slew_is_not_recorded_as_a_step(tmp_path: Path) -> None:
    found, _ = stepped(tmp_path, 0.1, at_fragment=5)

    assert [segment.stepped_by for segment in found] == [None, None]


def test_a_stepped_segment_recovered_after_a_crash_still_says_so(tmp_path: Path) -> None:
    clocks = Clocks(synchronised=False)
    timebase = clocks.timebase()
    segmenter = Segmenter(
        tmp_path, parsed_init(), timebase, timedelta(seconds=10), not_before=timebase.now()
    )
    for n, frag in enumerate(fragments(7)):
        clocks.tick(2)
        if n == 5:
            clocks.reading = Reading(source="ntp", synchronised=True, est_error_us=250)
            clocks.wall += 3600
        segmenter.add(frag, arrived_at=timebase.now())
    # The process dies here, the second Segment unfinished.

    (found,) = recover(tmp_path)

    assert found.stepped_by == 3600.0
    assert [segment.stepped_by for segment in segments(tmp_path)] == [None, 3600.0]


# --- a Fragment may be as long as a camera's keyframe interval ----------------------------------


def test_a_camera_with_fragments_longer_than_the_slack_records_across_several_rolls(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A keyframe every 40 s (1 fps with a GOP of 40) against 60 s windows: each Segment's last
    Fragment ends well past its window, which is what copy-only Footage is, and must neither stop
    the recording nor leave a Segment unfinished."""
    clocks = Clocks()
    clocks.wall = at("10:00:15").timestamp()
    timebase = clocks.timebase()
    segmenter = Segmenter(
        tmp_path, parsed_init(), timebase, timedelta(seconds=60), not_before=timebase.now()
    )
    caplog.set_level(logging.ERROR, logger="visionquery.recorder")
    rolled: list[Segment] = []
    for frag in fragments(8):
        clocks.tick(40)
        done = segmenter.add(
            replace(frag, duration=round(frag.timescale * 40)), arrived_at=timebase.now()
        )
        if done is not None:
            rolled.append(done)
    last = segmenter.finish("stream_ended")

    assert len(rolled) >= 3
    assert last is not None
    found = [*rolled, last]
    assert [segment.reason for segment in found] == ["rolled"] * len(rolled) + ["stream_ended"]
    assert all(segment.verified for segment in found)
    assert caplog.records == []
    assert segments(tmp_path) == found
    assert not list((tmp_path / ".open").iterdir()), "every Segment is finished"
    for before, after in pairwise(found):
        assert after.time_from == before.time_to


# --- a Segment that cannot be finished is recovered at once, and never raised into the camera ---


def fail_closed_writes(monkeypatch: pytest.MonkeyPatch, times: int) -> None:
    """The next `times` writes of a `.closed` fail, as they do on a full disk: the file is
    written aside and moved into place, so it is the move that fails."""
    real = Path.replace
    remaining = [times]

    def replace(self: Path, target: Any) -> Path:
        if self.name.endswith(".closed.tmp") and remaining[0] > 0:
            remaining[0] -= 1
            raise OSError(28, "No space left on device")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", replace)


def two_fragments_in(tmp_path: Path) -> Segmenter:
    clocks = Clocks()
    timebase = clocks.timebase()
    segmenter = Segmenter(
        tmp_path, parsed_init(), timebase, FIVE_MINUTES, not_before=timebase.now()
    )
    for frag in fragments(2):
        clocks.tick(2)
        segmenter.add(frag, arrived_at=timebase.now())
    return segmenter


@pytest.mark.parametrize("reason", ["stopped", "stream_ended"])
def test_a_segment_whose_finish_fails_is_finished_recovered_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    segmenter = two_fragments_in(tmp_path)
    fail_closed_writes(monkeypatch, 1)
    caplog.set_level(logging.ERROR, logger="visionquery.recorder")

    done = segmenter.finish(reason)  # type: ignore[arg-type]

    assert done is not None
    assert (done.reason, done.fragments) == ("recovered", 2)
    assert [segment.reason for segment in segments(tmp_path)] == ["recovered"]
    assert not list((tmp_path / ".open").iterdir()), "no note is left on a live connection"
    assert recover(tmp_path) == []


def test_a_segment_that_cannot_be_finished_at_all_is_left_for_the_next_start_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segmenter = two_fragments_in(tmp_path)
    fail_closed_writes(monkeypatch, 2)

    assert segmenter.finish("stopped") is None  # the disk is full: nothing raised

    assert segments(tmp_path) == []
    assert len(list((tmp_path / ".open").iterdir())) == 1, "its note stays, for the next start"
    monkeypatch.undo()
    (found,) = recover(tmp_path)
    assert (found.reason, found.fragments) == ("recovered", 2)
