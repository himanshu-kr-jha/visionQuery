"""The fake camera publisher of the `dev` profile, without Docker or a real ffmpeg.

What it publishes is checked for real by the stack tests (tests/test_stack.py); here are its
configuration, the ffmpeg commands it runs, and how it supervises them, with a stand-in
ffmpeg that reports progress the way the real one does.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fake_cameras import (
    SYNTHETIC,
    Camera,
    cache_key,
    not_publishing,
    parse_cameras,
    prepare_command,
    publish_command,
)

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "fake-cameras" / "fake_cameras.py"

# --- Configuration ----------------------------------------------------------------------------


def test_each_entry_is_a_camera_named_for_its_path_and_its_source() -> None:
    cameras = parse_cameras("gate-2=meva-school-g299.avi, lobby=synthetic,yard=clips/yard.mp4")
    assert cameras == [
        Camera("gate-2", "meva-school-g299.avi"),
        Camera("lobby", SYNTHETIC),
        Camera("yard", "clips/yard.mp4"),
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
        ("a=clips/../../outside.mp4", "outside.mp4"),
    ],
)
def test_a_malformed_configuration_is_refused_saying_why(spec: str, complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        parse_cameras(spec)


# --- The commands -----------------------------------------------------------------------------

URL = "rtsp://mediamtx:8554/lobby"


def _after(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]


def test_a_camera_loops_a_prepared_clip_in_real_time_without_transcoding() -> None:
    command = publish_command(Path("/cache/abc.mp4"), URL)
    assert command[0] == "ffmpeg"
    assert _after(command, "-stream_loop") == "-1"
    assert "-re" in command
    assert command.index("-re") < command.index("-i")
    assert _after(command, "-i") == "/cache/abc.mp4"
    assert _after(command, "-c") == "copy"


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


def test_preparing_a_clip_makes_it_camera_like() -> None:
    command = prepare_command(Path("/samples/a.avi"), Path("/cache/a.part"))
    assert _after(command, "-i") == "/samples/a.avi"
    _camera_like(command)
    assert command[-3:] == ["-f", "mp4", "/cache/a.part"]


def test_the_test_pattern_is_a_clip_prepared_like_any_other() -> None:
    # Encoded once and looped, so that many synthetic cameras cost no more than many clips.
    command = prepare_command(None, Path("/cache/b.part"))
    assert _after(command, "-f") == "lavfi"
    assert _after(command, "-i").startswith("testsrc2=")
    assert "duration=" in _after(command, "-i")
    _camera_like(command)
    assert command[-3:] == ["-f", "mp4", "/cache/b.part"]


def test_a_clip_is_prepared_again_only_when_its_content_changes(tmp_path: Path) -> None:
    clip = tmp_path / "walk.mp4"
    clip.write_bytes(b"one")
    first = cache_key(clip)
    renamed = tmp_path / "renamed.mp4"
    clip.rename(renamed)
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
#: clip (not the test pattern) takes until the file named by FAKE_FFMPEG_HOLD is gone.
FAKE_FFMPEG = textwrap.dedent(
    """\
    import json, os, sys, time
    from pathlib import Path
    with open(os.environ["FAKE_FFMPEG_LOG"], "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    if sys.argv[-2] == "mp4":
        while "lavfi" not in sys.argv and Path(os.environ["FAKE_FFMPEG_HOLD"]).exists():
            time.sleep(0.1)
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
    """Every ffmpeg started so far to prepare a clip, not the test pattern."""
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


def test_a_clip_is_prepared_once_then_looped_from_the_cache(
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
    # the clip is looped from the start.
    p.calls.unlink()
    p.status.unlink()
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    p.stop()
    calls = p.ffmpeg_calls()
    assert [c for c in calls if c[-2] == "mp4"] == []
    assert _inputs(calls) == {str(cached)}


def test_a_camera_shows_the_test_pattern_while_its_clip_is_prepared(
    publisher: Callable[[str], Publisher],
) -> None:
    # Preparing a long clip takes a minute or more, and `make up` waits for health meanwhile.
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


def test_a_clip_no_longer_configured_leaves_the_cache(
    publisher: Callable[[str], Publisher],
) -> None:
    p = publisher("a=synthetic")
    (p.cache / "0123-old.mp4").write_bytes(b"from a clip no camera uses now")
    (p.cache / "0123-old.mp4.part").write_bytes(b"from a killed preparation")
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    p.stop()
    # The test pattern is always kept: a camera whose video is missing falls back to it.
    assert list(p.cache.iterdir()) == [p.pattern]


def test_a_clip_not_fetched_yet_falls_back_to_the_test_pattern_and_says_so(
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
