"""The migration runner applies numbered SQL files once each, in order, and refuses to guess.

Every test runs against a real Postgres (see packages/conftest.py), in a database of its own,
with a directory of migrations written for the test.
"""

import hashlib
import logging
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import psycopg
import pytest
from visionquery.memory import migrate as runner
from visionquery.memory.migrate import (
    LOCK_KEY,
    ConnectionFailedError,
    MigrationFailedError,
    MigrationRefusedError,
    Problem,
    migrate,
)


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
    "name",
    [
        "4_pending.sql",
        "00004_pending.sql",
        "0004-pending.sql",
        "0004_Pending.sql",
        "0000_pending.sql",
    ],
)
def test_refuses_a_file_not_named_like_a_migration(
    database: str, migrations: Path, name: str
) -> None:
    (migrations / "0004_pending.sql").rename(migrations / name)
    refusal = _refusal(database, migrations)
    assert [problem.file for problem in refusal.problems] == [name]
    assert f"{name} is not named like NNNN_lowercase_words.sql" in str(refusal)


def test_a_migration_that_cannot_be_numbered_before_0001_is_refused_by_name(
    database: str, migrations: Path
) -> None:
    _write(migrations, "0000_zero.sql", "CREATE TABLE zero (id int);")
    with pytest.raises(MigrationRefusedError) as refused:
        migrate(database, migrations)
    assert [str(p) for p in refused.value.problems] == [
        str(Problem("0000_zero.sql", "0000_zero.sql is not named like NNNN_lowercase_words.sql"))
    ]


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


# --- one file's session settings do not reach the next ----------------------------------------


def test_session_settings_of_one_file_do_not_leak_into_the_next(
    database: str, migrations: Path
) -> None:
    # The timeout must outlast the runner's own bookkeeping statements in the same transaction
    # as 0001 (a loaded machine can take tens of ms) yet be shorter than the sleep in 0003: were
    # the setting to leak, that sleep would be cancelled.
    _write(
        migrations,
        "0001_settings.sql",
        "CREATE SCHEMA other;\nSET search_path TO other, public;\nSET statement_timeout = '300ms';\n",
    )
    _write(migrations, "0002_table.sql", "CREATE TABLE t2 (id int);")
    _write(migrations, "0003_sleep.sql", "SELECT pg_sleep(1);")

    assert migrate(database, migrations) == (
        "0001_settings.sql",
        "0002_table.sql",
        "0003_sleep.sql",
    )

    assert _table_exists(database, "public.t2")
    assert not _table_exists(database, "other.t2")
    assert _recorded(database) == ["0001_settings.sql", "0002_table.sql", "0003_sleep.sql"]


def test_the_advisory_lock_is_still_held_after_the_session_is_reset(
    database: str, migrations: Path
) -> None:
    _write(migrations, "0001_settings.sql", "SET statement_timeout = '5s';")
    _write(
        migrations,
        "0002_probe.sql",
        # Another session cannot take the lock, so it was still held between the two files.
        "CREATE TABLE probe AS SELECT pg_advisory_lock_held AS held FROM "
        f"(SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
        f"AND pid = pg_backend_pid() AND objid = {LOCK_KEY & 0xFFFFFFFF}) "
        "AS pg_advisory_lock_held) AS s;",
    )
    migrate(database, migrations)
    assert _query(database, "SELECT held FROM probe") == [(True,)]


def test_the_runner_finds_its_record_whatever_the_search_path_says(
    database: str, migrations: Path
) -> None:
    _write(migrations, "0001_other.sql", "CREATE SCHEMA other;\nSET search_path TO other;")
    _write(
        migrations,
        "0002_shadow.sql",
        "CREATE TABLE other.schema_migrations (decoy int);\nSELECT 1;",
    )
    assert migrate(database, migrations) == ("0001_other.sql", "0002_shadow.sql")
    assert migrate(database, migrations) == ()


# --- a run does not hang or stall others --------------------------------------------------------


def _listening_socket_that_never_answers() -> socket.socket:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)  # the kernel completes the handshake; nothing ever reads or replies
    return server


def test_connecting_gives_up_after_the_default_timeout(migrations: Path) -> None:
    with _listening_socket_that_never_answers() as server:
        url = f"postgresql://u:p@127.0.0.1:{server.getsockname()[1]}/db"
        started = time.monotonic()
        with pytest.raises(ConnectionFailedError, match=r"timed out"):
            migrate(url, migrations, connect_timeout=1)
    assert time.monotonic() - started < 8


def test_a_connect_timeout_in_the_url_wins_over_the_default(migrations: Path) -> None:
    with _listening_socket_that_never_answers() as server:
        url = f"postgresql://u:p@127.0.0.1:{server.getsockname()[1]}/db?connect_timeout=1"
        started = time.monotonic()
        with pytest.raises(ConnectionFailedError, match=r"timed out"):
            migrate(url, migrations, connect_timeout=60)
    assert time.monotonic() - started < 8


def test_a_blocked_statement_fails_naming_its_file_instead_of_stalling(
    database: str, migrations: Path
) -> None:
    with psycopg.connect(database, autocommit=True) as setup:
        setup.execute("CREATE TABLE busy (id int)")
    _write(migrations, "0001_alter.sql", "ALTER TABLE busy ADD COLUMN extra int;")

    outcome: list[BaseException] = []

    def run() -> None:
        try:
            migrate(database, migrations, lock_timeout=0.5)
        except BaseException as error:
            outcome.append(error)

    with psycopg.connect(database) as holder:
        holder.execute("LOCK TABLE busy IN ACCESS EXCLUSIVE MODE")  # until this closes
        started = time.monotonic()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        # A client-side deadline: were the lock_timeout not applied, the migration would wait
        # for the holder for ever, and this must fail instead of hanging.
        thread.join(timeout=10)
        gave_up_in_time = not thread.is_alive()
    thread.join(timeout=10)  # the holder is gone now, so a stuck run finishes
    assert gave_up_in_time, "the blocked migration was still waiting: lock_timeout is not applied"
    assert time.monotonic() - started < 15
    (error,) = outcome
    assert isinstance(error, MigrationFailedError)
    assert "0001_alter.sql" in str(error)
    assert isinstance(error.__cause__, psycopg.errors.LockNotAvailable)
    assert _recorded(database) == []


def test_waiting_for_the_advisory_lock_is_logged_until_it_is_free(
    database: str,
    migrations: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_REPORT_EVERY", 0.3)
    monkeypatch.setattr(runner, "_POLL", 0.05)
    _write(migrations, "0001_people.sql", "CREATE TABLE people (id int);")
    caplog.set_level(logging.INFO, logger=runner.__name__)
    done: list[tuple[str, ...]] = []

    with psycopg.connect(database, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        thread = threading.Thread(target=lambda: done.append(migrate(database, migrations)))
        thread.start()
        time.sleep(1.2)
        assert thread.is_alive()
        other.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    thread.join(timeout=10)

    assert done == [("0001_people.sql",)]
    waits = [r.getMessage() for r in caplog.records if "still waiting" in r.getMessage()]
    assert len(waits) >= 2


# --- what a run cannot be sure of ---------------------------------------------------------------


def test_a_file_that_ends_its_own_transaction_is_failed_by_name(
    database: str, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lexer refuses this up front; this is the second line of defence if it ever misses one.

    def no_problems(pending: Sequence[object]) -> list[Problem]:
        return []

    monkeypatch.setattr(runner, "_transaction_control", no_problems)
    _write(migrations, "0001_escape.sql", "CREATE TABLE escaped (id int);\nCOMMIT;")

    with pytest.raises(MigrationFailedError, match=r"0001_escape\.sql") as failed:
        migrate(database, migrations)

    assert failed.value.file == "0001_escape.sql"
    assert "ended the transaction" in str(failed.value)
    assert _recorded(database) == []


def test_a_broken_connection_says_the_outcome_is_unknown_and_that_rerunning_is_safe(
    database: str, migrations: Path
) -> None:
    _write(migrations, "0001_killed.sql", "SELECT pg_terminate_backend(pg_backend_pid());")

    with pytest.raises(MigrationFailedError, match=r"0001_killed\.sql") as failed:
        migrate(database, migrations)

    message = str(failed.value)
    assert "rolled back" not in message
    assert "not known" in message
    assert "rerunning is safe" in message
