"""`status` says what is applied, what is pending and what a run would refuse, changing nothing.

Every test runs against a real Postgres (see packages/conftest.py), in a database of its own,
with a directory of migrations written for the test.
"""

import hashlib
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from visionquery.memory.migrate import (
    LOCK_KEY,
    AppliedMigration,
    ForeignDatabaseError,
    MigrationRefusedError,
    SchemaStatus,
    migrate,
    status,
)


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text)
    return path


def _relations(url: str) -> list[str]:
    with psycopg.connect(url) as connection:
        rows = connection.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace ORDER BY 1"
        ).fetchall()
    return [str(name) for (name,) in rows]


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    return directory


def test_a_new_database_has_everything_pending_and_is_left_as_it_was(
    database: str, migrations: Path
) -> None:
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    _write(migrations, "0002_places.sql", "CREATE TABLE places (id int);")

    assert status(database, migrations) == SchemaStatus(
        applied=(), pending=("0001_people.sql", "0002_places.sql"), problems=()
    )
    # Not even the record was created.
    assert _relations(database) == []


def test_applied_migrations_are_listed_with_when_and_what_was_applied(
    database: str, migrations: Path
) -> None:
    first = _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    before = datetime.now().astimezone()
    migrate(database, migrations)
    _write(migrations, "0002_places.sql", "CREATE TABLE places (id int);")

    found = status(database, migrations)

    (applied,) = found.applied
    assert isinstance(applied, AppliedMigration)
    assert (applied.number, applied.name) == (1, "0001_people.sql")
    assert applied.sha256 == hashlib.sha256(first.read_bytes()).digest()
    assert before <= applied.applied_at <= datetime.now().astimezone()
    assert found.pending == ("0002_places.sql",)
    assert found.problems == ()
    assert "places" not in _relations(database)


def test_an_up_to_date_schema_has_nothing_pending(database: str, migrations: Path) -> None:
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    migrate(database, migrations)
    found = status(database, migrations)
    assert [m.name for m in found.applied] == ["0001_people.sql"]
    assert (found.pending, found.problems) == ((), ())


def test_what_a_run_would_refuse_is_named_and_nothing_is_changed(
    database: str, migrations: Path
) -> None:
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    migrate(database, migrations)
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id bigint);")
    _write(migrations, "0003_gap.sql", "CREATE TABLE gap (id int);")

    found = status(database, migrations)

    assert [problem.file for problem in found.problems] == ["0001_people.sql", "0003_gap.sql"]
    messages = " ".join(problem.message for problem in found.problems)
    assert "0001_people.sql was edited after it was applied" in messages
    assert "0003_gap.sql leaves a gap" in messages
    assert found.pending == ("0003_gap.sql",)
    # What status names is what a run refuses.
    with pytest.raises(MigrationRefusedError) as refused:
        migrate(database, migrations)
    assert refused.value.problems == found.problems


def test_a_file_that_controls_its_transaction_is_named_as_a_problem(
    database: str, migrations: Path
) -> None:
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);\nCOMMIT;")
    (problem,) = status(database, migrations).problems
    assert problem.file == "0001_people.sql"
    assert "contains COMMIT" in problem.message


def test_a_database_that_is_not_a_memory_is_refused(database: str, migrations: Path) -> None:
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    with psycopg.connect(database) as connection:
        connection.execute("CREATE TABLE invoices (id int)")
    with pytest.raises(ForeignDatabaseError, match=r"public\.invoices"):
        status(database, migrations)


def test_status_of_a_foreign_database_says_what_a_run_would_do_not_what_it_did(
    database: str, migrations: Path
) -> None:
    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute(b"CREATE TABLE invoices (id int)")
    with pytest.raises(ForeignDatabaseError) as refused:
        status(database, migrations)
    message = str(refused.value)
    assert "a run would be refused" in message
    assert "nothing was applied" not in message
    assert "public.invoices" in message


def test_status_does_not_wait_for_a_run_in_progress(database: str, migrations: Path) -> None:
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    with psycopg.connect(database, autocommit=True) as running:
        running.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        assert status(database, migrations).pending == ("0001_people.sql",)
