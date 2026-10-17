"""The record of applied migrations is writable only by the role that migrates.

Whatever another role was granted, by `GRANT` or by default privileges, each run takes back
everything but `SELECT` on `public.schema_migrations`, so only its owner (the migrating role)
can change what the runner trusts. Every test runs against a real Postgres (see
packages/conftest.py), in a database of its own, with a role made for the test.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import psycopg
import pytest
from psycopg import sql
from visionquery.memory.migrate import MigrationFailedError, migrate

WRITES = [
    "INSERT INTO schema_migrations (number, name, sha256) VALUES (99, 'x', sha256('x'))",
    "UPDATE schema_migrations SET name = name",
    "DELETE FROM schema_migrations",
    "TRUNCATE schema_migrations",
]


@pytest.fixture
def service(database: str) -> Iterator[str]:
    """A role standing in for a service's, with every privilege a careless setup could give."""
    name = f"service_{uuid.uuid4().hex[:12]}"
    role = sql.Identifier(name)
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {}").format(role))
        admin.execute(sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(role))
        admin.execute(sql.SQL("ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {}").format(role))
        admin.execute("ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO PUBLIC")
    yield name
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP OWNED BY {}").format(role))
        admin.execute(sql.SQL("DROP ROLE {}").format(role))


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_people.sql").write_text("CREATE TABLE people (id int);")
    return directory


def _cannot_write(database: str, role: str) -> None:
    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        # Reading what is applied stays allowed: a service may check the schema it runs on.
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone() != (0,)
        for statement in WRITES:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement.encode())


def test_a_new_record_is_not_writable_by_another_role(
    database: str, migrations: Path, service: str
) -> None:
    migrate(database, migrations)
    _cannot_write(database, service)
    # The default privileges still apply to the memory's own tables.
    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(service)))
        connection.execute("INSERT INTO people VALUES (1)")


def test_grants_made_on_an_existing_record_are_taken_back_by_the_next_run(
    database: str, migrations: Path, service: str
) -> None:
    migrate(database, migrations)
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute(
            sql.SQL("GRANT ALL ON schema_migrations TO {}, PUBLIC").format(sql.Identifier(service))
        )
    assert migrate(database, migrations) == ()
    _cannot_write(database, service)


def _role(database: str, *names: str) -> None:
    with psycopg.connect(database, autocommit=True) as admin:
        for name in names:
            admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(name)))


def _drop_roles(database: str, *names: str) -> None:
    with psycopg.connect(database, autocommit=True) as admin:
        for name in names:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def test_a_grant_passed_on_with_grant_option_is_taken_back_down_the_chain(
    database: str, migrations: Path
) -> None:
    first, second = f"chain_a_{uuid.uuid4().hex[:8]}", f"chain_b_{uuid.uuid4().hex[:8]}"
    _role(database, first, second)
    try:
        migrate(database, migrations)
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(
                sql.SQL("GRANT INSERT ON schema_migrations TO {} WITH GRANT OPTION").format(
                    sql.Identifier(first)
                )
            )
            admin.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(first)))
            admin.execute(
                sql.SQL("GRANT INSERT ON schema_migrations TO {}").format(sql.Identifier(second))
            )
        # The next run must succeed, not fail on the dependent grant, and must take both back.
        assert migrate(database, migrations) == ()
        for role in (first, second):
            with psycopg.connect(database, autocommit=True) as connection:
                connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(WRITES[0].encode())
    finally:
        _drop_roles(database, first, second)


@pytest.mark.parametrize("privilege", ["UPDATE", "INSERT"])
def test_a_column_level_grant_is_taken_back(
    database: str, migrations: Path, privilege: Literal["UPDATE", "INSERT"]
) -> None:
    role = f"column_{uuid.uuid4().hex[:8]}"
    _role(database, role)
    try:
        migrate(database, migrations)
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(
                sql.SQL("GRANT {} (sha256) ON schema_migrations TO {}").format(
                    sql.SQL(privilege), sql.Identifier(role)
                )
            )
        assert migrate(database, migrations) == ()
        with psycopg.connect(database, autocommit=True) as connection:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(b"UPDATE schema_migrations SET sha256 = sha256('x')")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    b"INSERT INTO schema_migrations (number, name, sha256)"
                    b" VALUES (98, 'y', sha256('y'))"
                )
        with psycopg.connect(database, autocommit=True) as connection:
            row = connection.execute(
                "SELECT count(*) FROM information_schema.column_privileges"
                " WHERE table_name = 'schema_migrations' AND grantee = %s",
                (role,),
            ).fetchone()
            assert row == (0,)
    finally:
        _drop_roles(database, role)


def test_a_grant_a_migration_makes_on_the_record_is_taken_back_in_the_same_run(
    database: str, migrations: Path, service: str
) -> None:
    # A migration that grants on every table (as one giving a role the memory's rows would)
    # reaches the record too; the run must not leave that until the next run.
    (migrations / "0002_grant.sql").write_text(
        f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{service}";'
    )
    assert migrate(database, migrations) == ("0001_people.sql", "0002_grant.sql")
    _cannot_write(database, service)


def test_a_grant_on_the_record_is_taken_back_even_when_a_later_migration_fails(
    database: str, migrations: Path, service: str
) -> None:
    (migrations / "0002_grant.sql").write_text(
        f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{service}";'
    )
    (migrations / "0003_broken.sql").write_text("SELECT * FROM nowhere;")
    with pytest.raises(MigrationFailedError):
        migrate(database, migrations)
    _cannot_write(database, service)
