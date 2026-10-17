"""Recording one camera from a real MediaMTX, for as long as it runs, whatever its stream does.

MediaMTX is the Deployment's own (real_mediamtx.py); a camera is an ffmpeg container publishing
a test pattern, a keyframe a second, and the recorder reads it over RTSP as `visionquery` with
the ffmpeg it ships with. Segments are seconds long so that they roll while a test runs.
"""

import asyncio
import logging
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from publishers import PASSWORD, open_paths
from pydantic import SecretStr
from real_mediamtx import SERVICE_USER, MediaMTX
from visionquery.memory.cameras import Camera
from visionquery.recorder.camera import Backoff, Timing, record_camera
from visionquery.recorder.ffmpeg import Source
from visionquery.recorder.segment import Segment, recover, segments

DOCK = Camera(1, "Dock", "dock", None, datetime(2026, 10, 6, tzinfo=UTC), None)
#: Short Segments and short waits, so that rolling and reconnecting happen while a test runs.
TIMING = Timing(segment=timedelta(seconds=3), first_wait=0.2, last_wait=1.0, stall_after=5.0)


@pytest.fixture
def dock(mediamtx: MediaMTX) -> MediaMTX:
    open_paths(mediamtx, DOCK.path)
    return mediamtx


def source(server: MediaMTX, ffmpeg: Path, path: str) -> Source:
    url = server.rtsp_url(path, SERVICE_USER, server.password)
    return Source(url=SecretStr(url), ffmpeg=str(ffmpeg), stall_after=TIMING.stall_after)


def recording(
    server: MediaMTX,
    ffmpeg: Path,
    directory: Path,
    scenario: Callable[[asyncio.Event, asyncio.Task[None]], Awaitable[None]],
) -> None:
    """Record DOCK while `scenario` runs, then stop and wait for the recorder to finish."""

    async def run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            record_camera(DOCK, source(server, ffmpeg, DOCK.path), directory, TIMING, stop)
        )
        try:
            await scenario(stop, task)
        finally:
            stop.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=30)

    asyncio.run(run())


async def until(condition: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"not within {timeout} s"
        await asyncio.sleep(0.1)


def frames(ffmpeg: Path, segment: Segment) -> int:
    """How many frames the Segment decodes to; the test fails if it does not play cleanly.

    A stream that ended may have ended inside its last frame, kept as it arrived (see
    test_process.py), so a `stream_ended` Segment need only play to its end.
    """
    decoded = subprocess.run(
        [str(ffmpeg), "-nostdin", "-v", "error", "-i", str(segment.path), "-f", "framecrc", "-"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert decoded.returncode == 0, decoded.stderr
    if segment.reason != "stream_ended":
        assert decoded.stderr == ""
    return len([line for line in decoded.stdout.splitlines() if not line.startswith("#")])


def attempts(camera: Camera) -> float:
    value = REGISTRY.get_sample_value(
        "visionquery_recorder_connections_total", {"camera": str(camera.id)}
    )
    return value or 0.0


# --- backing off ------------------------------------------------------------------------------


def test_the_wait_before_connecting_again_doubles_up_to_its_ceiling() -> None:
    backoff = Backoff(first=1.0, last=10.0)

    assert [backoff.next() for _ in range(6)] == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]


def test_a_connection_that_received_video_starts_the_waits_again() -> None:
    backoff = Backoff(first=1.0, last=10.0)
    backoff.next()
    backoff.next()

    backoff.reset()

    assert backoff.next() == 1.0


# --- a camera, end to end ---------------------------------------------------------------------


def test_a_camera_is_recorded_into_segments_that_tile_and_play(
    dock: MediaMTX, ffmpeg: Path, tmp_path: Path
) -> None:
    async def scenario(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        await asyncio.to_thread(dock.publish, DOCK.path, DOCK.path, PASSWORD, 8)
        await until(lambda: any(s.reason == "stream_ended" for s in segments(tmp_path)), 20)

    recording(dock, ffmpeg, tmp_path, scenario)

    found = segments(tmp_path)
    assert [segment.reason for segment in found][-1] == "stream_ended"
    assert "rolled" in {segment.reason for segment in found}
    assert all(a.time_to <= b.time_from for a, b in pairwise(found))
    assert all(frames(ffmpeg, segment) > 0 for segment in found)
    assert all(segment.path.parent.parent == tmp_path for segment in found)


def test_a_camera_not_publishing_yet_is_tried_again_with_backoff_until_it_is(
    dock: MediaMTX, ffmpeg: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="visionquery.recorder")
    before = attempts(DOCK)

    async def scenario(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        await asyncio.sleep(3)
        # 0.2 + 0.4 + 0.8 + 1.0 s of waiting, and the attempts themselves: never a spin.
        assert 2 <= attempts(DOCK) - before <= 6
        await asyncio.to_thread(dock.publish, DOCK.path, DOCK.path, PASSWORD, 4)
        await until(lambda: bool(segments(tmp_path)), 20)

    recording(dock, ffmpeg, tmp_path, scenario)

    assert segments(tmp_path)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 1
    assert sum("no video yet" in r.getMessage() for r in warnings) == 1, "once, not per attempt"
    assert any("receiving video after" in r.getMessage() for r in caplog.records)
    assert all(PASSWORD not in r.getMessage() for r in caplog.records)
    assert all(dock.password not in r.getMessage() for r in caplog.records)


def test_a_stream_that_drops_is_finished_and_recorded_again_when_it_is_back(
    dock: MediaMTX, ffmpeg: Path, tmp_path: Path
) -> None:
    async def scenario(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        await asyncio.to_thread(dock.publish, DOCK.path, DOCK.path, PASSWORD, 3)
        await until(lambda: any(s.reason == "stream_ended" for s in segments(tmp_path)), 20)
        await asyncio.sleep(2)
        await asyncio.to_thread(dock.publish, DOCK.path, DOCK.path, PASSWORD, 3)
        await until(lambda: sum(s.reason == "stream_ended" for s in segments(tmp_path)) == 2, 20)

    recording(dock, ffmpeg, tmp_path, scenario)

    found = segments(tmp_path)
    ended = [i for i, segment in enumerate(found) if segment.reason == "stream_ended"]
    first_end = found[ended[0]]
    after = found[ended[0] + 1]
    # The time the camera was away is a gap between two Segments, stated rather than filled.
    assert after.time_from - first_end.time_to >= timedelta(seconds=1.5)


def test_a_camera_stopped_while_recording_finishes_its_segment_stopped(
    dock: MediaMTX, ffmpeg: Path, tmp_path: Path
) -> None:
    async def scenario(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        with dock.publishing(DOCK.path, DOCK.path, PASSWORD):
            await until(lambda: attempts(DOCK) > 0 and bool(list(tmp_path.rglob("*.mp4"))), 20)
            await asyncio.sleep(2)
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    recording(dock, ffmpeg, tmp_path, scenario)

    last = segments(tmp_path)[-1]
    assert last.reason == "stopped"
    assert frames(ffmpeg, last) > 0
    assert recover(tmp_path) == []


def test_a_recorder_that_dies_mid_segment_leaves_it_to_be_recovered_playable(
    dock: MediaMTX, ffmpeg: Path, tmp_path: Path
) -> None:
    async def scenario(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        with dock.publishing(DOCK.path, DOCK.path, PASSWORD):
            await until(lambda: bool(list(tmp_path.rglob("*.frags"))), 20)
            await asyncio.sleep(1.5)
            task.cancel()  # as abrupt as the event loop allows; test_process.py kills the process
            with pytest.raises(asyncio.CancelledError):
                await task

    recording(dock, ffmpeg, tmp_path, scenario)
    unfinished = [
        path for path in tmp_path.rglob("*.mp4") if not path.with_suffix(".closed").exists()
    ]
    assert len(unfinished) == 1

    (found,) = recover(tmp_path)

    assert found.path == unfinished[0]
    assert found.reason == "recovered"
    assert frames(ffmpeg, found) >= 10  # at least one whole second, a Fragment
