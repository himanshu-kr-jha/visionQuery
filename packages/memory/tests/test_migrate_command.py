"""`visionquery-migrate`, run the way a Deployment runs it: a process configured by the environment.

Each test runs the installed command in a child process and looks only at what an Operator
could see: its exit status and its JSON log lines.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest

COMMAND = Path(sys.executable).parent / "visionquery-migrate"


def _run(**variables: str) -> tuple[int, list[dict[str, Any]], str]:
    inherited = {k: v for k, v in os.environ.items() if not k.upper().startswith("VISIONQUERY_")}
    environment = inherited | {f"VISIONQUERY_{k.upper()}": v for k, v in variables.items()}
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


def test_a_missing_database_url_is_named(database: str) -> None:
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
    assert failed["error"] == "OperationalError"
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
    ],
)
def test_a_malformed_url_is_a_settings_error_that_never_shows_it(url: str) -> None:
    status, lines, stderr = _run(database__url=url)
    assert status == 2
    assert SECRET not in json.dumps(lines) + stderr
    (line,) = lines
    assert line["variables"] == ["VISIONQUERY_DATABASE__URL"]


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://u:{SECRET}@127.0.0.1:1/db?sslmode=bogus",
        f"postgresql://u:{SECRET}@127.0.0.1:1/db?nosuchoption=1",
        f"postgresql://u:{SECRET}@127.0.0.1:1/db?options=%zz",
        f"postgresql://u:{SECRET}@127.0.0.1:1/db?connect_timeout=%ZZ",
    ],
)
def test_a_url_libpq_cannot_read_fails_without_showing_it(url: str) -> None:
    status, lines, stderr = _run(database__url=url)
    assert status == 1
    assert SECRET not in json.dumps(lines) + stderr
    assert any(line["level"] == "CRITICAL" for line in lines)
