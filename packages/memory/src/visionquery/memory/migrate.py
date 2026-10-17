"""`visionquery-migrate`: bring the memory's schema up to date, or refuse to guess (ADR 0011).

The schema is a directory of numbered SQL files, `NNNN_lowercase_words.sql`, numbered from
0001 with no gaps, and applied in that order.

**Each file runs once, in its own transaction, and is recorded with its checksum.**
The row in `schema_migrations` that records a file is written in the same transaction as the
file's statements, so a migration that fails leaves nothing behind and nothing recorded, while
the ones before it stay applied. The record keeps the SHA-256 of the file's bytes.
Only the record's owner, the role that migrates, may write it: every run takes back from any
other role, and from PUBLIC, every privilege on it but `SELECT`, before applying anything and
again after, since a migration may grant on it too.

**A run refuses when the files and the record disagree, naming each file.**
Migrations are append-only: an applied file that was edited, renamed, renumbered or removed
means the database and the files now describe different schemas, so the run applies nothing
and says which file is wrong. So does a gap or a repeat in the numbering, a file not named
like a migration, a new file numbered at or before the last one applied, and a pending file
with a statement that begins, commits or rolls back a transaction (it would escape its own).

**A run writes only to an empty database or to a VisionQuery memory.**
A database is a memory when its `public.schema_migrations` has the record's columns and at
least one row. Any other database that holds a table, view or sequence of its own (not one an
extension brought) belongs to something else, and so does one whose `schema_migrations` is not
shaped like the record: the run refuses and writes nothing to it, not even the record.

**Runners take turns.**
The whole run holds a Postgres advisory lock, so two runners started at once against one
database serialise: the second waits, then finds nothing left to apply.

**A run does not hang or stall others.**
Connecting gives up after `CONNECT_TIMEOUT` seconds unless the URL sets its own, each file's
transaction runs with a `lock_timeout` (`LOCK_TIMEOUT`) so a blocked statement fails its file by
name, and waiting for the advisory lock is logged every few seconds, however long it takes.
A connection that breaks mid-file leaves its outcome unknown; rerunning is safe.

The command reads `VISIONQUERY_DATABASE__URL` and exits 0 when the schema is up to date, 1
when a migration failed or the database could not be connected to (a fixed message by
category, never libpq's text), 2 when its settings are
missing or invalid, and 3 when it refused (the files and the record disagree, or the database
is not a memory).

`visionquery-migrate status` changes nothing: in one read-only transaction, without waiting for
a run in progress, it logs each applied migration (with when it was applied and its SHA-256),
each pending one, and what a run would refuse. It exits 0, or as a run would: 1 when the
database cannot be reached, 2 for bad settings, and 3 when a run would refuse.
"""

import argparse
import hashlib
import logging
import re
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, LiteralString

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from pydantic import Field
from visionquery.memory._sql import transaction_control
from visionquery.memory.connection import ConnectionFailedError, connect
from visionquery.memory.settings import DatabaseSettings
from visionquery.runtime import Settings, start

__all__ = [
    "CONNECT_TIMEOUT",
    "LOCK_KEY",
    "LOCK_TIMEOUT",
    "MIGRATIONS",
    "AppliedMigration",
    "ConnectionFailedError",
    "ForeignDatabaseError",
    "MigrationFailedError",
    "MigrationRefusedError",
    "Problem",
    "SchemaStatus",
    "main",
    "migrate",
    "status",
]

_log = logging.getLogger(__name__)

#: The migrations VisionQuery ships, inside the package so that an installed wheel has them.
MIGRATIONS = Path(__file__).parent / "migrations"

#: The advisory lock a run holds; advisory locks are per database, so runs against two
#: databases on one server never wait for each other. The bytes spell `vqschema`.
LOCK_KEY = int.from_bytes(b"vqschema")

_NAME = re.compile(r"(?!0000)(\d{4})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql")

FAILED, REFUSED = 1, 3

#: How long to wait for a connection, unless the URL says otherwise, in seconds.
CONNECT_TIMEOUT = 10
#: How long one statement of a migration may wait for a lock before the file fails, in seconds,
#: so a blocked DDL fails by name instead of queueing behind a long transaction and stalling
#: every writer that queues behind it.
LOCK_TIMEOUT = 30.0

#: While another run holds the advisory lock: how often to ask for it, and how often to say so.
_POLL = 0.25
_REPORT_EVERY = 10.0


@dataclass(frozen=True)
class Problem:
    """One way the migration files and the database's record disagree."""

    #: The file at fault, as it is named on disk or, for an applied one, in the record.
    file: str
    message: str


class MigrationRefusedError(Exception):
    """The files and the record disagree, so nothing was applied."""

    def __init__(
        self, problems: Sequence[Problem], lead: str = "migrations refused, nothing was applied"
    ) -> None:
        self.problems = tuple(problems)
        details = "\n".join(f"  {problem.message}" for problem in self.problems)
        super().__init__(f"{lead}:\n{details}")


class ForeignDatabaseError(Exception):
    """The database holds something and is not a VisionQuery memory, so nothing was applied."""

    def __init__(self, reason: str, lead: str = "migrations refused, nothing was applied") -> None:
        self.reason = reason
        super().__init__(
            f"{lead}: the database is not empty and is not a "
            f"VisionQuery memory ({reason}); point the URL at an empty database or at this "
            "Deployment's memory"
        )


class MigrationFailedError(Exception):
    """A migration failed; unless the message says otherwise it was rolled back and the ones
    before it stay."""

    def __init__(self, file: str, outcome: str = "was rolled back") -> None:
        self.file = file
        super().__init__(f"{file} failed and {outcome}")


@dataclass(frozen=True)
class _Migration:
    number: int
    name: str
    sql: bytes
    sha256: bytes


@dataclass(frozen=True)
class AppliedMigration:
    """One row of the record: a migration this database applied, and when."""

    number: int
    name: str
    sha256: bytes
    applied_at: datetime


@dataclass(frozen=True)
class SchemaStatus:
    """What `status` found: the applied migrations in number order, the pending ones by name,
    and what a run would refuse (when there is anything, a run applies nothing)."""

    applied: tuple[AppliedMigration, ...]
    pending: tuple[str, ...]
    problems: tuple[Problem, ...]


def status(
    url: str, directory: Path = MIGRATIONS, *, connect_timeout: int = CONNECT_TIMEOUT
) -> SchemaStatus:
    """What a run would do to the database at `url`, found without changing anything.

    Reads in one read-only transaction and does not wait for a run in progress; what it finds
    is what that run had committed when it looked. Raises `ForeignDatabaseError` when the
    database holds something and is not a VisionQuery memory.
    """
    files, misnamed = _read(directory)
    connection = connect(url, connect_timeout)
    connection.read_only = True
    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    with connection, connection.transaction():
        if reason := _foreign(connection):
            raise ForeignDatabaseError(reason, "a run would be refused, and nothing was changed")
        applied = _applied(connection)
    pending, problems = _plan(files, misnamed, applied)
    return SchemaStatus(tuple(applied), tuple(m.name for m in pending), tuple(problems))


def migrate(
    url: str,
    directory: Path = MIGRATIONS,
    *,
    connect_timeout: int = CONNECT_TIMEOUT,
    lock_timeout: float = LOCK_TIMEOUT,
) -> tuple[str, ...]:
    """Apply every pending migration in `directory` to the database at `url`.

    Returns the names applied, in order, which is empty when the schema was up to date.
    Raises `ForeignDatabaseError` when the database holds something and is not a VisionQuery
    memory, `MigrationRefusedError` when the files and the record disagree, and
    `MigrationFailedError` when a migration's statements fail. Connecting gives up after
    `connect_timeout` seconds unless the URL sets its own, and a statement of a migration that
    waits `lock_timeout` seconds for a lock fails its file.
    """
    files, misnamed = _read(directory)
    connection = connect(url, connect_timeout)
    with connection, _locked(connection):
        # Read under the lock, so what is pending is what nobody else is applying.
        if reason := _foreign(connection):
            raise ForeignDatabaseError(reason)
        connection.execute(_RECORD)
        _seal(connection)
        pending, problems = _plan(files, misnamed, _applied(connection))
        if problems:
            raise MigrationRefusedError(problems)
        applied: list[str] = []
        try:
            for migration in pending:
                _apply(connection, migration, lock_timeout)
                applied.append(migration.name)
                connection.execute(_RESET)
        finally:
            # A migration may have granted on the record too (`ON ALL TABLES IN SCHEMA`), and
            # what it did stays even when a later one fails.
            if applied and not connection.broken:
                _seal(connection)
        return tuple(applied)


_RECORD = b"""
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    number     integer     PRIMARY KEY CHECK (number > 0),
    name       text        NOT NULL UNIQUE,
    sha256     bytea       NOT NULL CHECK (octet_length(sha256) = 32),
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


#: Every privilege on the record, or on any of its columns, held by a role other than its
#: owner, except `SELECT`; the grantee 0 is PUBLIC. A column's privilege is collected with the
#: table's, and revoking the table's one takes the column's with it.
_GRANTS = b"""
SELECT DISTINCT r.rolname, a.privilege_type
FROM pg_catalog.pg_class c
CROSS JOIN LATERAL (
    SELECT acl FROM (VALUES (c.relacl)) AS t(acl)
    UNION ALL
    SELECT attacl FROM pg_catalog.pg_attribute WHERE attrelid = c.oid AND NOT attisdropped
) AS l(acl)
CROSS JOIN LATERAL aclexplode(l.acl) a
LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
WHERE c.oid = 'public.schema_migrations'::regclass
  AND a.grantee <> c.relowner AND a.privilege_type <> 'SELECT'
ORDER BY 1, 2
"""


#: The table privileges other than `SELECT`, as `aclexplode` names them and as SQL keywords.
_PRIVILEGES: dict[str, LiteralString] = {
    "INSERT": "INSERT",
    "UPDATE": "UPDATE",
    "DELETE": "DELETE",
    "TRUNCATE": "TRUNCATE",
    "REFERENCES": "REFERENCES",
    "TRIGGER": "TRIGGER",
    "MAINTAIN": "MAINTAIN",
}


def _seal(connection: psycopg.Connection) -> None:
    """Take back from every other role what would let it write the record.

    The runner trusts the record to say what was applied, so only its owner, the migrating
    role, may change it. Grants made by hand or by default privileges (which a Deployment uses
    to give its services the memory's tables) are revoked on every run; reading stays allowed.
    A superuser, or a member of the owning role, can still write it: that is the owner.
    """
    held: dict[str | None, set[LiteralString]] = {}
    for grantee, privilege in connection.execute(_GRANTS):
        # A privilege this runner does not know of is taken back with everything else.
        held.setdefault(grantee, set()).add(_PRIVILEGES.get(privilege, "ALL"))
    for grantee, privileges in held.items():
        keywords: list[LiteralString] = ["ALL"] if "ALL" in privileges else sorted(privileges)
        connection.execute(
            sql.SQL("REVOKE {} ON TABLE public.schema_migrations FROM {} CASCADE").format(
                sql.SQL(", ").join(sql.SQL(keyword) for keyword in keywords),
                sql.SQL("PUBLIC") if grantee is None else sql.Identifier(grantee),
            )
        )


#: The record's columns and types, as `format_type` names them. A migration that changes the
#: record's own columns must change this list with it, or every database would look foreign.
_RECORD_COLUMNS = [
    ("number", "integer"),
    ("name", "text"),
    ("sha256", "bytea"),
    ("applied_at", "timestamp with time zone"),
]

_COLUMNS = b"""
SELECT a.attname, format_type(a.atttypid, a.atttypmod)
FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
WHERE c.oid = to_regclass('public.schema_migrations') AND c.relkind = 'r'
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

#: What someone made in the database, leaving out the record (when it is ours), what the
#: catalogs and extensions bring (members of an extension depend on it with type `e`), and every
#: session's temporary tables: relations, schemas other than `public`, types that are not
#: implicit (a relation's row type, an array type, a multirange type), and functions,
#: procedures and aggregates.
_OBJECTS = b"""
SELECT name FROM (
    SELECT format('%%I.%%I', n.nspname, c.relname) AS name
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') AND c.relpersistence <> 't'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_toast'
      AND NOT (%(ours)s AND c.oid = to_regclass('public.schema_migrations'))
      AND NOT EXISTS (
          SELECT FROM pg_catalog.pg_depend d
          WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
            AND d.deptype = 'e'
      )
  UNION ALL
    SELECT n.nspname
    FROM pg_catalog.pg_namespace n
    WHERE n.nspname <> 'public' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
      AND NOT EXISTS (
          SELECT FROM pg_catalog.pg_depend d
          WHERE d.classid = 'pg_catalog.pg_namespace'::regclass AND d.objid = n.oid
            AND d.deptype = 'e'
      )
  UNION ALL
    SELECT format('%%I.%%I', n.nspname, t.typname)
    FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
      AND t.typcategory <> 'A' AND t.typtype <> 'm'
      AND (t.typrelid = 0
           OR (SELECT relkind FROM pg_catalog.pg_class WHERE oid = t.typrelid) = 'c')
      AND NOT EXISTS (
          SELECT FROM pg_catalog.pg_depend d
          WHERE d.classid = 'pg_catalog.pg_type'::regclass AND d.objid = t.oid
            AND d.deptype = 'e'
      )
  UNION ALL
    SELECT format('%%I.%%I', n.nspname, p.proname)
    FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
      AND NOT EXISTS (
          SELECT FROM pg_catalog.pg_depend d
          WHERE d.classid = 'pg_catalog.pg_proc'::regclass AND d.objid = p.oid
            AND d.deptype = 'e'
      )
) AS found
ORDER BY 1
LIMIT 4
"""


def _foreign(connection: psycopg.Connection) -> str | None:
    """Why the database is neither empty nor a VisionQuery memory, or None when it is one."""
    columns = connection.execute(_COLUMNS).fetchall()
    ours = columns == _RECORD_COLUMNS
    if columns and not ours:
        return "it has a schema_migrations table that VisionQuery did not create"
    if ours:
        row = connection.execute("SELECT EXISTS (SELECT FROM public.schema_migrations)").fetchone()
        if row and row[0]:
            return None
    objects = [str(name) for (name,) in connection.execute(_OBJECTS, {"ours": ours})]
    if not objects:
        return None
    named = ", ".join(objects[:3]) + (" and more" if len(objects) > 3 else "")
    return f"it has objects and no record of VisionQuery migrations: {named}"


#: What one file may have set or left on the session (`SET search_path`, `SET ROLE`, a temporary
#: table, a prepared statement, a held cursor, a `LISTEN`, a sequence's `currval`...) must not
#: reach the next, which a fresh run would not have seen. This is `DISCARD ALL` without the
#: `pg_advisory_unlock_all()` it includes, because the run's advisory lock must stay held.
_RESET = (
    b"RESET ALL; RESET ROLE; RESET SESSION AUTHORIZATION; DISCARD TEMP; DISCARD PLANS; "
    b"DISCARD SEQUENCES; DEALLOCATE ALL; CLOSE ALL; UNLISTEN *"
)


@contextmanager
def _locked(connection: psycopg.Connection) -> Generator[None]:
    waiting_since = time.monotonic()
    reported = waiting_since
    while True:
        row = connection.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()
        if row and row[0]:
            break
        now = time.monotonic()
        if reported == waiting_since:
            _log.info("waiting for another migration run to finish")
            reported = now
        elif now - reported >= _REPORT_EVERY:
            _log.info(
                "still waiting for another migration run to finish (%d s)",
                now - waiting_since,
                extra={"waited_seconds": round(now - waiting_since)},
            )
            reported = now
        time.sleep(_POLL)
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


def _applied(connection: psycopg.Connection) -> list[AppliedMigration]:
    """The record's rows in number order; none when there is no record yet."""
    row = connection.execute("SELECT to_regclass('public.schema_migrations')").fetchone()
    if row is None or row[0] is None:
        return []
    rows = connection.execute(
        "SELECT number, name, sha256, applied_at FROM public.schema_migrations ORDER BY number"
    ).fetchall()
    return [
        AppliedMigration(number, name, bytes(sha256), applied_at)
        for number, name, sha256, applied_at in rows
    ]


def _plan(
    files: Sequence[_Migration], misnamed: Sequence[Problem], applied: Sequence[AppliedMigration]
) -> tuple[list[_Migration], list[Problem]]:
    """The files a run would apply, in order, and every reason it would refuse to."""
    recorded = {record.name for record in applied}
    pending = [migration for migration in files if migration.name not in recorded]
    problems = [
        *_disagreements(files, applied),
        *misnamed,
        *_numbering(files, applied),
        *_transaction_control(pending),
    ]
    return pending, problems


def _disagreements(
    files: Sequence[_Migration], applied: Sequence[AppliedMigration]
) -> list[Problem]:
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


def _numbering(files: Sequence[_Migration], applied: Sequence[AppliedMigration]) -> list[Problem]:
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


def _transaction_control(pending: Sequence[_Migration]) -> list[Problem]:
    """Each pending file with a statement that would end the transaction it runs in."""
    problems: list[Problem] = []
    for migration in pending:
        statement = transaction_control(migration.sql.decode(errors="replace"))
        if statement is not None:
            message = (
                f"{migration.name} contains {statement}; each migration runs in a transaction "
                "of its own, so it must not begin, commit or roll one back"
            )
            problems.append(Problem(migration.name, message))
    return problems


def _apply(connection: psycopg.Connection, migration: _Migration, lock_timeout: float) -> None:
    try:
        with connection.transaction():
            connection.execute(f"SET LOCAL lock_timeout = {round(lock_timeout * 1000)}".encode())
            # The file's bytes and no parameters: psycopg sends them with the simple query
            # protocol, so the whole file runs as written, several statements and `%` included.
            connection.execute(migration.sql)
            if connection.info.transaction_status != TransactionStatus.INTRANS:
                # The pre-flight check missed a statement that ended our transaction, so what
                # the file did may already be committed. Say so rather than record it.
                raise MigrationFailedError(
                    migration.name,
                    "ended the transaction it runs in, so part of it may be committed without "
                    "a record; look at the database before running again",
                )
            connection.execute(
                "INSERT INTO public.schema_migrations (number, name, sha256) VALUES (%s, %s, %s)",
                (migration.number, migration.name, migration.sha256),
            )
    except psycopg.Error as error:
        if connection.broken:
            raise MigrationFailedError(
                migration.name,
                "the connection broke, so it is not known whether it was applied; "
                "rerunning is safe, because a run reads the record first",
            ) from error
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
    #: Seconds a statement of a migration may wait for a lock before its file fails.
    lock_timeout: Annotated[float, Field(gt=0)] = LOCK_TIMEOUT
    #: Seconds to wait for a connection, unless the URL sets `connect_timeout`.
    connect_timeout: Annotated[int, Field(ge=1)] = CONNECT_TIMEOUT


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="visionquery-migrate",
        description=(
            "Bring the memory's schema up to date, or with `status` list the applied and "
            "pending migrations and what a run would refuse, changing nothing. Reads "
            "VISIONQUERY_DATABASE__URL. Exits 0 when done, 1 when a migration failed or the "
            "database could not be reached, 2 when the settings are missing or invalid, and 3 "
            "when a run refuses (or, for `status`, would refuse)."
        ),
    )
    parser.add_argument(
        "command", nargs="?", choices=["status"], help="only report, change nothing"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """The `visionquery-migrate` command."""
    only_report = _arguments(argv).command == "status"
    process = start("migrate", MigrateSettings)
    try:
        settings = process.settings
        url = settings.database.url.get_secret_value()
        if only_report:
            _report(status(url, connect_timeout=settings.connect_timeout))
            return
        applied = migrate(
            url, connect_timeout=settings.connect_timeout, lock_timeout=settings.lock_timeout
        )
    except MigrationRefusedError as error:
        files = [problem.file for problem in error.problems]
        _log.critical("%s", error, extra={"error": type(error).__name__, "files": files})
        raise SystemExit(REFUSED) from None
    except ForeignDatabaseError as error:
        _log.critical("%s", error, extra={"error": type(error).__name__})
        raise SystemExit(REFUSED) from None
    except ConnectionFailedError as error:
        _log.critical(
            "%s",
            error,
            extra={"error": type(error).__name__, "category": error.category, "cause": error.cause},
        )
        raise SystemExit(FAILED) from None
    except MigrationFailedError as error:
        # The cause is the server's own message about the file's statements; it is scrubbed
        # like any log line, and it was raised by a connected session, never by parsing the URL.
        _log.critical(
            "%s: %s",
            error,
            error.__cause__,
            extra={"error": type(error).__name__, "files": [error.file]},
        )
        raise SystemExit(FAILED) from None
    except psycopg.Error as error:
        # Connecting is `ConnectionFailedError`'s, so this is a connected session's own error.
        _log.critical(
            "could not migrate the database: %s", error, extra={"error": type(error).__name__}
        )
        raise SystemExit(FAILED) from None
    finally:
        process.close()
    _log.info("migrated: %d applied", len(applied), extra={"applied": list(applied)})


def _report(found: SchemaStatus) -> None:
    """Log what `status` found, one line per migration; raise when a run would refuse."""
    for record in found.applied:
        _log.info(
            "applied %s at %s",
            record.name,
            record.applied_at.isoformat(),
            extra={
                "migration": record.name,
                "state": "applied",
                "applied_at": record.applied_at.isoformat(),
                "sha256": record.sha256.hex(),
            },
        )
    for name in found.pending:
        _log.info("pending %s", name, extra={"migration": name, "state": "pending"})
    _log.info(
        "status: %d applied, %d pending",
        len(found.applied),
        len(found.pending),
        extra={
            "applied": [record.name for record in found.applied],
            "pending": list(found.pending),
        },
    )
    if found.problems:
        raise MigrationRefusedError(found.problems, "a run would refuse and apply nothing")
