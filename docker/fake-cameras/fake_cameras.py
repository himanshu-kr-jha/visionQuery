#!/usr/bin/env python3
"""Fake cameras for development: loops videos into MediaMTX over RTSP, one path per camera.

The `dev` profile's camera service (compose.yaml). Each camera is one ffmpeg that publishes
to `<FAKE_CAMERAS_RTSP>/<camera>` and is restarted whenever it exits, which is how a camera
comes back after MediaMTX restarts. It is a development tool, never part of a Deployment.

MediaMTX lets a camera publish only with its own credentials: with FAKE_CAMERAS_PASSWORD set,
each camera publishes as the user named for it, with that password, which is how
scripts/register_fake_cameras.py registers them. Without it a camera publishes without
credentials, which MediaMTX refuses.

    FAKE_CAMERAS="gate=meva-school-g299.avi,lobby=synthetic" fake_cameras.py [run | health]

FAKE_CAMERAS lists the cameras as `name=source`, comma-separated: the name is the camera's
MediaMTX path, and the source is `synthetic` (a test pattern) or a video's path relative
to the samples directory. Each video, and the test pattern, is first prepared
once into the cache as a camera would send it (H.264, no B-frames, a keyframe every second, no
audio) and then looped in real time without transcoding, so a camera costs next to no CPU; a
video is prepared again only when its content changes, and shows the test pattern meanwhile.
A video that is not in the samples directory, or that the publisher cannot read, is replaced by
the test pattern, with a warning saying which and how to fix it, until the publisher restarts:
`make up` then works before `make fetch-samples`. A video that cannot be prepared (a few
attempts, FAKE_CAMERAS_PREPARE_ATTEMPTS) keeps its cameras on the test pattern, with one error.

All the videos are cut to the same FAKE_CAMERAS_DURATION seconds (300), at their own frame rate
rounded to a whole number, and every camera plays its video from the second the wall clock says,
counted from the Unix epoch: at every start of an ffmpeg, and at every restart, a camera joins
the timeline at the next whole second (where there is a keyframe). Videos recorded together are
therefore in step on every camera, whenever each was prepared or restarted, and the same footage
repeats every DURATION seconds, from the same minutes of every hour. Each ffmpeg sends its first
half second at once and then paces itself, and starts a little sooner or later than another, so
the cameras are a few tenths of a second ahead of the wall clock and differ by up to about 0.15 s,
which shifts a little when one restarts.

A camera playing a sample rejoins the timeline, disconnecting its viewers once, when its ffmpeg
has played the day its playlist lasts (less for a very short DURATION), and when the wall clock
steps (it is set, or the host was suspended), which every such camera does at the same moment.
An ffmpeg whose frame count has not moved for 15 s is hung (it goes on reporting the same count
while its write blocks): it is killed and restarted.

`health` exits 0 when every camera's ffmpeg has sent a new frame in the last few seconds.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import NoReturn
from urllib.parse import quote

SYNTHETIC = "synthetic"
#: The test pattern: what a modest camera sends.
SYNTHETIC_SIZE = "1280x720"
SYNTHETIC_RATE = 15
#: How long the test pattern runs before it loops.
SYNTHETIC_SECONDS = 60
#: A camera whose ffmpeg last sent a new frame longer ago than this is not publishing.
STALE_AFTER = 5.0
#: Changes whenever prepare_command does, so that samples prepared the old way are redone.
PREPARATION = "4"
#: Every sample is prepared to this many seconds (a whole number, so that every restart can
#: land on a keyframe), and every camera plays it from the same point of the shared timeline.
DURATION = 300
#: How long a camera plays its video before its ffmpeg ends and a new one joins the timeline
#: again, which lets a playlist stand in for a loop (see `playlist`).
PLAYLIST_HOURS = 24
#: But a playlist lists the sample at most this many times, which keeps it to a few hundred kB
#: however short the duration; one that ends sooner than a day rejoins the timeline sooner.
PLAYLIST_MOST_PASSES = 5000
#: Where that timeline starts, in Unix time: at 0, a loop of 300 s begins at every five minutes
#: of the wall clock (hh:00, hh:05, ...), whatever the machine and whenever the cameras started.
EPOCH = 0.0
#: A running ffmpeg that has not sent a new frame for this long is hung (a blocked RTSP write),
#: and is killed so that it restarts; health reports it from STALE_AFTER on.
HUNG_AFTER = 3 * STALE_AFTER
#: How often a running ffmpeg is checked for that.
WATCH_EVERY = 0.5
#: Every ffmpeg paces its sample by the monotonic clock, and the timeline is the wall clock's:
#: once the two have moved apart by more than this (the wall clock was set, or the host was
#: suspended, which stops the monotonic clock), every camera playing a sample rejoins the
#: timeline. Checked this often.
CLOCK_STEP = 0.5
CLOCK_EVERY = 1.0
#: Preparing a sample is tried this many times, this many seconds apart, before the cameras
#: that loop it keep the test pattern.
PREPARE_ATTEMPTS = 3
PREPARE_RETRY = 30.0
#: Seconds before restarting an ffmpeg that exited: doubled after each failure, up to the last.
BACKOFF = (1, 2, 4, 8)
#: A camera that published for this long is back to the shortest backoff when it exits.
PUBLISHED_LONG_ENOUGH = 30.0
#: An ffmpeg that exits with 0 has played its playlist to the end only if it published for at
#: least the playlist's length less this many seconds (it starts a little late, and ends early).
PLAYLIST_END_MARGIN = 30.0
#: A wall clock that steps more than this many times in CLOCK_STEPS_WITHIN seconds is a host to
#: look at (an NTP daemon fighting another, a VM that is paused): warned about, at most that
#: often.
CLOCK_STEPS_OFTEN = 3
CLOCK_STEPS_WITHIN = 3600.0

_NAME = re.compile(r"[A-Za-z0-9_-]+")
#: The `user:password@` of a URL.
_USERINFO = re.compile(r"(?<=://)[^/@\s]*@")
_LOG = logging.getLogger("fake-cameras")


@dataclass(frozen=True)
class Camera:
    #: Its MediaMTX path.
    name: str
    #: SYNTHETIC, or a video's path relative to the samples directory.
    source: str


def parse_cameras(spec: str) -> list[Camera]:
    """The cameras FAKE_CAMERAS lists; ValueError saying what is wrong otherwise."""
    cameras: list[Camera] = []
    for entry in (e.strip() for e in spec.split(",")):
        if not entry:
            continue
        name, _, source = (part.strip() for part in entry.partition("="))
        if not name or not source:
            raise ValueError(f"{entry!r} is not name=source")
        if not _NAME.fullmatch(name):
            raise ValueError(f"camera name {name!r} may use only letters, digits, - and _")
        if any(c.name == name for c in cameras):
            raise ValueError(f"camera {name!r} is listed twice")
        path = PurePosixPath(source)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{name}: {source!r} must be a path inside the samples directory")
        cameras.append(Camera(name, source))
    if not cameras:
        raise ValueError("no camera is listed")
    return cameras


def redact(text: str) -> str:
    """`text` without the user and password of any URL in it, which must not reach the log."""
    return _USERINFO.sub("***@", text)


def timeline_start(now: float, duration: int = DURATION, epoch: float = EPOCH) -> tuple[float, int]:
    """Where a camera that starts at `now` (Unix time) joins the timeline every camera shares:
    the seconds to wait until the next whole second, and the second of the sample to play from
    then. Whole seconds are where the samples have keyframes, so that cameras joining at
    different times, or restarting, all agree on the picture within a fraction of a second."""
    elapsed = now - epoch
    whole = math.ceil(elapsed)
    return whole - elapsed, whole % duration


def cache_key(source: Path | None, duration: int = DURATION) -> str:
    """Names the prepared copy of `source` (None for the test pattern): its content, and how
    it was prepared."""
    digest = hashlib.sha256(PREPARATION.encode())
    if source is None:
        digest.update(SYNTHETIC.encode())
    else:
        digest.update(str(duration).encode())
        with source.open("rb") as file:
            digest.update(hashlib.file_digest(file, "sha256").digest())
    return digest.hexdigest()[:32]


def find_sample(samples: Path, source: str, uid: int | None = None) -> Path | str:
    """The video `source` in the samples directory, or why it cannot be used: not there, there
    but not readable by this user, or in a directory this user cannot look into."""
    path = samples / source
    uid = os.getuid() if uid is None else uid
    try:
        path.stat()
    except FileNotFoundError:
        return (
            f"{source} is not in the samples directory, so it gets the test pattern instead; "
            "run `make fetch-samples` (or add the video), then restart fake-cameras"
        )
    except PermissionError:
        return (
            f"the samples directory cannot be listed by uid {uid}, so {source} gets the test "
            "pattern instead; run `chmod a+rX samples` (and `chmod a+r` on its videos), then "
            "restart fake-cameras"
        )
    if not path.is_file() or not os.access(path, os.R_OK):
        return (
            f"{source} exists but is not readable by uid {uid}, so it gets the test pattern "
            f"instead; run `chmod a+r samples/{source}` (or `chmod -R a+rX samples`), then "
            "restart fake-cameras"
        )
    return path


def _quiet(ffmpeg: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]


def _passes(duration: int, hours: int) -> int:
    return min(PLAYLIST_MOST_PASSES, max(2, math.ceil(hours * 3600 / duration)))


def playlist_seconds(start: int, duration: int = DURATION, hours: int = PLAYLIST_HOURS) -> int:
    """How long `playlist` (with the same arguments) plays: what is left of the first pass,
    then whole passes."""
    return duration - start + (_passes(duration, hours) - 1) * duration


def playlist(
    source: Path, start: int, duration: int = DURATION, hours: int = PLAYLIST_HOURS
) -> str:
    """An ffmpeg concat list that plays `source` from second `start` to its end, and then from
    its beginning again for `hours` hours. This is a loop that starts part-way through, which
    `-ss` with `-stream_loop` is not: that gives the pass after the first timestamps that do not
    move for the length of the video, and `-re` then sends all of it at once. Every pass is
    `duration` seconds long whatever the file's own length, so that the loops do not drift."""
    name = str(source).replace("'", "'\\''")
    first = [f"file '{name}'"]
    if start:
        first.append(f"inpoint {start}")
    first.append(f"duration {duration - start}")
    passes = _passes(duration, hours)
    return "\n".join([*first, *[f"file '{name}'\nduration {duration}"] * (passes - 1)]) + "\n"


def publish_command(
    source: Path, url: str, ffmpeg: str = "ffmpeg", playlist: Path | None = None
) -> list[str]:
    """Loop a prepared sample into MediaMTX in real time, as it is, reporting progress: for
    ever from its beginning, or (with a `playlist` of it, see `playlist`) from where the
    playlist starts."""
    reading = (
        ["-stream_loop", "-1", "-i", str(source)]
        if playlist is None
        else ["-f", "concat", "-safe", "0", "-i", str(playlist)]
    )
    return [
        *_quiet(ffmpeg),
        "-progress", "pipe:1", "-stats_period", "1",
        "-re", *reading,
        "-map", "0:v:0", "-c", "copy", "-an",
        # RTP packets that fit MediaMTX's limit, which would otherwise split every one again.
        "-pkt_size", "1400",
        "-rtsp_transport", "tcp", "-f", "rtsp", url,
    ]  # fmt: skip


def prepare_command(
    source: Path | None, output: Path, ffmpeg: str = "ffmpeg", duration: int = DURATION
) -> list[str]:
    """Encode a sample (None for the test pattern) as a camera sends video, so that looping it
    needs no transcoding. A sample is made exactly `duration` seconds long, whether it is a
    little longer (as MEVA's cameras' are, by different amounts) or shorter (its last picture
    is held), so that every camera loops it in step with the others."""
    if source is None:
        pattern = (
            f"testsrc2=duration={SYNTHETIC_SECONDS}:size={SYNTHETIC_SIZE}:rate={SYNTHETIC_RATE}"
        )
        given = ["-f", "lavfi", "-i", pattern]
    else:
        # The pictures start at 0 whatever the recording's first ones are (MEVA's lose two to
        # the AVI), and come a whole number of times a second (29.97 becomes 30), so that the
        # keyframes fall on whole seconds, where the timeline joins.
        pad = (
            r"setpts=PTS-STARTPTS,fps=fps=max(1\,round(source_fps)),"
            f"tpad=stop_mode=clone:stop_duration={duration}"
        )
        given = ["-i", str(source), "-map", "0:v:0", "-vf", pad, "-t", str(duration)]
    return [
        *_quiet(ffmpeg),
        *given,
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
        # No B-frames, which a browser's WebRTC cannot play, and a keyframe every second.
        "-bf", "0", "-force_key_frames", "expr:gte(t,n_forced)",
        "-movflags", "+faststart",
        "-f", "mp4", str(output),
    ]  # fmt: skip


def restart_failures(failures: int, published: float) -> int:
    """The failures in a row after an ffmpeg that published for `published` seconds exited."""
    return 0 if published > PUBLISHED_LONG_ENOUGH else failures + 1


def restart_delay(failures: int) -> int:
    """Seconds to wait before restarting after that many failures in a row."""
    return BACKOFF[min(max(failures - 1, 0), len(BACKOFF) - 1)]


def not_publishing(
    cameras: Sequence[Camera],
    progress: Mapping[str, float],
    now: float,
    stale_after: float = STALE_AFTER,
) -> list[str]:
    """The cameras whose ffmpeg has not sent a new frame recently."""
    return [
        c.name
        for c in cameras
        if (last := progress.get(c.name)) is None or now - last > stale_after
    ]


def publish_url(rtsp: str, camera: Camera, password: str | None) -> str:
    """Where a camera publishes: its path under `rtsp`, as itself with `password` if given."""
    if not password:
        return f"{rtsp}/{camera.name}"
    scheme, _, rest = rtsp.partition("://")
    return f"{scheme}://{camera.name}:{quote(password, safe='')}@{rest}/{camera.name}"


@dataclass(frozen=True)
class Settings:
    cameras: list[Camera]
    rtsp: str
    ffmpeg: str
    samples: Path
    cache: Path
    status: Path
    #: Seconds every sample is prepared to, which is how often the cameras all start over.
    duration: int = DURATION
    prepare_attempts: int = PREPARE_ATTEMPTS
    prepare_retry: float = PREPARE_RETRY
    #: Seconds without a new frame after which `health` says a camera is not publishing.
    stale_after: float = STALE_AFTER
    #: What every camera publishes with, as the user named for it; None to publish without.
    password: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> Settings:
        try:
            cameras = parse_cameras(env.get("FAKE_CAMERAS", ""))
        except ValueError as error:
            raise ValueError(f"FAKE_CAMERAS: {error}") from error
        try:
            duration = int(env.get("FAKE_CAMERAS_DURATION", DURATION))
            attempts = int(env.get("FAKE_CAMERAS_PREPARE_ATTEMPTS", PREPARE_ATTEMPTS))
            retry = float(env.get("FAKE_CAMERAS_PREPARE_RETRY", PREPARE_RETRY))
            stale = float(env.get("FAKE_CAMERAS_STALE_AFTER", STALE_AFTER))
        except ValueError as error:
            raise ValueError(
                f"FAKE_CAMERAS_DURATION and _PREPARE_ATTEMPTS must be whole numbers and "
                f"_PREPARE_RETRY and _STALE_AFTER numbers: {error}"
            ) from error
        if duration < 1 or attempts < 1 or retry < 0 or not stale > 0:
            raise ValueError(
                "FAKE_CAMERAS_DURATION and FAKE_CAMERAS_PREPARE_ATTEMPTS must be at least 1, "
                "FAKE_CAMERAS_PREPARE_RETRY at least 0 and FAKE_CAMERAS_STALE_AFTER above 0"
            )
        return cls(
            duration=duration,
            prepare_attempts=attempts,
            prepare_retry=retry,
            stale_after=stale,
            cameras=cameras,
            rtsp=env.get("FAKE_CAMERAS_RTSP", "rtsp://mediamtx:8554").rstrip("/"),
            ffmpeg=env.get("FAKE_CAMERAS_FFMPEG", "ffmpeg"),
            samples=Path(env.get("FAKE_CAMERAS_SAMPLES", "/samples")),
            cache=Path(env.get("FAKE_CAMERAS_CACHE", "/cache")),
            status=Path(env.get("FAKE_CAMERAS_STATUS", "/tmp/fake-cameras.json")),
            password=env.get("FAKE_CAMERAS_PASSWORD") or None,
        )


#: The `[component @ 0x55d1c0de]` ffmpeg puts before a message.
_COMPONENT = re.compile(r"^\[[^\]]* @ 0x[0-9a-f]+\] ")


@dataclass(frozen=True)
class Ended:
    """How an ffmpeg ended: its exit status (None when it could not start) and what it wrote
    to stderr, which with `-loglevel error` is why. Logged as one line: its first message is
    the cause, and the rest are its consequences, which would only repeat it for every camera."""

    status: int | None
    errors: list[str]
    #: It stopped reporting progress and was killed for it.
    hung: bool = False

    def __str__(self) -> str:
        if self.hung:
            said = f"sent no new frame for {HUNG_AFTER:.0f} s and was killed"
        else:
            said = "could not start" if self.status is None else f"exited with {self.status}"
        if not self.errors:
            return said
        first = redact(_COMPONENT.sub("", self.errors[0]))
        more = len(self.errors) - 1
        if more:
            first += f", and {more} more line{'s' if more > 1 else ''}"
        return f"{said} ({first})"


async def _collect(stderr: asyncio.StreamReader, into: list[str]) -> None:
    async for raw in stderr:
        if line := raw.decode(errors="replace").strip():
            into.append(line)


class Publisher:
    """Runs and restarts one ffmpeg per camera, and records when each last sent a new frame,
    by the monotonic clock."""

    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        # Replaceable, so that tests need not wait out the restart backoff, nor a hung ffmpeg's
        # time to be noticed, and can choose where on the timeline a camera starts.
        self._clock = clock
        self._sleep = sleep
        self._wall = wall
        self._pattern = settings.cache / f"{cache_key(None)}.mp4"
        self._progress: dict[str, float] = {}
        self._prepared: dict[Path, asyncio.Task[bool]] = {}
        self._keys: dict[Path, asyncio.Task[str]] = {}
        # One preparation at a time: each uses every core, and the first camera is up sooner.
        self._preparing = asyncio.Semaphore(1)
        # Set, and replaced by a new one, when the wall clock steps (see `_watch_clock`).
        self._stepped = asyncio.Event()

    async def run(self) -> None:
        # Nothing is publishing yet, whatever an earlier run left behind.
        self._save_status()
        await self._prune_cache()
        await self._prepare(None, self._pattern)
        async with asyncio.TaskGroup() as group:
            group.create_task(self._write_status())
            group.create_task(self._watch_clock())
            for camera in self._settings.cameras:
                group.create_task(self._camera(camera))

    def _video(self, camera: Camera) -> Path | None:
        """The camera's video, or None for the test pattern."""
        if camera.source == SYNTHETIC:
            return None
        found = find_sample(self._settings.samples, camera.source)
        return found if isinstance(found, Path) else None

    async def _key(self, video: Path) -> str:
        """The name of the video's prepared copy; its content is read once, however many
        cameras loop it."""
        if video not in self._keys:
            self._keys[video] = asyncio.create_task(
                asyncio.to_thread(cache_key, video, self._settings.duration)
            )
        return await asyncio.shield(self._keys[video])

    async def _prune_cache(self) -> None:
        """Delete prepared samples no camera could use now, and preparations a kill interrupted.
        A camera whose video is missing now may get it back (`make fetch-samples`, then a
        restart), and its prepared copy, named by the video's content, cannot be told from
        another's: while there is such a camera, every prepared sample is kept."""
        cameras = self._settings.cameras
        videos = {v for c in cameras if (v := self._video(c)) is not None}
        keep_all = any(c.source != SYNTHETIC and self._video(c) is None for c in cameras)
        keys = {self._pattern.stem}
        for video in videos:
            try:
                keys.add(await self._key(video))
            except OSError:
                # It went since it was found, and may come back as a missing one may.
                keep_all = True
        for path in self._settings.cache.iterdir():
            complete = path.name == f"{path.stem}.mp4"
            if not complete or (path.stem not in keys and not keep_all):
                path.unlink()

    async def _camera(self, camera: Camera) -> NoReturn:
        url = publish_url(self._settings.rtsp, camera, self._settings.password)
        video = self._video(camera)
        if video is None:
            if camera.source != SYNTHETIC:
                problem = find_sample(self._settings.samples, camera.source)
                _LOG.warning("%s: %s", camera.name, problem)
            await self._keep_publishing(camera, self._pattern, url, "the test pattern")
        try:
            key = await self._key(video)
        except OSError as error:
            # Deleted, or made unreadable, since it was found.
            _LOG.warning(
                "%s: %s could not be read (%s), so it gets the test pattern instead; restart "
                "fake-cameras once it is back",
                camera.name,
                camera.source,
                error.strerror or error,
            )
            await self._keep_publishing(camera, self._pattern, url, "the test pattern")
        prepared = self._settings.cache / f"{key}.mp4"
        if not prepared.exists():
            # Preparing takes a while, and the camera is up meanwhile.
            _LOG.info("%s: the test pattern until %s is prepared", camera.name, camera.source)
            meanwhile = asyncio.create_task(
                self._keep_publishing(camera, self._pattern, url, "the test pattern")
            )
            try:
                if not await self._prepare(video, prepared):
                    # Said once, when the last attempt failed: the camera stays as it is.
                    await meanwhile
            finally:
                meanwhile.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await meanwhile
        await self._keep_publishing(camera, prepared, url, camera.source, timed=True)

    async def _keep_publishing(
        self, camera: Camera, source: Path, url: str, label: str, timed: bool = False
    ) -> NoReturn:
        """Loop `source` (`label` in the log) into MediaMTX, restarting ffmpeg whenever it
        exits or hangs. A `timed` source (a sample, not the test pattern) is joined at the
        point of the shared timeline that every camera is at, at every start and restart: when
        its playlist ends, a day later, and when the wall clock steps, it rejoins at once."""
        _LOG.info("%s: publishing %s to %s", camera.name, label, redact(url))
        failures = 0
        while True:
            expected = 0.0
            if not timed:
                command = publish_command(source, url, self._settings.ffmpeg)
                started = self._clock()
                ended = await self._run(camera.name, command, HUNG_AFTER)
            else:
                # Taken before the clock is read: a step from then on is not missed.
                stepped = self._stepped
                wait, start = timeline_start(self._wall(), self._settings.duration)
                playing = self._settings.status.with_name(f"{camera.name}.concat")
                playing.write_text(playlist(source, start, self._settings.duration))
                expected = playlist_seconds(start, self._settings.duration)
                await self._sleep(wait)
                command = publish_command(source, url, self._settings.ffmpeg, playing)
                started = self._clock()
                ended = await self._run_on_the_timeline(camera.name, command, stepped)
            self._progress.pop(camera.name, None)
            published = self._clock() - started
            if ended is None:
                _LOG.info("%s: rejoining the timeline", camera.name)
                failures = 0
                continue
            if (
                timed
                and ended.status == 0
                and published > PUBLISHED_LONG_ENOUGH
                and published >= expected - PLAYLIST_END_MARGIN
            ):
                _LOG.info("%s: played its playlist to the end; rejoining the timeline", camera.name)
                failures = 0
                continue
            failures = restart_failures(failures, published)
            delay = restart_delay(failures)
            _LOG.warning("%s: ffmpeg %s; restarting in %d s", camera.name, ended, delay)
            await self._sleep(delay)

    async def _run_on_the_timeline(
        self, label: str, command: list[str], stepped: asyncio.Event
    ) -> Ended | None:
        """Run a camera's ffmpeg as `_run` does, unless the wall clock steps (`stepped` is set)
        before it ends, which stops it (None) so that the camera rejoins the timeline the clock
        now says."""
        if stepped.is_set():
            return None
        step = asyncio.create_task(stepped.wait())
        run = asyncio.create_task(self._run(label, command, HUNG_AFTER))
        try:
            await asyncio.wait({run, step}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            step.cancel()
            if not run.done():
                run.cancel()
                # Waited for, not awaited: this task's own cancellation must not be lost.
                await asyncio.wait({run})
        return None if run.cancelled() else run.result()

    async def _watch_clock(self) -> NoReturn:
        """Tell every camera playing a sample when the wall clock has stepped (it was set, or
        the host was suspended): each ffmpeg goes on at the pace of the monotonic clock, from
        where it was, so that the cameras are no longer where the timeline says, and one that
        restarted would no longer be in step with the others."""
        offset = self._wall() - self._clock()
        steps: list[float] = []
        warned: float | None = None
        while True:
            await self._sleep(CLOCK_EVERY)
            step = self._wall() - self._clock() - offset
            if abs(step) > CLOCK_STEP:
                _LOG.info(
                    "the wall clock stepped by %+.1f s: every camera playing a sample rejoins "
                    "the timeline",
                    step,
                )
                now = self._clock()
                steps = [at for at in steps if now - at < CLOCK_STEPS_WITHIN] + [now]
                if len(steps) > CLOCK_STEPS_OFTEN and (
                    warned is None or now - warned >= CLOCK_STEPS_WITHIN
                ):
                    warned = now
                    _LOG.warning(
                        "the wall clock stepped %d times in the last hour: the host's time is "
                        "not steady, and every camera playing a sample rejoined the timeline "
                        "each time",
                        len(steps),
                    )
                offset += step
                self._stepped.set()
                self._stepped = asyncio.Event()

    async def _prepare(self, source: Path | None, output: Path) -> bool:
        """Prepare `source` (None for the test pattern) into `output`, once however many
        cameras loop it; whether it is there now."""
        if output not in self._prepared:
            self._prepared[output] = asyncio.create_task(self._prepare_once(source, output))
        return await asyncio.shield(self._prepared[output])

    async def _prepare_once(self, source: Path | None, output: Path) -> bool:
        label = "the test pattern" if source is None else source.name
        settings = self._settings
        attempt = 0
        while not output.exists():
            attempt += 1
            async with self._preparing:
                _LOG.info("preparing %s, once (it is cached)", label)
                started = time.monotonic()
                part = output.with_name(f"{output.name}.part")
                command = prepare_command(source, part, settings.ffmpeg, settings.duration)
                ended = await self._run(label, command)
                if ended.status == 0:
                    part.replace(output)
                    _LOG.info("prepared %s in %.0f s", label, time.monotonic() - started)
                    return True
                part.unlink(missing_ok=True)
            if attempt >= settings.prepare_attempts:
                _LOG.error(
                    "preparing %s failed %d times, the last with ffmpeg %s; giving up, so the "
                    "cameras that loop it keep the test pattern until the publisher restarts",
                    label,
                    attempt,
                    ended,
                )
                return False
            _LOG.warning(
                "preparing %s failed: ffmpeg %s; trying again in %g s (attempt %d of %d)",
                label,
                ended,
                settings.prepare_retry,
                attempt + 1,
                settings.prepare_attempts,
            )
            await self._sleep(settings.prepare_retry)
        return True

    async def _run(self, label: str, command: list[str], hung_after: float | None = None) -> Ended:
        """Run ffmpeg to the end; how it ended, with what it wrote to stderr. `label` names
        the camera whose progress it reports. With `hung_after`, an ffmpeg that reports no
        frame for that many seconds is killed."""
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except OSError as error:
            return Ended(None, [str(error)])
        assert process.stdout is not None
        assert process.stderr is not None
        errors: list[str] = []
        hung = asyncio.Event()
        watchdog = (
            asyncio.create_task(self._watch(label, process, hung_after, hung))
            if hung_after is not None
            else None
        )
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._read_progress(label, process.stdout))
                group.create_task(_collect(process.stderr, errors))
            return Ended(await process.wait(), errors, hung.is_set())
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), 3)
                except TimeoutError:
                    process.kill()

    async def _watch(
        self,
        label: str,
        process: asyncio.subprocess.Process,
        hung_after: float,
        hung: asyncio.Event,
    ) -> None:
        """Kill the ffmpeg once it has sent no new frame for `hung_after` seconds, as when a
        write to a MediaMTX that stopped reading blocks and never fails. Timed by the monotonic
        clock, so that a step of the wall clock, or a suspended host, does not look like that."""
        started = self._clock()
        while process.returncode is None:
            await self._sleep(WATCH_EVERY)
            if self._clock() - self._progress.get(label, started) > hung_after:
                hung.set()
                process.kill()
                return

    async def _read_progress(self, camera: str, stdout: asyncio.StreamReader) -> None:
        """`-progress pipe:1` reports `frame=N` then `progress=continue` every second, and goes
        on reporting the same N while a write blocks (ffmpeg 7 and later report from a thread of
        their own): only a count that moved is progress."""
        frame = sent = 0
        async for raw in stdout:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key == "frame" and value.isdigit():
                frame = int(value)
            elif key == "progress" and frame > sent:
                sent = frame
                self._progress[camera] = self._clock()

    def _save_status(self) -> None:
        """Tell `health` when each camera last sent a frame, in Unix time."""
        status = self._settings.status
        part = status.with_name(f"{status.name}.part")
        ago = self._clock() - self._wall()
        part.write_text(json.dumps({name: at - ago for name, at in self._progress.items()}))
        part.replace(status)

    async def _write_status(self) -> NoReturn:
        while True:
            self._save_status()
            await asyncio.sleep(1)


def _health(settings: Settings) -> int:
    try:
        progress: dict[str, float] = json.loads(settings.status.read_text())
    except OSError, ValueError:
        progress = {}
    silent = not_publishing(settings.cameras, progress, time.time(), settings.stale_after)
    if silent:
        print(f"not publishing: {', '.join(silent)}")
        return 1
    return 0


async def _serve(settings: Settings) -> None:
    run = asyncio.create_task(Publisher(settings).run())
    loop = asyncio.get_running_loop()
    for stop in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(stop, run.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await run
    _LOG.info("stopped")


def main(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    command = argv[0] if argv else "run"
    if command not in {"run", "health"}:
        _LOG.error("usage: fake_cameras.py [run | health]")
        return 2
    try:
        settings = Settings.from_environment(os.environ)
    except ValueError as error:
        _LOG.error("%s", error)
        return 2
    if command == "health":
        return _health(settings)
    asyncio.run(_serve(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
