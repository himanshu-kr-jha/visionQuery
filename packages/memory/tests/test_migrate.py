"""The migration runner applies numbered SQL files once each, in order, and refuses to guess.

Every test runs against a real Postgres (see conftest.py), in a database of its own, with a
directory of migrations written for the test.
"""

import hashlib
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from visionquery.memory.migrate import MigrationFailedError, MigrationRefusedError, migrate


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text)
    return path


def _query(url: str, query: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as connection:
        return connection.execute(query.encode()).fetchall()


def _recorded(url: str) -> list[str]:
    return [str(row[0]) for row in _query(url, "SELECT name FROM schema_migrations ORDER BY 1")]


def _table_exists(url: str, name: str) -> bool:
    with psycopg.connect(url) as connection:
        row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return bool(row and row[0])


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    return directory


# --- applying ----------------------------------------------------------------------------


def test_applies_pending_migrations_in_numeric_order(database: str, migrations: Path) -> None:
    names = [f"{n:04d}_step_{n}.sql" for n in range(1, 13)]
    # Written last to first, so the order on disk is not the order they run in.
    for number, name in reversed(list(enumerate(names, start=1))):
        create = "CREATE TABLE steps (seq serial, number int);" if number == 1 else ""
        _write(migrations, name, f"{create}\nINSERT INTO steps (number) VALUES ({number});\n")

    assert migrate(database, migrations) == tuple(names)

    assert _query(database, "SELECT number FROM steps ORDER BY seq") == [(n,) for n in range(1, 13)]
    assert _recorded(database) == names


def test_a_later_run_applies_only_what_is_new(database: str, migrations: Path) -> None:
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    assert migrate(database, migrations) == ("0001_people.sql",)
    assert migrate(database, migrations) == ()

    _write(migrations, "0002_places.sql", "CREATE TABLE places (id int);")
    assert migrate(database, migrations) == ("0002_places.sql",)
    assert _recorded(database) == ["0001_people.sql", "0002_places.sql"]


def test_a_migration_may_hold_many_statements_and_percent_signs(
    database: str, migrations: Path
) -> None:
    _write(
        migrations,
        "0001_notes.sql",
        "CREATE TABLE notes (body text);\n"
        "INSERT INTO notes VALUES ('100%% sure'), ('50% %s %(name)s');\n",
    )
    migrate(database, migrations)
    assert _query(database, "SELECT body FROM notes ORDER BY 1") == [
        ("100%% sure",),
        ("50% %s %(name)s",),
    ]


def test_each_migration_runs_in_its_own_transaction(database: str, migrations: Path) -> None:
    _write(migrations, "0001_first.sql", "CREATE TABLE first (id int);")
    _write(migrations, "0002_broken.sql", "CREATE TABLE half (id int);\nSELECT 1 / 0;")
    _write(migrations, "0003_after.sql", "CREATE TABLE after (id int);")

    with pytest.raises(MigrationFailedError, match=r"0002_broken\.sql") as failed:
        migrate(database, migrations)
    assert failed.value.file == "0002_broken.sql"
    assert isinstance(failed.value.__cause__, psycopg.errors.DivisionByZero)

    # The one before stays applied; the broken one left nothing, not even its first statement;
    # nothing after it was tried.
    assert _recorded(database) == ["0001_first.sql"]
    assert _table_exists(database, "first")
    assert not _table_exists(database, "half")
    assert not _table_exists(database, "after")

    # It was never applied, so it may be fixed, and the run carries on where it stopped.
    _write(migrations, "0002_broken.sql", "CREATE TABLE half (id int);")
    assert migrate(database, migrations) == ("0002_broken.sql", "0003_after.sql")


def test_records_each_applied_file_with_its_sha256(database: str, migrations: Path) -> None:
    files = [
        _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);\n"),
        _write(migrations, "0002_places.sql", "-- é, not e\nCREATE TABLE places (id int);\n"),
    ]
    migrate(database, migrations)

    rows = _query(database, "SELECT number, name, sha256 FROM schema_migrations ORDER BY 1")
    assert rows == [
        (number, path.name, hashlib.sha256(path.read_bytes()).digest())
        for number, path in enumerate(files, start=1)
    ]


# --- refusing ----------------------------------------------------------------------------


def _refusal(database: str, migrations: Path) -> MigrationRefusedError:
    with pytest.raises(MigrationRefusedError) as refused:
        migrate(database, migrations)
    return refused.value


@pytest.fixture
def applied(database: str, migrations: Path) -> list[str]:
    """Three migrations applied, and a fourth waiting that a refused run must not apply."""
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    _write(migrations, "0002_places.sql", "CREATE TABLE places (id int);")
    _write(migrations, "0003_visits.sql", "CREATE TABLE visits (id int);")
    migrate(database, migrations)
    _write(migrations, "0004_pending.sql", "CREATE TABLE pending (id int);")
    return _recorded(database)


@pytest.fixture
def nothing_more_is_applied(database: str, applied: list[str]) -> Iterator[None]:
    yield
    assert _recorded(database) == applied
    assert not _table_exists(database, "pending")


#: A refused run applies nothing, not even the files that are fine.
refused_run = pytest.mark.usefixtures("nothing_more_is_applied")


@refused_run
def test_refuses_an_applied_file_that_was_edited(database: str, migrations: Path) -> None:
    _write(migrations, "0002_places.sql", "CREATE TABLE places (id bigint);")
    refusal = _refusal(database, migrations)
    assert [problem.file for problem in refusal.problems] == ["0002_places.sql"]
    assert "0002_places.sql was edited after it was applied" in str(refusal)


@refused_run
def test_refuses_an_applied_file_that_was_renamed(database: str, migrations: Path) -> None:
    (migrations / "0002_places.sql").rename(migrations / "0002_areas.sql")
    refusal = _refusal(database, migrations)
    assert [problem.file for problem in refusal.problems] == ["0002_places.sql"]
    assert "0002_places.sql was applied and is now named 0002_areas.sql" in str(refusal)


@refused_run
def test_refuses_applied_files_that_were_reordered(database: str, migrations: Path) -> None:
    # 0002 and 0003 swap places: each name now holds the other's statements.
    places = (migrations / "0002_places.sql").read_text()
    visits = (migrations / "0003_visits.sql").read_text()
    (migrations / "0002_places.sql").unlink()
    (migrations / "0003_visits.sql").unlink()
    _write(migrations, "0002_visits.sql", visits)
    _write(migrations, "0003_places.sql", places)

    refusal = _refusal(database, migrations)
    assert [problem.file for problem in refusal.problems] == [
        "0002_places.sql",
        "0003_visits.sql",
    ]
    assert "0002_places.sql was applied and is now named 0003_places.sql" in str(refusal)
    assert "0003_visits.sql was applied and is now named 0002_visits.sql" in str(refusal)


@refused_run
def test_refuses_a_new_file_numbered_before_the_last_applied(
    database: str, migrations: Path
) -> None:
    # A file slipped in at 0002 and the later ones renumbered to make room for it.
    (migrations / "0004_pending.sql").rename(migrations / "0005_pending.sql")
    (migrations / "0003_visits.sql").rename(migrations / "0004_visits.sql")
    (migrations / "0002_places.sql").rename(migrations / "0003_places.sql")
    _write(migrations, "0002_inserted.sql", "CREATE TABLE inserted (id int);")

    refusal = _refusal(database, migrations)
    assert [problem.file for problem in refusal.problems] == [
        "0002_places.sql",
        "0003_visits.sql",
        "0002_inserted.sql",
    ]
    assert (
        "0002_inserted.sql is not applied and is not numbered after 0003_visits.sql, "
        "the last migration applied"
    ) in str(refusal)


@refused_run
def test_refuses_an_applied_file_that_is_missing(database: str, migrations: Path) -> None:
    (migrations / "0003_visits.sql").unlink()
    (migrations / "0004_pending.sql").rename(migrations / "0003_pending.sql")
    refusal = _refusal(database, migrations)
    assert [problem.file for problem in refusal.problems] == [
        "0003_visits.sql",
        "0003_pending.sql",
    ]
    assert "0003_visits.sql was applied and there is no such file" in str(refusal)


@refused_run
def test_refuses_a_gap_in_the_numbering(database: str, migrations: Path) -> None:
    (migrations / "0004_pending.sql").rename(migrations / "0005_pending.sql")
    refusal = _refusal(database, migrations)
    assert [problem.file for problem in refusal.problems] == ["0005_pending.sql"]
    assert "0005_pending.sql leaves a gap: there is no migration numbered 0004" in str(refusal)


@refused_run
def test_refuses_two_files_with_one_number(database: str, migrations: Path) -> None:
    _write(migrations, "0004_twin.sql", "CREATE TABLE twin (id int);")
    refusal = _refusal(database, migrations)
    assert [problem.file for problem in refusal.problems] == ["0004_twin.sql"]
    assert "0004_twin.sql shares the number 0004 with 0004_pending.sql" in str(refusal)


@refused_run
@pytest.mark.parametrize(
    "name", ["4_pending.sql", "00004_pending.sql", "0004-pending.sql", "0004_Pending.sql"]
)
def test_refuses_a_file_not_named_like_a_migration(
    database: str, migrations: Path, name: str
) -> None:
    (migrations / "0004_pending.sql").rename(migrations / name)
    refusal = _refusal(database, migrations)
    assert [problem.file for problem in refusal.problems] == [name]
    assert f"{name} is not named like NNNN_lowercase_words.sql" in str(refusal)


def test_files_that_are_not_sql_are_not_migrations(database: str, migrations: Path) -> None:
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    _write(migrations, "README.md", "How to add a migration.")
    assert migrate(database, migrations) == ("0001_people.sql",)


def test_refuses_a_directory_that_is_not_there(database: str, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="nowhere"):
        migrate(database, tmp_path / "nowhere")


# --- a file and its record commit together --------------------------------------------------


def test_a_file_and_its_record_commit_together(database: str, migrations: Path) -> None:
    # The file's own statements succeed, then the runner's INSERT of its record collides with
    # a row the file wrote. Everything the file did must go with it.
    digest = "decode(repeat('00', 32), 'hex')"
    _write(
        migrations,
        "0001_first.sql",
        "CREATE TABLE leftovers (id int);\n"
        "INSERT INTO schema_migrations (number, name, sha256) "
        f"VALUES (1, '0001_first.sql', {digest});\n",
    )

    with pytest.raises(MigrationFailedError):
        migrate(database, migrations)

    assert not _table_exists(database, "leftovers")
    assert _recorded(database) == []


# --- transaction control inside a file ------------------------------------------------------

ESCAPES = ["COMMIT;", "commit", "BEGIN;", "START TRANSACTION;", "END;", "ROLLBACK;", "ABORT;"]


@pytest.mark.parametrize("statement", ESCAPES)
def test_refuses_a_file_that_controls_the_transaction(
    database: str, migrations: Path, statement: str
) -> None:
    _write(migrations, "0001_ok.sql", "CREATE TABLE fine (id int);")
    _write(migrations, "0002_bad.sql", f"CREATE TABLE gone (id int);\n{statement}\n")

    with pytest.raises(MigrationRefusedError) as refused:
        migrate(database, migrations)

    assert [problem.file for problem in refused.value.problems] == ["0002_bad.sql"]
    assert not _table_exists(database, "fine")  # a refused run applies nothing
    assert not _table_exists(database, "gone")


def test_accepts_transaction_words_in_strings_comments_and_function_bodies(
    database: str, migrations: Path
) -> None:
    _write(
        migrations,
        "0001_words.sql",
        """
-- COMMIT; BEGIN;
/* ROLLBACK; /* nested COMMIT; */ END; */
CREATE TABLE notes (body text);
INSERT INTO notes VALUES ('COMMIT;'), (E'it''s a BEGIN;\\n'), ($q$ROLLBACK; 'END;'$q$);
CREATE TABLE "commit" ("end" int);
CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$
BEGIN
    RETURN 1;
END;
$$;
CREATE FUNCTION g() RETURNS int LANGUAGE sql
BEGIN ATOMIC
    SELECT 1;
END;
SAVEPOINT s;
ROLLBACK TO SAVEPOINT s;
SELECT CASE WHEN true THEN 1 ELSE 2 END;
""",
    )

    assert migrate(database, migrations) == ("0001_words.sql",)
