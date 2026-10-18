"""Every camera in the registry recorded at once, each on its own.

A camera that goes away, or whose recording fails outright, is retried on its own, and every
other camera keeps recording without a gap.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from publishers import PASSWORD, open_paths
from pydantic import SecretStr
from real_mediamtx import READER_USER, MediaMTX
from visionquery.memory.cameras import Camera
from visionquery.recorder.camera import Timing
from visionquery.recorder.ffmpeg import Source
from visionquery.recorder.fleet import Fleet
from visionquery.recorder.segment import Segment, segments

ADDED = datetime(2026, 10, 6, tzinfo=UTC)
DOCK = Camera(1, "Dock", "dock", None, ADDED, None)
GATE = Camera(2, "Gate 2", "gate", None, ADDED, None)
TIMING = Timing(segment=timedelta(seconds=2), first_wait=0.2, last_wait=1.0, stall_after=5.0)


def sources(server: MediaMTX, ffmpeg: Path) -> Callable[[Camera], Source]:
    def source(camera: Camera) -> Source:
        url = server.rtsp_url(camera.path, READER_USER, server.reader_password)
        return Source(url=SecretStr(url), ffmpeg=str(ffmpeg), stall_after=TIMING.stall_after)

    return source


async def until(condition: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"not within {timeout} s"
        await asyncio.sleep(0.1)


def of(footage: Path, camera: Camera) -> list[Segment]:
    return segments(footage / str(camera.id))


def unbroken(found: list[Segment]) -> bool:
    """Whether the Segments run on one from the next, as rolling ones do."""
    return all(a.time_to == b.time_from for a, b in pairwise(found))


@pytest.fixture
def both(mediamtx: MediaMTX) -> MediaMTX:
    open_paths(mediamtx, DOCK.path, GATE.path)
    return mediamtx


def test_each_camera_is_recorded_into_a_directory_of_its_own(
    both: MediaMTX, ffmpeg: Path, tmp_path: Path
) -> None:
    async def run() -> None:
        fleet = Fleet(tmp_path, sources(both, ffmpeg), TIMING)
        fleet.reconcile([DOCK, GATE])
        try:
            with (
                both.publishing(DOCK.path, DOCK.path, PASSWORD),
                both.publishing(GATE.path, GATE.path, PASSWORD),
            ):
                await until(lambda: bool(of(tmp_path, DOCK)) and bool(of(tmp_path, GATE)), 30)
        finally:
            await fleet.stop()

    asyncio.run(run())

    assert {path.name for path in tmp_path.iterdir()} == {"1", "2"}


def test_a_camera_that_goes_away_does_not_stop_the_others(
    both: MediaMTX, ffmpeg: Path, tmp_path: Path
) -> None:
    """The gate publishes for three seconds and drops; the dock's Segments run on unbroken."""

    async def run() -> None:
        fleet = Fleet(tmp_path, sources(both, ffmpeg), TIMING)
        fleet.reconcile([DOCK, GATE])
        try:
            with both.publishing(DOCK.path, DOCK.path, PASSWORD):
                await until(lambda: len(of(tmp_path, DOCK)) >= 1, 30)
                await asyncio.to_thread(both.publish, GATE.path, GATE.path, PASSWORD, 3)
                await until(lambda: any(s.reason == "stream_ended" for s in of(tmp_path, GATE)), 30)
                dropped = time.monotonic()
                await until(lambda: time.monotonic() - dropped > 5, 10)
        finally:
            await fleet.stop()

    asyncio.run(run())

    dock = of(tmp_path, DOCK)
    assert len(dock) >= 5
    assert unbroken(dock)
    assert {s.reason for s in dock[:-1]} == {"rolled"}
    assert dock[-1].reason == "stopped"


def test_a_camera_whose_recording_fails_is_started_again_alone(
    both: MediaMTX, ffmpeg: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The gate's directory cannot be made (a file is in the way): its recording fails, is
    logged with what failed, and is started again after a wait, while the dock records on."""
    caplog.set_level(logging.INFO, logger="visionquery.recorder")
    (tmp_path / str(GATE.id)).write_text("in the way")

    async def run() -> None:
        fleet = Fleet(tmp_path, sources(both, ffmpeg), TIMING)
        fleet.reconcile([DOCK, GATE])
        try:
            with (
                both.publishing(DOCK.path, DOCK.path, PASSWORD),
                both.publishing(GATE.path, GATE.path, PASSWORD),
            ):
                failed = lambda: [r for r in caplog.records if r.levelno == logging.ERROR]  # noqa: E731
                await until(lambda: len(failed()) >= 2, 30)
                (tmp_path / str(GATE.id)).unlink()  # the way is clear again
                await until(lambda: bool(of(tmp_path, GATE)), 30)
                await until(lambda: len(of(tmp_path, DOCK)) >= 4, 30)
        finally:
            await fleet.stop()

    asyncio.run(run())

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert all("Gate 2" in r.getMessage() for r in errors)
    assert all(r.exc_info is not None for r in errors)
    assert unbroken(of(tmp_path, DOCK))


def test_cameras_added_and_removed_are_started_and_stopped(
    both: MediaMTX, ffmpeg: Path, tmp_path: Path
) -> None:
    async def run() -> None:
        fleet = Fleet(tmp_path, sources(both, ffmpeg), TIMING)
        try:
            with (
                both.publishing(DOCK.path, DOCK.path, PASSWORD),
                both.publishing(GATE.path, GATE.path, PASSWORD),
            ):
                fleet.reconcile([DOCK])
                await until(lambda: bool(of(tmp_path, DOCK)), 30)
                assert fleet.recording == {DOCK.id}

                fleet.reconcile([DOCK, GATE])
                await until(lambda: bool(of(tmp_path, GATE)), 30)
                assert fleet.recording == {DOCK.id, GATE.id}

                fleet.reconcile([GATE])
                await until(lambda: of(tmp_path, DOCK)[-1].reason == "stopped", 30)
                assert fleet.recording == {GATE.id}
        finally:
            await fleet.stop()

    asyncio.run(run())

    assert of(tmp_path, GATE)[-1].reason == "stopped"
