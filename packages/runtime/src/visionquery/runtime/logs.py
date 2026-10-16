"""Logs as one JSON object per line on stdout, scrubbed of credentials before anything sees them.

Everything goes through the standard library's logging, so third-party libraries are
covered too. Scrubbing happens in two places, so no single forgotten call can leak a camera
password: every record is scrubbed as it is made (its message, exception and stack), which
protects handlers a library installs for itself, and the JSON formatter scrubs every field
it writes, which covers structured fields.
"""

import json
import logging
import math
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, TextIO, cast

from visionquery.runtime.scrub import scrub
from visionquery.runtime.settings import LogLevel

__all__ = ["configure_logging"]

_log = logging.getLogger("visionquery.runtime")

#: The attributes every record has; anything else on a record came from `extra=`.
_STANDARD_ATTRIBUTES = frozenset(
    [*logging.LogRecord("", 0, "", 0, "", None, None).__dict__, "message", "asctime"]
)

_make_record = logging.getLogRecordFactory()
_exceptions = logging.Formatter()


def configure_logging(service: str, level: LogLevel, *, stream: TextIO | None = None) -> None:
    """Send every log record of this process to `stream` (stdout) as scrubbed JSON lines.

    Replaces whatever handlers the root logger had, and also routes warnings and uncaught
    exceptions, in the main thread or any other, through the same path.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.setLogRecordFactory(_scrubbed_record)
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_uncaught_in_thread


class JsonFormatter(logging.Formatter):
    """One JSON object per record: time, level, service, logger, message, then any extra fields."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line["exception"] = record.exc_text
        if record.stack_info:
            line["stack"] = record.stack_info
        for key, value in record.__dict__.items():
            # A structured field never hides one of the fields above.
            if key not in _STANDARD_ATTRIBUTES:
                line.setdefault(key, value)
        return json.dumps(_scrubbed(line), ensure_ascii=False)


def _scrubbed(value: object) -> object:
    """`value` as JSON-ready data, every string in it (keys included) scrubbed."""
    match value:
        case str():
            return scrub(value)
        case bool() | int() | None:
            return value
        case float():
            return value if math.isfinite(value) else str(value)
        case Mapping():
            items = cast(Mapping[object, object], value).items()
            return {scrub(str(key)): _scrubbed(item) for key, item in items}
        case list() | tuple() | set() | frozenset():
            return [_scrubbed(item) for item in cast(list[object], value)]
        case _:
            return scrub(str(value))


def _scrubbed_record(*args: Any, **kwargs: Any) -> logging.LogRecord:
    """A record whose message, exception and stack are already scrubbed and rendered."""
    record = _make_record(*args, **kwargs)
    try:
        message = record.getMessage()
    except Exception:  # arguments that do not fit must not cost the record
        message = f"{record.msg} {record.args!r}"
    record.msg, record.args = scrub(message), None
    if record.exc_info:
        # Formatters use `exc_text` when it is set, rather than rendering the exception again.
        record.exc_text = scrub(_exceptions.formatException(record.exc_info))
    if record.stack_info:
        record.stack_info = scrub(record.stack_info)
    return record


def _log_uncaught(
    kind: type[BaseException], error: BaseException, traceback: TracebackType | None
) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, error, traceback)
        return
    _log.critical("uncaught exception", exc_info=(kind, error, traceback))


def _log_uncaught_in_thread(uncaught: threading.ExceptHookArgs) -> None:
    if uncaught.exc_type is SystemExit:
        return
    name = uncaught.thread.name if uncaught.thread is not None else "unknown"
    _log.critical("uncaught exception in thread %s", name, exc_info=uncaught.exc_value)
