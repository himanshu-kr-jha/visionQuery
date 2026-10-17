"""The fake camera publisher of the `dev` profile, without Docker or a real ffmpeg.

What it publishes is checked for real by the stack tests (tests/test_stack.py); here are its
configuration, the ffmpeg commands it runs, and how it supervises them, with a stand-in
ffmpeg that reports progress the way the real one does.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import fake_cameras
import pytest
from fake_cameras import (
    DURATION,
    HUNG_AFTER,
    PUBLISHED_LONG_ENOUGH,
    SYNTHETIC,
    Camera,
    Ended,
    Settings,
    cache_key,
    find_sample,
    not_publishing,
    parse_cameras,
    prepare_command,
    publish_command,
    redact,
    timeline_start,
)
from fake_cameras import Publisher as RealPublisher

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "fake-cameras" / "fake_cameras.py"

# --- Configuration ----------------------------------------------------------------------------


def test_each_entry_is_a_camera_named_for_its_path_and_its_source() -> None:
    cameras = parse_cameras("gate-2=meva-school-g299.avi, lobby=synthetic,yard=samples/yard.mp4")
    assert cameras == [
        Camera("gate-2", "meva-school-g299.avi"),
        Camera("lobby", SYNTHETIC),
        Camera("yard", "samples/yard.mp4"),
    ]


def test_the_number_of_cameras_is_the_number_of_entries() -> None:
    spec = ",".join(f"cam-{n}=synthetic" for n in range(16))
    assert len(parse_cameras(spec)) == 16


def test_one_source_can_feed_several_cameras() -> None:
    cameras = parse_cameras("a=walk.mp4,b=walk.mp4")
    assert [c.source for c in cameras] == ["walk.mp4", "walk.mp4"]


@pytest.mark.parametrize(
    ("spec", "complaint"),
    [
        ("", "no camera"),
        (" , ", "no camera"),
        ("lobby", "name=source"),
        ("lobby=", "name=source"),
        ("=walk.mp4", "name=source"),
        ("a=synthetic,a=walk.mp4", "twice"),
        ("my camera=synthetic", "my camera"),
        ("a/b=synthetic", "a/b"),
        ("a=/etc/passwd", "/etc/passwd"),
        ("a=../outside.mp4", "../outside.mp4"),
        ("a=samples/../../outside.mp4", "outside.mp4"),
    ],
)
def test_a_malformed_configuration_is_refused_saying_why(spec: str, complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        parse_cameras(spec)


def _environment(**changes: str) -> dict[str, str]:
    return {"FAKE_CAMERAS": "a=synthetic"} | changes


def test_every_sample_is_five_minutes_long_unless_configured() -> None:
    assert DURATION == 300
    assert Settings.from_environment(_environment()).duration == 300
    assert Settings.from_environment(_environment(FAKE_CAMERAS_DURATION="20")).duration == 20


@pytest.mark.parametrize(
    "changes",
    [
        {"FAKE_CAMERAS_DURATION": "0"},
        {"FAKE_CAMERAS_DURATION": "2.5"},
        {"FAKE_CAMERAS_DURATION": "five"},
        {"FAKE_CAMERAS_PREPARE_ATTEMPTS": "0"},
        {"FAKE_CAMERAS_PREPARE_RETRY": "-1"},
    ],
)
def test_a_duration_or_attempts_that_make_no_sense_are_refused(changes: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="FAKE_CAMERAS_"):
        Settings.from_environment(_environment(**changes))


# --- The shared timeline ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "wait", "position"),
    [
        # Cameras join at the next whole second, where the samples have a keyframe.
        (1000.4, 0.6, 101),
        (1000.0, 0.0, 100),
        (1799.999, 0.001, 0),
        (299.25, 0.75, 0),
        (300.0, 0.0, 0),
        # Whenever they start, 300 s later they are at the same point.
        (1000.4 + 300, 0.6, 101),
    ],
)
def test_a_camera_joins_the_timeline_at_the_next_whole_second(
    now: float, wait: float, position: int
) -> None:
    got_wait, got_position = timeline_start(now, 300)
    assert got_wait == pytest.approx(wait, abs=1e-6)
    assert got_position == position


def test_the_timeline_follows_the_wall_clock_not_when_a_camera_started() -> None:
    # With the epoch at 0 and 300 s, a loop starts at every five minutes of the hour.
    five_past_ten = 1_700_000_000 - 1_700_000_000 % 300
    assert timeline_start(five_past_ten, 300) == (0, 0)
    assert timeline_start(five_past_ten + 17.5, 300)[1] == 18
    assert timeline_start(five_past_ten + 17.5, 20)[1] == 18


def test_the_timeline_can_start_elsewhere() -> None:
    assert timeline_start(1000.0, 300, epoch=990.0) == (0, 10)


# --- The commands -----------------------------------------------------------------------------

URL = "rtsp://mediamtx:8554/lobby"


def _after(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]


def test_a_camera_loops_a_prepared_sample_in_real_time_without_transcoding() -> None:
    command = publish_command(Path("/cache/abc.mp4"), URL)
    assert command[0] == "ffmpeg"
    assert _after(command, "-stream_loop") == "-1"
    assert "-re" in command
    assert command.index("-re") < command.index("-i")
    assert _after(command, "-i") == "/cache/abc.mp4"
    assert _after(command, "-c") == "copy"


def test_a_camera_joins_the_timeline_before_reading_its_input_and_then_loops_from_the_start() -> (
    None
):
    command = publish_command(Path("/cache/abc.mp4"), URL, start=101)
    assert _after(command, "-ss") == "101"
    # Before -i, so that it seeks (to a keyframe) rather than decodes up to the point.
    assert command.index("-ss") < command.index("-i")
    # A loop reads from the beginning again, which -ss does only once.
    assert _after(command, "-stream_loop") == "-1"
    assert _after(publish_command(Path("/cache/abc.mp4"), URL), "-ss") == "0"


def test_every_camera_publishes_over_rtsp_on_tcp_to_its_own_path() -> None:
    command = publish_command(Path("/cache/abc.mp4"), URL)
    assert command[-3:] == ["-f", "rtsp", URL]
    assert _after(command, "-rtsp_transport") == "tcp"
    # Video only, as from a camera without a microphone.
    assert "-an" in command
    # RTP packets small enough that MediaMTX need not split them again.
    assert int(_after(command, "-pkt_size")) <= 1400


def test_every_command_reports_progress_and_keeps_its_log_quiet() -> None:
    publish = publish_command(Path("/cache/abc.mp4"), URL)
    for command in (
        publish,
        prepare_command(Path("/samples/a.avi"), Path("/cache/a.part")),
        prepare_command(None, Path("/cache/b.part")),
    ):
        assert "-nostdin" in command
        assert "-hide_banner" in command
        assert _after(command, "-loglevel") == "error"
    assert _after(publish, "-progress") == "pipe:1"


def _camera_like(command: list[str]) -> None:
    assert _after(command, "-c:v") == "libx264"
    # No B-frames, which a browser's WebRTC cannot play, and a keyframe every second.
    assert _after(command, "-bf") == "0"
    assert _after(command, "-force_key_frames") == "expr:gte(t,n_forced)"
    assert _after(command, "-pix_fmt") == "yuv420p"
    assert "-an" in command


def test_preparing_a_sample_makes_it_camera_like() -> None:
    command = prepare_command(Path("/samples/a.avi"), Path("/cache/a.part"))
    assert _after(command, "-i") == "/samples/a.avi"
    _camera_like(command)
    assert command[-3:] == ["-f", "mp4", "/cache/a.part"]


def test_every_sample_is_prepared_to_the_same_duration() -> None:
    command = prepare_command(Path("/samples/a.avi"), Path("/cache/a.part"))
    # Samples differ in length (MEVA's are 300.0 and 300.3 s), which would drift their loops.
    assert _after(command, "-t") == "300"
    assert command.index("-t") > command.index("-i")
    # One shorter than that holds its last picture rather than looping early.
    assert "tpad=stop_mode=clone:stop_duration=300" in _after(command, "-vf")
    other = prepare_command(Path("/samples/a.avi"), Path("/cache/a.part"), duration=20)
    assert _after(other, "-t") == "20"
    assert "stop_duration=20" in _after(other, "-vf")


def test_a_sample_is_prepared_again_when_the_duration_changes(tmp_path: Path) -> None:
    sample = tmp_path / "walk.mp4"
    sample.write_bytes(b"one")
    assert cache_key(sample, 300) != cache_key(sample, 20)
    assert cache_key(sample) == cache_key(sample, DURATION)


def test_the_test_pattern_is_a_sample_prepared_like_any_other() -> None:
    # Encoded once and looped, so that many synthetic cameras cost no more than many samples.
    command = prepare_command(None, Path("/cache/b.part"))
    assert _after(command, "-f") == "lavfi"
    assert _after(command, "-i").startswith("testsrc2=")
    assert "duration=" in _after(command, "-i")
    assert "-t" not in command
    _camera_like(command)
    assert command[-3:] == ["-f", "mp4", "/cache/b.part"]


def test_a_sample_is_prepared_again_only_when_its_content_changes(tmp_path: Path) -> None:
    sample = tmp_path / "walk.mp4"
    sample.write_bytes(b"one")
    first = cache_key(sample)
    renamed = tmp_path / "renamed.mp4"
    sample.rename(renamed)
    assert cache_key(renamed) == first
    renamed.write_bytes(b"two")
    assert cache_key(renamed) != first
    assert cache_key(None) not in {first, cache_key(renamed)}


# --- Health -----------------------------------------------------------------------------------


def test_a_camera_is_publishing_while_its_frames_keep_coming() -> None:
    cameras = [Camera("a", SYNTHETIC), Camera("b", SYNTHETIC), Camera("c", SYNTHETIC)]
    now = 1000.0
    progress = {"a": now - 1, "b": now - 30}
    assert not_publishing(cameras, progress, now) == ["b", "c"]
    assert not_publishing(cameras[:1], progress, now) == []


# --- Supervising ffmpeg -----------------------------------------------------------------------

#: Stands in for ffmpeg: logs its arguments, reports progress like `-progress pipe:1` until
#: told to stop, or exits at once when the file named by FAKE_FFMPEG_FAIL exists. Preparing a
#: sample (not the test pattern) takes until the file named by FAKE_FFMPEG_HOLD is gone, and
#: fails while the file named by FAKE_FFMPEG_FAIL_PREPARE exists.
FAKE_FFMPEG = textwrap.dedent(
    """\
    import json, os, sys, time
    from pathlib import Path
    with open(os.environ["FAKE_FFMPEG_LOG"], "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    if sys.argv[-2] == "mp4":
        while "lavfi" not in sys.argv and Path(os.environ["FAKE_FFMPEG_HOLD"]).exists():
            time.sleep(0.1)
        if "lavfi" not in sys.argv and Path(os.environ["FAKE_FFMPEG_FAIL_PREPARE"]).exists():
            print("[libx264 @ 0x7f02] the encoder broke", file=sys.stderr, flush=True)
            sys.exit(1)
        Path(sys.argv[-1]).write_bytes(b"prepared")
        sys.exit(0)
    if Path(os.environ["FAKE_FFMPEG_FAIL"]).exists():
        print("[tcp @ 0x7f00] Connection to tcp://mediamtx.test:8554 failed: Connection refused",
              file=sys.stderr, flush=True)
        print("[out#0/rtsp @ 0x7f01] Could not write header: Connection refused",
              file=sys.stderr, flush=True)
        sys.exit(1)
    frame = 0
    while True:
        frame += 15
        print(f"frame={frame}\\nprogress=continue", flush=True)
        time.sleep(0.2)
    """
)


class Publisher:
    """The publisher run as a process, as in its container, with the stand-in ffmpeg."""

    def __init__(self, tmp_path: Path, spec: str) -> None:
        self.tmp = tmp_path
        self.samples = tmp_path / "samples"
        self.cache = tmp_path / "cache"
        self.status = tmp_path / "status.json"
        self.calls = tmp_path / "ffmpeg.log"
        self.fail = tmp_path / "fail"
        self.hold = tmp_path / "hold"
        self.fail_prepare = tmp_path / "fail-prepare"
        #: Where the test pattern is prepared.
        self.pattern = self.cache / f"{cache_key(None)}.mp4"
        for directory in (self.samples, self.cache):
            directory.mkdir()
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_text(f"#!{sys.executable}\n{FAKE_FFMPEG}")
        ffmpeg.chmod(0o755)
        self.env = {
            "PATH": os.environ["PATH"],
            "FAKE_CAMERAS": spec,
            "FAKE_CAMERAS_FFMPEG": str(ffmpeg),
            "FAKE_CAMERAS_SAMPLES": str(self.samples),
            "FAKE_CAMERAS_CACHE": str(self.cache),
            "FAKE_CAMERAS_STATUS": str(self.status),
            "FAKE_CAMERAS_RTSP": "rtsp://mediamtx.test:8554",
            "FAKE_FFMPEG_LOG": str(self.calls),
            "FAKE_FFMPEG_FAIL": str(self.fail),
            "FAKE_FFMPEG_HOLD": str(self.hold),
            "FAKE_FFMPEG_FAIL_PREPARE": str(self.fail_prepare),
            "FAKE_CAMERAS_PREPARE_RETRY": "0.05",
        }
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "run"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop(self) -> str:
        assert self.process is not None
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        output, _ = self.process.communicate(timeout=10)
        return output

    def health(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "health"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def ffmpeg_calls(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]


@pytest.fixture
def publisher(tmp_path: Path) -> Iterator[Callable[[str], Publisher]]:
    started: list[Publisher] = []

    def start(spec: str) -> Publisher:
        p = Publisher(tmp_path, spec)
        started.append(p)
        return p

    yield start
    for p in started:
        if p.process is not None and p.process.poll() is None:
            p.process.kill()
            p.process.wait()


def _eventually(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "gave up waiting"
        time.sleep(0.1)


def _publishing(calls: list[list[str]]) -> list[str]:
    """The RTSP URL of every publishing ffmpeg started so far."""
    return [c[-1] for c in calls if c[-1].startswith("rtsp://")]


def _inputs(calls: list[list[str]]) -> set[str]:
    """What every publishing ffmpeg started so far read."""
    return {c[c.index("-i") + 1] for c in calls if c[-1].startswith("rtsp://")}


def _prepared(calls: list[list[str]]) -> list[list[str]]:
    """Every ffmpeg started so far to prepare a sample, not the test pattern."""
    return [c for c in calls if c[-2] == "mp4" and "lavfi" not in c]


def test_every_camera_gets_its_own_ffmpeg_and_turns_healthy(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("lobby=synthetic,yard=synthetic")
    assert p.health().returncode != 0
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    assert sorted(_publishing(p.ffmpeg_calls())) == [
        "rtsp://mediamtx.test:8554/lobby",
        "rtsp://mediamtx.test:8554/yard",
    ]
    p.stop()


def test_a_camera_that_fails_is_restarted_and_is_unhealthy_meanwhile(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("lobby=synthetic")
    p.fail.touch()
    p.start()
    _eventually(lambda: len(p.ffmpeg_calls()) >= 2)
    unhealthy = p.health()
    assert unhealthy.returncode != 0
    assert "lobby" in unhealthy.stdout + unhealthy.stderr
    # MediaMTX is back: the next attempt publishes.
    p.fail.unlink()
    _eventually(lambda: p.health().returncode == 0, timeout=20)
    output = p.stop()
    # One line each time, saying why in ffmpeg's words, not every line ffmpeg wrote.
    reasons = [line for line in output.splitlines() if "lobby: ffmpeg exited" in line]
    assert reasons
    assert reasons[0] == (
        "WARNING lobby: ffmpeg exited with 1 (Connection to tcp://mediamtx.test:8554 failed: "
        "Connection refused, and 1 more line); restarting in 1 s"
    )
    assert "Could not write header" not in output


def test_a_sample_is_prepared_once_then_looped_from_the_cache(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("a=walk.avi,b=walk.avi")
    (p.samples / "walk.avi").write_bytes(b"a video")
    p.start()
    _eventually(lambda: _prepared(p.ffmpeg_calls()) != [])
    cached = p.cache / f"{cache_key(p.samples / 'walk.avi')}.mp4"
    _eventually(lambda: str(cached) in _inputs(p.ffmpeg_calls()))
    _eventually(lambda: p.health().returncode == 0)
    p.stop()
    prepared = _prepared(p.ffmpeg_calls())
    assert len(prepared) == 1
    assert prepared[0][prepared[0].index("-i") + 1] == str(p.samples / "walk.avi")
    assert sorted(p.cache.iterdir()) == sorted([cached, p.pattern])

    # Started again (in a new container, whose /tmp is new), nothing is prepared again and
    # the sample is looped from the start.
    p.calls.unlink()
    p.status.unlink()
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    p.stop()
    calls = p.ffmpeg_calls()
    assert [c for c in calls if c[-2] == "mp4"] == []
    assert _inputs(calls) == {str(cached)}


def test_a_camera_shows_the_test_pattern_while_its_sample_is_prepared(
    publisher: Callable[[str], Publisher],
) -> None:
    # Preparing a long sample takes a minute or more, and `make up` waits for health meanwhile.
    p = publisher("a=walk.avi")
    (p.samples / "walk.avi").write_bytes(b"a video")
    p.hold.touch()
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    assert _inputs(p.ffmpeg_calls()) == {str(p.pattern)}
    p.hold.unlink()
    cached = str(p.cache / f"{cache_key(p.samples / 'walk.avi')}.mp4")
    _eventually(lambda: cached in _inputs(p.ffmpeg_calls()))
    _eventually(lambda: p.health().returncode == 0)
    output = p.stop()
    assert "test pattern until walk.avi is prepared" in output


def test_a_sample_no_longer_configured_leaves_the_cache(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("a=synthetic")
    (p.cache / "0123-old.mp4").write_bytes(b"from a sample no camera uses now")
    (p.cache / "0123-old.mp4.part").write_bytes(b"from a killed preparation")
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    p.stop()
    # The test pattern is always kept: a camera whose video is missing falls back to it.
    assert list(p.cache.iterdir()) == [p.pattern]


def test_a_sample_not_fetched_yet_falls_back_to_the_test_pattern_and_says_so(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("gate=meva-school-g299.avi")
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    output = p.stop()
    assert _inputs(p.ffmpeg_calls()) == {str(p.pattern)}
    assert "meva-school-g299.avi" in output
    assert "make fetch-samples" in output


def test_terminating_the_publisher_stops_every_ffmpeg(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("a=synthetic,b=synthetic")
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    started = time.monotonic()
    p.stop()
    assert p.process is not None
    assert p.process.returncode == 0
    assert time.monotonic() - started < 5
    left = subprocess.run(
        ["pgrep", "-f", str(p.tmp / "ffmpeg")], capture_output=True, text=True, check=False
    )
    assert left.stdout == ""


def test_a_malformed_configuration_stops_the_publisher_with_status_2(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("lobby")
    p.start()
    assert p.process is not None
    output, _ = p.process.communicate(timeout=10)
    assert p.process.returncode == 2
    assert "FAKE_CAMERAS" in output


def test_the_test_pattern_is_prepared_once_for_every_camera_showing_it(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("a=synthetic,b=synthetic,c=not-fetched.mp4")
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    p.stop()
    calls = p.ffmpeg_calls()
    assert len([c for c in calls if "lavfi" in c]) == 1
    assert sorted(_publishing(calls)) == [f"rtsp://mediamtx.test:8554/{n}" for n in "abc"]
    assert _inputs(calls) == {str(p.pattern)}


# --- Videos that cannot be used ---------------------------------------------------------------

needs_a_user_without_privileges = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads whatever the mode says"
)


def test_a_video_that_is_there_is_found(tmp_path: Path) -> None:
    (tmp_path / "gate.avi").write_bytes(b"x")
    assert find_sample(tmp_path, "gate.avi") == tmp_path / "gate.avi"


def test_a_missing_video_is_to_be_fetched(tmp_path: Path) -> None:
    problem = find_sample(tmp_path, "gate.avi")
    assert isinstance(problem, str)
    assert "gate.avi is not in the samples directory" in problem
    assert "make fetch-samples" in problem


@needs_a_user_without_privileges
def test_an_unreadable_video_is_not_called_missing(tmp_path: Path) -> None:
    video = tmp_path / "gate.avi"
    video.write_bytes(b"x")
    video.chmod(0o000)
    problem = find_sample(tmp_path, "gate.avi", uid=10001)
    assert isinstance(problem, str)
    assert "exists but is not readable by uid 10001" in problem
    assert "chmod" in problem
    assert "not in the samples directory" not in problem
    assert "make fetch-samples" not in problem


@needs_a_user_without_privileges
def test_a_directory_that_cannot_be_listed_is_not_called_missing(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "gate.avi").write_bytes(b"x")
    samples.chmod(0o000)
    try:
        problem = find_sample(samples, "gate.avi", uid=10001)
    finally:
        samples.chmod(0o755)
    assert isinstance(problem, str)
    assert "samples directory cannot be listed by uid 10001" in problem
    assert "chmod a+rX samples" in problem
    assert "make fetch-samples" not in problem


@needs_a_user_without_privileges
def test_an_unreadable_video_gets_the_test_pattern_and_the_right_advice(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("gate=meva-school-g299.avi")
    video = p.samples / "meva-school-g299.avi"
    video.write_bytes(b"x")
    video.chmod(0o000)
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    output = p.stop()
    assert _inputs(p.ffmpeg_calls()) == {str(p.pattern)}
    assert "exists but is not readable" in output
    assert "make fetch-samples" not in output


# --- Restarting an ffmpeg that exited ---------------------------------------------------------


class _Time:
    """A clock that moves only when an ffmpeg runs or the publisher sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        #: How long each ffmpeg to come ran before it exited.
        self.runs: list[float] = []
        self.delays: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        if not self.runs:
            raise asyncio.CancelledError


def _restart_delays(tmp_path: Path, runs: list[float]) -> list[float]:
    """The delays before each restart of an ffmpeg that ran for `runs` seconds each."""
    time_ = _Time()
    time_.runs = list(runs)
    settings = Settings(
        cameras=[Camera("lobby", SYNTHETIC)],
        rtsp="rtsp://mediamtx.test:8554",
        ffmpeg="ffmpeg",
        samples=tmp_path,
        cache=tmp_path,
        status=tmp_path / "status.json",
    )
    publisher = RealPublisher(settings, clock=lambda: time_.now, sleep=time_.sleep)

    async def run(label: str, command: list[str], hung_after: float | None = None) -> Ended:
        time_.now += time_.runs.pop(0)
        return Ended(1, [])

    publisher._run = run  # type: ignore[method-assign]
    keep_publishing = publisher._keep_publishing  # pyright: ignore[reportPrivateUsage]
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(keep_publishing(settings.cameras[0], tmp_path / "x.mp4", "u", "l"))
    return time_.delays


def test_a_camera_that_keeps_failing_waits_longer_each_time_up_to_the_last_backoff(
    tmp_path: Path,
) -> None:
    assert _restart_delays(tmp_path, [0.1] * 6) == [1, 2, 4, 8, 8, 8]


def test_a_camera_that_published_long_enough_starts_again_at_the_shortest_backoff(
    tmp_path: Path,
) -> None:
    long = PUBLISHED_LONG_ENOUGH + 1
    assert _restart_delays(tmp_path, [0.1, 0.1, 0.1, long, 0.1, 0.1]) == [1, 2, 4, 1, 1, 2]
    # Exactly as long as it takes is not long enough.
    assert _restart_delays(tmp_path, [0.1, PUBLISHED_LONG_ENOUGH, 0.1]) == [1, 2, 4]


def test_a_restart_is_logged_with_its_delay(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="fake-cameras"):
        _restart_delays(tmp_path, [0.1, 0.1])
    assert "restarting in 1 s" in caplog.text
    assert "restarting in 2 s" in caplog.text


# --- Rejoining the timeline at every start ----------------------------------------------------


def _settings(tmp_path: Path, spec: str = "lobby=synthetic", **changes: object) -> Settings:
    base = Settings(
        cameras=parse_cameras(spec),
        rtsp="rtsp://mediamtx.test:8554",
        ffmpeg="ffmpeg",
        samples=tmp_path / "samples",
        cache=tmp_path / "cache",
        status=tmp_path / "status.json",
    )
    return replace(base, **changes)


def test_every_start_and_restart_of_a_sample_rejoins_the_shared_timeline(tmp_path: Path) -> None:
    wall = [1000.4]
    commands: list[list[str]] = []

    async def sleep(seconds: float) -> None:
        wall[0] += seconds
        if len(commands) >= 3:
            raise asyncio.CancelledError

    publisher = RealPublisher(
        _settings(tmp_path), clock=lambda: wall[0], sleep=sleep, wall=lambda: wall[0]
    )

    async def run(label: str, command: list[str], hung_after: float | None = None) -> Ended:
        commands.append(command)
        wall[0] += 40  # It published for a while, and then exited.
        return Ended(1, [])

    publisher._run = run  # type: ignore[method-assign]
    keep_publishing = publisher._keep_publishing  # pyright: ignore[reportPrivateUsage]
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(keep_publishing(Camera("lobby", "a.mp4"), tmp_path / "x.mp4", "u", "l", True))
    # Started at 1001 s (the next whole second), then restarted at 1042 s and at 1083 s, each
    # time at the point of the sample the other cameras are at: not from its beginning.
    assert [_after(c, "-ss") for c in commands] == ["101", "142", "183"]


def test_the_test_pattern_has_no_timeline_to_join(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    async def sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    publisher = RealPublisher(_settings(tmp_path), sleep=sleep, wall=lambda: 1000.4)

    async def run(label: str, command: list[str], hung_after: float | None = None) -> Ended:
        commands.append(command)
        return Ended(1, [])

    publisher._run = run  # type: ignore[method-assign]
    keep_publishing = publisher._keep_publishing  # pyright: ignore[reportPrivateUsage]
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(keep_publishing(Camera("lobby", SYNTHETIC), tmp_path / "x.mp4", "u", "l"))
    assert [_after(c, "-ss") for c in commands] == ["0"]


# --- A hung ffmpeg ----------------------------------------------------------------------------

#: Stands in for a publishing ffmpeg: writes its pid to the file named by its first argument,
#: reports progress every 0.1 s, and (with `hang`) stops reporting after three reports while
#: staying alive, as when a write to a MediaMTX that stopped reading blocks.
HANGING_FFMPEG = textwrap.dedent(
    """\
    import os, sys, time
    from pathlib import Path
    Path({pid!r}).write_text(str(os.getpid()))
    frame = 0
    while True:
        frame += 15
        print(f"frame={{frame}}\\nprogress=continue", flush=True)
        time.sleep(0.1)
        if {hang} and frame >= 45:
            time.sleep(3600)
    """
)


class _Watched:
    """A publisher whose clock moves a tenth of a second per watchdog check, so that the
    fifteen seconds before a hung ffmpeg is killed take about a second and a half."""

    def __init__(self, tmp_path: Path, hang: bool) -> None:
        self.pid = tmp_path / "ffmpeg.pid"
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_text(
            f"#!{sys.executable}\n" + HANGING_FFMPEG.format(pid=str(self.pid), hang=hang)
        )
        ffmpeg.chmod(0o755)
        self.wall = 1000.0
        self.checks = 0
        #: The restart delays, which are the other thing the publisher sleeps for.
        self.restarts: list[float] = []
        self.publisher = RealPublisher(
            _settings(tmp_path, ffmpeg=str(ffmpeg)), sleep=self.sleep, wall=lambda: self.wall
        )

    async def sleep(self, seconds: float) -> None:
        if seconds == fake_cameras.WATCH_EVERY:
            self.checks += 1
            self.wall += 0.1
            await asyncio.sleep(0.01)
        else:
            self.restarts.append(seconds)
            raise asyncio.CancelledError

    def alive(self) -> bool:
        try:
            os.kill(int(self.pid.read_text()), 0)
        except ProcessLookupError:
            return False
        return True

    def keep_publishing(self, tmp_path: Path) -> Coroutine[None, None, NoReturn]:
        keep_publishing = self.publisher._keep_publishing  # pyright: ignore[reportPrivateUsage]
        return keep_publishing(Camera("lobby", SYNTHETIC), tmp_path / "x.mp4", "u", "l")


def test_an_ffmpeg_that_stops_reporting_progress_is_killed_and_restarted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    watched = _Watched(tmp_path, hang=True)
    with (
        caplog.at_level(logging.WARNING, logger="fake-cameras"),
        contextlib.suppress(asyncio.CancelledError),
    ):
        asyncio.run(asyncio.wait_for(watched.keep_publishing(tmp_path), timeout=30))
    assert HUNG_AFTER == 15
    assert watched.restarts == [1]
    assert not watched.alive()
    # Not before it was hung for HUNG_AFTER seconds of the clock, which moves 0.1 s a check.
    assert watched.checks > HUNG_AFTER / 0.1
    assert "lobby: ffmpeg reported no progress for 15 s and was killed; restarting in 1 s" in (
        caplog.text
    )


def test_an_ffmpeg_that_keeps_reporting_progress_is_left_running(tmp_path: Path) -> None:
    watched = _Watched(tmp_path, hang=False)

    async def run() -> None:
        task = asyncio.create_task(watched.keep_publishing(tmp_path))
        # Twice as long as it takes to kill a hung one.
        while watched.checks < 3 * int(HUNG_AFTER / 0.1):
            await asyncio.sleep(0.05)
            assert not task.done()
        assert watched.alive()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert watched.restarts == []
    # Cancelling the publisher stops its ffmpeg too.
    assert not watched.alive()


# --- A video that cannot be prepared ----------------------------------------------------------


def test_a_video_that_will_not_prepare_is_given_up_after_a_few_attempts(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("a=walk.avi,b=walk.avi,c=synthetic")
    (p.samples / "walk.avi").write_bytes(b"a video")
    p.fail_prepare.touch()
    p.start()
    attempts = fake_cameras.PREPARE_ATTEMPTS
    _eventually(lambda: len(_prepared(p.ffmpeg_calls())) == attempts)
    # Not retried for ever, and not for each camera that loops it.
    time.sleep(1)
    assert len(_prepared(p.ffmpeg_calls())) == attempts
    # The cameras are up all the same, on the test pattern.
    _eventually(lambda: p.health().returncode == 0)
    output = p.stop()
    assert _inputs(p.ffmpeg_calls()) == {str(p.pattern)}
    errors = [line for line in output.splitlines() if line.startswith("ERROR")]
    # One error, naming the file and ffmpeg's reason.
    assert len(errors) == 1, output
    assert "walk.avi" in errors[0]
    assert f"failed {attempts} times" in errors[0]
    assert "the encoder broke" in errors[0]
    assert "keep the test pattern" in errors[0]


def test_the_attempts_to_prepare_a_video_can_be_configured(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("a=walk.avi")
    p.env["FAKE_CAMERAS_PREPARE_ATTEMPTS"] = "1"
    (p.samples / "walk.avi").write_bytes(b"a video")
    p.fail_prepare.touch()
    p.start()
    _eventually(lambda: len(_prepared(p.ffmpeg_calls())) == 1)
    time.sleep(0.5)
    p.stop()
    assert len(_prepared(p.ffmpeg_calls())) == 1


def test_a_video_that_prepares_at_the_second_attempt_is_looped(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("a=walk.avi")
    (p.samples / "walk.avi").write_bytes(b"a video")
    p.fail_prepare.touch()
    p.start()
    _eventually(lambda: len(_prepared(p.ffmpeg_calls())) >= 1)
    p.fail_prepare.unlink()
    cached = str(p.cache / f"{cache_key(p.samples / 'walk.avi')}.mp4")
    _eventually(lambda: cached in _inputs(p.ffmpeg_calls()))
    output = p.stop()
    assert "ERROR" not in output


# --- Credentials never reach the log ----------------------------------------------------------


def test_the_user_and_password_of_a_url_are_redacted() -> None:
    assert redact("rtsp://user:s3cret@mediamtx:8554/gate") == "rtsp://***@mediamtx:8554/gate"
    assert redact("rtsp://mediamtx:8554/gate") == "rtsp://mediamtx:8554/gate"
    assert redact("a path/with@sign") == "a path/with@sign"
    assert redact("tcp://u:p@h:1 and rtsp://v:q@i:2/x") == "tcp://***@h:1 and rtsp://***@i:2/x"


def test_a_cameras_url_is_logged_without_its_credentials(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    publisher = RealPublisher(_settings(tmp_path), sleep=sleep)

    async def run(label: str, command: list[str], hung_after: float | None = None) -> Ended:
        return Ended(1, ["[tcp @ 0x7f00] Connection to tcp://user:s3cret@mediamtx:8554 failed"])

    publisher._run = run  # type: ignore[method-assign]
    keep_publishing = publisher._keep_publishing  # pyright: ignore[reportPrivateUsage]
    url = "rtsp://user:s3cret@mediamtx:8554/lobby"
    with (
        caplog.at_level(logging.INFO, logger="fake-cameras"),
        contextlib.suppress(asyncio.CancelledError),
    ):
        asyncio.run(keep_publishing(Camera("lobby", SYNTHETIC), tmp_path / "x.mp4", url, "l"))
    assert "s3cret" not in caplog.text
    assert "publishing l to rtsp://***@mediamtx:8554/lobby" in caplog.text
    assert "Connection to tcp://***@mediamtx:8554 failed" in caplog.text


# --- The cache --------------------------------------------------------------------------------


def test_a_video_missing_for_now_keeps_its_prepared_copy(
    publisher: Callable[[str], Publisher],
) -> None:
    # `make fetch-samples` was not run yet (or the samples directory was unmounted): the
    # prepared copy, named by the video's content, may be this camera's, so it stays.
    p = publisher("a=synthetic,b=walk.avi")
    (p.cache / "0123-old.mp4").write_bytes(b"prepared from a video that is missing now")
    (p.cache / "0123-old.mp4.part").write_bytes(b"from a killed preparation")
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    p.stop()
    assert sorted(p.cache.iterdir()) == sorted([p.cache / "0123-old.mp4", p.pattern])


def test_a_prepared_copy_no_video_could_use_is_deleted(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("b=walk.avi")
    (p.samples / "walk.avi").write_bytes(b"a video")
    (p.cache / "0123-old.mp4").write_bytes(b"prepared from another video")
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    p.stop()
    assert not (p.cache / "0123-old.mp4").exists()


def test_a_video_looped_by_several_cameras_is_read_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashed: list[Path | None] = []
    real = fake_cameras.cache_key

    def counting(source: Path | None, duration: int = DURATION) -> str:
        hashed.append(source)
        return real(source, duration)

    monkeypatch.setattr(fake_cameras, "cache_key", counting)
    settings = _settings(tmp_path, "a=walk.avi,b=walk.avi,c=other.avi,d=synthetic")
    for directory in (settings.samples, settings.cache):
        directory.mkdir()
    for name in ("walk.avi", "other.avi"):
        (settings.samples / name).write_bytes(name.encode())
    publisher = RealPublisher(settings)

    async def use() -> None:
        await publisher._prune_cache()  # pyright: ignore[reportPrivateUsage]
        for video in (settings.samples / "walk.avi", settings.samples / "walk.avi"):
            await publisher._key(video)  # pyright: ignore[reportPrivateUsage]

    asyncio.run(use())
    assert sorted(h.name for h in hashed if h is not None) == ["other.avi", "walk.avi"]
