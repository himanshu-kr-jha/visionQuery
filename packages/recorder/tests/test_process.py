"""`visionquery-recorder`, end to end: the camera registry in, played-back Footage out.

Against a real Postgres (packages/conftest.py), the Deployment's real MediaMTX and the ffmpeg
the recorder ships with (conftest.py). Cameras are registered in the registry as an Operator
would; MediaMTX's paths are opened as the reconciler would open them (publishers.py).
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from publishers import PASSWORD, open_paths
from pydantic import SecretStr
from real_mediamtx import SERVICE_USER, MediaMTX
from visionquery.memory.cameras import CameraRegistry
from visionquery.memory.connection import connect
from visionquery.memory.migrate import migrate
from visionquery.memory.settings import DatabaseSettings
from visionquery.recorder.segment import segments
from visionquery.recorder.serve import MediaMTXReader, RecorderSettings, health, run
from visionquery.recorder.timebase import read_kernel_clock

#: What the recorder may take, beyond the wait it is asked for, on a busy machine.
SLACK = 10.0


@pytest.fixture
def migrated(database: str) -> str:
    migrate(database)
    return database


@pytest.fixture
def registry(migrated: str) -> Iterator[CameraRegistry]:
    with connect(migrated, 5) as connection:
        yield CameraRegistry(connection)


@pytest.fixture
def on_path(ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped ffmpeg is the one the recorder finds, as it is in the image."""
    monkeypatch.setenv("PATH", f"{ffmpeg.parent}{os.pathsep}{os.environ['PATH']}")


def settings(
    database_url: str, server: MediaMTX, footage: Path, segment_seconds: int
) -> RecorderSettings:
    return RecorderSettings.model_construct(
        database=DatabaseSettings(url=SecretStr(database_url)),
        mediamtx=MediaMTXReader(
            rtsp_url=f"rtsp://127.0.0.1:{server.rtsp_port}",
            user=SERVICE_USER,
            password=SecretStr(server.password),
        ),
        footage_dir=footage,
        segment_seconds=segment_seconds,
        refresh_interval=0.5,
    )


class Running:
    """`run`, in an event loop of its own on a thread, until the test stops it."""

    def __init__(self, recorder: RecorderSettings, heartbeat: Path) -> None:
        self.heartbeat = heartbeat
        self._loop = asyncio.new_event_loop()
        self._stop = asyncio.Event()
        self.failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(recorder,))
        self._thread.start()

    def _run(self, recorder: RecorderSettings) -> None:
        try:
            self._loop.run_until_complete(run(recorder, self._stop, heartbeat=self.heartbeat))
        except BaseException as error:
            self.failure = error

    def stop(self) -> None:
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._stop.set)
            self._thread.join(timeout=60)
        assert not self._thread.is_alive()
        self._loop.close()


@pytest.fixture
def start(tmp_path: Path) -> Iterator[Callable[[RecorderSettings], Running]]:
    started: list[Running] = []

    def starting(recorder: RecorderSettings) -> Running:
        running = Running(recorder, tmp_path / f"healthy-{len(started)}")
        started.append(running)
        return running

    yield starting
    for running in started:
        running.stop()


def within(seconds: float, condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"not within {seconds} s"
        time.sleep(0.1)


def frame_hashes(ffmpeg: Path, video: Path) -> list[str]:
    """The MD5 of every decoded frame, in order; the test fails unless it decodes cleanly."""
    decoded = subprocess.run(
        [
            str(ffmpeg), "-nostdin", "-v", "error", "-i", str(video),
            "-fps_mode", "passthrough", "-f", "framemd5", "-",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )  # fmt: skip
    assert (decoded.returncode, decoded.stderr) == (0, "")
    return [
        line.rsplit(",", 1)[1].strip()
        for line in decoded.stdout.splitlines()
        if line and not line.startswith("#")
    ]


@pytest.fixture(scope="module")
def known_clip(ffmpeg: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Four seconds of colour bars as a lead-in, then eight of a pattern no two frames of which
    are alike: H.264 at 10 fps, a keyframe every second, as a camera sends it."""
    path = tmp_path_factory.mktemp("clip") / "known.mp4"
    subprocess.run(
        [
            str(ffmpeg), "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "smptebars=size=320x240:rate=10:duration=4",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=8",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
            "-c:v", "libx264", "-preset", "ultrafast", "-bf", "0",
            "-g", "10", "-keyint_min", "10", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )  # fmt: skip
    return path


# --- end to end -------------------------------------------------------------------------------


@pytest.mark.usefixtures("on_path")
def test_a_published_clip_is_read_back_whole_from_the_segments_covering_it(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    ffmpeg: Path,
    known_clip: Path,
    tmp_path: Path,
    start: Callable[[RecorderSettings], Running],
) -> None:
    """A camera publishes a known clip; the recorder, started once the camera is publishing,
    writes Segments that decode to the clip's own frames, every one of its known part included
    and in order, and whose times cover when it was published."""
    dock = registry.add_pushed("Dock", "dock")
    open_paths(mediamtx, dock.path)
    published: list[subprocess.CompletedProcess[str]] = []
    publisher = threading.Thread(
        target=lambda: published.append(
            mediamtx.publish_file(dock.path, dock.path, PASSWORD, known_clip)
        )
    )
    footage = tmp_path / "footage"
    publisher.start()
    try:
        mediamtx.wait_until_published(dock.path)
        running = start(settings(migrated, mediamtx, footage, segment_seconds=3))
    finally:
        publisher.join(timeout=120)
    ended = datetime.now(UTC)
    assert published[0].returncode == 0, published[0].stderr
    camera = footage / str(dock.id)
    within(SLACK, lambda: any(s.reason == "stream_ended" for s in segments(camera)))
    running.stop()

    found = segments(camera)
    recorded = [frame for segment in found for frame in frame_hashes(ffmpeg, segment.path)]
    clip = frame_hashes(ffmpeg, known_clip)
    known = clip[40:]  # the eight seconds after the lead-in
    assert len(set(known)) == 80
    # Copy-only and gapless: a run of the clip's own frames, ending where the clip does.
    assert recorded == clip[-len(recorded) :]
    assert recorded[-80:] == known
    # Cut on 3-second boundaries of the epoch, one Segment running on from the next.
    assert {segment.reason for segment in found[:-1]} == {"rolled"}
    assert found[-1].reason == "stream_ended"
    assert all(a.time_to == b.time_from for a, b in pairwise(found))
    assert all(int(s.time_from.timestamp()) % 3 == 0 for s in found[1:])
    # The span is the time the clip arrived, on the recorder's NTP-anchored timebase.
    assert abs(found[-1].time_to - ended) < timedelta(seconds=2)
    assert found[-1].time_to - found[0].time_from >= timedelta(seconds=8)
    assert all(segment.verified for segment in found) or not _ntp_synchronised()


def _ntp_synchronised() -> bool:
    return read_kernel_clock().synchronised


@pytest.mark.usefixtures("on_path")
def test_cameras_added_to_and_removed_from_the_registry_are_recorded_and_stopped(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    tmp_path: Path,
    start: Callable[[RecorderSettings], Running],
) -> None:
    footage = tmp_path / "footage"
    running = start(settings(migrated, mediamtx, footage, segment_seconds=2))
    within(SLACK, lambda: health(running.heartbeat) == 0)
    dock = registry.add_pushed("Dock", "dock")
    gate = registry.add_pushed("Gate 2", "gate")
    open_paths(mediamtx, dock.path, gate.path)

    with (
        mediamtx.publishing(dock.path, dock.path, PASSWORD),
        mediamtx.publishing(gate.path, gate.path, PASSWORD),
    ):
        within(SLACK * 2, lambda: bool(segments(footage / str(dock.id))))
        within(SLACK * 2, lambda: bool(segments(footage / str(gate.id))))
        registry.remove(dock.id)
        within(SLACK, lambda: segments(footage / str(dock.id))[-1].reason == "stopped")
        stopped = len(segments(footage / str(dock.id)))
        gate_before = len(segments(footage / str(gate.id)))
        within(SLACK, lambda: len(segments(footage / str(gate.id))) >= gate_before + 2)

    assert len(segments(footage / str(dock.id))) == stopped
    assert running.failure is None


# --- after a crash ----------------------------------------------------------------------------


def _recorder(environment: dict[str, str]) -> subprocess.Popen[str]:
    command = Path(sys.executable).parent / "visionquery-recorder"
    return subprocess.Popen(
        [str(command)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_a_recorder_killed_mid_segment_recovers_it_playable_when_started_again(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    ffmpeg: Path,
    tmp_path: Path,
) -> None:
    """The process is killed outright (SIGKILL) a few seconds into a minute-long Segment; the
    next one finishes it `recovered`, playable to its last whole Fragment, before recording on."""
    dock = registry.add_pushed("Dock", "dock")
    open_paths(mediamtx, dock.path)
    footage = tmp_path / "footage"
    environment = {
        **os.environ,
        "PATH": f"{ffmpeg.parent}{os.pathsep}{os.environ['PATH']}",
        "VISIONQUERY_DATABASE__URL": migrated,
        "VISIONQUERY_MEDIAMTX__RTSP_URL": f"rtsp://127.0.0.1:{mediamtx.rtsp_port}",
        "VISIONQUERY_MEDIAMTX__PASSWORD": mediamtx.password,
        "VISIONQUERY_FOOTAGE_DIR": str(footage),
        "VISIONQUERY_SEGMENT_SECONDS": "60",
        "VISIONQUERY_METRICS_PORT": "0",
    }
    camera = footage / str(dock.id)
    with mediamtx.publishing(dock.path, dock.path, PASSWORD):
        first = _recorder(environment)
        try:
            within(SLACK * 2, lambda: _fragments(camera) >= 3)
        finally:
            first.send_signal(signal.SIGKILL)
            first.communicate(timeout=30)
        assert segments(camera) == [], "nothing was finished: the process was killed"
        unfinished = list(camera.rglob("*.mp4"))
        assert len(unfinished) == 1

        second = _recorder(environment)
        try:
            within(SLACK, lambda: any(s.reason == "recovered" for s in segments(camera)))
            within(SLACK * 2, lambda: _fragments(camera) >= 2 + _recovered_fragments(camera))
        finally:
            second.send_signal(signal.SIGTERM)
            out, _ = second.communicate(timeout=60)
    assert second.returncode == 0, out

    recovered, stopped = segments(camera)
    assert (recovered.path, recovered.reason) == (unfinished[0], "recovered")
    assert frame_hashes(ffmpeg, recovered.path)
    assert stopped.reason == "stopped"
    assert frame_hashes(ffmpeg, stopped.path)
    assert recovered.time_to <= stopped.time_from
    assert "recovered" in out
    assert mediamtx.password not in out


def _fragments(camera: Path) -> int:
    """Fragments written so far, finished Segments or not, from their index files."""
    return sum(len(index.read_text().splitlines()) for index in camera.rglob("*.frags"))


def _recovered_fragments(camera: Path) -> int:
    return sum(s.fragments for s in segments(camera) if s.reason == "recovered")


def test_a_second_recorder_on_the_same_footage_is_refused(
    mediamtx: MediaMTX,
    migrated: str,
    tmp_path: Path,
    start: Callable[[RecorderSettings], Running],
) -> None:
    """Two recorders writing one camera's Footage would each finish the other's Segments."""
    footage = tmp_path / "footage"
    first = start(settings(migrated, mediamtx, footage, segment_seconds=60))
    within(SLACK, lambda: health(first.heartbeat) == 0)

    second = start(settings(migrated, mediamtx, footage, segment_seconds=60))
    within(SLACK, lambda: second.failure is not None)

    assert "already recording" in str(second.failure)
    assert health(first.heartbeat) == 0


# --- health -----------------------------------------------------------------------------------


def test_a_recorder_that_cannot_read_the_registry_says_so_once_and_is_unhealthy(
    mediamtx: MediaMTX,
    tmp_path: Path,
    start: Callable[[RecorderSettings], Running],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="visionquery.recorder")
    unreachable = "postgresql://visionquery:db-pa55word@127.0.0.1:9/visionquery?connect_timeout=1"
    running = start(settings(unreachable, mediamtx, tmp_path / "footage", segment_seconds=60))

    time.sleep(3)

    assert health(running.heartbeat) != 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "camera registry" in warnings[0].getMessage()
    assert all("db-pa55word" not in r.getMessage() for r in caplog.records)


def test_health_is_kept_only_for_a_few_refreshes(tmp_path: Path) -> None:
    heartbeat = tmp_path / "healthy"

    assert health(heartbeat) == 1
    heartbeat.write_text(f"{time.time() + 5}\n")
    assert health(heartbeat) == 0
    heartbeat.write_text(f"{time.time() - 1}\n")
    assert health(heartbeat) == 1
    heartbeat.write_text("garbage")
    assert health(heartbeat) == 1
