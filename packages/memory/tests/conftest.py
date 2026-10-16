"""A real Postgres with TimescaleDB and pgvector for the memory's tests, and a database per test.

The server is the one named by `VISIONQUERY_TEST_DATABASE_URL`, a superuser's `postgresql://`
URL, when that is set. Otherwise the session starts a disposable container of `IMAGE` under a
name of its own, publishes it on an ephemeral port of the loopback interface, and removes that
container, and only that one, at the end. Without either, every test that needs a database is
skipped with the reason.

Each test that asks for `database` gets a database of its own, created from `template0` so
that no extension is installed in it beforehand, and dropped afterwards.
"""

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from urllib.parse import quote, urlsplit

import psycopg
import pytest
from psycopg import sql

#: TimescaleDB at the version the Deployment runs, on PostgreSQL 18, with pgvector bundled.
IMAGE = "timescale/timescaledb:2.30.1-pg18"
URL_VARIABLE = "VISIONQUERY_TEST_DATABASE_URL"
PASSWORD = "visionquery-test"


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    info = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30)
    return info.returncode == 0


def _wait_until_ready(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with psycopg.connect(url, connect_timeout=2) as connection:
                connection.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.25)


@pytest.fixture(scope="session")
def postgres() -> Iterator[str]:
    """A superuser's `postgresql://` URL to a server with TimescaleDB and pgvector available."""
    if url := os.environ.get(URL_VARIABLE):
        yield url
        return
    if not _docker_works():
        pytest.skip(f"needs Postgres: set {URL_VARIABLE} or make Docker available")
    name = f"visionquery-test-postgres-{uuid.uuid4().hex[:12]}"
    subprocess.run(
        [
            "docker", "run", "--detach", "--rm", "--name", name,
            "--env", f"POSTGRES_PASSWORD={PASSWORD}",
            # An ephemeral port on loopback only, so no port anyone else uses is touched.
            "--publish", "127.0.0.1::5432",
            IMAGE,
            # Durability is not what is being tested; speed is welcome.
            "postgres", "-c", "fsync=off", "-c", "synchronous_commit=off",
            "-c", "full_page_writes=off", "-c", "timescaledb.telemetry_level=off",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    try:
        published = subprocess.run(
            ["docker", "port", name, "5432/tcp"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()[0]
        host, port = published.rsplit(":", 1)
        url = f"postgresql://postgres:{quote(PASSWORD)}@{host}:{port}/postgres"
        # The image's entrypoint initialises on a socket only, then restarts listening on TCP,
        # so the first TCP connection is to the server that stays.
        _wait_until_ready(url, timeout=90)
        yield url
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True)


@pytest.fixture
def database(postgres: str) -> Iterator[str]:
    """A URL to a new, empty database of its own on `postgres`, dropped after the test."""
    name = f"test_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(postgres, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    try:
        yield urlsplit(postgres)._replace(path=f"/{name}").geturl()
    finally:
        with psycopg.connect(postgres, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
