"""`visionquery-reconciler`: reconcile MediaMTX with the camera registry every cycle (ADR 0014).

    visionquery-reconciler            run until stopped (SIGTERM or SIGINT)
    visionquery-reconciler health     exit 0 while a recent cycle succeeded, 1 otherwise
    visionquery-reconciler status     log whether each camera's video is arriving in MediaMTX;
                                      exit 0 when every camera's is, 1 otherwise

Every `VISIONQUERY_RECONCILE_INTERVAL` seconds (5) it reads the cameras not removed and makes
MediaMTX match them (`visionquery.reconciler.reconcile`), so a change to the registry, and a
restarted MediaMTX, which forgets everything its API was told, are both put right within one
cycle. A cycle that fails (the database or MediaMTX unreachable, or MediaMTX holding a user it
must not overwrite) is logged once, as a WARNING, while the failure stays the same, and the next
cycle tries again; an INFO says when a cycle succeeds again. Nothing logged names a credential:
a database failure is named by its category only, and MediaMTX's errors are scrubbed.

After each successful cycle it writes a heartbeat file holding the time until which it counts
as healthy, three cycles on, which `health` (Compose's healthcheck) reads.

`status` changes nothing: it logs one line per camera not removed, saying whether MediaMTX has
its path ready with video arriving (INFO) or not (WARNING), and a `status: N cameras, M
receiving video` line. It runs beside the reconciler (`docker compose exec reconciler
visionquery-reconciler status`), so it serves its metrics on any free port.
"""

import logging
import signal
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Annotated
from urllib.parse import urlsplit

from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field, SecretStr, field_validator
from visionquery.memory.cameras import CameraRegistry, CameraRegistryError
from visionquery.memory.connection import Connection, ConnectionFailedError, connect
from visionquery.memory.settings import DatabaseSettings
from visionquery.reconciler.mediamtx import MediaMTXClient, MediaMTXError
from visionquery.reconciler.reconcile import Changes, ReconcileError, Reconciler
from visionquery.runtime import Settings, start

__all__ = [
    "HEARTBEAT",
    "MediaMTXSettings",
    "ReconcilerSettings",
    "health",
    "main",
    "run",
    "status",
]

_log = logging.getLogger(__name__)

#: Where the heartbeat is written; /tmp is a tmpfs in the container.
HEARTBEAT = Path("/tmp/visionquery-reconciler-healthy")
#: How many cycles a success counts as healthy for, and the grace on top for a slow cycle.
_HEALTHY_CYCLES = 3
_HEALTHY_GRACE = 2.0
#: Bounds on talking to the database, so that one that stops answering cannot hold a cycle.
_CONNECT_TIMEOUT = 5
_STATEMENT_TIMEOUT_MS = 5000

_CYCLES = Counter(
    "visionquery_reconciler_cycles",
    "Reconcile cycles run, by outcome (ok or failed).",
    ["outcome"],
)
_CHANGES = Counter(
    "visionquery_reconciler_changes",
    "Changes made to MediaMTX: paths added, updated or removed, and publishers written.",
    ["change"],
)
_LAST_SUCCESS = Gauge(
    "visionquery_reconciler_last_success_timestamp_seconds",
    "When a reconcile cycle last succeeded, as a Unix time.",
)
_CAMERAS = Gauge(
    "visionquery_reconciler_cameras", "Cameras not removed, as of the last successful cycle."
)


class MediaMTXSettings(BaseModel):
    """Where MediaMTX's control API is, and who drives it (`VISIONQUERY_MEDIAMTX__*`)."""

    #: An `http://` or `https://` URL, without credentials, which go in `user` and `password`.
    url: str = "http://127.0.0.1:9997"
    user: str = "visionquery"
    password: SecretStr

    @field_validator("url")
    @classmethod
    def _is_an_http_url(cls, url: str) -> str:
        # Never quoted: a URL given here by mistake may hold a password.
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - raises for a port that is not a number in range
        except ValueError:
            raise ValueError("must be an http:// or https:// URL, and this one is not") from None
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("must be an http:// or https:// URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError(
                "must not hold credentials: set VISIONQUERY_MEDIAMTX__USER and "
                "VISIONQUERY_MEDIAMTX__PASSWORD"
            )
        return url


class ReconcilerSettings(Settings):
    """What `visionquery-reconciler` reads from the environment."""

    database: DatabaseSettings
    mediamtx: MediaMTXSettings
    #: Seconds between the starts of two cycles: the most a change waits to reach MediaMTX.
    reconcile_interval: Annotated[float, Field(gt=0, le=3600)] = 5.0


def run(
    settings: ReconcilerSettings,
    *,
    stop: threading.Event | None = None,
    heartbeat: Path = HEARTBEAT,
) -> None:
    """Reconcile every cycle until `stop` is set."""
    stop = stop or threading.Event()
    loop = _Loop(settings, heartbeat)
    try:
        while not stop.is_set():
            began = time.monotonic()
            loop.cycle()
            stop.wait(max(0.0, settings.reconcile_interval - (time.monotonic() - began)))
    finally:
        loop.close()


class _Loop:
    def __init__(self, settings: ReconcilerSettings, heartbeat: Path) -> None:
        self._settings = settings
        self._heartbeat = heartbeat
        mediamtx = settings.mediamtx
        self._reconciler = Reconciler(
            MediaMTXClient(mediamtx.url, mediamtx.user, mediamtx.password)
        )
        self._connection: Connection | None = None
        #: What the last cycle failed with; None after a success, and before the first cycle.
        self._failing: str | None = None

    def cycle(self) -> None:
        problem: str
        try:
            cameras = CameraRegistry(self._connected()).list()
            changes = self._reconciler.reconcile(cameras)
        except ConnectionFailedError as error:
            problem = f"the database could not be reached: {error.category} ({error.cause})"
        except CameraRegistryError as error:
            self.close()
            problem = f"the camera registry could not be read: {error}"
        except (MediaMTXError, ReconcileError) as error:
            problem = str(error)
        else:
            self._succeeded(len(cameras), changes)
            return
        _CYCLES.labels("failed").inc()
        if problem != self._failing:
            _log.warning("reconciling failed, and is retried every cycle: %s", problem)
        self._failing = problem

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connected(self) -> Connection:
        if self._connection is None or self._connection.closed:
            self._connection = connect(
                self._settings.database.url.get_secret_value(),
                _CONNECT_TIMEOUT,
                statement_timeout_ms=_STATEMENT_TIMEOUT_MS,
            )
        return self._connection

    def _succeeded(self, cameras: int, changes: Changes) -> None:
        _CYCLES.labels("ok").inc()
        _CAMERAS.set(cameras)
        _LAST_SUCCESS.set_to_current_time()
        if self._failing is not None:
            _log.info("reconciling again: MediaMTX matches the camera registry")
            self._failing = None
        if changes:
            for change, paths in (
                ("added", changes.added),
                ("updated", changes.updated),
                ("removed", changes.removed),
            ):
                _CHANGES.labels(change).inc(len(paths))
            _CHANGES.labels("publishers").inc(int(changes.publishers))
            _log.info(
                "reconciled: %d paths added, %d updated, %d removed%s",
                len(changes.added),
                len(changes.updated),
                len(changes.removed),
                ", publishers written" if changes.publishers else "",
                extra={
                    "paths_added": list(changes.added),
                    "paths_updated": list(changes.updated),
                    "paths_removed": list(changes.removed),
                    "publishers_written": changes.publishers,
                },
            )
        healthy_for = _HEALTHY_CYCLES * self._settings.reconcile_interval + _HEALTHY_GRACE
        self._heartbeat.write_text(f"{time.time() + healthy_for}\n")


def health(heartbeat: Path = HEARTBEAT) -> int:
    """0 while the heartbeat says a recent cycle succeeded, 1 otherwise."""
    try:
        healthy_until = float(heartbeat.read_text())
    except OSError, ValueError:
        return 1
    return 0 if time.time() < healthy_until else 1


class _StatusSettings(ReconcilerSettings):
    #: `status` runs beside the reconciler, which holds the metrics port: any free one.
    metrics_port: int = 0


def status(settings: ReconcilerSettings) -> int:
    """Log whether each camera's video is arriving in MediaMTX; 0 when every camera's is."""
    mediamtx = settings.mediamtx
    client = MediaMTXClient(mediamtx.url, mediamtx.user, mediamtx.password)
    problem: str
    try:
        with connect(settings.database.url.get_secret_value(), _CONNECT_TIMEOUT) as connection:
            cameras = CameraRegistry(connection).list()
        paths = client.paths()
    except ConnectionFailedError as error:
        problem = f"the database could not be reached: {error.category} ({error.cause})"
    except (CameraRegistryError, MediaMTXError) as error:
        problem = str(error)
    else:
        receiving = 0
        for camera in cameras:
            path = paths.get(camera.path) or {}
            arriving = bool(path.get("ready")) and int(path.get("inboundBytes") or 0) > 0
            receiving += arriving
            _log.log(
                logging.INFO if arriving else logging.WARNING,
                "%s (%s): %s",
                camera.name,
                camera.path,
                "receiving video" if arriving else "no video arriving",
                extra={"camera": camera.name, "path": camera.path, "receiving": arriving},
            )
        _log.info("status: %d cameras, %d receiving video", len(cameras), receiving)
        return 0 if receiving == len(cameras) else 1
    _log.error("status: %s", problem)
    return 1


def main(argv: Sequence[str] | None = None) -> None:
    """The `visionquery-reconciler` command."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["health"]:
        raise SystemExit(health(HEARTBEAT))
    if arguments == ["status"]:
        process = start("reconciler", _StatusSettings)
        try:
            raise SystemExit(status(process.settings))
        finally:
            process.close()
    if arguments:
        raise SystemExit("usage: visionquery-reconciler [health | status]")
    settings = start("reconciler", ReconcilerSettings).settings
    stop = threading.Event()

    def stopping(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    run(settings, stop=stop, heartbeat=HEARTBEAT)
    _log.info("stopped")
