"""The roles a Deployment's processes connect as, so that only migrating runs as a superuser.

The runtime role (every long-running service) reads and writes the rows of the memory's
tables, today's and those later migrations create, and nothing more. The monitoring role (the
Postgres exporter) reads the server's statistics and none of the memory's rows. Neither can log
in until `enable_logins` gives it a password. Every test runs against a real Postgres (see
packages/conftest.py), in a database of its own; roles belong to the server, so every database
migrated there shares them.
"""

import shutil
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote, urlsplit

import psycopg
import pytest
from psycopg import sql
from visionquery.memory.migrate import MIGRATIONS, migrate
from visionquery.memory.roles import MONITORING, RUNTIME, enable_logins

RUNTIME_PASSWORD = "runtime p@ss/w:rd#1?%&= '\"\\"
MONITORING_PASSWORD = "monitoring-password"


def _as(database: str, role: str, password: str) -> str:
    """The URL of `database` with `role` and `password` in place of the superuser's."""
    parts = urlsplit(database)
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}:{parts.port}"
    return parts._replace(netloc=netloc).geturl()


@pytest.fixture
def later(tmp_path: Path) -> Path:
    """The shipped migrations followed by one standing in for a later milestone's."""
    directory = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, directory)
    number = len(list(directory.glob("*.sql"))) + 1
    (directory / f"{number:04d}_later.sql").write_text(
        "CREATE TABLE sightings (id bigint GENERATED ALWAYS AS IDENTITY, at timestamptz NOT NULL,"
        " camera text NOT NULL);\n"
        "SELECT create_hypertable('sightings', by_range('at', interval '1 day'));\n"
        "CREATE TABLE notes (id serial PRIMARY KEY, body text NOT NULL);\n"
    )
    return directory


@pytest.fixture
def runtime(database: str, later: Path) -> Iterator[psycopg.Connection]:
    """A connection to a migrated memory, logged in as the runtime role."""
    migrate(database, later)
    enable_logins(database, {RUNTIME: RUNTIME_PASSWORD})
    with psycopg.connect(_as(database, RUNTIME, RUNTIME_PASSWORD), autocommit=True) as connection:
        yield connection


def test_migrating_makes_both_roles_with_no_powers_of_their_own(database: str) -> None:
    migrate(database)
    with psycopg.connect(database) as connection:
        rows = connection.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls"
            " FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            ([RUNTIME, MONITORING],),
        ).fetchall()
        assert rows == [
            (r, False, False, False, False, False) for r in sorted((RUNTIME, MONITORING))
        ]
        member_of = connection.execute(
            "SELECT r.rolname, g.rolname FROM pg_auth_members m"
            " JOIN pg_roles r ON r.oid = m.member JOIN pg_roles g ON g.oid = m.roleid"
            " WHERE r.rolname = ANY(%s) ORDER BY 1, 2",
            ([RUNTIME, MONITORING],),
        ).fetchall()
        assert set(member_of) == {(MONITORING, "pg_monitor")}


def test_a_role_made_beforehand_with_powers_loses_them(database: str, postgres: str) -> None:
    # Roles belong to the server, so the name may be taken already, by anyone.
    with psycopg.connect(postgres, autocommit=True) as admin:
        exists = admin.execute("SELECT FROM pg_roles WHERE rolname = %s", (RUNTIME,)).fetchone()
        if exists is None:
            admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(RUNTIME)))
        admin.execute(
            sql.SQL("ALTER ROLE {} SUPERUSER CREATEDB CREATEROLE BYPASSRLS").format(
                sql.Identifier(RUNTIME)
            )
        )
        admin.execute(sql.SQL("GRANT pg_write_all_data TO {}").format(sql.Identifier(RUNTIME)))
    migrate(database)
    with psycopg.connect(database) as connection:
        row = connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles"
            " WHERE rolname = %s",
            (RUNTIME,),
        ).fetchone()
        assert row == (False, False, False, False)
        member = connection.execute(
            "SELECT pg_has_role(%s, 'pg_write_all_data', 'MEMBER')", (RUNTIME,)
        ).fetchone()
        assert member == (False,)


def test_two_databases_on_one_server_both_migrate(database: str, postgres: str) -> None:
    migrate(database)
    other = urlsplit(postgres)._replace(path="/roles_second").geturl()
    with psycopg.connect(postgres, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS roles_second WITH (FORCE)")
        admin.execute("CREATE DATABASE roles_second TEMPLATE template0")
    try:
        assert "0002_roles.sql" in migrate(other)
    finally:
        with psycopg.connect(postgres, autocommit=True) as admin:
            admin.execute("DROP DATABASE roles_second WITH (FORCE)")


def test_neither_role_can_log_in_until_given_a_password(database: str) -> None:
    migrate(database)
    with psycopg.connect(database, autocommit=True) as admin:
        # Another test may have let them log in already: roles belong to the server.
        for role in (RUNTIME, MONITORING):
            admin.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(role)))
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_as(database, RUNTIME, RUNTIME_PASSWORD), connect_timeout=5)

    enable_logins(database, {RUNTIME: RUNTIME_PASSWORD, MONITORING: MONITORING_PASSWORD})

    for role, password in ((RUNTIME, RUNTIME_PASSWORD), (MONITORING, MONITORING_PASSWORD)):
        with psycopg.connect(_as(database, role, password), connect_timeout=5) as connection:
            assert connection.execute("SELECT current_user").fetchone() == (role,)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(_as(database, role, "wrong"), connect_timeout=5)


def test_a_password_is_stored_only_as_a_verifier_and_can_be_changed(database: str) -> None:
    migrate(database)
    enable_logins(database, {RUNTIME: "first"})
    enable_logins(database, {RUNTIME: RUNTIME_PASSWORD})
    with psycopg.connect(database) as connection:
        stored = connection.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (RUNTIME,)
        ).fetchone()
    assert stored is not None
    assert str(stored[0]).startswith("SCRAM-SHA-256$")
    assert RUNTIME_PASSWORD not in str(stored[0])
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_as(database, RUNTIME, "first"), connect_timeout=5)
    psycopg.connect(_as(database, RUNTIME, RUNTIME_PASSWORD), connect_timeout=5).close()


def test_only_the_deployments_roles_are_given_logins(database: str) -> None:
    migrate(database)
    with pytest.raises(ValueError, match="postgres"):
        enable_logins(database, {"postgres": "x"})


# --- What the runtime role may do ------------------------------------------------------------


def test_the_runtime_role_reads_and_writes_rows_of_tables_later_migrations_create(
    runtime: psycopg.Connection,
) -> None:
    runtime.execute("INSERT INTO sightings (at, camera) VALUES (now(), 'gate-2')")
    runtime.execute("UPDATE sightings SET camera = 'gate-3'")
    assert runtime.execute("SELECT camera FROM sightings").fetchall() == [("gate-3",)]
    runtime.execute("DELETE FROM sightings")
    runtime.execute("INSERT INTO notes (body) VALUES ('serial columns work too')")
    assert runtime.execute("SELECT count(*) FROM notes").fetchone() == (1,)
    # It may read what was applied, to check the schema it runs on.
    assert runtime.execute("SELECT count(*) FROM schema_migrations").fetchone() != (0,)


CANNOT = [
    pytest.param("CREATE EXTENSION pgcrypto", id="create-a-trusted-extension"),
    pytest.param("CREATE EXTENSION postgres_fdw", id="create-an-untrusted-extension"),
    pytest.param("ALTER TABLE schema_migrations ADD COLUMN x int", id="alter-schema-migrations"),
    pytest.param(
        "INSERT INTO schema_migrations (number, name, sha256) VALUES (99, 'x', sha256('x'))",
        id="write-schema-migrations",
    ),
    pytest.param("DELETE FROM schema_migrations", id="delete-from-schema-migrations"),
    pytest.param("DROP TABLE schema_migrations", id="drop-schema-migrations"),
    pytest.param("DROP TABLE notes", id="drop-a-table"),
    pytest.param("ALTER TABLE notes ADD COLUMN x int", id="alter-a-table"),
    pytest.param("TRUNCATE notes", id="truncate-a-table"),
    pytest.param("CREATE TABLE mine (id int)", id="create-a-table"),
    pytest.param("CREATE SCHEMA mine", id="create-a-schema"),
    pytest.param("CREATE TEMPORARY TABLE mine (id int)", id="create-a-temporary-table"),
]


@pytest.mark.parametrize("statement", CANNOT)
def test_the_runtime_role_cannot(runtime: psycopg.Connection, statement: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        runtime.execute(statement.encode())


def test_the_runtime_role_keeps_none_of_its_privileges_on_schema_migrations_but_select(
    database: str, later: Path
) -> None:
    migrate(database, later)
    with psycopg.connect(database) as connection:
        held = connection.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants"
            " WHERE table_name = 'schema_migrations' AND grantee = %s",
            (RUNTIME,),
        ).fetchall()
    assert held == [("SELECT",)]


# --- What the monitoring role may do ---------------------------------------------------------


def test_the_monitoring_role_reads_statistics_and_no_rows(database: str, later: Path) -> None:
    migrate(database, later)
    enable_logins(database, {MONITORING: MONITORING_PASSWORD})
    with psycopg.connect(
        _as(database, MONITORING, MONITORING_PASSWORD), autocommit=True
    ) as connection:
        # pg_monitor reads every session's activity and the server's settings.
        assert connection.execute("SELECT count(*) FROM pg_stat_activity").fetchone() != (0,)
        connection.execute("SELECT pg_database_size(current_database())")
        for statement in ("SELECT * FROM notes", "INSERT INTO notes (body) VALUES ('x')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement.encode())
