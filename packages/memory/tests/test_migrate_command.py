"""`visionquery-migrate`, run the way a Deployment runs it: a process configured by the environment.

Each test runs the installed command in a child process and looks only at what an Operator
could see: its exit status and its JSON log lines.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest

COMMAND = Path(sys.executable).parent / "visionquery-migrate"


def _run(
    extra: dict[str, str] | None = None, **variables: str
) -> tuple[int, list[dict[str, Any]], str]:
    inherited = {k: v for k, v in os.environ.items() if not k.upper().startswith("VISIONQUERY_")}
    environment = inherited | {f"VISIONQUERY_{k.upper()}": v for k, v in variables.items()}
    environment |= extra or {}
    # Bound to loopback; the port is the command's own default, which must not be a fixed one.
    environment["VISIONQUERY_METRICS_HOST"] = "127.0.0.1"
    done = subprocess.run(
        [str(COMMAND)], env=environment, capture_output=True, text=True, timeout=60, check=False
    )
    return done.returncode, [json.loads(line) for line in done.stdout.splitlines()], done.stderr


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
    bare = parts._replace(netloc=f"{parts.username}@{parts.hostname}:{parts.port}").geturl()
    status, lines, stderr = _run(extra={"PGPASSWORD": parts.password}, database__url=bare)
    assert (status, stderr) == (0, "")
    assert any(m.startswith("migrated") for m in _messages(lines))
    assert parts.password not in json.dumps(lines)


def test_a_wrong_pgpassword_fails_without_showing_it(database: str) -> None:
    parts = urlsplit(database)
    bare = parts._replace(netloc=f"{parts.username}@{parts.hostname}:{parts.port}").geturl()
    status, lines, stderr = _run(extra={"PGPASSWORD": "wrongPW9"}, database__url=bare)
    assert status == 1
    (failed,) = [line for line in lines if line["level"] == "CRITICAL"]
    assert failed["error"] == "ConnectionFailedError"
    assert "wrongPW9" not in json.dumps(lines) + stderr


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


def test_a_missing_database_url_is_named() -> None:
    status, lines, _ = _run()
    assert status == 2
    (line,) = lines
    assert line["variables"] == ["VISIONQUERY_DATABASE__URL"]


def test_an_unreachable_database_is_reported_without_its_password(database: str) -> None:
    # A port nothing listens on, with a password that must not appear anywhere.
    parts = urlsplit(database)
    unreachable = parts._replace(netloc=f"vq:hunter2@{parts.hostname}:1").geturl()
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
    wrong = parts._replace(netloc=f"{parts.username}:wrongPW9@{parts.hostname}:{parts.port}")
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
