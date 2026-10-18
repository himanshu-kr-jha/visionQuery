"""A role of either name that exists already makes migration 0003 refuse, and change nothing.

Roles belong to the server and a role that migration did not make is not trusted, whatever it
holds, so 0003 does not try to clean it: it fails, naming the role, and the operator drops it by
hand. Everything before 0003 stays applied, 0003 itself leaves no trace, and the role stays as
it was.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from visionquery.memory.migrate import MIGRATIONS, MigrationFailedError, migrate

COMMAND = Path(sys.executable).parent / "visionquery-migrate"
ROLES = ("visionquery_runtime", "visionquery_monitoring")
SECRET = "old-password-4711"
CONFIGURED = "configured-password-9"

Setup = Callable[[psycopg.Connection, str], None]


def _plain(admin: psycopg.Connection, role: str) -> None:
    admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))


def _with_login(admin: psycopg.Connection, role: str) -> None:
    admin.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT 3").format(
            sql.Identifier(role), sql.Literal(SECRET)
        )
    )


def _with_members(admin: psycopg.Connection, role: str) -> None:
    _plain(admin, role)
    member = sql.Identifier(f"member_of_{role}")
    admin.execute(sql.SQL("CREATE ROLE {}").format(member))
    admin.execute(sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(sql.Identifier(role), member))


def _with_admin_held_by_a_creator(admin: psycopg.Connection, role: str) -> None:
    creator = sql.Identifier(f"creator_of_{role}")
    admin.execute(sql.SQL("CREATE ROLE {} CREATEROLE").format(creator))
    admin.execute(sql.SQL("SET ROLE {}").format(creator))
    try:
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
    finally:
        admin.execute("RESET ROLE")


VARIANTS: dict[str, Setup] = {
    "plain": _plain,
    "with-login": _with_login,
    "with-members": _with_members,
    "with-admin-held-by-a-creator": _with_admin_held_by_a_creator,
}


@pytest.fixture
def leftovers(postgres: str) -> Iterator[None]:
    """Drops the helper roles a test made; the `database` fixture drops the two it guards."""
    yield
    with psycopg.connect(postgres, autocommit=True) as admin:
        for role in ROLES:
            for other in (f"member_of_{role}", f"creator_of_{role}"):
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(other)))


@pytest.fixture
def before_roles(database: str, tmp_path: Path) -> str:
    """The memory migrated up to, and not including, the roles."""
    before = tmp_path / "migrations"
    before.mkdir()
    for name in ("0001_extensions.sql", "0002_cameras.sql"):
        (before / name).write_text((MIGRATIONS / name).read_text())
    migrate(database, before)
    return database


def _snapshot(url: str, role: str) -> tuple[object, ...]:
    """Everything about `role` that a cleaning, or a grant, would change."""
    with psycopg.connect(url) as connection:
        row = connection.execute("SELECT * FROM pg_authid WHERE rolname = %s", (role,)).fetchone()
        members = connection.execute(
            "SELECT r.rolname, g.rolname, b.rolname, m.admin_option FROM pg_auth_members m"
            " JOIN pg_roles r ON r.oid = m.member JOIN pg_roles g ON g.oid = m.roleid"
            " JOIN pg_roles b ON b.oid = m.grantor WHERE g.rolname = %(r)s OR r.rolname = %(r)s"
            " ORDER BY 1, 2, 3",
            {"r": role},
        ).fetchall()
    return (row, members)


def _nothing_of_0003_applied(url: str, role: str) -> None:
    with psycopg.connect(url) as connection:
        applied = [r[0] for r in connection.execute("SELECT name FROM schema_migrations")]
        assert applied == ["0001_extensions.sql", "0002_cameras.sql"]
        # No grant and no default privilege, and PUBLIC still holds what a new database gives it.
        assert connection.execute(
            "SELECT datacl IS NULL FROM pg_database WHERE datname = current_database()"
        ).fetchone() == (True,)
        assert connection.execute("SELECT count(*) FROM pg_default_acl").fetchone() == (0,)
        assert connection.execute(
            "SELECT coalesce(relacl::text, '') ~ %s FROM pg_class WHERE relname = 'cameras'",
            (role,),
        ).fetchone() == (False,)
        # Only the one that existed; the other was not made.
        assert connection.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(ROLES),)
        ).fetchone() == (1,)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.usefixtures("leftovers")
def test_a_role_that_exists_already_makes_the_migration_refuse_and_apply_nothing(
    postgres: str, before_roles: str, role: str, variant: str
) -> None:
    with psycopg.connect(postgres, autocommit=True) as admin:
        VARIANTS[variant](admin, role)
    untouched = _snapshot(before_roles, role)

    with pytest.raises(MigrationFailedError, match=r"0003_roles\.sql") as failed:
        migrate(before_roles)

    assert failed.value.file == "0003_roles.sql"
    cause = str(failed.value.__cause__)
    assert role in cause
    assert [other for other in ROLES if other != role and other in cause] == []
    assert SECRET not in cause
    assert _snapshot(before_roles, role) == untouched
    _nothing_of_0003_applied(before_roles, role)


@pytest.mark.usefixtures("leftovers")
def test_both_roles_existing_are_both_named(postgres: str, before_roles: str) -> None:
    with psycopg.connect(postgres, autocommit=True) as admin:
        for role in ROLES:
            _plain(admin, role)

    with pytest.raises(MigrationFailedError, match=r"0003_roles\.sql") as failed:
        migrate(before_roles)

    assert all(role in str(failed.value.__cause__) for role in ROLES)


@pytest.mark.usefixtures("leftovers")
def test_the_command_exits_1_naming_the_file_and_the_role_and_leaks_nothing(
    postgres: str, before_roles: str
) -> None:
    with psycopg.connect(postgres, autocommit=True) as admin:
        _with_login(admin, ROLES[0])
    untouched = _snapshot(before_roles, ROLES[0])
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("VISIONQUERY_")}
    environment |= {
        "VISIONQUERY_DATABASE__URL": before_roles,
        "VISIONQUERY_METRICS_HOST": "127.0.0.1",
        "VISIONQUERY_RUNTIME_PASSWORD": CONFIGURED,
    }
    done = subprocess.run(
        [str(COMMAND)], env=environment, capture_output=True, text=True, timeout=60, check=False
    )

    assert done.returncode == 1
    lines = [json.loads(line) for line in done.stdout.splitlines()]
    (failure,) = [line for line in lines if line.get("files")]
    assert failure["files"] == ["0003_roles.sql"]
    assert ROLES[0] in failure["message"]
    everything = done.stdout + done.stderr
    assert SECRET not in everything
    assert CONFIGURED not in everything
    assert "logins enabled" not in everything
    # The password it was configured with was not given to the role it would not trust.
    assert _snapshot(before_roles, ROLES[0]) == untouched
