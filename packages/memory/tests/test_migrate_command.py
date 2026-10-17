"""`visionquery-migrate`, run the way a Deployment runs it: a process configured by the environment.

Each test runs the installed command in a child process and looks only at what an Operator
could see: its exit status and its JSON log lines.
"""

import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, quote, urlsplit

import psycopg
import pytest
from visionquery.memory.migrate import MIGRATIONS

COMMAND = Path(sys.executable).parent / "visionquery-migrate"
#: The migrations VisionQuery ships, in order.
SHIPPED = sorted(path.name for path in MIGRATIONS.glob("*.sql"))


def _run(
    extra: dict[str, str] | None = None, *, args: tuple[str, ...] = (), **variables: str
) -> tuple[int, list[dict[str, Any]], str]:
    inherited = {k: v for k, v in os.environ.items() if not k.upper().startswith("VISIONQUERY_")}
    environment = inherited | {f"VISIONQUERY_{k.upper()}": v for k, v in variables.items()}
    environment |= extra or {}
    # Bound to loopback; the port is the command's own default, which must not be a fixed one.
    environment["VISIONQUERY_METRICS_HOST"] = "127.0.0.1"
    done = subprocess.run(
        [str(COMMAND), *args],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done.returncode, [json.loads(line) for line in done.stdout.splitlines()], done.stderr


def _host(parts: SplitResult) -> str:
    """The URL's host as a netloc needs it: an IPv6 address keeps its brackets."""
    host = parts.hostname or ""
    return f"[{host}]" if ":" in host else host


def _messages(lines: list[dict[str, Any]]) -> list[str]:
    return [line["message"] for line in lines]


def test_migrates_a_new_database_and_then_has_nothing_to_do(database: str) -> None:
    status, lines, stderr = _run(database__url=database)
    assert (status, stderr) == (0, "")
    assert all(line["service"] == "migrate" for line in lines)
    assert "applied 0001_extensions.sql" in _messages(lines)
    (done,) = [line for line in lines if line["message"].startswith("migrated")]
    assert done["applied"][0] == "0001_extensions.sql"

    status, lines, _ = _run(database__url=database)
    assert status == 0
    (done,) = [line for line in lines if line["message"].startswith("migrated")]
    assert done["applied"] == []
    assert not any(m.startswith("applied ") for m in _messages(lines))


def test_the_password_may_come_from_pgpassword_instead_of_the_url(database: str) -> None:
    # How the Compose stack runs it: the URL names no password, so any password works.
    parts = urlsplit(database)
    assert parts.password is not None
    bare = parts._replace(netloc=f"{parts.username}@{_host(parts)}:{parts.port}").geturl()
    status, lines, stderr = _run(extra={"PGPASSWORD": parts.password}, database__url=bare)
    assert (status, stderr) == (0, "")
    assert any(m.startswith("migrated") for m in _messages(lines))
    assert parts.password not in json.dumps(lines)


def test_a_wrong_pgpassword_fails_without_showing_it(database: str) -> None:
    parts = urlsplit(database)
    bare = parts._replace(netloc=f"{parts.username}@{_host(parts)}:{parts.port}").geturl()
    status, lines, stderr = _run(extra={"PGPASSWORD": "wrongPW9"}, database__url=bare)
    assert status == 1
    (failed,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert failed["error"] == "ConnectionFailedError"
    assert "wrongPW9" not in json.dumps(lines) + stderr


def _login(database: str, role: str, password: str) -> str:
    parts = urlsplit(database)
    netloc = f"{role}:{quote(password, safe='')}@{_host(parts)}:{parts.port}"
    with psycopg.connect(parts._replace(netloc=netloc).geturl(), connect_timeout=5) as connection:
        row = connection.execute("SELECT current_user").fetchone()
    assert row is not None
    return str(row[0])


def test_after_migrating_the_runtime_and_monitoring_roles_log_in_with_their_passwords(
    database: str,
) -> None:
    status, lines, stderr = _run(
        database__url=database,
        runtime_password="runtimePW7 @/:#",
        monitoring_password="monitoringPW8",
    )
    assert (status, stderr) == (0, "")
    (enabled,) = [line for line in lines if line["message"] == "logins enabled"]
    assert enabled["roles"] == ["visionquery_monitoring", "visionquery_runtime"]
    assert "runtimePW7" not in json.dumps(lines)
    assert "monitoringPW8" not in json.dumps(lines)
    assert _login(database, "visionquery_runtime", "runtimePW7 @/:#") == "visionquery_runtime"
    assert _login(database, "visionquery_monitoring", "monitoringPW8") == "visionquery_monitoring"


def test_status_never_changes_a_login(database: str) -> None:
    assert _run(database__url=database, runtime_password="before1")[0] == 0
    status, lines, _ = _run(args=("status",), database__url=database, runtime_password="after2")
    assert status == 0
    assert not any(line["message"] == "logins enabled" for line in lines)
    assert _login(database, "visionquery_runtime", "before1") == "visionquery_runtime"


def test_a_refused_run_changes_no_login(database: str) -> None:
    assert _run(database__url=database, runtime_password="before3")[0] == 0
    with psycopg.connect(database) as connection:
        connection.execute(
            "UPDATE schema_migrations SET sha256 = sha256('before'::bytea) WHERE number = 1"
        )
    assert _run(database__url=database, runtime_password="after4")[0] == 3
    assert _login(database, "visionquery_runtime", "before3") == "visionquery_runtime"


def test_an_edited_migration_is_refused_by_name(database: str) -> None:
    assert _run(database__url=database)[0] == 0
    with psycopg.connect(database) as connection:
        # As if the shipped file had changed since this database applied it.
        connection.execute(
            "UPDATE schema_migrations SET sha256 = sha256('before'::bytea) WHERE number = 1"
        )

    status, lines, stderr = _run(database__url=database)
    assert (status, stderr) == (3, "")
    (refused,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert refused["error"] == "MigrationRefusedError"
    assert refused["files"] == ["0001_extensions.sql"]
    assert "0001_extensions.sql was edited after it was applied" in refused["message"]


def test_a_database_that_is_not_a_memory_is_refused_and_left_alone(database: str) -> None:
    with psycopg.connect(database) as connection:
        connection.execute("CREATE TABLE schema_migrations (version bigint, dirty boolean)")

    status, lines, stderr = _run(database__url=database)
    assert (status, stderr) == (3, "")
    (refused,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert refused["error"] == "ForeignDatabaseError"
    assert "not a VisionQuery memory" in refused["message"]
    assert "schema_migrations table that VisionQuery did not create" in refused["message"]
    with psycopg.connect(database) as connection:
        # Not even the first migration's extensions were created.
        extensions = connection.execute("SELECT extname FROM pg_extension").fetchall()
    assert extensions == [("plpgsql",)]


def test_a_url_with_several_hosts_migrates_through_the_one_that_answers(database: str) -> None:
    # libpq's per-host port form: the first host refuses, the second is the database.
    parts = urlsplit(database)
    hosts = f"127.0.0.1:1,{_host(parts)}:{parts.port}"
    url = parts._replace(netloc=f"{parts.username}:{parts.password}@{hosts}").geturl()
    status, lines, stderr = _run(database__url=url)
    assert (status, stderr) == (0, "")
    assert "applied 0001_extensions.sql" in _messages(lines)


# --- `visionquery-migrate status` ----------------------------------------------------------


def _extensions(database: str) -> list[str]:
    with psycopg.connect(database) as connection:
        rows = connection.execute("SELECT extname FROM pg_extension ORDER BY 1").fetchall()
    return [str(name) for (name,) in rows]


def test_status_lists_pending_migrations_and_changes_nothing(database: str) -> None:
    status, lines, stderr = _run(args=("status",), database__url=database)
    assert (status, stderr) == (0, "")
    pending = [line for line in lines if line.get("state") == "pending"]
    assert [line["migration"] for line in pending] == SHIPPED
    assert pending[0]["message"] == "pending 0001_extensions.sql"
    (summary,) = [line for line in lines if line["message"].startswith("status:")]
    assert summary["message"] == f"status: 0 applied, {len(SHIPPED)} pending"
    assert summary["applied"] == []
    assert summary["pending"] == SHIPPED
    # Nothing was applied, and not even the record was created.
    assert _extensions(database) == ["plpgsql"]
    with psycopg.connect(database) as connection:
        row = connection.execute("SELECT to_regclass('public.schema_migrations')").fetchone()
    assert row == (None,)


def test_status_lists_applied_migrations_with_when_they_were_applied(database: str) -> None:
    assert _run(database__url=database)[0] == 0
    status, lines, stderr = _run(args=("status",), database__url=database)
    assert (status, stderr) == (0, "")
    every = [line for line in lines if line.get("state") == "applied"]
    assert [line["migration"] for line in every] == SHIPPED
    applied = every[0]
    assert applied["message"].startswith("applied 0001_extensions.sql at ")
    assert datetime.fromisoformat(applied["applied_at"]).tzinfo is not None
    assert len(applied["sha256"]) == 64
    (summary,) = [line for line in lines if line["message"].startswith("status:")]
    assert summary["message"] == f"status: {len(SHIPPED)} applied, 0 pending"


def test_status_names_what_a_run_would_refuse(database: str) -> None:
    assert _run(database__url=database)[0] == 0
    with psycopg.connect(database) as connection:
        connection.execute(
            "UPDATE schema_migrations SET sha256 = sha256('before'::bytea) WHERE number = 1"
        )
    status, lines, stderr = _run(args=("status",), database__url=database)
    assert (status, stderr) == (3, "")
    (refused,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert refused["error"] == "MigrationRefusedError"
    assert refused["files"] == ["0001_extensions.sql"]
    assert "a run would refuse" in refused["message"]
    assert "0001_extensions.sql was edited after it was applied" in refused["message"]


def test_status_refuses_a_database_that_is_not_a_memory(database: str) -> None:
    with psycopg.connect(database) as connection:
        connection.execute("CREATE TABLE invoices (id int)")
    status, lines, stderr = _run(args=("status",), database__url=database)
    assert (status, stderr) == (3, "")
    (refused,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert refused["error"] == "ForeignDatabaseError"
    assert "public.invoices" in refused["message"]
    # It reports what a run would do; nothing was applied by this command either way.
    assert "a run would be refused" in refused["message"]


def test_a_pgconnect_timeout_that_is_not_a_number_is_a_settings_error_naming_it(
    database: str,
) -> None:
    status, lines, stderr = _run({"PGCONNECT_TIMEOUT": "soon"}, database__url=database)
    assert (status, stderr) == (2, "")
    (line,) = lines
    assert "PGCONNECT_TIMEOUT is invalid" in line["message"]
    assert "whole number of seconds, 0 to wait forever" in line["message"]
    # The variable to change is PGCONNECT_TIMEOUT alone, not the database URL's.
    assert "VISIONQUERY_DATABASE" not in line["message"]
    assert "soon" not in line["message"]


def test_status_of_an_unreachable_database_fails_without_its_password() -> None:
    url = "postgresql://vq:hunter2@127.0.0.1:1/db"
    status, lines, stderr = _run(args=("status",), database__url=url)
    assert status == 1
    (failed,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert failed["error"] == "ConnectionFailedError"
    assert "hunter2" not in json.dumps(lines) + stderr


def test_an_unknown_command_is_a_usage_error() -> None:
    status, _, stderr = _run(args=("upgrade",))
    assert status == 2
    assert "status" in stderr


def test_a_missing_database_url_is_named() -> None:
    status, lines, _ = _run()
    assert status == 2
    (line,) = lines
    assert line["variables"] == ["VISIONQUERY_DATABASE__URL"]


def _with(database: str, variable: str, value: str) -> Any:
    return {"database__url": database, variable: value}


@pytest.mark.parametrize("variable", ["runtime_password", "monitoring_password"])
def test_an_empty_role_password_is_unset_and_clears_nothing(database: str, variable: str) -> None:
    role = "visionquery_" + variable.removesuffix("_password")
    status, _, _ = _run(**_with(database, variable, "first-password"))
    assert status == 0
    status, _, _ = _run(**_with(database, variable, ""))
    assert status == 0
    with psycopg.connect(database) as connection:
        stored = connection.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,)
        ).fetchone()
    assert stored is not None
    assert str(stored[0]).startswith("SCRAM-SHA-256$")


def test_an_unreachable_database_is_reported_without_its_password(database: str) -> None:
    # A port nothing listens on, with a password that must not appear anywhere.
    parts = urlsplit(database)
    unreachable = parts._replace(netloc=f"vq:hunter2@{_host(parts)}:1").geturl()
    status, lines, stderr = _run(database__url=unreachable)
    assert status == 1
    (failed,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert failed["error"] == "ConnectionFailedError"
    assert failed["message"] == (
        "could not connect to the database: connection refused or host unreachable "
        "(OperationalError)"
    )
    assert "hunter2" not in json.dumps(lines) + stderr


# --- a URL that is not a URL must not leak into the output --------------------------------

SECRET = "hunter2"


@pytest.mark.parametrize(
    "url",
    [
        f"u:{SECRET}",
        f"user:{SECRET}@host/db",
        f"host=h password={SECRET}",
        f"password={SECRET}",
        f"!!{SECRET}!! not a url",
        f"mysql://u:{SECRET}@host/db",
        f"postgresql://u:{SECRET}@host:notaport/db",
        f"postgresql://u:{SECRET}@host:99999999/db",
        f"postgresql://u:{SECRET}@[::1/db",
        f"POSTGRESQL://u:{SECRET}@host/db",
        f"postgresql://u:{SECRET}@127.0.0.1:1/db?sslmode=bogus",
        f"postgresql://u:{SECRET}@127.0.0.1:1/db?nosuchoption=1",
        f"postgresql://u:{SECRET}@127.0.0.1:1/db?options=%zz",
        f"postgresql://u:{SECRET}@127.0.0.1:1/db?connect_timeout=%ZZ",
    ],
)
def test_a_malformed_url_is_a_settings_error_that_never_shows_it(url: str) -> None:
    status, lines, stderr = _run(database__url=url)
    assert status == 2
    assert SECRET not in json.dumps(lines) + stderr
    (line,) = lines
    assert line["variables"] == ["VISIONQUERY_DATABASE__URL"]


# --- libpq's own text must never reach the logs ------------------------------------------

LEAK = "SECRETPW"


@pytest.mark.parametrize(
    "url",
    [
        # An unescaped `@`: libpq splits at the first, so the rest of the password was a host.
        "postgresql://postgres:SEC@RETPW@127.0.0.1:55432/d",
        "postgresql://us@er:SEC@RETPW@127.0.0.1:55432/d",
        "postgresql://u:SEC@RET@PW@127.0.0.1:55432/d",
        "postgresql://u:SEC@RETPW@[::1]:55432/d",
        "postgresql://u:SEC@RETPW@[::1@x]/d",
        f"postgresql://u:{LEAK}@127.0.0.1:55432/d?password={LEAK}&connect_timeout={LEAK}",
        f"postgresql://u:{LEAK}@127.0.0.1/d?port={LEAK}",
        f"postgresql://u:{LEAK}@127.0.0.1:1/d?connect_timeout=abc",
        f"postgresql://u:{LEAK}@127.0.0.1:1/d?connect_timeout=10s",
        f"postgresql://u:{LEAK}@/d?host={LEAK}@x",
        f"postgresql://u:{LEAK}@127.0.0.1/d?sslmode={LEAK}",
        f"postgresql://u:{LEAK}@127.0.0.1/d?gssencmode={LEAK}",
        f"postgresql://u:{LEAK}@127.0.0.1/d?target_session_attrs={LEAK}",
    ],
)
def test_a_url_that_makes_libpq_quote_it_back_is_a_settings_error(url: str) -> None:
    status, lines, stderr = _run(database__url=url)
    assert status == 2
    output = json.dumps(lines) + stderr
    for fragment in ("SEC", "RETPW", LEAK, "RET"):
        assert fragment not in output
    (line,) = lines
    assert line["variables"] == ["VISIONQUERY_DATABASE__URL"]


def test_a_secret_used_as_a_host_name_never_reaches_the_logs() -> None:
    # A host libpq cannot resolve: it quotes it back, so the runner must not repeat libpq.
    url = f"postgresql://u:{LEAK}@/d?host={LEAK}&connect_timeout=3"
    status, lines, stderr = _run(database__url=url)
    assert status == 1
    assert LEAK not in json.dumps(lines) + stderr
    (failed,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert failed["error"] == "ConnectionFailedError"


def test_a_secret_never_reaches_the_logs_even_at_debug() -> None:
    # psycopg logs every failed attempt at DEBUG with libpq's text; the runtime keeps it quiet.
    url = f"postgresql://u:{LEAK}@/d?host={LEAK},127.0.0.1&port=,1&connect_timeout=3"
    status, lines, stderr = _run(database__url=url, log_level="DEBUG")
    assert status == 1
    assert LEAK not in json.dumps(lines) + stderr


def _failure(url: str) -> str:
    status, lines, stderr = _run(database__url=url)
    assert status == 1
    assert (url_secret := urlsplit(url).password) is not None
    output = json.dumps(lines) + stderr
    assert url_secret not in output
    (failed,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert failed["error"] == "ConnectionFailedError"
    # No fragment of the URL: not a host, a database, a user nor a port.
    for fragment in (urlsplit(url).hostname, urlsplit(url).username, urlsplit(url).path.strip("/")):
        if fragment:
            assert fragment not in failed["message"]
    return str(failed["message"])


def test_a_wrong_password_is_reported_as_an_authentication_failure(database: str) -> None:
    parts = urlsplit(database)
    wrong = parts._replace(netloc=f"{parts.username}:wrongPW9@{_host(parts)}:{parts.port}")
    assert _failure(wrong.geturl()) == (
        "could not connect to the database: authentication failed (OperationalError)"
    )


def test_a_missing_database_is_reported_as_such(database: str) -> None:
    parts = urlsplit(database)
    missing = parts._replace(path="/no_such_database_zz").geturl()
    assert _failure(missing) == (
        "could not connect to the database: database does not exist (OperationalError)"
    )


def test_a_refused_port_is_reported_as_such() -> None:
    assert _failure("postgresql://vq:pw9@127.0.0.1:1/db") == (
        "could not connect to the database: connection refused or host unreachable "
        "(OperationalError)"
    )


def test_an_unresolvable_host_is_reported_as_such() -> None:
    message = _failure("postgresql://vq:pw9@nohost-zz.invalid:5432/db?connect_timeout=5")
    assert message == (
        "could not connect to the database: host name could not be resolved (OperationalError)"
    )


def test_a_server_that_never_answers_is_reported_as_a_timeout() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)  # accepts into the backlog and never answers
        port = server.getsockname()[1]
        message = _failure(f"postgresql://vq:pw9@127.0.0.1:{port}/db?connect_timeout=1")
    assert message == "could not connect to the database: timed out (ConnectionTimeout)"
