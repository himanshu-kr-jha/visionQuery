"""One run of the shipped ffmpeg against one source: copied, never decoded, into fragmented MP4.

The sources here are clips on disk, played in real time (`-re`) where the test needs a live
stream; MediaMTX itself is in test_camera.py and test_process.py.
"""

import asyncio
import logging
import subprocess
import time
from pathlib import Path

import pytest
from pydantic import SecretStr
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
    argv = source(ffmpeg, "rtsp://visionquery:pw@mediamtx:8554/dock").argv()

    assert argv[argv.index("-rtsp_transport") + 1] == "tcp"
    assert int(argv[argv.index("-timeout") + 1]) == 10_000_000
    assert argv[argv.index("-c") + 1] == "copy"
    assert argv[argv.index("-map") + 1] == "0:v:0"
