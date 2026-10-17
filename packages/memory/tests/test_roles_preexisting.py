"""Roles that exist already, with grants of their own, end up with exactly the roles' rights."""

import contextlib
from pathlib import Path

import psycopg
from visionquery.memory.migrate import MIGRATIONS, migrate

ROLES = ("visionquery_runtime", "visionquery_monitoring")
TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")


def _held(url: str, role: str) -> tuple[set[str], set[str], list[str]]:
    """The privileges `role` holds on `cameras`, on its sequence, and on single columns."""
    with psycopg.connect(url) as connection:
        tables = {
            privilege
            for privilege in TABLE_PRIVILEGES
            if connection.execute(
                "SELECT has_table_privilege(%s, 'public.cameras', %s)", (role, privilege)
            ).fetchone()
            == (True,)
        }
        sequences = {
            privilege
            for privilege in SEQUENCE_PRIVILEGES
            if connection.execute(
                "SELECT has_sequence_privilege(%s, 'public.cameras_id_seq', %s)",
                (role, privilege),
            ).fetchone()
            == (True,)
        }
        columns = [
            row[0]
            for row in connection.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = 'public.cameras'::regclass"
                " AND attnum > 0 AND attacl IS NOT NULL AND EXISTS"
                " (SELECT 1 FROM aclexplode(attacl) a"
                "  WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = %s))",
                (role,),
            )
        ]
    return tables, sequences, columns


def test_grants_a_pre_existing_role_already_holds_do_not_survive(
    database: str, tmp_path: Path
) -> None:
    before = tmp_path / "migrations"
    before.mkdir()
    for name in ("0001_extensions.sql", "0002_cameras.sql"):
        (before / name).write_text((MIGRATIONS / name).read_text())
    migrate(database, before)
    with psycopg.connect(database, autocommit=True) as connection:
        for role in ROLES:
            with contextlib.suppress(psycopg.errors.DuplicateObject):  # made by another test
                connection.execute(f"CREATE ROLE {role} NOLOGIN")
            connection.execute(f"GRANT ALL ON cameras TO {role}")
            connection.execute(f"GRANT ALL (name, pull_credentials) ON cameras TO {role}")
            connection.execute(f"GRANT ALL ON SEQUENCE cameras_id_seq TO {role}")

    migrate(database)

    assert _held(database, "visionquery_runtime") == (
        {"SELECT", "INSERT", "UPDATE", "DELETE"},
        {"USAGE", "SELECT"},
        [],
    )
    assert _held(database, "visionquery_monitoring") == (set(), set(), [])
