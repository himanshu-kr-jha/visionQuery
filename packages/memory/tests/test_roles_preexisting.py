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


def _extension_function_acls(url: str) -> list[tuple[str, str | None]]:
    with psycopg.connect(url) as connection:
        return [
            (str(row[0]), None if row[1] is None else str(row[1]))
            for row in connection.execute(
                "SELECT p.oid::regprocedure::text, p.proacl::text FROM pg_proc p"
                " JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid"
                " AND d.deptype = 'e' ORDER BY 1"
            )
        ]


def test_what_a_pre_existing_role_holds_beyond_current_objects_does_not_survive(
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
            connection.execute(f"ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {role}")
            connection.execute(f"ALTER DEFAULT PRIVILEGES GRANT ALL ON SEQUENCES TO {role}")
            connection.execute(f"ALTER DEFAULT PRIVILEGES GRANT ALL ON FUNCTIONS TO {role}")
            connection.execute(f"GRANT SET ON PARAMETER log_statement TO {role}")
        connection.execute("ALTER DEFAULT PRIVILEGES GRANT ALL ON SCHEMAS TO visionquery_runtime")
        connection.execute("CREATE PROCEDURE old_procedure() LANGUAGE sql AS 'SELECT 1'")
        connection.execute("REVOKE ALL ON PROCEDURE old_procedure() FROM PUBLIC")
        connection.execute("GRANT EXECUTE ON PROCEDURE old_procedure() TO visionquery_runtime")
        connection.execute("CREATE TABLE owned_by_runtime (id integer)")
        connection.execute("ALTER TABLE owned_by_runtime OWNER TO visionquery_runtime")
    functions_before = _extension_function_acls(database)

    migrate(database)

    with psycopg.connect(database, autocommit=True) as connection:
        migrating = connection.execute("SELECT current_user").fetchone()
        connection.execute("CREATE TABLE later_table (id serial PRIMARY KEY)")
        connection.execute("CREATE SCHEMA later_schema")
        held = {
            role: {
                privilege
                for privilege in TABLE_PRIVILEGES
                if connection.execute(
                    "SELECT has_table_privilege(%s, 'later_table', %s)", (role, privilege)
                ).fetchone()
                == (True,)
            }
            for role in ROLES
        }
        schema = {
            (role, privilege): connection.execute(
                "SELECT has_schema_privilege(%s, 'later_schema', %s)", (role, privilege)
            ).fetchone()
            for role in ROLES
            for privilege in ("USAGE", "CREATE")
        }
        procedure = {
            role: connection.execute(
                "SELECT has_function_privilege(%s, 'old_procedure()', 'EXECUTE')", (role,)
            ).fetchone()
            for role in ROLES
        }
        parameter = {
            role: connection.execute(
                "SELECT has_parameter_privilege(%s, 'log_statement', 'SET')", (role,)
            ).fetchone()
            for role in ROLES
        }
        owner = connection.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'owned_by_runtime'"
        ).fetchone()
        defaults = sorted(
            (str(kind), str(grantee), str(privilege))
            for kind, grantee, privilege in connection.execute(
                "SELECT d.defaclobjtype, pg_get_userbyid(a.grantee), a.privilege_type"
                " FROM pg_default_acl d, aclexplode(d.defaclacl) a"
                " WHERE pg_get_userbyid(a.grantee) = ANY(%s)"
                " OR pg_get_userbyid(d.defaclrole) = ANY(%s)",
                (list(ROLES), list(ROLES)),
            )
        )

    assert held == {
        "visionquery_runtime": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "visionquery_monitoring": set(),
    }
    assert not any(row == (True,) for row in schema.values())
    assert procedure == dict.fromkeys(ROLES, (False,))
    assert parameter == dict.fromkeys(ROLES, (False,))
    assert owner == migrating
    assert defaults == [
        ("S", "visionquery_runtime", "SELECT"),
        ("S", "visionquery_runtime", "USAGE"),
        ("r", "visionquery_runtime", "DELETE"),
        ("r", "visionquery_runtime", "INSERT"),
        ("r", "visionquery_runtime", "SELECT"),
        ("r", "visionquery_runtime", "UPDATE"),
    ]
    assert _extension_function_acls(database) == functions_before
