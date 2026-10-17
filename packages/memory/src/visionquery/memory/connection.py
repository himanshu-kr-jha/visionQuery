"""Connecting to the memory's database, and saying why it failed without repeating libpq.

libpq's own messages quote back the parts of a URL they could not use, so they may hold the
password. A failure to connect is therefore reported only as a fixed category, chosen from the
symptom and never quoted, plus the name of the error's class. This layer is the guarantee;
validating the URL in `visionquery.memory.settings` is best effort.
"""

import os
from itertools import pairwise
from typing import Any, Literal

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

__all__ = ["ConnectionFailedError", "ConnectionProblem", "categorize", "connect"]

#: Why connecting failed, as fixed text that holds nothing from the URL.
type ConnectionProblem = Literal[
    "authentication failed",
    "database does not exist",
    "server is not accepting connections",
    "server has too many connections",
    "host name could not be resolved",
    "TLS required or failed",
    "server rejected a startup option",
    "timed out",
    "connection refused or host unreachable",
    "other connection failure",
    # Not a failure: said while the first check has not finished, so a caller need not wait.
    "still checking",
]


class ConnectionFailedError(Exception):
    """The database could not be connected to.

    Its message is fixed text by category and never libpq's own, which quotes back the parts of
    the URL it could not use, and so may hold the password. The host and port are left out for
    the same reason: a URL can put a secret anywhere.
    """

    def __init__(self, category: ConnectionProblem, cause: str) -> None:
        self.category: ConnectionProblem = category
        #: The name of the class of the error that was raised, never its text.
        self.cause = cause
        super().__init__(f"could not connect to the database: {category} ({cause})")


#: What a failure says, read only to choose a category and never repeated. A failure to connect
#: carries no SQLSTATE, even when the server answered: only libpq's text, with any FATAL in it.
_BY_SYMPTOM: tuple[tuple[ConnectionProblem, tuple[str, ...]], ...] = (
    ("authentication failed", ("authentication failed", "pg_hba.conf", 'role "')),
    (
        "server rejected a startup option",
        ("unrecognized configuration parameter", "invalid value for parameter"),
    ),
    # Phrases, never one word: libpq's text quotes the host, which may be `sslproxy.example`.
    (
        "TLS required or failed",
        (
            "does not support ssl",
            "ssl error",
            "ssl syscall",
            "ssl connection",
            "ssl negotiation",
            "establish ssl",
            "server certificate",
            "root certificate",
            "certificate verify",
        ),
    ),
    ("database does not exist", ("does not exist",)),
    ("server is not accepting connections", ("starting up", "shutting down", "recovery mode")),
    ("server has too many connections", ("too many clients", "remaining connection slots")),
    ("host name could not be resolved", ("resolve host", "translate host", "name resolution")),
    ("timed out", ("timeout expired", "timed out")),
    (
        "connection refused or host unreachable",
        ("refused", "unreachable", "no such file or directory", "no route to host"),
    ),
)


def categorize(error: Exception) -> ConnectionProblem:
    """A fixed description of why connecting, or a connected session, failed."""
    if isinstance(error, psycopg.errors.QueryCanceled):
        # A connected session's statement the server gave up on after `statement_timeout`.
        return "timed out"
    text = str(error).lower()
    for category, symptoms in _BY_SYMPTOM:
        if any(symptom in text for symptom in symptoms):
            return category
    return "other connection failure"


def connect(
    url: str,
    connect_timeout: int,
    *,
    connect_limit: int | None = None,
    statement_timeout_ms: int | None = None,
) -> psycopg.Connection:
    """An autocommit connection, or `ConnectionFailedError` without a word of libpq's text.

    Connecting gives up after the URL's `connect_timeout`, else libpq's `PGCONNECT_TIMEOUT`
    from the environment (empty means unset, and 0 waits forever, as in libpq), else
    `connect_timeout` seconds. With `connect_limit` it gives up after at most that many seconds
    whatever the URL or `PGCONNECT_TIMEOUT` say, 0 included: a smaller value of theirs is kept.
    Each of these bounds one address: a URL naming several hosts may try each in turn.

    With `statement_timeout_ms` the connection is also bounded once made: the server cancels a
    statement that runs longer, and the kernel drops a connection whose data goes
    unacknowledged for that long, or whose peer stops answering keepalives, so a server that
    vanishes mid-query cannot hold the caller forever. The statement bound is appended to the
    URL's `options`, else to libpq's `PGOPTIONS` (carried over, since an `options` keyword takes
    its place), unless they already set `statement_timeout`, which is then theirs to choose.
    """
    try:
        parameters = conninfo_to_dict(url)
        extra: dict[str, str | int | None] = {
            "connect_timeout": _connect_timeout(parameters, connect_timeout, connect_limit)
        }
        if statement_timeout_ms is not None:
            extra |= _statement_bounds(parameters, statement_timeout_ms)
        return psycopg.connect(make_conninfo(url, **extra), autocommit=True)
    # Every exception, not only psycopg's: whatever parsing or connecting raised may quote the
    # URL (psycopg's own ProgrammingError for a bad timeout does), and a traceback would print
    # it. Secrecy is worth more than the traceback, so the caller gets a category, the class
    # name, and `from None` drops the original from the chain.
    except Exception as error:
        raise ConnectionFailedError(categorize(error), type(error).__name__) from None


def _connect_timeout(parameters: dict[str, Any], default: int, limit: int | None) -> int | None:
    """The `connect_timeout` keyword to pass; `None` leaves it out, so libpq (and psycopg,
    which enforces the timeout) read the URL's, then `PGCONNECT_TIMEOUT`."""
    if limit is None:
        ours = "connect_timeout" not in parameters and not os.environ.get("PGCONNECT_TIMEOUT")
        return default if ours else None
    given: object = parameters.get("connect_timeout", os.environ.get("PGCONNECT_TIMEOUT"))
    try:
        # As psycopg reads it; 0 or less is forever, and what cannot be read is the limit too.
        seconds = int(float(str(given)))
    except ValueError:
        return limit
    return min(seconds, limit) if seconds > 0 else limit


def _statement_bounds(parameters: dict[str, Any], milliseconds: int) -> dict[str, str | int]:
    """The keywords that bound a connected session, leaving out any the URL sets itself."""
    bounds: dict[str, str | int] = {
        "tcp_user_timeout": milliseconds,
        "keepalives": 1,
        "keepalives_idle": 1,
        "keepalives_interval": 1,
        "keepalives_count": 3,
    }
    bounds = {k: v for k, v in bounds.items() if k not in parameters}
    options = str(parameters.get("options", os.environ.get("PGOPTIONS", "")))
    if "statement_timeout" not in _settings_named(options):
        bound = f"-c statement_timeout={milliseconds}"
        bounds["options"] = f"{options} {bound}" if options.strip() else bound
    return bounds


def _settings_named(options: str) -> set[str]:
    """The settings an `options` value sets, named as the server reads them: in lower case,
    with `-` as `_`, from `-c name=value`, `-cname=value` and `--name=value`."""
    words = _split_options(options)
    named: set[str] = set()
    for word, following in pairwise([*words, ""]):
        if word == "-c":
            setting = following
        elif word.startswith(("-c", "--")):
            setting = word[2:]
        else:
            continue
        named.add(setting.partition("=")[0].replace("-", "_").lower())
    return named


def _split_options(options: str) -> list[str]:
    """`options` split into words as the server splits it: at whitespace, where a backslash
    makes the character after it part of the word."""
    words: list[str] = []
    word, in_word, escaped = "", False, False
    for character in options:
        if not escaped and character.isspace():
            if in_word:
                words.append(word)
            word, in_word = "", False
        elif not escaped and character == "\\":
            escaped = in_word = True
        else:
            word, in_word, escaped = word + character, True, False
    if in_word:
        words.append(word)
    return words
