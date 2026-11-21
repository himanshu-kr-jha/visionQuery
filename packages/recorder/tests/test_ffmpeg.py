"""One run of the shipped ffmpeg against one source: copied, never decoded, into fragmented MP4.

The sources here are clips on disk, played in real time (`-re`) where the test needs a live
stream; MediaMTX itself is in test_camera.py and test_process.py.
"""

import asyncio
import logging
import subprocess
import time
import uuid
from itertools import pairwise
from pathlib import Path

import pytest
from publishers import PASSWORD, open_paths
from pydantic import SecretStr
from real_mediamtx import READER_USER, MediaMTX
from visionquery.recorder.ffmpeg import Recording, Source
from visionquery.recorder.mp4 import Fragment, Init


@pytest.fixture(scope="module")
def clip(ffmpeg: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Four seconds of H.264 at 10 fps, a keyframe every second."""
    path = tmp_path_factory.mktemp("clip") / "clip.mp4"
    subprocess.run(
        [
            str(ffmpeg), "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10", "-t", "4",
            "-c:v", "libx264", "-g", "10", "-keyint_min", "10", "-sc_threshold", "0",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )  # fmt: skip
    return path


def source(ffmpeg: Path, url: str, *options: str) -> Source:
    return Source(url=SecretStr(url), options=options, ffmpeg=str(ffmpeg))


async def everything(recording: Recording) -> list[Init | Fragment]:
    async with recording:
        return [item async for item in recording]


def test_a_source_is_copied_into_an_init_and_a_fragment_per_keyframe(
    ffmpeg: Path, clip: Path
) -> None:
    recording = Recording(source(ffmpeg, str(clip)))

    head, *frags = asyncio.run(everything(recording))

    assert isinstance(head, Init)
    assert [type(frag) for frag in frags] == [Fragment] * 4
    assert [round(frag.seconds, 3) for frag in frags if isinstance(frag, Fragment)] == [1.0] * 4
    assert recording.ended_because == "the source ended"


def test_only_the_video_is_kept(ffmpeg: Path, tmp_path: Path) -> None:
    """Cameras commonly send G.711 audio, which MP4 cannot hold: video only, or no Footage."""
    with_audio = tmp_path / "with-audio.mkv"
    subprocess.run(
        [
            str(ffmpeg), "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
            "-t", "2", "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p",
            "-c:a", "pcm_alaw", str(with_audio),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )  # fmt: skip

    head, *_ = asyncio.run(everything(Recording(source(ffmpeg, str(with_audio)))))

    assert isinstance(head, Init)
    assert [track.is_video for track in head.tracks.values()] == [True]


def test_a_failure_is_said_in_ffmpegs_words_without_the_password(
    ffmpeg: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ffmpeg repeats the URL it was given in its errors; they are logged, so the password goes."""
    caplog.set_level(logging.DEBUG, logger="visionquery.recorder")
    password = "s3cret-Pa55"
    recording = Recording(source(ffmpeg, f"rtsp://visionquery:{password}@127.0.0.1:9/nobody"))

    items = asyncio.run(everything(recording))

    assert items == []
    assert recording.ended_because is not None
    assert recording.ended_because.startswith("ffmpeg exited ")
    assert password not in recording.ended_because
    assert caplog.records, "ffmpeg's stderr is logged, line by line"
    assert all(password not in record.getMessage() for record in caplog.records)
    assert password not in repr(recording.source)


def test_stop_ends_a_live_recording_promptly_with_its_last_fragment(
    ffmpeg: Path, clip: Path
) -> None:
    async def run() -> tuple[list[Init | Fragment], float]:
        recording = Recording(source(ffmpeg, str(clip), "-re", "-stream_loop", "-1"))
        items: list[Init | Fragment] = []
        async with recording:

            async def stop_soon() -> float:
                await asyncio.sleep(2.5)
                began = time.monotonic()
                await recording.stop()
                return time.monotonic() - began

            stopping = asyncio.create_task(stop_soon())
            items = [item async for item in recording]
            return items, await stopping

    items, took = asyncio.run(run())

    assert took < 6
    assert isinstance(items[0], Init)
    assert len(items) >= 2  # at least one whole second was copied before the stop


def test_leaving_without_reading_does_not_hang(ffmpeg: Path, clip: Path) -> None:
    """A pipe nobody reads fills, ffmpeg blocks on it, and waiting for it would last for ever."""

    async def run() -> None:
        async with Recording(source(ffmpeg, str(clip), "-stream_loop", "-1")) as recording:
            async for _ in recording:
                break

    began = time.monotonic()
    asyncio.run(asyncio.wait_for(run(), timeout=20))
    assert time.monotonic() - began < 20


def test_an_rtsp_source_is_read_over_tcp_and_given_up_on_when_silent(ffmpeg: Path) -> None:
    """The options are the input file's, where they apply to the camera's connection."""
    seen: list[str] = []
    with source(ffmpeg, "rtsp://visionquery:pw@mediamtx:8554/dock").command() as argv:
        listed = Path(argv[argv.index("-i") + 1])
        seen = listed.read_text().splitlines()

    assert argv[argv.index("-c") + 1] == "copy"
    assert argv[argv.index("-map") + 1] == "0:v:0"
    assert seen == [
        "ffconcat version 1.0",
        "file 'rtsp://visionquery:pw@mediamtx:8554/dock'",
        "option rtsp_transport tcp",
        "option timeout 10000000",
    ]
    assert not listed.exists(), "the file lives as long as the command does"


# --- credentials never reach a command line (ADR 0019) -----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://visionquery-reader:Pa55w0rd-Zz@mediamtx:8554/dock",
        "rtsps://visionquery-reader:Pa55w0rd-Zz@mediamtx:8322/dock",
        "rtsp://visionquery-reader:Pa55w0rd-Zz@mediamtx:8554/dock?token=Pa55w0rd-Zz",
    ],
)
def test_no_command_line_the_recorder_builds_holds_the_password_or_the_user(
    ffmpeg: Path, url: str
) -> None:
    with source(ffmpeg, url).command() as argv:
        joined = " ".join(argv)

    assert "Pa55w0rd-Zz" not in joined
    assert "visionquery-reader" not in joined
    assert "mediamtx" not in joined, "the URL is wholly in the file"


def test_the_read_password_is_in_no_process_listing_while_ffmpeg_reads_a_camera(
    mediamtx: MediaMTX, ffmpeg: Path
) -> None:
    """What any user of the host sees with `ps` while the recorder reads a camera: the password
    of the MediaMTX user that can read every camera must not be in it, nor in the process's
    `/proc/<pid>/cmdline`, which is where `ps` reads it.

    It also proves the input file's `rtsp_transport` option is applied: ffmpeg is allowed only
    the file, RTSP and TCP, and fails outright the moment it tries UDP."""
    open_paths(mediamtx, "dock")
    url = mediamtx.rtsp_url("dock", READER_USER, mediamtx.reader_password)
    password = mediamtx.reader_password

    async def run() -> tuple[str, str]:
        async with Recording(source(ffmpeg, url)) as recording:
            pid = recording.pid
            assert pid is not None
            items = aiter(recording)
            try:
                await anext(items)  # the Init: ffmpeg has read the camera
            except StopAsyncIteration:
                pytest.fail(f"ffmpeg read nothing: {recording.ended_because}")
            listing = subprocess.run(
                ["ps", "-eo", "args"], capture_output=True, text=True, check=True, timeout=30
            ).stdout
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            return listing, cmdline

    with mediamtx.publishing("dock", "dock", PASSWORD):
        listing, cmdline = asyncio.run(asyncio.wait_for(run(), timeout=60))

    assert str(ffmpeg) in cmdline, "the process looked at is the recorder's ffmpeg"
    assert password not in cmdline
    assert password not in listing
    assert READER_USER not in cmdline, "nor the user's name: it is half of the credential"
    assert "127.0.0.1" not in cmdline, "the URL itself is in a file only the recorder can read"


def test_a_camera_that_goes_silent_is_given_up_on_after_the_timeout_in_the_file(
    mediamtx: MediaMTX, ffmpeg: Path
) -> None:
    """A camera can stop sending without anything closing its connection (here its container is
    paused). The `timeout` option, now an `option` line of the input file, is what makes ffmpeg
    give up on it, so that the recorder finishes what it has and connects again."""
    open_paths(mediamtx, "dock")
    url = mediamtx.rtsp_url("dock", READER_USER, mediamtx.reader_password)
    name = f"vq-test-silent-{uuid.uuid4().hex[:12]}"

    async def run() -> tuple[float, str | None]:
        stalled = Source(url=SecretStr(url), ffmpeg=str(ffmpeg), stall_after=2.0)
        async with Recording(stalled) as recording:
            items = aiter(recording)
            await anext(items)
            subprocess.run(["docker", "pause", name], check=True, capture_output=True, timeout=30)
            began = time.monotonic()
            async for _ in items:
                pass
            return time.monotonic() - began, recording.ended_because

    with mediamtx.publishing("dock", "dock", PASSWORD, name=name):
        try:
            waited, why = asyncio.run(asyncio.wait_for(run(), timeout=60))
        finally:
            subprocess.run(["docker", "unpause", name], capture_output=True, timeout=30)

    assert 1.5 <= waited < 12, f"given up on after {waited:.1f} s, not the 2 s asked for"
    assert why is not None
    assert why.startswith("ffmpeg exited ")


def test_a_live_camera_read_through_the_file_keeps_its_own_timestamps(
    mediamtx: MediaMTX, ffmpeg: Path, tmp_path: Path
) -> None:
    """Reading through the concat demuxer must not move a frame: Fragments follow one another
    in decode time, each lasting what the camera's keyframe interval says (a keyframe a
    second), and the whole plays from time zero without a jump."""
    open_paths(mediamtx, "dock")
    url = mediamtx.rtsp_url("dock", READER_USER, mediamtx.reader_password)

    async def run() -> list[Init | Fragment]:
        async with Recording(source(ffmpeg, url)) as recording:
            items: list[Init | Fragment] = []
            async for item in recording:
                items.append(item)
                if sum(isinstance(i, Fragment) for i in items) == 4:
                    await recording.stop()
            return items

    with mediamtx.publishing("dock", "dock", PASSWORD):
        items = asyncio.run(asyncio.wait_for(run(), timeout=60))

    fragments = [item for item in items if isinstance(item, Fragment)]
    assert len(fragments) >= 4
    assert all(abs(frag.seconds - 1.0) < 0.15 for frag in fragments[:3]), [
        frag.seconds for frag in fragments
    ]
    for before, after in pairwise(fragments):
        assert after.decode_time == before.decode_time + before.duration
    whole = tmp_path / "live.mp4"
    whole.write_bytes(b"".join(item.data for item in items if not isinstance(item, Init)))
    head = next(item for item in items if isinstance(item, Init))
    whole.write_bytes(head.data + whole.read_bytes())
    decoded = subprocess.run(
        [str(ffmpeg), "-nostdin", "-v", "error", "-i", str(whole), "-f", "framecrc", "-"],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert decoded.returncode == 0, decoded.stderr
    times = [
        int(line.split(",")[2]) for line in decoded.stdout.splitlines() if not line.startswith("#")
    ]
    assert times == sorted(times), "frames in order"
    assert times[0] < 15_000, "the timeline starts at the camera's first frame"
