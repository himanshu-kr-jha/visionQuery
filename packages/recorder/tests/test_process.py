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
from boxes import fragments, parsed_init
from prometheus_client import REGISTRY
from publishers import PASSWORD, open_paths
from pydantic import SecretStr
from real_mediamtx import READER_USER, MediaMTX
from visionquery.memory.cameras import CameraRegistry
from visionquery.memory.connection import connect
from visionquery.memory.migrate import migrate
from visionquery.memory.settings import DatabaseSettings
from visionquery.recorder.segment import SegmentFile, segments
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
            user=READER_USER,
            password=SecretStr(server.reader_password),
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


def frame_hashes(ffmpeg: Path, video: Path, *, ended: bool = False) -> list[str]:
    """The MD5 of every decoded frame, in order; the test fails unless it decodes cleanly.

    A stream that `ended` may have ended inside its last frame: when a publisher leaves,
    MediaMTX ends its readers' sessions with whatever of the last frame they have, and that
    frame is kept as it arrived (damaged; decoders conceal it). It must still play to the end.
    """
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
    assert decoded.returncode == 0, decoded.stderr
    if not ended:
        assert decoded.stderr == ""
    return [
        line.rsplit(",", 1)[1].strip()
        for line in decoded.stdout.splitlines()
        if line and not line.startswith("#")
    ]


#: The known clip: a lead-in the recorder may join part-way through, then the part every frame of
#: which is checked (no two frames alike), then a lead-out. The stream ends when the clip does,
#: and MediaMTX ends a reader's session then with whatever it has already sent it, so the last
#: second or so of what a camera sent may never reach a reader that is a little behind: the
#: lead-out is what that can cost, never the known part.
LEAD_IN, KNOWN, LEAD_OUT = 6, 8, 4
FPS = 10


@pytest.fixture(scope="module")
def known_clip(ffmpeg: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Colour bars as a lead-in, then a pattern no two frames of which are alike, then another
    such pattern as a lead-out: H.264 at 10 fps, a keyframe every second, as a camera sends it."""
    path = tmp_path_factory.mktemp("clip") / "known.mp4"
    subprocess.run(
        [
            str(ffmpeg), "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", f"smptebars=size=320x240:rate={FPS}:duration={LEAD_IN}",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={FPS}:duration={KNOWN}",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate={FPS}:duration={LEAD_OUT}",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1[v]", "-map", "[v]",
            "-c:v", "libx264", "-preset", "ultrafast", "-bf", "0",
            "-g", str(FPS), "-keyint_min", str(FPS), "-sc_threshold", "0", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )  # fmt: skip
    return path


def assert_gapless_copy(recorded: list[str], clip: list[str], known_from: int, known: int) -> None:
    """`recorded` (every decoded frame's hash, in order) is a run of `clip`'s own frames with
    nothing missing or repeated, and holds the whole of the known part, `known` frames from
    `known_from`. It may begin anywhere before the known part (the recorder joined a stream
    already running) and end anywhere after it (a stream ends with what it has sent), and its very
    last frame is left out, since a stream may have ended inside it."""
    part = clip[known_from : known_from + known]
    assert len(set(part)) == known, "the known part's frames must differ for this to mean anything"
    assert part[0] in recorded, "the recorder joined after the known part began"
    start = recorded.index(part[0])
    offset = known_from - start
    assert offset >= 0, "more of the lead-in recorded than the clip has"
    assert recorded[:-1] == clip[offset : offset + len(recorded) - 1], "not a gapless run"
    assert start + known < len(recorded), "the known part is not all there"


def test_a_recording_that_ends_a_little_before_the_clip_does_is_still_a_gapless_copy() -> None:
    clip = [f"lead-in-{n % 3}" for n in range(60)] + [f"known-{n}" for n in range(80)]
    clip += [f"lead-out-{n}" for n in range(40)]
    joined_late = clip[15:150]  # the last 10 frames of the clip never arrived

    assert_gapless_copy(joined_late, clip, 60, 80)


def lost(frames: list[str]) -> list[str]:
    return frames[:70] + frames[71:]  # a frame lost in the known part


def repeated(frames: list[str]) -> list[str]:
    return frames[:70] + frames[69:]


def lost_in_lead_in(frames: list[str]) -> list[str]:
    return frames[:20] + frames[21:]


def cut_short(frames: list[str]) -> list[str]:
    return frames[:100]  # the recording ends inside the known part


def joined_late(frames: list[str]) -> list[str]:
    return frames[70:]  # it joined after the known part began


@pytest.mark.parametrize("damage", [lost, repeated, lost_in_lead_in, cut_short, joined_late])
def test_a_recording_with_a_gap_or_without_the_known_part_is_not_a_gapless_copy(
    damage: Callable[[list[str]], list[str]],
) -> None:
    clip = [f"lead-in-{n}" for n in range(60)] + [f"known-{n}" for n in range(80)]
    clip += [f"lead-out-{n}" for n in range(40)]

    with pytest.raises(AssertionError):
        assert_gapless_copy(damage(clip[10:]), clip, 60, 80)


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
    recorded = [
        frame
        for segment in found
        for frame in frame_hashes(ffmpeg, segment.path, ended=segment is found[-1])
    ]
    clip = frame_hashes(ffmpeg, known_clip)
    assert len(clip) == (LEAD_IN + KNOWN + LEAD_OUT) * FPS
    # Copy-only and gapless: a run of the clip's own frames that holds the whole known part.
    assert_gapless_copy(recorded, clip, LEAD_IN * FPS, KNOWN * FPS)
    # Cut on 3-second boundaries of the epoch, one Segment running on from the next.
    assert {segment.reason for segment in found[:-1]} == {"rolled"}
    assert found[-1].reason == "stream_ended"
    assert all(a.time_to == b.time_from for a, b in pairwise(found))
    assert all(int(s.time_from.timestamp()) % 3 == 0 for s in found[1:])
    # The span is the time the clip arrived, on the recorder's NTP-anchored timebase.
    assert abs(found[-1].time_to - ended) < timedelta(seconds=2)
    assert found[-1].time_to - found[0].time_from >= timedelta(seconds=KNOWN)
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
        "VISIONQUERY_MEDIAMTX__PASSWORD": mediamtx.reader_password,
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
        # Whatever window boundary fell in its few seconds, only one Segment is unfinished, and
        # whatever was finished was rolled: the process was killed, nothing was stopped.
        assert {s.reason for s in segments(camera)} <= {"rolled"}
        unfinished = [p for p in camera.rglob("*.mp4") if not p.with_suffix(".closed").exists()]
        assert len(unfinished) == 1

        second = _recorder(environment)
        try:
            within(SLACK, lambda: any(s.reason == "recovered" for s in segments(camera)))
            within(SLACK * 2, lambda: _fragments(camera) >= 2 + _recovered_fragments(camera))
        finally:
            second.send_signal(signal.SIGTERM)
            out, _ = second.communicate(timeout=60)
    assert second.returncode == 0, out

    # Structure, not count: the windows are the epoch's, so a boundary may fall in either run.
    # The one unfinished Segment is recovered, the last is stopped, any other was rolled.
    found = segments(camera)
    (recovered,) = [s for s in found if s.reason == "recovered"]
    stopped = found[-1]
    assert (recovered.path, recovered.reason) == (unfinished[0], "recovered")
    assert frame_hashes(ffmpeg, recovered.path)
    assert stopped.reason == "stopped"
    assert frame_hashes(ffmpeg, stopped.path)
    assert {s.reason for s in found} <= {"rolled", "recovered", "stopped"}
    for before, after in pairwise(found):
        # A rolled Segment runs on into the next; after a recovery a gap is allowed.
        assert after.time_from >= before.time_to
        if before.reason == "rolled":
            assert after.time_from == before.time_to
    assert "recovered" in out
    assert mediamtx.reader_password not in out


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


@pytest.mark.usefixtures("on_path")
def test_a_malformed_segment_on_disk_stops_no_camera_from_recording(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    tmp_path: Path,
    start: Callable[[RecorderSettings], Running],
) -> None:
    """A Segment the last run left damaged is quarantined, with its camera's id counted and its
    path named, while a good unfinished one beside it is recovered and every camera records. It
    used to raise at the start, so the process exited, Compose started it again, and nothing was
    ever recorded until an Operator deleted the file by hand."""
    footage = tmp_path / "footage"
    bad = footage / "900" / "2026-10-06" / "100000-0123456789abcdef0123456789abcdef.mp4"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\0" * 64)
    bad.with_suffix(".frags").write_text('{"end":50,"arr\0\0\n')
    (bad.parent.parent / ".open").mkdir()
    (bad.parent.parent / ".open" / f"2026-10-06_{bad.stem}").touch()
    good = SegmentFile.open(
        footage / "901",
        parsed_init(),
        window_start=datetime(2026, 10, 6, 10, tzinfo=UTC),
        not_before=None,
        verified=True,
    )
    good.append(fragments(1)[0], arrived_at=datetime(2026, 10, 6, 10, 0, 2, tzinfo=UTC))
    dock = registry.add_pushed("Dock", "dock")
    open_paths(mediamtx, dock.path)
    before = REGISTRY.get_sample_value(
        "visionquery_recorder_segments_quarantined_total", {"camera": "900"}
    )

    with mediamtx.publishing(dock.path, dock.path, PASSWORD):
        running = start(settings(migrated, mediamtx, footage, segment_seconds=2))
        within(SLACK * 2, lambda: bool(segments(footage / str(dock.id))))

    assert running.failure is None
    assert [segment.reason for segment in segments(footage / "901")] == ["recovered"]
    assert (footage / ".quarantine" / "900" / "2026-10-06" / bad.name).exists()
    after = REGISTRY.get_sample_value(
        "visionquery_recorder_segments_quarantined_total", {"camera": "900"}
    )
    assert after == (before or 0) + 1


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
