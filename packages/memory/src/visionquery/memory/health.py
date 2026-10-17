"""Whether the memory's database can be reached, for a process to report as its health."""

from dataclasses import dataclass

import psycopg
from visionquery.memory.connection import (
    ConnectionFailedError,
    ConnectionProblem,
    categorize,
    connect,
)

__all__ = ["HEALTH_TIMEOUT", "DatabaseHealth", "check_database"]

#: Seconds a check waits for a connection, unless the URL sets `connect_timeout`; well inside
#: the few seconds a container's healthcheck allows.
HEALTH_TIMEOUT = 3


@dataclass(frozen=True)
class DatabaseHealth:
    """What one check found: connected, or why not, in words that hold nothing of the URL."""

    problem: ConnectionProblem | None

    @property
    def connected(self) -> bool:
        return self.problem is None


def check_database(url: str, *, timeout: int = HEALTH_TIMEOUT) -> DatabaseHealth:
    """Connect to the database at `url` and run a query, then say whether that worked."""
    try:
        with connect(url, timeout) as connection:
            connection.execute("SELECT 1")
    except ConnectionFailedError as error:
        return DatabaseHealth(error.category)
    except psycopg.Error as error:
        # Connected, then lost or refused by the server: the same categories apply.
        return DatabaseHealth(categorize(error))
    return DatabaseHealth(None)
