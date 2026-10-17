"""Cutting one connection's Fragments into Segments, on epoch-aligned boundaries, in the
timebase's time.

The Fragments are hand-built and their arrivals told by a clock moved by hand, so a boundary
falls exactly where a test puts it. A Segment rolls at the first Fragment that starts at or
after its window's end: copy-only Footage can be cut only at a keyframe, so each Segment begins
at the first keyframe in its window, and ends with the Fragment that holds the boundary.
"""

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from boxes import fragments, parsed_init
from clocks import Clocks
from visionquery.recorder.camera import Segmenter
from visionquery.recorder.segment import Segment, segments

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
    assert list(tmp_path.rglob("*.*")) == []
