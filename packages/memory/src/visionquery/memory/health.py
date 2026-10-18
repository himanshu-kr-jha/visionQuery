"""Whether the memory's database can be reached, for a process to report as its health."""

from dataclasses import dataclass

import psycopg
from visionquery.memory.connection import (
    ConnectionFailedError,
    ConnectionProblem,
    categorize,
    connect,
)

# `ConnectionProblem` is re-exported: it is what a DatabaseHealth holds, so those who read one
# can name its type without importing the connection module.
__all__ = [
    "CONNECT_SECONDS",
    "HEALTH_BUDGET_SECONDS",
    "STATEMENT_SECONDS",
    "ConnectionProblem",
    "DatabaseHealth",
    "check_database",
]

#: Seconds a check waits for a connection at most, whatever the URL's `connect_timeout` or
#: `PGCONNECT_TIMEOUT` say (0, which waits forever in libpq, included). libpq allows no less.
CONNECT_SECONDS = 2

#: Seconds the check's query may take once connected, before the server cancels it and the
#: kernel gives up on a server that stopped acknowledging.
STATEMENT_SECONDS = 2

#: The longest one check takes on one address: what the api's probe waits for, plus a margin,
#: and what every client of `/health` waits longer than.
HEALTH_BUDGET_SECONDS = CONNECT_SECONDS + STATEMENT_SECONDS


@dataclass(frozen=True)
class DatabaseHealth:
    """What one check found: connected, or why not, in words that hold nothing of the URL."""

    problem: ConnectionProblem | None

    @property
    def connected(self) -> bool:
        return self.problem is None


def check_database(url: str) -> DatabaseHealth:
    """Connect to the database at `url` and run a query, then say whether that worked.

    Takes at most `HEALTH_BUDGET_SECONDS` on one address, whatever the URL or libpq's
    environment ask for (see `connect`), except while the host name is resolved, or when the URL
    names several hosts: only the caller's own deadline bounds those.
    """
    try:
        with connect(
            url,
            CONNECT_SECONDS,
            connect_limit=CONNECT_SECONDS,
            statement_timeout_ms=STATEMENT_SECONDS * 1000,
        ) as connection:
            connection.execute("SELECT 1")
    except ConnectionFailedError as error:
        return DatabaseHealth(error.category)
    except psycopg.Error as error:
        # Connected, then lost or refused by the server: the same categories apply.
        return DatabaseHealth(categorize(error))
    return DatabaseHealth(None)
