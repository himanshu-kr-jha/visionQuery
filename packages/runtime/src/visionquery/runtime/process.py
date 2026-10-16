"""Starting a process the way every VisionQuery process starts."""

import logging
from dataclasses import dataclass

from visionquery.runtime.logs import configure_logging
from visionquery.runtime.metrics import MetricsServer, serve_metrics
from visionquery.runtime.settings import Settings, SettingsError

__all__ = ["Process", "start"]

_log = logging.getLogger("visionquery.runtime")

#: The exit status of a process whose settings are missing or invalid (as for a usage error).
BAD_SETTINGS = 2


@dataclass(frozen=True)
class Process[S: Settings]:
    """What `start` set up: the loaded settings and the running metrics server."""

    settings: S
    metrics: MetricsServer

    @property
    def metrics_port(self) -> int:
        """The port `/metrics` is served on, which is the chosen one when 0 was asked for."""
        return self.metrics.port

    def close(self) -> None:
        """Stop serving `/metrics` and release its port; a service rarely needs to."""
        self.metrics.close()


def start[S: Settings](service: str, settings: type[S]) -> Process[S]:
    """Read `settings` from the environment, log as `service`, and serve `/metrics`.

    The first thing a service's entry point calls. Missing or invalid variables are logged
    by name and end the process with status 2. The metrics server runs for the life of the
    process unless the returned `Process` is closed.
    """
    # Logging comes first, so that even bad settings are reported as a scrubbed JSON line.
    configure_logging(service, "INFO")
    try:
        loaded = settings.load()
    except SettingsError as error:
        _log.critical("%s", error, extra={"variables": list(error.variables)})
        raise SystemExit(BAD_SETTINGS) from None
    configure_logging(service, loaded.log_level)
    metrics = serve_metrics(loaded.metrics_host, loaded.metrics_port)
    _log.info("started", extra={"metrics_port": metrics.port})
    return Process(loaded, metrics)
