"""Connecting to the memory's database, and saying why it failed without repeating libpq.

libpq's own messages quote back the parts of a URL they could not use, so they may hold the
password. A failure to connect is therefore reported only as a fixed category, chosen from the
SQLSTATE or the symptom and never quoted, plus the name of the error's class.
"""

from typing import Literal

import psycopg
from psycopg.conninfo import conninfo_to_dict

__all__ = ["ConnectionFailedError", "ConnectionProblem", "categorize", "connect"]

#: Why connecting failed, as fixed text that holds nothing from the URL.
type ConnectionProblem = Literal[
    "authentication failed",
    "database does not exist",
    "server is not accepting connections",
    "server has too many connections",
    "host name could not be resolved",
    "timed out",
    "connection refused or host unreachable",
    "other connection failure",
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


#: libpq's SQLSTATE for a server that answered and refused.
_BY_SQLSTATE: dict[str, ConnectionProblem] = {
    "28P01": "authentication failed",
    "28000": "authentication failed",
    "3D000": "database does not exist",
    "57P03": "server is not accepting connections",
    "53300": "server has too many connections",
}
#: What a client-side failure says, read only to choose a category and never repeated.
_BY_SYMPTOM: tuple[tuple[ConnectionProblem, tuple[str, ...]], ...] = (
    # A server that answered: a connection failure carries no SQLSTATE, only the FATAL text.
    ("authentication failed", ("authentication failed", "pg_hba.conf", 'role "')),
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
    """A fixed description of why connecting failed, from the SQLSTATE or the symptom."""
    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate in _BY_SQLSTATE:
        return _BY_SQLSTATE[sqlstate]
    text = str(error).lower()
    for category, symptoms in _BY_SYMPTOM:
        if any(symptom in text for symptom in symptoms):
            return category
    return "other connection failure"


def connect(url: str, connect_timeout: int) -> psycopg.Connection:
    """An autocommit connection, or `ConnectionFailedError` without a word of libpq's text.

    Connecting gives up after `connect_timeout` seconds unless the URL sets its own.
    """
    try:
        # A timeout in the URL wins; `None` leaves the keyword out.
        timeout = None if "connect_timeout" in conninfo_to_dict(url) else connect_timeout
        return psycopg.connect(url, autocommit=True, connect_timeout=timeout)
    except Exception as error:
        raise ConnectionFailedError(categorize(error), type(error).__name__) from None
