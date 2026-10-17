"""Connecting to the memory's database, and saying why it failed without repeating libpq.

libpq's own messages quote back the parts of a URL they could not use, so they may hold the
password. A failure to connect is therefore reported only as a fixed category, chosen from the
symptom and never quoted, plus the name of the error's class. This layer is the guarantee;
validating the URL in `visionquery.memory.settings` is best effort.
"""

import os
from typing import Literal

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

__all__ = ["Connection", "ConnectionFailedError", "ConnectionProblem", "categorize", "connect"]

#: A connection to the memory's database, for a caller that holds one without naming the driver.
type Connection = psycopg.Connection

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
    url: str, connect_timeout: int, *, statement_timeout_ms: int | None = None
) -> psycopg.Connection:
    """An autocommit connection, or `ConnectionFailedError` without a word of libpq's text.

    Connecting gives up after the URL's `connect_timeout`, else libpq's `PGCONNECT_TIMEOUT`
    from the environment (empty means unset, and 0 waits forever, as in libpq), else
    `connect_timeout` seconds.

    With `statement_timeout_ms` the connection is also bounded once made: the server cancels a
    statement that runs longer (unless the URL sets its own `options`), and the kernel drops a
    connection whose data goes unacknowledged for that long, or whose peer stops answering
    keepalives, so a server that vanishes mid-query cannot hold the caller forever.
    """
    try:
        parameters = conninfo_to_dict(url)
        # A setting in the URL wins, then libpq's environment variable; `None` leaves the
        # keyword out, so libpq (and psycopg, which enforces the timeout) reads them.
        ours = "connect_timeout" not in parameters and not os.environ.get("PGCONNECT_TIMEOUT")
        extra: dict[str, str | int | None] = {"connect_timeout": connect_timeout if ours else None}
        if statement_timeout_ms is not None:
            if "options" not in parameters:
                extra["options"] = f"-c statement_timeout={statement_timeout_ms}"
            bounds = {
                "tcp_user_timeout": statement_timeout_ms,
                "keepalives": 1,
                "keepalives_idle": 1,
                "keepalives_interval": 1,
                "keepalives_count": 3,
            }
            extra |= {k: v for k, v in bounds.items() if k not in parameters}
        return psycopg.connect(make_conninfo(url, **extra), autocommit=True)
    # Every exception, not only psycopg's: whatever parsing or connecting raised may quote the
    # URL (psycopg's own ProgrammingError for a bad timeout does), and a traceback would print
    # it. Secrecy is worth more than the traceback, so the caller gets a category, the class
    # name, and `from None` drops the original from the chain.
    except Exception as error:
        raise ConnectionFailedError(categorize(error), type(error).__name__) from None
