"""`visionquery-recorder`: record every camera in the registry into Footage, until stopped.

    visionquery-recorder            run until stopped (SIGTERM or SIGINT)
    visionquery-recorder health     exit 0 while the camera registry was read recently, 1 otherwise

It reads each camera's video from MediaMTX over RTSP, as the `visionquery` user (ADR 0015), at
the path the registry gives it, and copies it into Segments under `VISIONQUERY_FOOTAGE_DIR`, one
directory per camera id (`visionquery.recorder.fleet`). Footage is Visual Data and never leaves
the Deployment (ADR 0002): the recorder writes it to its own disk and sends it nowhere.

When it starts, before any camera is recorded, it finishes every Segment a crash left unfinished
(`segment.recover`), whichever camera it was, and holds a lock on the Footage directory for as
long as it runs, so that no second recorder writes to it. Then, every `VISIONQUERY_REFRESH_INTERVAL`
seconds (5), it reads the cameras not removed and starts and stops recording to match. A
registry that cannot be read is logged once, as a WARNING, for as long as it cannot (an INFO
says when it can again), and the cameras already being recorded go on being recorded.

After each successful read it writes a heartbeat file holding the time until which it counts as
healthy, three reads on, which `health` (Compose's healthcheck) reads.

On SIGTERM or SIGINT every camera finishes the Segment it is writing (`stopped`), and the
process exits 0.
"""

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator
from visionquery.memory.cameras import Camera, CameraRegistry, CameraRegistryError
from visionquery.memory.connection import Connection, ConnectionFailedError, connect
from visionquery.memory.settings import DatabaseSettings
from visionquery.recorder import metrics
from visionquery.recorder.camera import Timing
from visionquery.recorder.ffmpeg import Source
from visionquery.recorder.fleet import Fleet
from visionquery.recorder.segment import recover
from visionquery.runtime import Settings, start

__all__ = [
    "HEARTBEAT",
    "AlreadyRecordingError",
    "MediaMTXReader",
    "RecorderSettings",
    "health",
    "main",
    "run",
    "source_for",
]

_log = logging.getLogger(__name__)

#: Where the heartbeat is written; /tmp is a tmpfs in the container.
HEARTBEAT = Path("/tmp/visionquery-recorder-healthy")
#: How many refreshes a success counts as healthy for, and the grace on top for a slow one.
_HEALTHY_REFRESHES = 3
_HEALTHY_GRACE = 2.0
#: Bounds on talking to the database, so that one that stops answering cannot hold a refresh.
_CONNECT_TIMEOUT = 5
_STATEMENT_TIMEOUT_MS = 5000
#: The file in the Footage directory a running recorder holds a lock on.
_LOCK = ".lock"
_DAY = 24 * 60 * 60


class AlreadyRecordingError(Exception):
    """Another recorder holds the Footage directory."""


class MediaMTXReader(BaseModel):
    """Where MediaMTX serves cameras over RTSP, and who reads them (`VISIONQUERY_MEDIAMTX__*`)."""

    #: An `rtsp://` or `rtsps://` URL to MediaMTX itself, without credentials (they go in `user`
    #: and `password`) or a path (each camera's is the registry's).
    rtsp_url: str = "rtsp://127.0.0.1:8554"
    user: str = "visionquery"
    password: SecretStr

    @field_validator("rtsp_url")
    @classmethod
    def _is_mediamtx(cls, url: str) -> str:
        # Never quoted: a URL given here by mistake may hold a password.
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - raises for a port that is not a number in range
        except ValueError:
            raise ValueError("must be an rtsp:// or rtsps:// URL, and this one is not") from None
        if parts.scheme not in {"rtsp", "rtsps"} or not parts.hostname:
            raise ValueError("must be an rtsp:// or rtsps:// URL to MediaMTX")
        if parts.username is not None or parts.password is not None:
            raise ValueError(
                "must be an rtsp:// URL without credentials: set VISIONQUERY_MEDIAMTX__USER "
                "and VISIONQUERY_MEDIAMTX__PASSWORD"
            )
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ValueError("must be an rtsp:// URL to MediaMTX itself, without a path")
        return url


class RecorderSettings(Settings):
    """What `visionquery-recorder` reads from the environment."""

    database: DatabaseSettings
    mediamtx: MediaMTXReader
    #: Where Footage is written: one directory per camera id.
    footage_dir: Path
    #: The length of every Segment's window, aligned to the epoch. It divides a day, so that no
    #: Segment straddles midnight and a day of a camera's Footage is one directory.
    segment_seconds: Annotated[int, Field(gt=0, le=3600)] = 300
    #: Seconds between reads of the camera registry: the most a camera added or removed waits.
    refresh_interval: Annotated[float, Field(gt=0, le=3600)] = 5.0

    @field_validator("segment_seconds")
    @classmethod
    def _divides_a_day(cls, seconds: int) -> int:
        if _DAY % seconds:
            raise ValueError("must divide a day (86400 s) evenly, as 60, 300 or 600 do")
        return seconds


def source_for(reader: MediaMTXReader) -> Callable[[Camera], Source]:
    """Where each camera is read from: its path in MediaMTX, as `reader.user`."""
    parts = urlsplit(reader.rtsp_url)
    user = quote(reader.user, safe="")
    password = quote(reader.password.get_secret_value(), safe="")

    def source(camera: Camera) -> Source:
        url = f"{parts.scheme}://{user}:{password}@{parts.netloc}/{quote(camera.path, safe='/')}"
        return Source(url=SecretStr(url))

    return source


@contextmanager
def _held(footage: Path) -> Generator[None]:
    """Hold the Footage directory's lock while the block runs, or refuse at once. The kernel
    releases it when the process dies, however it dies, so a crash never locks a recorder out."""
    footage.mkdir(parents=True, exist_ok=True)
    fd = os.open(footage / _LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRecordingError(
                f"another recorder is already recording into {footage}; one Footage directory "
                "has one recorder"
            ) from None
        yield
    finally:
        os.close(fd)


async def run(
    settings: RecorderSettings, stop: asyncio.Event, *, heartbeat: Path = HEARTBEAT
) -> None:
    """Record every camera in the registry until `stop` is set."""
    footage = settings.footage_dir
    with _held(footage):
        for segment in await asyncio.to_thread(recover, footage):
            camera_id = segment.path.parent.parent.name
            metrics.SEGMENTS.labels(camera_id, segment.reason).inc()
            _log.info(
                "camera %s: Segment %s, left unfinished, recovered, %s to %s",
                camera_id,
                segment.path.name,
                segment.time_from.isoformat(),
                segment.time_to.isoformat(),
                extra={
                    "camera_id": camera_id,
                    "segment": str(segment.path),
                    "fragments": segment.fragments,
                },
            )
        timing = Timing(segment=timedelta(seconds=settings.segment_seconds))
        fleet = Fleet(footage, source_for(settings.mediamtx), timing)
        registry = _Registry(settings.database)
        try:
            while not stop.is_set():
                began = time.monotonic()
                cameras = await asyncio.to_thread(registry.read)
                if cameras is not None:
                    fleet.reconcile(cameras)
                    metrics.CAMERAS.set(len(fleet.recording))
                    healthy_for = _HEALTHY_REFRESHES * settings.refresh_interval + _HEALTHY_GRACE
                    heartbeat.write_text(f"{time.time() + healthy_for}\n")
                wait = max(0.0, settings.refresh_interval - (time.monotonic() - began))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=wait)
        finally:
            await fleet.stop()
            registry.close()


class _Registry:
    """The cameras not removed, read on a connection kept between reads."""

    def __init__(self, database: DatabaseSettings) -> None:
        self._database = database
        self._connection: Connection | None = None
        #: What the last read failed on; None after a success, and before the first read.
        self._failing: str | None = None

    def read(self) -> Sequence[Camera] | None:
        """The cameras not removed, or None when the registry could not be read."""
        problem: str
        try:
            cameras = CameraRegistry(self._connected()).list()
        except ConnectionFailedError as error:
            failing = "database"
            problem = f"the database could not be reached: {error.category} ({error.cause})"
        except CameraRegistryError as error:
            self.close()
            failing, problem = "registry", str(error)
        else:
            if self._failing is not None:
                _log.info("reading the camera registry again")
                self._failing = None
            return cameras
        if failing != self._failing:
            _log.warning(
                "the camera registry could not be read, and is read again every refresh; the "
                "cameras being recorded go on being recorded: %s",
                problem,
            )
        self._failing = failing
        return None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connected(self) -> Connection:
        if self._connection is None or self._connection.closed:
            self._connection = connect(
                self._database.url.get_secret_value(),
                _CONNECT_TIMEOUT,
                statement_timeout_ms=_STATEMENT_TIMEOUT_MS,
            )
        return self._connection


def health(heartbeat: Path = HEARTBEAT) -> int:
    """0 while the heartbeat says the registry was read recently, 1 otherwise."""
    try:
        healthy_until = float(heartbeat.read_text())
    except OSError, ValueError:
        return 1
    return 0 if time.time() < healthy_until else 1


async def _serve(settings: RecorderSettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await run(settings, stop)


def main(argv: Sequence[str] | None = None) -> None:
    """The `visionquery-recorder` command."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["health"]:
        raise SystemExit(health(HEARTBEAT))
    if arguments:
        raise SystemExit("usage: visionquery-recorder [health]")
    settings = start("recorder", RecorderSettings).settings
    try:
        asyncio.run(_serve(settings))
    except AlreadyRecordingError as error:
        _log.critical("%s", error)
        raise SystemExit(1) from None
    _log.info("stopped")
