#!/usr/bin/env python3
"""Fake cameras for development: loops videos into MediaMTX over RTSP, one path per camera.

The `dev` profile's only service (compose.yaml). Each camera is one ffmpeg that publishes
to `<FAKE_CAMERAS_RTSP>/<camera>` and is restarted whenever it exits, which is how a camera
comes back after MediaMTX restarts. It is a development tool, never part of a Deployment.

    FAKE_CAMERAS="gate=meva-school-g299.avi,lobby=synthetic" fake_cameras.py [run | health]

FAKE_CAMERAS lists the cameras as `name=source`, comma-separated: the name is the camera's
MediaMTX path, and the source is `synthetic` (a test pattern, encoded live) or a video's
path relative to the samples directory. Each video, and the test pattern, is first prepared
once into the cache as a camera would send it (H.264, no B-frames, a keyframe every second, no
audio) and then looped in real time without transcoding, so a camera costs next to no CPU; a
video is prepared again only when its content changes, and shows the test pattern meanwhile.
A video that is not in the samples directory is replaced by the test pattern, with a warning,
until the publisher restarts: `make up` then works before `make fetch-samples`.

`health` exits 0 when every camera's ffmpeg has reported a frame in the last few seconds.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

SYNTHETIC = "synthetic"
#: The test pattern: what a modest camera sends.
SYNTHETIC_SIZE = "1280x720"
SYNTHETIC_RATE = 15
#: How long the test pattern runs before it loops.
SYNTHETIC_SECONDS = 60
#: A camera whose ffmpeg last reported a frame longer ago than this is not publishing.
STALE_AFTER = 5.0
#: Changes whenever prepare_command does, so that clips prepared the old way are redone.
PREPARATION = "1"
#: Seconds before restarting an ffmpeg that exited: doubled after each failure, up to the last.
BACKOFF = (1, 2, 4, 8)
#: A camera that published for this long is back to the shortest backoff when it exits.
PUBLISHED_LONG_ENOUGH = 30.0

_NAME = re.compile(r"[A-Za-z0-9_-]+")
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


def cache_key(source: Path | None) -> str:
    """Names the prepared copy of `source` (None for the test pattern): its content, and how
    it was prepared."""
    digest = hashlib.sha256(PREPARATION.encode())
    if source is None:
        digest.update(SYNTHETIC.encode())
    else:
        with source.open("rb") as file:
            digest.update(hashlib.file_digest(file, "sha256").digest())
    return digest.hexdigest()[:32]


def _quiet(ffmpeg: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]


def publish_command(source: Path, url: str, ffmpeg: str = "ffmpeg") -> list[str]:
    """Loop a prepared clip into MediaMTX in real time, as it is, reporting progress."""
    return [
        *_quiet(ffmpeg),
        "-progress", "pipe:1", "-stats_period", "1",
        "-re", "-stream_loop", "-1", "-i", str(source),
        "-map", "0:v:0", "-c", "copy", "-an",
        # RTP packets that fit MediaMTX's limit, which would otherwise split every one again.
        "-pkt_size", "1400",
        "-rtsp_transport", "tcp", "-f", "rtsp", url,
    ]  # fmt: skip


def prepare_command(source: Path | None, output: Path, ffmpeg: str = "ffmpeg") -> list[str]:
    """Encode a clip (None for the test pattern) as a camera sends video, so that looping it
    needs no transcoding."""
    if source is None:
        pattern = (
            f"testsrc2=duration={SYNTHETIC_SECONDS}:size={SYNTHETIC_SIZE}:rate={SYNTHETIC_RATE}"
        )
        given = ["-f", "lavfi", "-i", pattern]
    else:
        given = ["-i", str(source), "-map", "0:v:0"]
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


def not_publishing(
    cameras: Sequence[Camera], progress: Mapping[str, float], now: float
) -> list[str]:
    """The cameras whose ffmpeg has not reported a frame recently."""
    return [
        c.name
        for c in cameras
        if (last := progress.get(c.name)) is None or now - last > STALE_AFTER
    ]


@dataclass(frozen=True)
class Settings:
    cameras: list[Camera]
    rtsp: str
    ffmpeg: str
    samples: Path
    cache: Path
    status: Path

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> Settings:
        try:
            cameras = parse_cameras(env.get("FAKE_CAMERAS", ""))
        except ValueError as error:
            raise ValueError(f"FAKE_CAMERAS: {error}") from error
        return cls(
            cameras=cameras,
            rtsp=env.get("FAKE_CAMERAS_RTSP", "rtsp://mediamtx:8554").rstrip("/"),
            ffmpeg=env.get("FAKE_CAMERAS_FFMPEG", "ffmpeg"),
            samples=Path(env.get("FAKE_CAMERAS_SAMPLES", "/samples")),
            cache=Path(env.get("FAKE_CAMERAS_CACHE", "/cache")),
            status=Path(env.get("FAKE_CAMERAS_STATUS", "/tmp/fake-cameras.json")),
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

    def __str__(self) -> str:
        said = "could not start" if self.status is None else f"exited with {self.status}"
        if not self.errors:
            return said
        first = _COMPONENT.sub("", self.errors[0])
        more = len(self.errors) - 1
        if more:
            first += f", and {more} more line{'s' if more > 1 else ''}"
        return f"{said} ({first})"


async def _collect(stderr: asyncio.StreamReader, into: list[str]) -> None:
    async for raw in stderr:
        if line := raw.decode(errors="replace").strip():
            into.append(line)


class Publisher:
    """Runs and restarts one ffmpeg per camera, and records when each last sent a frame."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pattern = settings.cache / f"{cache_key(None)}.mp4"
        self._progress: dict[str, float] = {}
        self._prepared: dict[Path, asyncio.Task[None]] = {}
        # One preparation at a time: each uses every core, and the first camera is up sooner.
        self._preparing = asyncio.Semaphore(1)

    async def run(self) -> None:
        # Nothing is publishing yet, whatever an earlier run left behind.
        self._save_status()
        await self._prune_cache()
        await self._prepare(None, self._pattern)
        async with asyncio.TaskGroup() as group:
            group.create_task(self._write_status())
            for camera in self._settings.cameras:
                group.create_task(self._camera(camera))

    def _video(self, camera: Camera) -> Path | None:
        """The camera's video, or None for the test pattern."""
        if camera.source == SYNTHETIC:
            return None
        path = self._settings.samples / camera.source
        return path if path.is_file() else None

    async def _prune_cache(self) -> None:
        """Delete prepared clips no camera uses now, and preparations a kill interrupted."""
        videos = {v for c in self._settings.cameras if (v := self._video(c)) is not None}
        keys = {await asyncio.to_thread(cache_key, v) for v in videos} | {self._pattern.stem}
        for path in self._settings.cache.iterdir():
            if path.name != f"{path.stem}.mp4" or path.stem not in keys:
                path.unlink()

    async def _camera(self, camera: Camera) -> NoReturn:
        url = f"{self._settings.rtsp}/{camera.name}"
        video = self._video(camera)
        if video is None:
            if camera.source != SYNTHETIC:
                _LOG.warning(
                    "%s: %s is not in the samples directory, so it gets the test pattern "
                    "instead; run `make fetch-samples` (or add the video), then restart "
                    "fake-cameras",
                    camera.name,
                    camera.source,
                )
            await self._keep_publishing(camera, self._pattern, url, "the test pattern")
        prepared = self._settings.cache / f"{await asyncio.to_thread(cache_key, video)}.mp4"
        if not prepared.exists():
            # Preparing takes a while, and the camera is up meanwhile.
            _LOG.info("%s: the test pattern until %s is prepared", camera.name, camera.source)
            meanwhile = asyncio.create_task(
                self._keep_publishing(camera, self._pattern, url, "the test pattern")
            )
            try:
                await self._prepare(video, prepared)
            finally:
                meanwhile.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await meanwhile
        await self._keep_publishing(camera, prepared, url, camera.source)

    async def _keep_publishing(
        self, camera: Camera, source: Path, url: str, label: str
    ) -> NoReturn:
        """Loop `source` (`label` in the log) into MediaMTX, restarting ffmpeg whenever it
        exits."""
        _LOG.info("%s: publishing %s to %s", camera.name, label, url)
        command = publish_command(source, url, self._settings.ffmpeg)
        failures = 0
        while True:
            started = time.monotonic()
            ended = await self._run(camera.name, command)
            self._progress.pop(camera.name, None)
            failures = 0 if time.monotonic() - started > PUBLISHED_LONG_ENOUGH else failures + 1
            delay = BACKOFF[min(max(failures - 1, 0), len(BACKOFF) - 1)]
            _LOG.warning("%s: ffmpeg %s; restarting in %d s", camera.name, ended, delay)
            await asyncio.sleep(delay)

    async def _prepare(self, source: Path | None, output: Path) -> None:
        """Prepare `source` (None for the test pattern) into `output`, once however many
        cameras loop it."""
        if output not in self._prepared:
            self._prepared[output] = asyncio.create_task(self._prepare_once(source, output))
        await asyncio.shield(self._prepared[output])

    async def _prepare_once(self, source: Path | None, output: Path) -> None:
        label = "the test pattern" if source is None else source.name
        while not output.exists():
            async with self._preparing:
                _LOG.info("preparing %s, once (it is cached)", label)
                started = time.monotonic()
                part = output.with_name(f"{output.name}.part")
                ended = await self._run(label, prepare_command(source, part, self._settings.ffmpeg))
                if ended.status == 0:
                    part.replace(output)
                    _LOG.info("prepared %s in %.0f s", label, time.monotonic() - started)
                    return
                part.unlink(missing_ok=True)
            _LOG.error("preparing %s failed: ffmpeg %s; trying again in 30 s", label, ended)
            await asyncio.sleep(30)

    async def _run(self, label: str, command: list[str]) -> Ended:
        """Run ffmpeg to the end; how it ended, with what it wrote to stderr. `label` names
        the camera whose progress it reports."""
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except OSError as error:
            return Ended(None, [str(error)])
        assert process.stdout is not None
        assert process.stderr is not None
        errors: list[str] = []
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._read_progress(label, process.stdout))
                group.create_task(_collect(process.stderr, errors))
            return Ended(await process.wait(), errors)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), 3)
                except TimeoutError:
                    process.kill()

    async def _read_progress(self, camera: str, stdout: asyncio.StreamReader) -> None:
        """`-progress pipe:1` reports `frame=N` then `progress=continue` every second."""
        frame = 0
        async for raw in stdout:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key == "frame" and value.isdigit():
                frame = int(value)
            elif key == "progress" and frame > 0:
                self._progress[camera] = time.time()

    def _save_status(self) -> None:
        """Tell `health` when each camera last sent a frame."""
        status = self._settings.status
        part = status.with_name(f"{status.name}.part")
        part.write_text(json.dumps(self._progress))
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
    silent = not_publishing(settings.cameras, progress, time.time())
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
