"""The Compose stack, brought up for real under a project of its own, and always taken down.

These tests are marked `stack`, so `make check` leaves them out and `make test-stack` runs
them. They need a running Docker daemon with the Compose plugin, and are skipped without one.

Each stack is a Compose project named `vqtest-<random>` with fresh secrets and every host
port ephemeral on loopback, so it never touches a stack a developer is running or anything
else on the host. Taking it down removes its containers, network and volumes. A session
starts by taking down `vqtest-` projects over an hour old, which a killed session left behind.

Triton's image is about 25 GB on disk, so its test runs only when `VISIONQUERY_TEST_TRITON` says how:
`gpu` reserves the host's GPUs as a Deployment does, `cpu` adds `compose.no-gpu.yaml` so that
Triton starts on a machine without one.
"""

import base64
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from visionquery.memory.migrate import MIGRATIONS

pytestmark = pytest.mark.stack

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
NO_GPU = ROOT / "compose.no-gpu.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
PREFIX = "vqtest-"
STALE_AFTER = 3600
#: Building the image, and pulling every other one the first time, is the slow part.
BUILD_TIMEOUT = 3600
#: How long `up --wait` gives every healthcheck once the images are present.
WAIT_TIMEOUT = 300
TRITON_MODE_VARIABLE = "VISIONQUERY_TEST_TRITON"
TRITON_MODE = os.environ.get(TRITON_MODE_VARIABLE, "")

# --- Bringing a stack up and down -----------------------------------------------------------


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return False
    return info.returncode == 0


def _sweep_stale_projects() -> None:
    """Take down test projects left by sessions that were killed before they could."""
    listed = subprocess.run(
        ["docker", "ps", "--all", "--format", '{{.ID}} {{.Label "com.docker.compose.project"}}'],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    projects: dict[str, list[str]] = {}
    for line in listed.stdout.splitlines():
        identifier, _, project = line.partition(" ")
        if project.startswith(PREFIX):
            projects.setdefault(project, []).append(identifier)
    for project, containers in projects.items():
        created = subprocess.run(
            ["docker", "container", "inspect", "--format", "{{.Created}}", *containers],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        ).stdout.split()
        newest = max((datetime.fromisoformat(c) for c in created), default=None)
        if newest is not None and (datetime.now(UTC) - newest).total_seconds() > STALE_AFTER:
            subprocess.run(
                ["docker", "compose", "-p", project, "down", "--volumes", "--remove-orphans"],
                capture_output=True,
                check=False,
                timeout=300,
            )


@pytest.fixture(scope="session", autouse=True)
def _docker() -> None:
    if not _docker_works():
        pytest.skip("needs a running Docker daemon with the Compose plugin")
    _sweep_stale_projects()


def _documented() -> dict[str, str]:
    variables: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            variables[name.strip()] = value.strip()
    return variables


@dataclass(frozen=True)
class Stack:
    """A running Compose project, and how to reach it from the host."""

    project: str
    files: tuple[Path, ...]
    profiles: tuple[str, ...]
    env: dict[str, str] = field(repr=False)

    def secret(self, name: str) -> str:
        return self.env[name]

    def compose(
        self, *args: str, timeout: float = 300, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose", "-p", self.project, "--project-directory", str(ROOT)]
        for file in self.files:
            command += ["-f", str(file)]
        # Never a developer's `.env`: every value comes from `.env.example` or `env`.
        command += ["--env-file", str(ENV_EXAMPLE)]
        for profile in self.profiles:
            command += ["--profile", profile]
        result = subprocess.run(
            [*command, *args],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"`docker compose {' '.join(args)}` exited {result.returncode}:\n"
                f"{result.stdout}\n{result.stderr}\n{self.diagnostics()}"
            )
        return result

    def address(self, service: str, port: int) -> tuple[str, int]:
        """Where on the host the service's container port is published."""
        published = self.compose("port", service, str(port)).stdout.strip()
        host, _, number = published.rpartition(":")
        return host, int(number)

    def url(self, service: str, port: int, path: str) -> str:
        host, number = self.address(service, port)
        return f"http://{host}:{number}{path}"

    def containers(self) -> dict[str, dict[str, Any]]:
        """Each service's container, as `docker compose ps --all` describes it."""
        listed = self.compose("ps", "--all", "--format", "json").stdout
        rows: list[dict[str, Any]] = [json.loads(line) for line in listed.splitlines() if line]
        return {row["Service"]: row for row in rows}

    def diagnostics(self) -> str:
        """What every container is doing and said last, for a failure message."""
        ps = self.compose("ps", "--all", check=False).stdout
        logs = self.compose("logs", "--no-color", "--tail", "30", check=False).stdout
        return f"--- ps ---\n{ps}\n--- logs ---\n{logs}"


def _one_shot(stack: Stack) -> list[str]:
    """The services that run once and exit, such as `migrate`."""
    services = json.loads(stack.compose("config", "--format", "json").stdout)["services"]
    return sorted(name for name, s in services.items() if s.get("restart", "no") == "no")


@contextmanager
def running_stack(profiles: Iterable[str] = (), files: Iterable[Path] = ()) -> Generator[Stack]:
    """Bring the stack up with these profiles, wait for every healthcheck, then take it down."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VISIONQUERY_", "COMPOSE_"))}
    for name, value in _documented().items():
        if not value:
            env[name] = secrets.token_hex(16)
        elif name.endswith("_PORT"):
            env[name] = "0"  # an ephemeral port, so nothing on the host is ever in the way
    stack = Stack(
        project=f"{PREFIX}{uuid.uuid4().hex[:10]}",
        files=(COMPOSE, *files),
        profiles=tuple(profiles),
        env=env,
    )
    try:
        stack.compose("build", timeout=BUILD_TIMEOUT)
        stack.compose("pull", "--ignore-buildable", "--quiet", timeout=BUILD_TIMEOUT)
        started = time.monotonic()
        stack.compose("up", "--detach", "--wait", "--wait-timeout", str(WAIT_TIMEOUT))
        # `--wait` counts a one-shot job that is still running as started, so wait for it to
        # finish; its exit status is for the tests to judge.
        stack.compose("wait", *_one_shot(stack), timeout=WAIT_TIMEOUT, check=False)
        print(f"{stack.project} {stack.profiles} healthy in {time.monotonic() - started:.1f} s")
        yield stack
    finally:
        stack.compose("down", "--volumes", "--remove-orphans", timeout=300, check=False)


def wait_for[T](probe: Callable[[], tuple[T | None, str]], timeout: float) -> T:
    """Call `probe` until it returns something, or fail with what it last said was missing."""
    deadline = time.monotonic() + timeout
    while True:
        result, why = probe()
        if result is not None:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"gave up after {timeout} s: {why}")
        time.sleep(1)


def _get(
    url: str, credentials: tuple[str, str] | None = None, data: bytes | None = None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data)
    if credentials is not None:
        token = base64.b64encode(":".join(credentials).encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _assert_healthy(stack: Stack, services: set[str]) -> None:
    containers = stack.containers()
    unhealthy = {
        name: (c["State"], c["Health"])
        for name, c in containers.items()
        if name in services and (c["State"], c["Health"]) != ("running", "healthy")
    }
    assert unhealthy == {}, stack.diagnostics()


# --- The default stack: no profile, no GPU --------------------------------------------------


@pytest.fixture(scope="module")
def default_stack() -> Iterator[Stack]:
    with running_stack() as stack:
        yield stack


def test_the_default_stack_starts_postgres_and_mediamtx_healthy(default_stack: Stack) -> None:
    containers = default_stack.containers()
    assert set(containers) == {"postgres", "migrate", "mediamtx"}
    _assert_healthy(default_stack, {"postgres", "mediamtx"})


def test_migrate_ran_once_and_succeeded(default_stack: Stack) -> None:
    migrate = default_stack.containers()["migrate"]
    assert (migrate["State"], migrate["ExitCode"]) == ("exited", 0), default_stack.diagnostics()


def _database_url(stack: Stack) -> str:
    host, port = stack.address("postgres", 5432)
    password = stack.secret("VISIONQUERY_POSTGRES_PASSWORD")
    return f"postgresql://visionquery:{password}@{host}:{port}/visionquery"


def test_the_memory_has_its_extensions_and_every_migration(default_stack: Stack) -> None:
    with psycopg.connect(_database_url(default_stack), connect_timeout=10) as connection:
        extensions = {row[0] for row in connection.execute("SELECT extname FROM pg_extension")}
        applied = [row[0] for row in connection.execute("SELECT name FROM schema_migrations")]
    assert {"timescaledb", "vector"} <= extensions
    assert sorted(applied) == sorted(p.name for p in MIGRATIONS.glob("[0-9]*.sql"))


def test_postgres_sends_no_telemetry(default_stack: Stack) -> None:
    # A Deployment is closed (ADR 0001, ADR 0002): nothing phones home.
    with psycopg.connect(_database_url(default_stack), connect_timeout=10) as connection:
        level = connection.execute("SHOW timescaledb.telemetry_level").fetchone()
    assert level == ("off",)


def test_migrate_is_safe_to_run_again(default_stack: Stack) -> None:
    rerun = default_stack.compose("run", "--rm", "--no-deps", "migrate", check=False)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr


def test_visionquery_processes_do_not_run_as_root(default_stack: Stack) -> None:
    whoami = default_stack.compose("run", "--rm", "--no-deps", "migrate", "id", "-u")
    assert whoami.stdout.strip() not in {"", "0"}


def test_mediamtx_api_answers_the_visionquery_user_only(default_stack: Stack) -> None:
    url = default_stack.url("mediamtx", 9997, "/v3/paths/list")
    password = default_stack.secret("VISIONQUERY_MEDIAMTX_API_PASSWORD")
    status, body = _get(url, ("visionquery", password))
    assert status == 200, body
    assert json.loads(body)["items"] == []
    assert _get(url)[0] == 401
    assert _get(url, ("visionquery", "wrong"))[0] == 401


# --- The observability profile --------------------------------------------------------------

#: Every Prometheus job with a target in the stack without `gpu`.
OBSERVED_JOBS = {"prometheus", "grafana", "postgres", "mediamtx"}


@pytest.fixture(scope="module")
def observed_stack() -> Iterator[Stack]:
    with running_stack(profiles=["observability"]) as stack:
        yield stack


def test_observability_adds_prometheus_and_grafana_healthy(observed_stack: Stack) -> None:
    _assert_healthy(observed_stack, {"prometheus", "grafana", "postgres-exporter"})


def test_prometheus_sees_every_target_up(observed_stack: Stack) -> None:
    url = observed_stack.url("prometheus", 9090, "/api/v1/targets?state=active")

    def every_target_up() -> tuple[dict[str, str] | None, str]:
        status, body = _get(url)
        if status != 200:
            return None, f"Prometheus answered {status}"
        targets = json.loads(body)["data"]["activeTargets"]
        health = {t["labels"]["job"]: t["health"] for t in targets}
        if set(health) != OBSERVED_JOBS or set(health.values()) != {"up"}:
            return None, f"targets are {health}, every one of {sorted(OBSERVED_JOBS)} is wanted up"
        return health, ""

    wait_for(every_target_up, timeout=90)


def test_grafana_reaches_prometheus_through_its_provisioned_datasource(
    observed_stack: Stack,
) -> None:
    url = observed_stack.url("grafana", 3000, "/api/datasources/uid/prometheus/health")
    password = observed_stack.secret("VISIONQUERY_GRAFANA_ADMIN_PASSWORD")
    status, body = _get(url, ("admin", password))
    assert status == 200, body
    assert json.loads(body)["status"] == "OK"


# --- The gpu profile ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def triton_stack() -> Iterator[Stack]:
    if TRITON_MODE not in {"gpu", "cpu"}:
        pytest.skip(
            f"Triton's image is about 25 GB on disk: set {TRITON_MODE_VARIABLE}=gpu, or =cpu on a "
            "machine without a GPU, to run it"
        )
    with running_stack(profiles=["gpu"], files=[NO_GPU] if TRITON_MODE == "cpu" else []) as stack:
        yield stack


def test_the_gpu_profile_adds_triton_healthy_with_an_empty_model_repository(
    triton_stack: Stack,
) -> None:
    _assert_healthy(triton_stack, {"postgres", "mediamtx", "triton"})
    assert _get(triton_stack.url("triton", 8000, "/v2/health/ready"))[0] == 200
    status, body = _get(triton_stack.url("triton", 8000, "/v2/repository/index"), data=b"{}")
    assert status == 200, body
    assert json.loads(body) == []
