"""The roles a Deployment's processes connect as, and letting them log in (ADR 0016).

Migration `0003_roles.sql` makes them, unable to log in: `RUNTIME`, which every long-running
service connects as and which reads and writes the rows of the memory's tables and nothing
more, and `MONITORING`, which the Postgres exporter connects as, with `pg_monitor`. Only
`visionquery-migrate` connects as the superuser that owns the memory, and after migrating it
gives each role its login with the password it was configured with.

A password never reaches the server as text: libpq turns it into a SCRAM verifier on this
side, and the server stores that, so neither its logs nor its statistics can show the password.
"""

import logging
from collections.abc import Mapping

from psycopg import sql
from visionquery.memory.connection import connect

__all__ = ["MONITORING", "ROLES", "RUNTIME", "enable_logins"]

_log = logging.getLogger(__name__)

#: What every long-running service connects as.
RUNTIME = "visionquery_runtime"
#: What the Postgres exporter connects as.
MONITORING = "visionquery_monitoring"
#: The roles `enable_logins` may give a login; it never touches any other.
ROLES = (RUNTIME, MONITORING)


def enable_logins(url: str, passwords: Mapping[str, str], *, connect_timeout: int = 10) -> None:
    """Let each of the Deployment's roles in `passwords` log in, with its password.

    Run as a role that may alter them (the superuser that migrates). A role's previous password
    stops working. Raises `ValueError`, changing nothing, when asked about any other role or for an empty
    password (which would leave the role without one).
    """
    if unknown := sorted(set(passwords) - set(ROLES)):
        raise ValueError(f"only {', '.join(ROLES)} are given logins, not {', '.join(unknown)}")
    if empty := sorted(role for role, password in passwords.items() if not password):
        raise ValueError(f"the password of {', '.join(empty)} is empty")
    with connect(url, connect_timeout) as connection:
        for role, password in passwords.items():
            verifier = connection.pgconn.encrypt_password(
                password.encode(), role.encode(), algorithm=b"scram-sha-256"
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(verifier.decode())
                )
            )
    _log.info("logins enabled", extra={"roles": sorted(passwords)})
