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
import re
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import fake_cameras
import pytest
from fake_cameras import (
    DURATION,
    HUNG_AFTER,
    PLAYLIST_END_MARGIN,
    PLAYLIST_HOURS,
    PLAYLIST_MOST_PASSES,
    PUBLISHED_LONG_ENOUGH,
    SYNTHETIC,
    Camera,
    Ended,
    Settings,
    cache_key,
    find_sample,
    not_publishing,
    parse_cameras,
    playlist,
    playlist_seconds,
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


def test_a_camera_joining_the_timeline_reads_a_playlist_not_a_seek_and_a_loop() -> None:
    command = publish_command(Path("/cache/abc.mp4"), URL, playlist=Path("/tmp/lobby.concat"))
    assert _after(command, "-f") == "concat"
    assert _after(command, "-safe") == "0"
    assert _after(command, "-i") == "/tmp/lobby.concat"
    # `-ss` with `-stream_loop` gives the pass after the first timestamps that do not move,
    # so that `-re` sends the whole loop at once: neither is used.
    assert "-ss" not in command
    assert "-stream_loop" not in command
    assert command.index("-re") < command.index("-i")


def _entries(text: str) -> list[list[str]]:
    """The `file` entries of a concat list, each as its lines."""
    entries: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("file "):
            entries.append([])
        entries[-1].append(line)
    return entries


def test_a_playlist_plays_the_rest_of_the_sample_and_then_the_whole_of_it_again() -> None:
    entries = _entries(playlist(Path("/cache/abc.mp4"), 101, 300))
    # Cut at a whole second, which is a keyframe, and as long as what is left of the duration.
    assert entries[0] == ["file '/cache/abc.mp4'", "inpoint 101", "duration 199"]
    # Every later pass is the duration long, whatever the file's own length.
    assert len(entries) > 1
    assert all(e == ["file '/cache/abc.mp4'", "duration 300"] for e in entries[1:])
    # Long enough that an ffmpeg runs for a day before it joins the timeline anew.
    assert sum(300 for _ in entries) >= PLAYLIST_HOURS * 3600


def test_a_playlist_from_the_beginning_has_no_inpoint() -> None:
    entries = _entries(playlist(Path("/cache/abc.mp4"), 0, 20))
    assert entries[0] == ["file '/cache/abc.mp4'", "duration 20"]
    assert len(entries) == PLAYLIST_HOURS * 3600 // 20


def test_a_playlist_stays_small_however_short_the_duration() -> None:
    # A day of one-second loops would be 86,400 entries; it ends sooner, and rejoins.
    text = playlist(Path("/cache/abc.mp4"), 0, 1)
    assert len(_entries(text)) == PLAYLIST_MOST_PASSES
    assert len(text.encode()) < 256 * 1024
    # Long enough to need no rejoining more than every hour or so at any sensible duration.
    assert PLAYLIST_MOST_PASSES >= 3600
    assert len(_entries(playlist(Path("/cache/abc.mp4"), 0, 20))) < PLAYLIST_MOST_PASSES


@pytest.mark.parametrize(("start", "duration"), [(101, 300), (0, 20), (0, 1), (19, 20)])
def test_the_length_of_a_playlist_is_what_its_entries_add_up_to(start: int, duration: int) -> None:
    entries = _entries(playlist(Path("/cache/abc.mp4"), start, duration))
    listed = sum(int(e[-1].removeprefix("duration ")) for e in entries)
    assert playlist_seconds(start, duration) == listed


def test_a_playlist_quotes_the_names_it_lists() -> None:
    assert _entries(playlist(Path("/cache/it's.mp4"), 0, 20))[0][0] == r"file '/cache/it'\''s.mp4'"


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
    # And starts at 0, so that its keyframes (every second) are at whole seconds.
    assert _after(command, "-vf").startswith("setpts=PTS-STARTPTS,")
    other = prepare_command(Path("/samples/a.avi"), Path("/cache/a.part"), duration=20)
    assert _after(other, "-t") == "20"
    assert "stop_duration=20" in _after(other, "-vf")


def test_a_sample_is_prepared_at_a_whole_number_of_frames_a_second() -> None:
    # At 29.97 frames a second no picture falls on a whole second after the first, so neither
    # would a keyframe, where every camera joins the timeline: its own rate, rounded, does.
    command = prepare_command(Path("/samples/a.avi"), Path("/cache/a.part"))
    # The filters, split at the commas between them, not at those escaped inside one.
    filters = re.split(r"(?<!\\),", _after(command, "-vf"))
    rate = r"fps=fps=max(1\,round(source_fps))"
    assert rate in filters
    # Its pictures counted from 0 first, and the last held only once the rate is whole.
    assert filters.index("setpts=PTS-STARTPTS") < filters.index(rate)
    assert filters.index(rate) < [f.startswith("tpad=") for f in filters].index(True)


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


def test_the_time_without_a_frame_after_which_a_camera_is_not_publishing_is_a_setting() -> None:
    cameras = [Camera("a", SYNTHETIC)]
    assert not_publishing(cameras, {"a": 995.0}, 1000.0, stale_after=10) == []
    assert not_publishing(cameras, {"a": 995.0}, 1000.0, stale_after=2) == ["a"]
    env = {"FAKE_CAMERAS": "a=synthetic"}
    assert Settings.from_environment(env).stale_after == fake_cameras.STALE_AFTER
    assert Settings.from_environment({**env, "FAKE_CAMERAS_STALE_AFTER": "2"}).stale_after == 2
    with pytest.raises(ValueError, match="FAKE_CAMERAS_STALE_AFTER"):
        Settings.from_environment({**env, "FAKE_CAMERAS_STALE_AFTER": "0"})


# --- Supervising ffmpeg -----------------------------------------------------------------------

#: Stands in for ffmpeg: logs its arguments, reports progress like `-progress pipe:1` until
#: told to stop, or exits at once when the file named by FAKE_FFMPEG_FAIL exists. While the file
#: named by FAKE_FFMPEG_STALL exists its frame count stops, as a real ffmpeg's does when its
#: write blocks, but it goes on reporting it, as a real ffmpeg does. Preparing a
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
        # A write MediaMTX stopped reading blocks, and ffmpeg goes on reporting the same count.
        if not Path(os.environ["FAKE_FFMPEG_STALL"]).exists():
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
        self.stall = tmp_path / "stall"
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
            "FAKE_FFMPEG_STALL": str(self.stall),
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
    """What every publishing ffmpeg started so far read: the file it loops, or the one a
    playlist it was given lists."""
    return {_played(c) for c in calls if c[-1].startswith("rtsp://")}


def _played(command: list[str]) -> str:
    """The file a publishing ffmpeg plays."""
    read = command[command.index("-i") + 1]
    if "concat" not in command:
        return read
    return Path(read).read_text().splitlines()[0].removeprefix("file '").removesuffix("'")


def _prepared(calls: list[list[str]]) -> list[list[str]]:
    """Every ffmpeg started so far to prepare a sample, not the test pattern."""
    return [c for c in calls if c[-2] == "mp4" and "lavfi" not in c]


def test_with_a_password_each_camera_publishes_as_itself_with_it(
    publisher: Callable[[str], Publisher],
) -> None:
    # MediaMTX lets a camera publish only with its own credentials (ADR 0002): the fake cameras
    # are registered as pushed cameras whose publish user is their name, sharing one password.
    p = publisher("lobby=synthetic,yard-2=synthetic")
    p.env["FAKE_CAMERAS_PASSWORD"] = "fake/camera pa55word+"
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    assert sorted(_publishing(p.ffmpeg_calls())) == [
        "rtsp://lobby:fake%2Fcamera%20pa55word%2B@mediamtx.test:8554/lobby",
        "rtsp://yard-2:fake%2Fcamera%20pa55word%2B@mediamtx.test:8554/yard-2",
    ]
    logged = p.stop()
    assert "pa55word" not in logged
    assert "publishing the test pattern to rtsp://***@mediamtx.test:8554/lobby" in logged


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


def test_a_camera_whose_frame_count_stops_is_unhealthy_though_ffmpeg_still_reports(
    publisher: Callable[[str], Publisher],
) -> None:
    # A real ffmpeg whose RTSP write blocks prints the same `frame=` every second.
    p = publisher("lobby=synthetic")
    # Reported by the status file, which is written once a second: two seconds is stale.
    p.env["FAKE_CAMERAS_STALE_AFTER"] = "2"
    p.start()
    _eventually(lambda: p.health().returncode == 0)
    p.stall.touch()
    _eventually(lambda: p.health().returncode != 0, timeout=8)
    assert "lobby" in p.health().stdout
    # Its frames move again: it is publishing.
    p.stall.unlink()
    _eventually(lambda: p.health().returncode == 0)
    p.stop()


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
    starts: list[str] = []

    async def sleep(seconds: float) -> None:
        wall[0] += seconds
        if len(commands) >= 3:
            raise asyncio.CancelledError

    publisher = RealPublisher(
        _settings(tmp_path), clock=lambda: wall[0], sleep=sleep, wall=lambda: wall[0]
    )

    async def run(label: str, command: list[str], hung_after: float | None = None) -> Ended:
        commands.append(command)
        starts.append(Path(_after(command, "-i")).read_text().splitlines()[1])
        wall[0] += 40  # It published for a while, and then exited.
        return Ended(1, [])

    publisher._run = run  # type: ignore[method-assign]
    keep_publishing = publisher._keep_publishing  # pyright: ignore[reportPrivateUsage]
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(keep_publishing(Camera("lobby", "a.mp4"), tmp_path / "x.mp4", "u", "l", True))
    # Started at 1001 s (the next whole second), then restarted at 1042 s and at 1083 s, each
    # time at the point of the sample the other cameras are at: not from its beginning.
    assert starts == ["inpoint 101", "inpoint 142", "inpoint 183"]
    assert all(_played(c) == str(tmp_path / "x.mp4") for c in commands)


def _play_to_the_end(tmp_path: Path, runs: list[float]) -> tuple[list[float], list[str]]:
    """Publish a sample with ffmpegs that each exit with 0 after `runs` seconds, as one does
    at the end of its playlist; what the publisher slept for, and where each ffmpeg started."""
    wall = [1000.4]
    starts: list[str] = []
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        wall[0] += seconds
        if not runs:
            raise asyncio.CancelledError

    publisher = RealPublisher(
        _settings(tmp_path), clock=lambda: wall[0], sleep=sleep, wall=lambda: wall[0]
    )

    async def run(label: str, command: list[str], hung_after: float | None = None) -> Ended:
        starts.append(Path(_after(command, "-i")).read_text().splitlines()[1])
        wall[0] += runs.pop(0)
        return Ended(0, [])

    publisher._run = run  # type: ignore[method-assign]
    keep_publishing = publisher._keep_publishing  # pyright: ignore[reportPrivateUsage]
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(keep_publishing(Camera("lobby", "a.mp4"), tmp_path / "x.mp4", "u", "l", True))
    return delays, starts


def test_a_playlist_played_to_its_end_rejoins_the_timeline_at_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    day = PLAYLIST_HOURS * 3600.0
    with caplog.at_level(logging.INFO, logger="fake-cameras"):
        delays, starts = _play_to_the_end(tmp_path, [day, day])
    # Only the waits for the next whole second (none: a day of whole seconds went by), never a
    # restart's backoff.
    assert delays == [pytest.approx(0.6), 0, 0]
    assert starts == ["inpoint 101", "inpoint 101"]
    # The daily reconnect is expected: said, not warned about.
    assert [r.levelname for r in caplog.records if "lobby: " in r.message] == ["INFO"] * 3
    assert "lobby: played its playlist to the end; rejoining the timeline" in caplog.text


def test_an_ffmpeg_that_ends_soon_after_it_started_is_restarted_as_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A playlist is a day long: one that ended at once did not play, whatever ffmpeg says.
    with caplog.at_level(logging.WARNING, logger="fake-cameras"):
        delays, _ = _play_to_the_end(tmp_path, [0.1, 0.1])
    assert 1 in delays
    assert 2 in delays
    assert "lobby: ffmpeg exited with 0; restarting in 1 s" in caplog.text


def test_an_ffmpeg_that_ends_well_before_its_playlist_is_restarted_as_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Long enough to be told from a failure at once, but nowhere near the playlist's length: an
    # ffmpeg that exited with 0 then did not play it to the end.
    early = playlist_seconds(101, DURATION) - PLAYLIST_END_MARGIN - 1
    assert early > PUBLISHED_LONG_ENOUGH
    with caplog.at_level(logging.INFO, logger="fake-cameras"):
        delays, _ = _play_to_the_end(tmp_path, [early])
    assert "lobby: ffmpeg exited with 0; restarting in 1 s" in caplog.text
    assert "played its playlist to the end" not in caplog.text
    assert 1 in delays


def test_an_ffmpeg_that_ends_within_the_margin_of_its_playlist_played_it_to_the_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    just = playlist_seconds(101, DURATION) - PLAYLIST_END_MARGIN
    with caplog.at_level(logging.INFO, logger="fake-cameras"):
        delays, _ = _play_to_the_end(tmp_path, [just])
    assert "lobby: played its playlist to the end; rejoining the timeline" in caplog.text
    assert "restarting in" not in caplog.text
    assert 1 not in delays


async def _a_moment(seconds: float) -> None:
    await asyncio.sleep(0.001)


def _publish_samples(
    settings: Settings,
    wall: list[float],
    clock: list[float],
    sleep: Callable[[float], Awaitable[None]] = _a_moment,
) -> tuple[RealPublisher, dict[str, list[str]]]:
    """A publisher of `settings`' cameras, whose samples are prepared already, with ffmpegs
    that publish until they are stopped; and, for each camera, where each of its ffmpegs
    started on the timeline (`loop` for the test pattern, which has none). Whatever it waits
    for, it waits for a moment, or as `sleep` says."""
    for directory in (settings.samples, settings.cache):
        directory.mkdir()
    for camera in settings.cameras:
        if camera.source != SYNTHETIC:
            video = settings.samples / camera.source
            video.write_bytes(camera.source.encode())
            (settings.cache / f"{cache_key(video, settings.duration)}.mp4").write_bytes(b"x")
    (settings.cache / f"{cache_key(None)}.mp4").write_bytes(b"x")
    starts: dict[str, list[str]] = {}
    publisher = RealPublisher(settings, clock=lambda: clock[0], sleep=sleep, wall=lambda: wall[0])

    async def run(label: str, command: list[str], hung_after: float | None = None) -> Ended:
        read = _after(command, "-i")
        start = "loop" if "concat" not in command else Path(read).read_text().splitlines()[1]
        starts.setdefault(label, []).append(start)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    publisher._run = run  # type: ignore[method-assign]
    return publisher, starts


def _until(condition: Callable[[], bool]) -> Coroutine[None, None, None]:
    async def wait() -> None:
        async with asyncio.timeout(10):
            while not condition():
                await asyncio.sleep(0.01)

    return wait()


@pytest.mark.parametrize(
    ("step", "position"),
    [
        # A step under a second is not rounded up to one.
        (0.7, 102),
        # The host was suspended for 1234 s: the wall clock moved on, the monotonic one did not.
        (1234.0, 135),
        # The wall clock was set back by a minute.
        (-60.0, 41),
    ],
)
def test_a_step_of_the_wall_clock_makes_every_sample_rejoin_the_timeline_together(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, step: float, position: int
) -> None:
    settings = _settings(tmp_path, "a=walk.avi,b=other.avi,c=synthetic")
    wall, clock = [1000.4], [50.0]
    publisher, starts = _publish_samples(settings, wall, clock)

    async def scenario() -> None:
        running = asyncio.create_task(publisher.run())
        await _until(lambda: all(len(starts.get(c, [])) == 1 for c in "abc"))
        wall[0] += step
        await _until(lambda: all(len(starts.get(c, [])) == 2 for c in "ab"))
        await asyncio.sleep(0.1)
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running

    with caplog.at_level(logging.INFO, logger="fake-cameras"):
        asyncio.run(scenario())
    assert starts["a"] == starts["b"] == ["inpoint 101", f"inpoint {position}"]
    # The test pattern is on no timeline, and goes on as it was.
    assert starts["c"] == ["loop"]
    assert f"the wall clock stepped by {step:+.1f} s" in caplog.text
    # Not a failure: no backoff, no warning.
    assert "restarting in" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def _watch_steps(steps: list[tuple[float, float]]) -> list[logging.LogRecord]:
    """What `_watch_clock` logs while the clock goes on, a check a second, and the wall clock
    steps as `steps` say: (seconds of the clock when, by how much)."""
    wall, clock = [10_000.0], [0.0]
    pending = sorted(steps)

    async def sleep(seconds: float) -> None:
        clock[0] += seconds
        wall[0] += seconds
        while pending and pending[0][0] <= clock[0]:
            wall[0] += pending.pop(0)[1]
        if not pending and clock[0] > 3 * 3600 + 10:
            raise asyncio.CancelledError

    publisher = RealPublisher(
        _settings(Path("/nonexistent")), clock=lambda: clock[0], sleep=sleep, wall=lambda: wall[0]
    )
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("fake-cameras")
    handler = Collect(logging.WARNING)
    logger.addHandler(handler)
    try:
        with contextlib.suppress(asyncio.CancelledError):
            asyncio.run(publisher._watch_clock())  # pyright: ignore[reportPrivateUsage]
    finally:
        logger.removeHandler(handler)
    return records


def test_a_host_whose_clock_steps_often_is_warned_about_by_the_count() -> None:
    # Four steps within an hour (more than three).
    records = _watch_steps([(100.0, 5.0), (200.0, -5.0), (300.0, 5.0), (400.0, -5.0)])
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "4 times" in records[0].getMessage()
    assert "hour" in records[0].getMessage()


def test_a_few_steps_of_the_wall_clock_are_not_warned_about() -> None:
    assert _watch_steps([(100.0, 5.0), (200.0, -5.0), (300.0, 5.0)]) == []
    # Nor are many, when they are spread over hours.
    assert _watch_steps([(i * 1500.0 + 100, 5.0) for i in range(6)]) == []


def test_the_warning_about_a_clock_that_steps_often_comes_at_most_once_an_hour() -> None:
    # Eight steps within a quarter of an hour: warned at the fourth, not at the next four.
    # Four more, long after, are warned about again.
    steps = [(100.0 + i * 100, 5.0) for i in range(8)] + [(5000.0 + i * 100, 5.0) for i in range(4)]
    records = _watch_steps(steps)
    assert len(records) == 2
    assert all("4 times" in r.getMessage() for r in records)


def test_a_step_of_the_wall_clock_while_a_camera_waits_to_join_is_not_missed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, "a=walk.avi")
    wall, clock = [1000.4], [50.0]
    joins: list[float] = []

    async def sleep(seconds: float) -> None:
        if seconds != fake_cameras.CLOCK_EVERY and not joins:
            # The camera waits for the next whole second, and the clock steps meanwhile.
            joins.append(seconds)
            wall[0] += 1234.0
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.001)

    publisher, starts = _publish_samples(settings, wall, clock, sleep)

    async def scenario() -> None:
        running = asyncio.create_task(publisher.run())
        await _until(lambda: len(starts.get("a", [])) >= 1)
        await asyncio.sleep(0.1)
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running

    asyncio.run(scenario())
    # It starts where the clock says now, not where it said before it stepped.
    assert joins == [pytest.approx(0.6)]
    assert starts["a"] == ["inpoint 135"]


def test_the_wall_clock_moving_with_the_monotonic_one_restarts_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "a=walk.avi")
    wall, clock = [1000.4], [50.0]
    publisher, starts = _publish_samples(settings, wall, clock)

    async def scenario() -> None:
        running = asyncio.create_task(publisher.run())
        await _until(lambda: len(starts.get("a", [])) == 1)
        for _ in range(20):
            wall[0] += 0.2 + 0.001  # A clock slewed by NTP gains a little.
            clock[0] += 0.2
            await asyncio.sleep(0.01)
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running

    asyncio.run(scenario())
    assert starts["a"] == ["inpoint 101"]


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
    assert [_after(c, "-stream_loop") for c in commands] == ["-1"]
    assert "concat" not in commands[0]


# --- A hung ffmpeg ----------------------------------------------------------------------------

#: Stands in for a publishing ffmpeg: writes its pid to the file named by its first argument,
#: and reports progress every 0.1 s. With `hang`, its frame count stops after three reports,
#: as when a write to a MediaMTX that stopped reading blocks; and, as a real ffmpeg 7 or later
#: does then, it goes on reporting that same count every time, alive.
HANGING_FFMPEG = textwrap.dedent(
    """\
    import os, sys, time
    from pathlib import Path
    Path({pid!r}).write_text(str(os.getpid()))
    frame = 0
    while True:
        if not ({hang} and frame >= 45):
            frame += 15
        print(f"frame={{frame}}\\nprogress=continue", flush=True)
        time.sleep(0.1)
    """
)


class _Watched:
    """A publisher whose clock moves a tenth of a second per watchdog check, so that the
    fifteen seconds before a hung ffmpeg is killed take about a second and a half. Its wall
    clock does not move: the watchdog times ffmpeg by the monotonic clock, which a step of
    the wall clock (or a suspended host) does not move."""

    def __init__(self, tmp_path: Path, hang: bool) -> None:
        self.pid = tmp_path / "ffmpeg.pid"
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_text(
            f"#!{sys.executable}\n" + HANGING_FFMPEG.format(pid=str(self.pid), hang=hang)
        )
        ffmpeg.chmod(0o755)
        self.now = 1000.0
        self.checks = 0
        #: The restart delays, which are the other thing the publisher sleeps for.
        self.restarts: list[float] = []
        self.publisher = RealPublisher(
            _settings(tmp_path, ffmpeg=str(ffmpeg)),
            clock=lambda: self.now,
            sleep=self.sleep,
            wall=lambda: 1000.0,
        )

    async def sleep(self, seconds: float) -> None:
        if seconds == fake_cameras.WATCH_EVERY:
            self.checks += 1
            self.now += 0.1
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


def test_an_ffmpeg_whose_frame_count_stops_is_killed_and_restarted(
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
    assert "lobby: ffmpeg sent no new frame for 15 s and was killed; restarting in 1 s" in (
        caplog.text
    )


def test_an_ffmpeg_whose_frame_count_moves_is_left_running(tmp_path: Path) -> None:
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


@pytest.mark.parametrize("gone", ["deleted", "made unreadable"])
def test_a_video_that_goes_after_it_was_found_gets_the_test_pattern(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    gone: str,
) -> None:
    # It was there when the publisher looked for it, and is gone when it is read to be named.
    if gone == "made unreadable" and os.geteuid() == 0:
        pytest.skip("root reads whatever the mode says")
    settings = _settings(tmp_path, "a=walk.avi,b=synthetic")
    wall, clock = [1000.4], [50.0]
    publisher, starts = _publish_samples(settings, wall, clock)
    video = settings.samples / "walk.avi"
    if gone == "deleted":
        video.unlink()
    else:
        video.chmod(0o000)

    def found(samples: Path, source: str) -> Path:
        return samples / source

    monkeypatch.setattr(fake_cameras, "find_sample", found)
    (settings.cache / "0123-old.mp4").write_bytes(b"perhaps walk.avi's, once it is back")

    async def scenario() -> None:
        running = asyncio.create_task(publisher.run())
        # Every camera is up, the one whose video went on the test pattern.
        await _until(lambda: running.done() or all(len(starts.get(c, [])) == 1 for c in "ab"))
        await asyncio.sleep(0.1)
        if running.done():
            running.result()  # Says why it ended, which it must not.
            pytest.fail("the publisher ended")
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running

    with caplog.at_level(logging.WARNING, logger="fake-cameras"):
        asyncio.run(scenario())
    assert starts == {"a": ["loop"], "b": ["loop"]}
    assert "a: walk.avi could not be read" in caplog.text
    assert "gets the test pattern instead" in caplog.text
    # Its prepared copy may come back into use: it is kept, as for a video that is missing.
    assert (settings.cache / "0123-old.mp4").exists()
