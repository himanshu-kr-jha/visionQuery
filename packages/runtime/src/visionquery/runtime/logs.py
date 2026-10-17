"""Logs as one JSON object per line on stdout, scrubbed of credentials before anything sees them.

Everything goes through the standard library's logging, so third-party libraries are
covered too. Scrubbing happens in two places, so no single forgotten call can leak a camera
password: a record factory scrubs every record as it is made (leaving it untouched when there
is nothing to remove), and a wrapper around `Logger.makeRecord` scrubs the `extra=` fields,
which the standard library attaches only after the factory has run. Together they protect
handlers a library installs for itself. The JSON formatter scrubs every field it writes and
masks credential-named structured fields as well.

Output that does not go through logging is not covered: subprocess stderr, stderr written by
C libraries, and `print`. Subprocess stderr must be piped and logged line by line (ADR 0012).
"""

import json
import logging
import math
import re
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, TextIO, cast

from visionquery.runtime.scrub import REDACTED, is_credential_key, scrub
from visionquery.runtime.settings import LogLevel

__all__ = ["configure_logging"]

_log = logging.getLogger("visionquery.runtime")

#: The attributes every record has; anything else on a record came from `extra=`.
_STANDARD_ATTRIBUTES = frozenset(
    [*logging.LogRecord("", 0, "", 0, "", None, None).__dict__, "message", "asctime"]
)

_SURROGATES = re.compile("[\ud800-\udfff]")
_UNPRINTABLE = "<unprintable>"

#: Marks a record factory or `makeRecord` as the runtime's scrubbing wrapper, so that
#: configuring again finds it in place and does not wrap it a second time.
_SCRUBS = "_visionquery_scrubs"

_exceptions = logging.Formatter()


def configure_logging(service: str, level: LogLevel, *, stream: TextIO | None = None) -> None:
    """Send every log record of this process to `stream` (stdout) as scrubbed JSON lines.

    Replaces whatever handlers the root logger had, and also routes warnings and uncaught
    exceptions, in the main thread or any other, through the same path. The record factory
    and `Logger.makeRecord` in place when it is called (OpenTelemetry's, say) are wrapped,
    not replaced, so whatever fields they add are kept.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    factory = logging.getLogRecordFactory()
    if not _scrubs(factory):
        logging.setLogRecordFactory(_scrubbing_factory(factory))
    # Replaced on the class, process-wide, which covers loggers that already exist (ADR 0012).
    make_record = logging.Logger.makeRecord
    if not _scrubs(make_record):
        logging.Logger.makeRecord = _scrubbing_make_record(make_record)
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
    """`value` as JSON-ready data: every string scrubbed, credential-named keys' values masked."""
    match value:
        case str():
            return scrub(value)
        case bool() | int() | None:
            return value
        case float():
            return value if math.isfinite(value) else str(value)
        case Mapping():
            items = cast(Mapping[object, object], value).items()
            return {
                scrub(str(key)): REDACTED if is_credential_key(str(key)) else _scrubbed(item)
                for key, item in items
            }
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


def _scrubs(hook: object) -> bool:
    """Whether `hook` is one of the runtime's scrubbing wrappers."""
    return getattr(hook, _SCRUBS, False) is True


def _scrubbing_make_record(
    make_record: Callable[..., logging.LogRecord],
) -> Callable[..., logging.LogRecord]:
    """`make_record`, then the `extra=` fields it attached, scrubbed."""

    def make_scrubbed_record(
        self: logging.Logger,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: Mapping[str, object] | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        record = make_record(self, name, level, fn, lno, msg, args, exc_info, func, extra, sinfo)
        for key in extra or ():
            try:
                record.__dict__[key] = _kept_scrubbed(key, record.__dict__[key])
            except Exception:  # logging must never raise into the caller
                record.__dict__[key] = _UNPRINTABLE
        return record

    setattr(make_scrubbed_record, _SCRUBS, True)
    return make_scrubbed_record


def _kept_scrubbed(key: str, value: object) -> object:
    """`value` with credentials removed and its type kept, so `%(count)d` still formats.

    Credential-named keys are masked, strings and nested containers are scrubbed, and any other
    object is replaced by its scrubbed text only when its text held a credential. A container
    with nothing to remove is kept as it is, whatever its type.
    """
    if is_credential_key(key):
        return REDACTED
    match value:
        case str():
            return scrub(value)
        case bool() | int() | float() | None:
            return value
        case Mapping():
            mapping = cast(Mapping[object, object], value)
            items = mapping.items()
            kept = {
                (scrub(k) if isinstance(k, str) else k): _kept_scrubbed(str(k), item)
                for k, item in items
            }
            unchanged = len(kept) == len(items) and all(
                k in kept and _same(kept[k], item) for k, item in items
            )
            return mapping if unchanged else kept
        case list() | tuple() | set() | frozenset():
            members = cast(list[object], value)
            kept = [_kept_scrubbed("", item) for item in members]
            return (
                members if all(map(_same, kept, members, strict=True)) else _rebuilt(members, kept)
            )
        case _:
            text = _text(value)
            clean = scrub(text)
            return value if clean == text else clean


def _same(kept: object, original: object) -> bool:
    return kept is original or (type(kept) is str and kept == original)


def _rebuilt(original: object, members: list[object]) -> object:
    """A container of `original`'s type holding `members`, or a plain tuple or list."""
    kind = cast(type[Any], type(original))
    make = getattr(kind, "_make", None)  # a namedtuple takes its fields one by one
    try:
        return make(members) if callable(make) else kind(members)
    except Exception:
        return tuple(members) if isinstance(original, tuple) else members


def _scrubbed_args(args: object) -> tuple[object, ...] | Mapping[str, object] | None:
    """The arguments with each credential-bearing one replaced by its scrubbed text, same shape."""
    if isinstance(args, Mapping):
        mapping = cast(Mapping[str, object], args)
        return {key: _scrubbed_argument(item) for key, item in mapping.items()}
    if isinstance(args, tuple):
        return tuple(_scrubbed_argument(item) for item in cast(tuple[object, ...], args))
    return None


def _scrubbed_argument(argument: object) -> object:
    text = argument if isinstance(argument, str) else _text(argument)
    clean = scrub(text)
    return argument if clean == text else clean


def _scrubbing_factory(
    factory: Callable[..., logging.LogRecord],
) -> Callable[..., logging.LogRecord]:
    """A record factory making each record with `factory`, then scrubbing it."""

    def scrubbed_record(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return _scrub_record(factory(*args, **kwargs))

    setattr(scrubbed_record, _SCRUBS, True)
    return scrubbed_record


def _scrub_record(record: logging.LogRecord) -> logging.LogRecord:
    """`record` holding no credential, left exactly as made when there was none to remove.

    Formatters other libraries install may read `record.args` (uvicorn's access log does), so
    the arguments keep their shape: only those that hold a credential are replaced by their
    scrubbed text. The message and arguments are replaced by the scrubbed rendering (and
    `args` dropped) only when that does not remove the credential, as when it is split across
    the message and an argument, or when the record could not be rendered. Logging must never
    raise into the caller, so every rendering step is guarded.
    """
    rendered, fits = _rendered(record)
    scrubbed = scrub(rendered)
    needs_scrubbing = scrubbed != rendered or not fits
    if needs_scrubbing and not (fits and _scrub_arguments_in_place(record)):
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


def _scrub_arguments_in_place(record: logging.LogRecord) -> bool:
    """Scrub the record's arguments one by one; whether that left a rendering with no credential."""
    original = record.args
    replaced = _scrubbed_args(original)
    if replaced is None:
        return False
    record.args = replaced
    again, fits = _rendered(record)
    if fits and scrub(again) == again:
        return True
    record.args = original
    return False


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
