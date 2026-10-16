"""A real Postgres with TimescaleDB and pgvector for the memory's tests, and a database per test.

The server is the one named by `VISIONQUERY_TEST_DATABASE_URL`, a superuser's `postgresql://`
URL, when that is set. Otherwise the session starts a disposable container of `IMAGE` under a
name of its own, labelled `visionquery-test=1`, publishes it on an ephemeral port of the
loopback interface, and removes that container, and only that one, at the end. A session starts
by removing labelled containers over an hour old, which a killed session left behind.
Without either, every test that needs a database is skipped with the reason.

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
#: Every container this file starts carries it, and the sweep removes only what carries it.
LABEL = "visionquery-test=1"
STARTED_LABEL = "visionquery-test-started"
#: A container older than this belongs to a session that is gone; a younger one may be another
#: session running at the same time, which the sweep must not pull the database from under.
STALE_AFTER = 3600
#: Pulling the image the first time is the slow part.
START_TIMEOUT = 600


def _sweep_stale_containers() -> None:
    """Remove containers left by sessions that were killed before they could clean up."""
    listed = subprocess.run(
        [
            "docker", "ps", "--all", "--filter", f"label={LABEL}",
            "--format", '{{.ID}} {{.Label "' + STARTED_LABEL + '"}}',
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )  # fmt: skip
    for line in listed.stdout.splitlines():
        identifier, _, started = line.partition(" ")
        if started.isdigit() and time.time() - int(started) > STALE_AFTER:
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", identifier],
                capture_output=True,
                check=False,
                timeout=60,
            )


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return False
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
    _sweep_stale_containers()
    name = f"visionquery-test-postgres-{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(
            [
                "docker", "run", "--detach", "--rm", "--name", name,
                "--label", LABEL, "--label", f"{STARTED_LABEL}={int(time.time())}",
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
            text=True,
            timeout=START_TIMEOUT,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True)
        pytest.fail(
            f"starting {IMAGE} took over {START_TIMEOUT} s (pulling it the first time is slow): "
            f"pull it with `docker pull {IMAGE}`, or set {URL_VARIABLE}",
            pytrace=False,
        )
    except subprocess.CalledProcessError as error:
        pytest.fail(f"docker could not start {IMAGE}: {error.stderr.strip()}", pytrace=False)
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
