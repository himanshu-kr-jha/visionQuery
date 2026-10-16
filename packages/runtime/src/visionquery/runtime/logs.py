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
import re
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

_SURROGATES = re.compile("[\ud800-\udfff]")
_UNPRINTABLE = "<unprintable>"

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
        text = json.dumps(_scrubbed(line), ensure_ascii=False)
        # A lone surrogate cannot be encoded to UTF-8; as a JSON escape it round-trips.
        return _SURROGATES.sub(lambda found: f"\\u{ord(found.group()):04x}", text)


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
            return scrub(_text(value))


def _text(value: object) -> str:
    """`str(value)`, or a placeholder when printing it raises."""
    try:
        return str(value)
    except Exception:
        return _UNPRINTABLE


def _rendered(record: logging.LogRecord) -> tuple[str, bool]:
    """The record's message and whether it rendered; when it did not, the message text alone.

    Never raises, and never loses the message text because of an argument that will not print.
    """
    try:
        return record.getMessage(), True
    except Exception:  # arguments that do not fit must not cost the record
        pass
    try:
        return f"{_text(record.msg)} {record.args!r}", False
    except Exception:
        return f"{_text(record.msg)} {_UNPRINTABLE}", False


def _scrubbed_record(*args: Any, **kwargs: Any) -> logging.LogRecord:
    """A record that holds no credential, left exactly as made when there was none to remove.

    Formatters other libraries install may read `record.args` (uvicorn's access log does), so
    the message and arguments are replaced by the scrubbed rendering only when the rendering
    held something to scrub, or could not be made. Logging must never raise into the caller,
    so every rendering step is guarded.
    """
    record = _make_record(*args, **kwargs)
    rendered, fits = _rendered(record)
    scrubbed = scrub(rendered)
    if scrubbed != rendered or not fits:
        record.msg, record.args = scrubbed, None
    if record.exc_info:
        # Formatters use `exc_text` when it is set, rather than rendering the exception again.
        try:
            record.exc_text = scrub(_exceptions.formatException(record.exc_info))
        except Exception:
            record.exc_text = _UNPRINTABLE
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
