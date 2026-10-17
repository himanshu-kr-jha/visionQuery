"""The runner migrates only an empty database or a VisionQuery memory, never someone else's.

A URL pointed at the wrong database (another application's, or the Postgres an Operator keeps
for something else) must not get the memory's schema written into it. Every test runs against
a real Postgres (see packages/conftest.py), in a database of its own.
"""

from pathlib import Path

import psycopg
import pytest
from visionquery.memory.migrate import ForeignDatabaseError, migrate


def _execute(url: str, *statements: str) -> None:
    with psycopg.connect(url, autocommit=True) as connection:
        for statement in statements:
            connection.execute(statement.encode())


def _exists(url: str, name: str) -> bool:
    with psycopg.connect(url) as connection:
        row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return bool(row and row[0])


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_people.sql").write_text("CREATE TABLE people (id int);")
    return directory


def _refused(database: str, migrations: Path) -> str:
    with pytest.raises(ForeignDatabaseError) as refused:
        migrate(database, migrations)
    # Nothing was written: no table of ours, not even the record.
    assert not _exists(database, "public.people")
    return str(refused.value)


def test_a_database_with_unrelated_tables_and_no_record_is_refused(
    database: str, migrations: Path
) -> None:
    _execute(database, "CREATE TABLE invoices (id int)")
    message = _refused(database, migrations)
    assert "not a VisionQuery memory" in message
    assert "public.invoices" in message
    assert not _exists(database, "public.schema_migrations")


def test_a_table_in_another_schema_makes_a_database_foreign_too(
    database: str, migrations: Path
) -> None:
    _execute(database, "CREATE SCHEMA billing", "CREATE VIEW billing.totals AS SELECT 1 AS n")
    message = _refused(database, migrations)
    assert "billing.totals" in message
    # A view is not a table, so the refusal must not claim the database has tables.
    assert "it has objects and no record" in message
    assert "tables" not in message


def test_a_schema_migrations_table_that_is_not_ours_is_refused(
    database: str, migrations: Path
) -> None:
    # The shape another migration tool (golang-migrate) gives its record.
    _execute(
        database,
        "CREATE TABLE schema_migrations (version bigint PRIMARY KEY, dirty boolean NOT NULL)",
        "INSERT INTO schema_migrations VALUES (20240101, false)",
    )
    message = _refused(database, migrations)
    assert "schema_migrations table that VisionQuery did not create" in message
    with psycopg.connect(database) as connection:
        rows = connection.execute("SELECT * FROM schema_migrations").fetchall()
    assert rows == [(20240101, False)]


def test_an_empty_record_beside_unrelated_tables_is_refused(
    database: str, migrations: Path
) -> None:
    # Our record, with nothing in it, proves nothing about the tables beside it.
    _execute(
        database,
        "CREATE TABLE schema_migrations (number integer PRIMARY KEY, name text NOT NULL UNIQUE,"
        " sha256 bytea NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())",
        "CREATE TABLE invoices (id int)",
    )
    assert "public.invoices" in _refused(database, migrations)


def test_an_empty_database_is_migrated_even_with_extensions_installed(
    database: str, migrations: Path
) -> None:
    # As in a database made from the TimescaleDB image's template1, which has the extension.
    _execute(database, "CREATE EXTENSION timescaledb", "CREATE EXTENSION vector")
    assert migrate(database, migrations) == ("0001_people.sql",)


def test_another_sessions_temporary_tables_do_not_make_a_database_foreign(
    database: str, migrations: Path
) -> None:
    with psycopg.connect(database, autocommit=True) as other:
        other.execute("CREATE TEMP TABLE scratch (id int)")
        assert migrate(database, migrations) == ("0001_people.sql",)


def test_a_memory_with_tables_of_its_own_keeps_being_migrated(
    database: str, migrations: Path
) -> None:
    assert migrate(database, migrations) == ("0001_people.sql",)
    (migrations / "0002_places.sql").write_text("CREATE TABLE places (id int);")
    assert migrate(database, migrations) == ("0002_places.sql",)
