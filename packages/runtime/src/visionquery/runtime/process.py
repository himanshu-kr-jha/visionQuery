"""Starting a process the way every VisionQuery process starts."""

import logging

from visionquery.runtime.logs import configure_logging
from visionquery.runtime.metrics import serve_metrics
from visionquery.runtime.settings import Settings, SettingsError

__all__ = ["start"]

_log = logging.getLogger("visionquery.runtime")

#: The exit status of a process whose settings are missing or invalid (as for a usage error).
BAD_SETTINGS = 2


def start[S: Settings](service: str, settings: type[S]) -> S:
    """Read `settings` from the environment, log as `service`, and serve `/metrics`.

    The first thing a service's entry point calls. Missing or invalid variables are logged
    by name and end the process with status 2; the metrics server runs for the life of the
    process.
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
    return loaded
