"""A Segment on disk, from its first Fragment until it is finished, and after a crash.

The file is the init followed by every Fragment, byte for byte as ffmpeg wrote them but for each
Fragment's decode time, rebased so the file's own timeline starts at zero. Each Fragment is on
disk before the index line that vouches for it, so a crash costs at most the Fragment being
written, and the next start truncates an unfinished Segment to its last whole Fragment and
finishes it `recovered`, playable and with the span it really reached.
"""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from boxes import HEAD, fragments, parsed_init
from visionquery.recorder.mp4 import Fragment, Init, Stream
from visionquery.recorder.segment import Segment, SegmentFile, recover, segments, window

AT = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
FIVE_MINUTES = timedelta(minutes=5)


def opened(directory: Path, *, not_before: datetime | None = None) -> SegmentFile:
    return SegmentFile.open(
        directory, parsed_init(), window_start=AT, not_before=not_before, verified=True
    )


def written(directory: Path, count: int) -> SegmentFile:
    """A Segment of `count` two-second Fragments, the first arriving at AT + 2 s, left open."""
    segment = opened(directory)
    for n, frag in enumerate(fragments(count)):
        segment.append(frag, arrived_at=AT + timedelta(seconds=2 * (n + 1)))
    return segment


def finished(segment: SegmentFile, reason: str = "rolled") -> Segment:
    done = segment.close(reason)  # type: ignore[arg-type]
    assert done is not None
    return done


# --- epoch-aligned windows --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "start"),
    [
        (datetime(2026, 10, 6, 10, 0, tzinfo=UTC), datetime(2026, 10, 6, 10, 0, tzinfo=UTC)),
        (
            datetime(2026, 10, 6, 10, 4, 59, 999999, tzinfo=UTC),
            datetime(2026, 10, 6, 10, 0, tzinfo=UTC),
        ),
        (datetime(2026, 10, 6, 10, 7, 30, tzinfo=UTC), datetime(2026, 10, 6, 10, 5, tzinfo=UTC)),
        (datetime(2026, 10, 6, 23, 58, tzinfo=UTC), datetime(2026, 10, 6, 23, 55, tzinfo=UTC)),
    ],
)
def test_a_window_is_aligned_to_the_epoch_not_to_when_recording_began(
    at: datetime, start: datetime
) -> None:
    assert window(at, FIVE_MINUTES) == (start, start + FIVE_MINUTES)


def test_windows_of_any_length_tile_time_from_the_epoch() -> None:
    length = timedelta(seconds=7)
    start, end = window(datetime(2026, 10, 6, 10, 0, 3, tzinfo=UTC), length)

    assert (start.timestamp() % 7, end - start) == (0, length)
    assert start <= datetime(2026, 10, 6, 10, 0, 3, tzinfo=UTC) < end


def test_a_window_is_told_in_utc_whatever_zone_the_instant_is_in() -> None:
    at = datetime(2026, 10, 6, 15, 33, tzinfo=UTC).astimezone()

    start, _ = window(at, FIVE_MINUTES)

    assert start == datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
    assert start.tzinfo is UTC


# --- the file ---------------------------------------------------------------------------------


def test_the_file_is_the_init_and_then_every_fragment(tmp_path: Path) -> None:
    segment = opened(tmp_path)
    parts = fragments(3)
    for n, frag in enumerate(parts):
        segment.append(frag, arrived_at=AT + timedelta(seconds=2 * (n + 1)))

    assert segment.path.read_bytes() == HEAD + b"".join(frag.data for frag in parts)


def test_a_segment_is_filed_by_day_and_by_the_window_it_starts(tmp_path: Path) -> None:
    """`<YYYY-MM-DD>/<HHMMSS>-<id>.mp4`, in UTC, so a day's Footage is one directory."""
    segment = SegmentFile.open(
        tmp_path,
        parsed_init(),
        window_start=datetime(2026, 10, 6, 23, 55, tzinfo=UTC),
        not_before=None,
        verified=True,
    )

    assert segment.path.parent == tmp_path / "2026-10-06"
    assert segment.path.name == f"235500-{segment.segment_id.hex}.mp4"


def test_the_span_runs_from_the_first_fragments_start_to_the_last_ones_arrival(
    tmp_path: Path,
) -> None:
    """A Fragment is written when the keyframe after it arrives, so it arrives at its end, and
    its own length, read from the media, dates its start."""
    done = finished(written(tmp_path, 3))

    assert done.time_from == AT  # the first arrived at +2 s and lasted 2 s
    assert done.time_to == AT + timedelta(seconds=6)
    assert (done.fragments, done.reason, done.verified) == (3, "rolled", True)


def test_a_segment_never_begins_before_it_could_have_been_received(tmp_path: Path) -> None:
    """MediaMTX sends a burst on connecting, so the first Fragment can arrive early: its arrival
    less its length would claim reception before the connection existed, or before the Segment
    before it ended. The bound is written down, so recovery reaches the same answer."""
    segment = opened(tmp_path / "a", not_before=AT + timedelta(seconds=1.5))
    segment.append(fragments(1)[0], arrived_at=AT + timedelta(seconds=2))
    assert finished(segment).time_from == AT + timedelta(seconds=1.5)

    crashed = opened(tmp_path / "b", not_before=AT + timedelta(seconds=1.5))
    crashed.append(fragments(1)[0], arrived_at=AT + timedelta(seconds=2))
    (found,) = recover(tmp_path / "b")
    assert found.time_from == AT + timedelta(seconds=1.5)


def test_a_segment_files_own_timeline_starts_at_zero(tmp_path: Path) -> None:
    """The stream's clock runs on across Segments; each file's must not, or it plays late."""
    later = fragments(3, first=38)
    segment = opened(tmp_path)
    for n, frag in enumerate(later):
        segment.append(frag, arrived_at=AT + timedelta(seconds=2 * (n + 1)))

    _, *stored = Stream().feed(segment.path.read_bytes())

    assert [frag.decode_time for frag in stored if isinstance(frag, Fragment)] == [0, 25600, 51200]


def test_a_segment_closed_with_nothing_in_it_leaves_nothing_behind(tmp_path: Path) -> None:
    segment = opened(tmp_path)

    assert segment.close("stream_ended") is None
    assert list(tmp_path.rglob("*.*")) == []


def test_a_reason_outside_the_closed_set_is_refused(tmp_path: Path) -> None:
    segment = written(tmp_path, 1)

    with pytest.raises(ValueError, match="closed set"):
        segment.close("timed_out")  # type: ignore[arg-type]


def test_an_unverified_clock_is_written_down_with_the_segment(tmp_path: Path) -> None:
    segment = SegmentFile.open(
        tmp_path, parsed_init(), window_start=AT, not_before=None, verified=False
    )
    segment.append(fragments(1)[0], arrived_at=AT + timedelta(seconds=2))

    assert not finished(segment).verified
    (found,) = segments(tmp_path)
    assert not found.verified


# --- finished Segments ------------------------------------------------------------------------


def test_finished_segments_are_found_again_with_their_reason(tmp_path: Path) -> None:
    segment = written(tmp_path, 2)
    finished(segment, "stream_ended")

    assert segments(tmp_path) == [
        Segment(
            segment_id=segment.segment_id,
            path=segment.path,
            time_from=AT,
            time_to=AT + timedelta(seconds=4),
            reason="stream_ended",
            fragments=2,
            verified=True,
        )
    ]


def test_segments_come_back_oldest_first_across_days(tmp_path: Path) -> None:
    later = SegmentFile.open(
        tmp_path, parsed_init(), window_start=AT + timedelta(days=1), not_before=None, verified=True
    )
    later.append(fragments(1)[0], arrived_at=AT + timedelta(days=1, seconds=2))
    finished(later)
    earlier = written(tmp_path, 1)
    finished(earlier)

    found = [segment.segment_id for segment in segments(tmp_path)]

    assert found == [earlier.segment_id, later.segment_id]


def test_a_segment_still_being_written_is_not_finished(tmp_path: Path) -> None:
    written(tmp_path, 2)

    assert segments(tmp_path) == []


def test_the_segment_id_is_fixed_when_it_opens(tmp_path: Path) -> None:
    segment = opened(tmp_path)

    assert isinstance(segment.segment_id, UUID)
    segment.append(fragments(1)[0], arrived_at=AT)
    assert finished(segment).segment_id == segment.segment_id


# --- after a crash ----------------------------------------------------------------------------


def test_an_unfinished_segment_found_after_a_crash_is_finished_recovered(tmp_path: Path) -> None:
    segment = written(tmp_path, 3)

    (found,) = recover(tmp_path)

    assert found.segment_id == segment.segment_id
    assert found.reason == "recovered"
    assert (found.time_from, found.time_to) == (AT, AT + timedelta(seconds=6))
    assert found.fragments == 3
    assert segments(tmp_path) == [found]


def test_a_fragment_torn_by_a_crash_is_cut_off(tmp_path: Path) -> None:
    """Half a Fragment on disk and no line for it: the file ends at the last whole one."""
    segment = written(tmp_path, 2)
    whole = segment.path.read_bytes()
    with segment.path.open("ab") as file:
        file.write(fragments(3)[2].data[:50])

    (found,) = recover(tmp_path)

    assert found.time_to == AT + timedelta(seconds=4)
    assert segment.path.read_bytes() == whole
    assert len(Stream().feed(segment.path.read_bytes())) == 3  # init and two Fragments


def test_a_fragment_whose_arrival_was_never_written_down_is_cut_off(tmp_path: Path) -> None:
    """The bytes landed and the line saying when did not: it cannot be dated, so it goes."""
    segment = written(tmp_path, 2)
    two = segment.path.read_bytes()
    with segment.path.open("ab") as file:  # the third Fragment lands whole...
        file.write(fragments(3)[2].data)
    with segment.path.with_suffix(".frags").open("a") as file:  # ...and its line is torn
        file.write('{"end": 9')

    (found,) = recover(tmp_path)

    assert found.fragments == 2
    assert found.time_to == AT + timedelta(seconds=4)
    assert segment.path.read_bytes() == two


def test_a_recovered_segments_index_names_only_bytes_the_file_holds(tmp_path: Path) -> None:
    """Every index line vouches for a whole Fragment, and the last one ends the file, so the
    index can be trusted to find any instant in the Segment."""
    segment = written(tmp_path, 3)
    with segment.path.open("ab") as file:
        file.write(fragments(4)[3].data[:70])

    recover(tmp_path)

    lines = segment.path.with_suffix(".frags").read_text().splitlines()
    ends = [json.loads(line)["end"] for line in lines]
    assert len(ends) == 3
    assert ends[-1] == segment.path.stat().st_size
    assert ends == sorted(ends)


def test_an_unfinished_segment_with_no_whole_fragment_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    segment = opened(tmp_path)
    with segment.path.open("ab") as file:
        file.write(fragments(1)[0].data[:30])

    assert recover(tmp_path) == []
    assert list(tmp_path.rglob("*.*")) == []


def test_recovery_is_written_down_so_a_second_start_finds_the_same_answer(
    tmp_path: Path,
) -> None:
    written(tmp_path, 2)
    first = recover(tmp_path)

    assert recover(tmp_path) == []
    assert segments(tmp_path) == first


def test_a_finished_segment_is_left_as_it_was(tmp_path: Path) -> None:
    finished(written(tmp_path, 2), "stopped")

    assert recover(tmp_path) == []
    (found,) = segments(tmp_path)
    assert found.reason == "stopped"


def test_a_segment_recovered_after_a_crash_plays_to_its_last_whole_fragment(
    tmp_path: Path, ffmpeg: Path
) -> None:
    """Real H.264 from the shipped ffmpeg, a keyframe a second: the Segment is cut mid-Fragment
    and recovered, and ffmpeg then decodes every frame of its whole Fragments, without error."""
    out = subprocess.run(
        [
            str(ffmpeg), "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10", "-t", "5",
            "-c:v", "libx264", "-g", "10", "-keyint_min", "10", "-sc_threshold", "0",
            "-pix_fmt", "yuv420p",
            "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout  # fmt: skip
    head, *items = Stream().feed(out)
    assert isinstance(head, Init)
    parts = [item for item in items if isinstance(item, Fragment)]
    segment = SegmentFile.open(tmp_path, head, window_start=AT, not_before=None, verified=True)
    for n, frag in enumerate(parts[:3]):
        segment.append(frag, arrived_at=AT + timedelta(seconds=n + 1))
    with segment.path.open("ab") as file:  # the process dies writing the fourth
        file.write(parts[3].data[: len(parts[3].data) // 2])

    (found,) = recover(tmp_path)

    decoded = subprocess.run(
        [str(ffmpeg), "-nostdin", "-v", "error", "-i", str(found.path), "-f", "framecrc", "-"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert decoded.returncode == 0
    assert decoded.stderr == ""
    frames = [line for line in decoded.stdout.splitlines() if not line.startswith("#")]
    assert len(frames) == 30  # three one-second Fragments at 10 fps
