"""The recorder reads fragmented MP4 as ffmpeg writes it, one Fragment at a time.

A Fragment is the only place a Segment can be cut, rolled, or truncated after a crash and still
be a file a player opens, so the reader must know where each one ends while the stream is still
arriving, and how long each one lasts, because a Segment's span is read from the media. It
therefore never hands a Fragment over until its `mdat` is complete, and never accepts one it
cannot date.
"""

import random
import struct
import subprocess
from pathlib import Path

import pytest
from boxes import VIDEO, box, fragment, init, moof, traf
from visionquery.recorder.mp4 import Fragment, Init, MalformedError, Stream, rebase


def everything(data: bytes, chunk: int | None = None) -> list[Init | Fragment]:
    stream = Stream()
    if chunk is None:
        return stream.feed(data)
    out: list[Init | Fragment] = []
    for at in range(0, len(data), chunk):
        out += stream.feed(data[at : at + chunk])
    return out


def two(data: bytes) -> tuple[Init, Fragment]:
    head, frag = everything(data)
    assert isinstance(head, Init)
    assert isinstance(frag, Fragment)
    return head, frag


# --- the shape --------------------------------------------------------------------------------


def test_the_init_comes_first_and_then_one_item_per_fragment() -> None:
    head = init(VIDEO)
    one = fragment(1, 0, [512] * 50)
    second = fragment(1, 25600, [512] * 50)

    items = everything(head + one + second)

    assert [type(item) for item in items] == [Init, Fragment, Fragment]
    assert [item.data for item in items] == [head, one, second]


def test_a_fragment_carries_when_it_starts_and_how_long_it_lasts() -> None:
    """Both from the media: `tfdt` for the start, the samples' durations for the length."""
    _, first, second = everything(
        init(VIDEO) + fragment(1, 0, [512] * 50) + fragment(1, 25600, [512] * 50)
    )
    assert isinstance(first, Fragment)
    assert isinstance(second, Fragment)

    assert (first.decode_time, first.duration, first.timescale) == (0, 25600, 12800)
    assert first.seconds == pytest.approx(2.0)
    assert second.start_seconds == pytest.approx(2.0)


def test_a_duration_may_come_from_the_track_fragment_default() -> None:
    _, frag = two(init(VIDEO) + fragment(1, 0, default_duration=256, samples=100))

    assert frag.duration == 25600


def test_a_duration_may_come_from_the_movie_extends_default() -> None:
    _, frag = two(init(VIDEO, trex={1: 128}) + fragment(1, 0, samples=10))

    assert frag.duration == 1280


def test_the_video_track_dates_a_fragment_that_also_carries_audio() -> None:
    """A camera with a microphone writes a run per track into one `moof`; the Fragment is dated
    by its video, because the keyframe that closed it is video."""
    audio = (2, 48000, b"soun")
    both = moof(traf(2, 0, [1024] * 94), traf(1, 25600, [512] * 50))

    _, frag = two(init(audio, VIDEO) + both)

    assert (frag.track_id, frag.timescale) == (1, 12800)
    assert (frag.decode_time, frag.duration) == (25600, 25600)


def test_a_stream_without_video_is_dated_by_its_only_track() -> None:
    _, frag = two(init((2, 48000, b"soun")) + fragment(2, 0, [1024] * 94))

    assert (frag.track_id, frag.timescale, frag.duration) == (2, 48000, 96256)


# --- arriving in pieces -----------------------------------------------------------------------


def test_a_fragment_is_withheld_until_its_mdat_is_complete() -> None:
    head = init(VIDEO)
    frag = fragment(1, 0, [512] * 50)
    stream = Stream()

    assert [type(item) for item in stream.feed(head + frag[:-1])] == [Init]
    assert stream.buffered == len(frag) - 1

    (done,) = stream.feed(frag[-1:])
    assert done.data == frag
    assert stream.buffered == 0


@pytest.mark.parametrize("chunk", [1, 7, 4096])
def test_bytes_arriving_in_any_pieces_give_the_same_items(chunk: int) -> None:
    data = init(VIDEO) + b"".join(fragment(1, n * 25600, [512] * 50) for n in range(4))

    assert everything(data, chunk) == everything(data)


def test_a_random_chunking_gives_the_same_items() -> None:
    data = init(VIDEO) + b"".join(fragment(1, n * 25600, [512] * 50) for n in range(6))
    rng = random.Random(13)
    stream, out, at = Stream(), list[Init | Fragment](), 0
    while at < len(data):
        step = rng.randint(1, 300)
        out += stream.feed(data[at : at + step])
        at += step

    assert out == everything(data)


def test_boxes_between_fragments_are_skipped_and_attached_to_neither() -> None:
    """`mfra` at the end of a cleanly closed stream, `free` padding, `sidx` indexes."""
    one = fragment(1, 0, [512] * 50)
    second = fragment(1, 25600, [512] * 50)
    data = init(VIDEO) + one + box(b"free", b"\0" * 10) + second + box(b"mfra", b"\0" * 16)

    _, a, b = everything(data)

    assert (a.data, b.data) == (one, second)


def test_a_box_with_a_64_bit_size_is_read() -> None:
    payload = b"\x02" * 40
    big_mdat = struct.pack(">I4sQ", 1, b"mdat", 16 + len(payload)) + payload
    whole = fragment(1, 0, [512] * 50)
    moof_only = whole[: len(whole) - len(box(b"mdat", b"\x01" * 32))]

    _, frag = two(init(VIDEO) + moof_only + big_mdat)

    assert frag.data == moof_only + big_mdat


# --- refusals ---------------------------------------------------------------------------------


def test_a_fragment_before_any_init_is_refused() -> None:
    with pytest.raises(MalformedError, match="before"):
        Stream().feed(fragment(1, 0, [512] * 50))


def test_a_box_that_runs_to_the_end_of_the_stream_is_refused() -> None:
    """Size 0 means "to the end of the file", which a live stream does not have."""
    with pytest.raises(MalformedError, match="end of the stream"):
        Stream().feed(init(VIDEO) + struct.pack(">I4s", 0, b"mdat") + b"\0" * 64)


def test_a_box_shorter_than_its_own_header_is_refused() -> None:
    with pytest.raises(MalformedError, match="shorter than"):
        Stream().feed(init(VIDEO) + struct.pack(">I4s", 4, b"moof"))


def test_a_moof_not_followed_by_its_mdat_is_refused() -> None:
    whole = fragment(1, 0, [512] * 50)
    moof_only = whole[: len(whole) - len(box(b"mdat", b"\x01" * 32))]
    with pytest.raises(MalformedError, match="mdat"):
        Stream().feed(init(VIDEO) + moof_only + moof_only)


def test_a_fragment_for_a_track_the_init_never_named_is_refused() -> None:
    with pytest.raises(MalformedError, match="track 9"):
        Stream().feed(init(VIDEO) + fragment(9, 0, [512] * 50))


def test_a_fragment_that_cannot_be_dated_is_refused() -> None:
    """No per-sample durations and no default anywhere: its length would be a guess."""
    with pytest.raises(MalformedError, match="duration"):
        Stream().feed(init(VIDEO) + fragment(1, 0, samples=10))


# --- what ffmpeg writes -----------------------------------------------------------------------


def test_the_shipped_ffmpeg_writes_a_fragment_per_keyframe_interval(ffmpeg: Path) -> None:
    """The real writer, a keyframe every two seconds: three Fragments of two seconds each."""
    out = subprocess.run(
        [
            str(ffmpeg), "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25", "-t", "6",
            "-c:v", "libx264", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
            "-pix_fmt", "yuv420p",
            "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout  # fmt: skip

    head, *frags = everything(out, 1000)

    assert isinstance(head, Init)
    assert all(isinstance(frag, Fragment) for frag in frags)
    dated = [frag for frag in frags if isinstance(frag, Fragment)]
    assert [round(frag.start_seconds, 3) for frag in dated] == [0.0, 2.0, 4.0]
    assert [round(frag.seconds, 3) for frag in dated] == [2.0, 2.0, 2.0]
    # Everything ffmpeg wrote is accounted for but the index it adds at the end.
    assert head.data + b"".join(frag.data for frag in dated) == out[: out.rfind(b"mfra") - 4]


# --- rebasing ---------------------------------------------------------------------------------


def test_a_fragment_rebased_onto_a_segment_starts_where_the_segment_does() -> None:
    """Every Segment file's own timeline starts at zero, or a player shows it starting late.

    Only `tfdt` changes, in place, so the Fragment keeps its length and every other byte.
    """
    _, frag = two(init(VIDEO) + fragment(1, 76 * 12800, [512] * 50))

    rebased = rebase(frag, {1: 76 * 12800})

    assert rebased.decode_time == 0
    assert len(rebased.data) == len(frag.data)
    _, again = two(init(VIDEO) + rebased.data)
    assert again.decode_time == 0
    changed = [at for at, (a, b) in enumerate(zip(frag.data, rebased.data, strict=True)) if a != b]
    tfdt = frag.data.index(b"tfdt")
    assert all(tfdt + 8 <= at < tfdt + 16 for at in changed), "only the decode time moved"


def test_every_track_is_rebased_by_its_own_start() -> None:
    audio = (2, 48000, b"soun")
    both = moof(traf(2, 48000 * 76, [1024] * 94), traf(1, 12800 * 76, [512] * 50))
    _, frag = two(init(audio, VIDEO) + both)

    rebased = rebase(frag, {1: 12800 * 76, 2: 48000 * 76})

    _, again = two(init(audio, VIDEO) + rebased.data)
    assert again.decode_time == 0
    assert again.starts == {1: 0, 2: 0}


def test_a_fragment_cannot_be_rebased_to_before_its_segment_began() -> None:
    _, frag = two(init(VIDEO) + fragment(1, 100, [512] * 50))

    with pytest.raises(MalformedError, match="before"):
        rebase(frag, {1: 200})
