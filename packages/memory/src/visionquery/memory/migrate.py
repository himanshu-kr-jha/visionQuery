"""`visionquery-migrate`: bring the memory's schema up to date, or refuse to guess (ADR 0011).

The schema is a directory of numbered SQL files, `NNNN_lowercase_words.sql`, numbered from
0001 with no gaps, and applied in that order.

**Each file runs once, in its own transaction, and is recorded with its checksum.**
The row in `schema_migrations` that records a file is written in the same transaction as the
file's statements, so a migration that fails leaves nothing behind and nothing recorded, while
the ones before it stay applied. The record keeps the SHA-256 of the file's bytes.

**A run refuses when the files and the record disagree, naming each file.**
Migrations are append-only: an applied file that was edited, renamed, renumbered or removed
means the database and the files now describe different schemas, so the run applies nothing
and says which file is wrong. So does a gap or a repeat in the numbering, a file not named
like a migration, and a new file numbered at or before the last one applied.

**Runners take turns.**
The whole run holds a Postgres advisory lock, so two runners started at once against one
database serialise: the second waits, then finds nothing left to apply.

The command reads `VISIONQUERY_DATABASE__URL` and exits 0 when the schema is up to date, 1
when a migration failed or the database could not be reached, 2 when its settings are
missing or invalid, and 3 when it refused.
"""

import hashlib
import logging
import re
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import psycopg
from pydantic import Field
from visionquery.memory.settings import DatabaseSettings
from visionquery.runtime import Settings, start

__all__ = [
    "LOCK_KEY",
    "MIGRATIONS",
    "MigrationFailedError",
    "MigrationRefusedError",
    "Problem",
    "main",
    "migrate",
]

_log = logging.getLogger(__name__)

#: The migrations VisionQuery ships, inside the package so that an installed wheel has them.
MIGRATIONS = Path(__file__).parent / "migrations"

#: The advisory lock a run holds; advisory locks are per database, so runs against two
#: databases on one server never wait for each other. The bytes spell `vqschema`.
LOCK_KEY = int.from_bytes(b"vqschema")

_NAME = re.compile(r"(\d{4})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql")

FAILED, REFUSED = 1, 3


@dataclass(frozen=True)
class Problem:
    """One way the migration files and the database's record disagree."""

    #: The file at fault, as it is named on disk or, for an applied one, in the record.
    file: str
    message: str


class MigrationRefusedError(Exception):
    """The files and the record disagree, so nothing was applied."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        details = "\n".join(f"  {problem.message}" for problem in self.problems)
        super().__init__(f"migrations refused, nothing was applied:\n{details}")


class MigrationFailedError(Exception):
    """A migration's statements failed; it was rolled back, and the ones before it stay."""

    def __init__(self, file: str) -> None:
        self.file = file
        super().__init__(f"{file} failed and was rolled back")


@dataclass(frozen=True)
class _Migration:
    number: int
    name: str
    sql: bytes
    sha256: bytes


@dataclass(frozen=True)
class _Applied:
    number: int
    name: str
    sha256: bytes


def migrate(url: str, directory: Path = MIGRATIONS) -> tuple[str, ...]:
    """Apply every pending migration in `directory` to the database at `url`.

    Returns the names applied, in order, which is empty when the schema was up to date.
    Raises `MigrationRefusedError` when the files and the record disagree, and
    `MigrationFailedError` when a migration's statements fail.
    """
    files, problems = _read(directory)
    with psycopg.connect(url, autocommit=True) as connection, _locked(connection):
        # Read under the lock, so what is pending is what nobody else is applying.
        connection.execute(_RECORD)
        applied = _applied(connection)
        problems = _disagreements(files, applied) + problems + _numbering(files, applied)
        if problems:
            raise MigrationRefusedError(problems)
        recorded = {record.name for record in applied}
        pending = [migration for migration in files if migration.name not in recorded]
        for migration in pending:
            _apply(connection, migration)
        return tuple(migration.name for migration in pending)


_RECORD = b"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    number     integer     PRIMARY KEY CHECK (number > 0),
    name       text        NOT NULL UNIQUE,
    sha256     bytea       NOT NULL CHECK (octet_length(sha256) = 32),
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


@contextmanager
def _locked(connection: psycopg.Connection) -> Generator[None]:
    row = connection.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()
    if not (row and row[0]):
        _log.info("waiting for another migration run to finish")
        connection.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        yield
    finally:
        # A broken connection has ended its session, and the lock with it.
        if not connection.broken:
            connection.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def _read(directory: Path) -> tuple[list[_Migration], list[Problem]]:
    """Every migration file in name order, which is number order, and every misnamed one."""
    if not directory.is_dir():
        raise FileNotFoundError(f"{directory} is not a directory of migrations")
    files: list[_Migration] = []
    problems: list[Problem] = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.fullmatch(path.name)
        if match is None:
            message = f"{path.name} is not named like NNNN_lowercase_words.sql"
            problems.append(Problem(path.name, message))
            continue
        sql = path.read_bytes()
        files.append(_Migration(int(match[1]), path.name, sql, hashlib.sha256(sql).digest()))
    return files, problems


def _applied(connection: psycopg.Connection) -> list[_Applied]:
    rows = connection.execute(
        "SELECT number, name, sha256 FROM schema_migrations ORDER BY number"
    ).fetchall()
    return [_Applied(number, name, bytes(sha256)) for number, name, sha256 in rows]


def _disagreements(files: Sequence[_Migration], applied: Sequence[_Applied]) -> list[Problem]:
    """Each applied migration whose file is no longer exactly what was applied."""
    by_name = {migration.name: migration for migration in files}
    recorded = {record.name for record in applied}
    problems: list[Problem] = []
    for record in applied:
        migration = by_name.get(record.name)
        if migration is None:
            moved = next(
                (m for m in files if m.sha256 == record.sha256 and m.name not in recorded),
                None,
            )
            if moved is not None:
                message = (
                    f"{record.name} was applied and is now named {moved.name}; an applied "
                    "migration keeps its name and number"
                )
            else:
                message = (
                    f"{record.name} was applied and there is no such file; restore it, or "
                    "use the migrations this database was migrated with"
                )
            problems.append(Problem(record.name, message))
        elif migration.sha256 != record.sha256:
            message = (
                f"{record.name} was edited after it was applied (applied sha256 "
                f"{record.sha256.hex()[:12]}, file now {migration.sha256.hex()[:12]}); "
                "migrations are append-only, so put it back and add a new numbered file"
            )
            problems.append(Problem(record.name, message))
    return problems


def _numbering(files: Sequence[_Migration], applied: Sequence[_Applied]) -> list[Problem]:
    """Repeated or missing numbers, and new files numbered among the applied ones."""
    recorded = {record.name for record in applied}
    applied_digests = {record.sha256 for record in applied}
    last = applied[-1] if applied else None
    numbered: dict[int, str] = {}
    expected = 1
    problems: list[Problem] = []
    for migration in files:
        name, number = migration.name, migration.number
        if number in numbered:
            message = f"{name} shares the number {number:04d} with {numbered[number]}"
            problems.append(Problem(name, message))
            continue
        numbered[number] = name
        if number > expected:
            message = f"{name} leaves a gap: there is no migration numbered {expected:04d}"
            problems.append(Problem(name, message))
        expected = number + 1
        new = name not in recorded and migration.sha256 not in applied_digests
        if new and last is not None and number <= last.number:
            message = (
                f"{name} is not applied and is not numbered after {last.name}, the last "
                f"migration applied; number it {last.number + 1:04d} or later"
            )
            problems.append(Problem(name, message))
    return problems


def _apply(connection: psycopg.Connection, migration: _Migration) -> None:
    try:
        with connection.transaction():
            # The file's bytes and no parameters: psycopg sends them with the simple query
            # protocol, so the whole file runs as written, several statements and `%` included.
            connection.execute(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations (number, name, sha256) VALUES (%s, %s, %s)",
                (migration.number, migration.name, migration.sha256),
            )
    except psycopg.Error as error:
        raise MigrationFailedError(migration.name) from error
    _log.info(
        "applied %s",
        migration.name,
        extra={"migration": migration.name, "sha256": migration.sha256.hex()},
    )


# --- the command -------------------------------------------------------------------------


class MigrateSettings(Settings):
    """What `visionquery-migrate` reads from the environment."""

    database: DatabaseSettings
    #: A run lasts seconds and nothing scrapes it, so it takes any free port rather than one
    #: a long-running service beside it may hold.
    metrics_port: Annotated[int, Field(ge=0, le=65535)] = 0


def main() -> None:
    """The `visionquery-migrate` command."""
    process = start("migrate", MigrateSettings)
    try:
        applied = migrate(process.settings.database.url.get_secret_value())
    except MigrationRefusedError as error:
        files = [problem.file for problem in error.problems]
        _log.critical("%s", error, extra={"error": type(error).__name__, "files": files})
        raise SystemExit(REFUSED) from None
    except MigrationFailedError as error:
        _log.critical(
            "%s: %s",
            error,
            error.__cause__,
            extra={"error": type(error).__name__, "files": [error.file]},
        )
        raise SystemExit(FAILED) from None
    except psycopg.Error as error:
        _log.critical(
            "could not migrate the database: %s", error, extra={"error": type(error).__name__}
        )
        raise SystemExit(FAILED) from None
    finally:
        process.close()
    _log.info("migrated: %d applied", len(applied), extra={"applied": list(applied)})
