"""The Compose stack, brought up for real under a project of its own, and always taken down.

These tests are marked `stack`, so `make check` leaves them out and `make test-stack` runs
them. They need a running Docker daemon with the Compose plugin, and are skipped without one.

Each stack is a Compose project named `vqtest-<random>` with fresh secrets and every host
port ephemeral on loopback, so it never touches a stack a developer is running or anything
else on the host. Taking it down removes its containers, network and volumes. A session
starts by taking down `vqtest-` projects older than twice the build timeout, which a killed
session left behind (a younger one may be another session's, running concurrently).

Images are built only through `make up`, which turns BuildKit's attestations off (see the
Makefile); an image built any other way gets a new ID on every build, and the tests that check
that nothing is recreated would then fail. Every image is named for its project, so concurrent
sessions never touch one another's.

Triton's image is about 25 GB on disk, so its test runs only when
`VISIONQUERY_TEST_TRITON` says how: `gpu` reserves the host's GPUs as a Deployment does, `cpu`
adds `compose.no-gpu.yaml` so that Triton starts on a machine without one.
"""

import base64
import json
import math
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
from fake_cameras import parse_cameras, prepare_command
from visionquery.memory.cameras import CameraRegistry, PublishCredentials
from visionquery.memory.migrate import MIGRATIONS

pytestmark = pytest.mark.stack

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
NO_GPU = ROOT / "compose.no-gpu.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
PREFIX = "vqtest-"
#: Building the image, and pulling every other one the first time, is the slow part.
BUILD_TIMEOUT = 3600
#: How long `up --wait` gives every healthcheck once the images are present.
WAIT_TIMEOUT = 300
#: A session younger than this may still be building, so it is not swept as stale.
STALE_AFTER = 2 * (BUILD_TIMEOUT + WAIT_TIMEOUT)
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
    #: The tree the project is built from, which is the repository unless a test copied it.
    root: Path = ROOT

    def secret(self, name: str) -> str:
        return self.env[name]

    def command(self) -> list[str]:
        """`docker compose` aimed at this project, its files and its profiles."""
        command = ["docker", "compose", "-p", self.project, "--project-directory", str(self.root)]
        for file in self.files:
            command += ["-f", str(file)]
        # Never a developer's `.env`: every value comes from `.env.example` or `env`.
        command += ["--env-file", str(ENV_EXAMPLE)]
        for profile in self.profiles:
            command += ["--profile", profile]
        return command

    def image(self, name: str) -> str:
        """The image Compose builds for `app` (every VisionQuery process), `mediamtx` or
        `fake-cameras`."""
        tag = {"app": "dev", "mediamtx": "1.21.1", "fake-cameras": "dev"}[name]
        return f"{self.project}-{name}:{tag}"

    def make(
        self, target: str, *variables: str, timeout: float = 600
    ) -> subprocess.CompletedProcess[str]:
        """A target of the Makefile, run against this project."""
        return subprocess.run(
            [
                "make",
                "--no-print-directory",
                target,
                f"COMPOSE={shlex.join(self.command())}",
                *variables,
            ],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def compose(
        self, *args: str, timeout: float = 300, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [*self.command(), *args],
            cwd=self.root,
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


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _new_stack(
    profiles: Iterable[str] = (),
    files: Iterable[Path] = (),
    environment: Mapping[str, str] | None = None,
    root: Path = ROOT,
) -> Stack:
    """A project of its own, with fresh secrets and every host port ephemeral."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VISIONQUERY_", "COMPOSE_"))}
    for name, value in _documented().items():
        if not value:
            env[name] = secrets.token_hex(16)
        elif name == "VISIONQUERY_MEDIAMTX_WEBRTC_UDP_PORT":
            # MediaMTX announces this number to browsers, so it is the same inside and out and
            # cannot be ephemeral: take one that is free now.
            env[name] = str(_free_udp_port())
        elif name.endswith("_PORT"):
            env[name] = "0"  # an ephemeral port, so nothing on the host is ever in the way
    env |= environment or {}
    return Stack(
        project=f"{PREFIX}{uuid.uuid4().hex[:10]}",
        files=(root / "compose.yaml", *files),
        profiles=tuple(profiles),
        env=env,
        root=root,
    )


@contextmanager
def running_stack(
    profiles: Iterable[str] = (),
    files: Iterable[Path] = (),
    environment: Mapping[str, str] | None = None,
) -> Generator[Stack]:
    """Bring the stack up with `make up`, which builds, waits for every healthcheck and for
    `migrate` to succeed, then take it down."""
    stack = _new_stack(profiles, files, environment)
    try:
        stack.compose("pull", "--ignore-buildable", "--quiet", timeout=BUILD_TIMEOUT)
        started = time.monotonic()
        # `make up` builds first, so the image is built once, with the attestations off.
        up = stack.make("up", timeout=BUILD_TIMEOUT + WAIT_TIMEOUT)
        if up.returncode != 0:
            raise AssertionError(
                f"`make up` failed:\n{up.stdout}\n{up.stderr}\n{stack.diagnostics()}"
            )
        print(f"{stack.project} {stack.profiles} healthy in {time.monotonic() - started:.1f} s")
        yield stack
    finally:
        _remove(stack)


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


def _healthy(stack: Stack, services: set[str]) -> bool:
    containers = stack.containers()
    return all((containers[n]["State"], containers[n]["Health"]) == ("running", "healthy")
               for n in services)  # fmt: skip


def _assert_healthy(stack: Stack, services: set[str]) -> None:
    containers = stack.containers()
    unhealthy = {
        name: (c["State"], c["Health"])
        for name, c in containers.items()
        if name in services and (c["State"], c["Health"]) != ("running", "healthy")
    }
    assert unhealthy == {}, stack.diagnostics()


# --- The default stack: no profile, no GPU --------------------------------------------------


#: Every character that breaks a URL, and a space, a quote and a backslash for good measure.
AWKWARD_PASSWORD = "p@ss/w:rd#1?%&= '\"\\"


@pytest.fixture(scope="module")
def default_stack() -> Iterator[Stack]:
    # A password full of special characters: the stack must not care what the password is.
    environment = {"VISIONQUERY_POSTGRES_PASSWORD": AWKWARD_PASSWORD}
    with running_stack(environment=environment) as stack:
        yield stack


def test_the_default_stack_starts_postgres_mediamtx_its_reconciler_and_the_api_healthy(
    default_stack: Stack,
) -> None:
    containers = default_stack.containers()
    assert set(containers) == {"postgres", "migrate", "mediamtx", "reconciler", "api"}
    _assert_healthy(default_stack, {"postgres", "mediamtx", "reconciler", "api"})


def _health(stack: Stack) -> tuple[int, dict[str, Any]]:
    status, body = _get(stack.url("api", 8080, "/health"))
    return status, json.loads(body)


def test_the_api_reaches_the_database_with_the_awkward_password(default_stack: Stack) -> None:
    assert _health(default_stack) == (
        200,
        {"status": "healthy", "database": {"status": "connected", "problem": None}},
    )


def test_the_api_logs_json_lines_only(default_stack: Stack) -> None:
    # uvicorn runs with log_config=None, so its lines go through the runtime's JSON handler.
    logs = default_stack.compose("logs", "--no-color", "--no-log-prefix", "api").stdout
    lines = [json.loads(line) for line in logs.splitlines() if line.strip()]
    assert any(line["logger"].startswith("uvicorn") for line in lines), logs
    assert {line["service"] for line in lines} == {"api"}


def test_the_api_says_when_the_database_is_down_and_recovers(default_stack: Stack) -> None:
    default_stack.compose("stop", "postgres")
    try:
        # The api reuses its last database answer for a second, and one check in flight for up
        # to five (DatabaseProbe), so it may say 200 for a moment after the database stops.
        def unhealthy() -> tuple[tuple[int, dict[str, Any]] | None, str]:
            status, body = _health(default_stack)
            return ((status, body), "") if status == 503 else (None, f"/health answered {status}")

        status, body = wait_for(unhealthy, timeout=15)
        assert status == 503, body
        assert body["status"] == "unhealthy"
        assert body["database"]["status"] == "unreachable"
        assert body["database"]["problem"] in {
            "host name could not be resolved",
            "connection refused or host unreachable",
            "timed out",
        }
        assert default_stack.secret("VISIONQUERY_POSTGRES_PASSWORD") not in json.dumps(body)
    finally:
        default_stack.compose("up", "--detach", "--wait", "postgres", timeout=WAIT_TIMEOUT)

    def healthy() -> tuple[int | None, str]:
        status, body = _health(default_stack)
        return (status, "") if status == 200 else (None, f"/health answered {status}: {body}")

    wait_for(healthy, timeout=60)


def test_migrate_ran_once_and_succeeded(default_stack: Stack) -> None:
    migrate = default_stack.containers()["migrate"]
    assert (migrate["State"], migrate["ExitCode"]) == ("exited", 0), default_stack.diagnostics()


def _database_url(stack: Stack) -> str:
    host, port = stack.address("postgres", 5432)
    password = quote(stack.secret("VISIONQUERY_POSTGRES_PASSWORD"), safe="")
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


def test_migrate_status_lists_every_migration_as_applied(default_stack: Stack) -> None:
    # As CLAUDE.md tells an Operator to run it against the stack.
    found = default_stack.compose(
        "run", "--rm", "--no-deps", "migrate", "visionquery-migrate", "status", check=False
    )
    assert found.returncode == 0, found.stdout + found.stderr
    lines = [json.loads(line) for line in found.stdout.splitlines() if line.startswith("{")]
    (summary,) = [line for line in lines if line["message"].startswith("status:")]
    assert sorted(summary["applied"]) == sorted(p.name for p in MIGRATIONS.glob("[0-9]*.sql"))
    assert summary["pending"] == []


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


#: The reconciler's cycle in the stack (VISIONQUERY_RECONCILE_INTERVAL), and what a cycle may
#: take beyond it on a busy machine.
RECONCILE_INTERVAL = 5
RECONCILE_SLACK = 10


@contextmanager
def _registry(stack: Stack) -> Generator[CameraRegistry]:
    """The stack's camera registry, as the api will reach it."""
    with psycopg.connect(_database_url(stack), connect_timeout=10, autocommit=True) as connection:
        yield CameraRegistry(connection)


def _wait_for_paths(stack: Stack, wanted: set[str], timeout: float) -> None:
    """Until MediaMTX has exactly the paths `wanted` (it may be restarting meanwhile)."""
    password = stack.secret("VISIONQUERY_MEDIAMTX_API_PASSWORD")

    def matches() -> tuple[bool | None, str]:
        try:
            url = stack.url("mediamtx", 9997, "/v3/config/paths/list")
            status, body = _get(url, ("visionquery", password))
        except (AssertionError, OSError) as error:
            return None, f"MediaMTX's API cannot be reached: {error}"
        if status != 200:
            return None, f"MediaMTX's API answered {status}"
        paths = {item["name"] for item in json.loads(body)["items"]}
        return (True, "") if paths == wanted else (None, f"MediaMTX has {sorted(paths)}")

    wait_for(matches, timeout)


def test_cameras_added_and_removed_reach_mediamtx_and_survive_its_restart(
    default_stack: Stack,
) -> None:
    # The reconciler, in the stack as a Deployment runs it, against the stack's own registry.
    credentials = PublishCredentials.of("dock", "dock-pa55word-stack")
    cycle = RECONCILE_INTERVAL + RECONCILE_SLACK
    with _registry(default_stack) as registry:
        dock = registry.add_pushed("Dock", "dock", credentials)
        gate = registry.add_pulled("Gate", "rtsp://admin:pw@192.0.2.7/stream")
        try:
            _wait_for_paths(default_stack, {"dock", gate.path}, cycle)
            # MediaMTX forgets what its API was told when it restarts; the reconciler puts it
            # back by itself.
            default_stack.compose("restart", "mediamtx")
            _wait_for_paths(default_stack, {"dock", gate.path}, cycle)
        finally:
            registry.remove(dock.id)
            registry.remove(gate.id)
    _wait_for_paths(default_stack, set(), cycle)
    wait_for(
        lambda: (True, "") if _healthy(default_stack, {"mediamtx", "reconciler"}) else (None, ""),
        60,
    )
    logs = default_stack.compose("logs", "--no-color", "reconciler").stdout
    assert "reconciled" in logs
    assert "admin:pw" not in logs


def test_mediamtx_advertises_a_host_a_browser_can_send_webrtc_media_to(
    default_stack: Stack,
) -> None:
    url = default_stack.url("mediamtx", 9997, "/v3/config/global/get")
    password = default_stack.secret("VISIONQUERY_MEDIAMTX_API_PASSWORD")
    status, body = _get(url, ("visionquery", password))
    assert status == 200, body
    config = json.loads(body)
    assert config["webrtcAdditionalHosts"] == ["127.0.0.1"]
    udp = default_stack.env["VISIONQUERY_MEDIAMTX_WEBRTC_UDP_PORT"]
    assert config["webrtcLocalUDPAddress"] == f":{udp}"
    # The port it announces is the one published on the host.
    published = default_stack.compose("port", "--protocol", "udp", "mediamtx", udp).stdout
    assert published.strip().endswith(f":{udp}"), published


def test_the_awkward_password_reached_both_postgres_and_the_migrations(
    default_stack: Stack,
) -> None:
    assert default_stack.secret("VISIONQUERY_POSTGRES_PASSWORD") == AWKWARD_PASSWORD
    # The migrations connected with it, from another container, through PGPASSWORD.
    assert default_stack.containers()["migrate"]["ExitCode"] == 0


def _image_id(reference: str) -> str:
    return subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_up_on_an_unchanged_tree_recreates_nothing(default_stack: Stack) -> None:
    # `make up` builds first, and the build must give the same image ID when nothing changed
    # (no attestation, whose timestamp would change it), or Compose recreates migrate and
    # mediamtx, which drops live streams. `make up` also runs migrate again, which is safe.
    def identifiers() -> dict[str, str]:
        return {name: c["ID"] for name, c in default_stack.containers().items()}

    before, image = identifiers(), _image_id(default_stack.image("app"))
    for _ in range(2):
        up = default_stack.make("up")
        assert up.returncode == 0, up.stdout + up.stderr
        assert _image_id(default_stack.image("app")) == image
        assert identifiers() == before
    default_stack.compose("up", "--detach")
    assert identifiers() == before
    assert default_stack.containers()["migrate"]["ExitCode"] == 0


def test_make_up_runs_changed_code_and_recreates_only_what_changed(tmp_path: Path) -> None:
    # A copy of what the build uses, never the repository itself.
    root = tmp_path / "tree"
    root.mkdir()
    for name in ("Makefile", "Dockerfile", "compose.yaml", "pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / name, root / name)
    shutil.copy(ROOT / ".python-version", root / ".python-version")
    shutil.copy(ROOT / ".dockerignore", root / ".dockerignore")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "packages", root / "packages", ignore=ignore)
    shutil.copytree(ROOT / "docker" / "mediamtx", root / "docker" / "mediamtx")
    stack = _new_stack(root=root)
    image = stack.image("app")
    try:
        up = stack.make("up", timeout=BUILD_TIMEOUT)
        assert up.returncode == 0, up.stdout + up.stderr + stack.diagnostics()
        before = {name: c["ID"] for name, c in stack.containers().items()}
        first_image = _image_id(image)
        marker = "migrated-after-the-edit"
        assert marker not in stack.compose("logs", "migrate").stdout

        source = root / "packages/memory/src/visionquery/memory/migrate.py"
        edited = source.read_text().replace('"migrated: %d applied"', f'"{marker}: %d applied"')
        assert edited != source.read_text()
        source.write_text(edited)
        up = stack.make("up", timeout=BUILD_TIMEOUT)
        assert up.returncode == 0, up.stdout + up.stderr + stack.diagnostics()

        assert _image_id(image) != first_image
        assert marker in stack.compose("logs", "migrate").stdout
        after = {name: c["ID"] for name, c in stack.containers().items()}
        assert after["migrate"] != before["migrate"]
        assert after["mediamtx"] == before["mediamtx"]
        assert after["postgres"] == before["postgres"]
    finally:
        _remove(stack)


def test_make_up_fails_visibly_when_the_migrations_fail(tmp_path: Path) -> None:
    broken = tmp_path / "compose.broken-migrate.yaml"
    broken.write_text(
        "services:\n  migrate:\n    environment:\n"
        "      VISIONQUERY_DATABASE__URL: postgresql://visionquery@127.0.0.1:1/visionquery\n"
    )
    stack = _new_stack(files=[broken])
    try:
        stack.compose("pull", "--ignore-buildable", "--quiet", timeout=BUILD_TIMEOUT)
        up = stack.make("up", timeout=BUILD_TIMEOUT + WAIT_TIMEOUT)
        assert up.returncode != 0, up.stdout + up.stderr
        assert "migrations failed" in up.stderr, up.stdout + up.stderr
        assert stack.containers()["migrate"]["ExitCode"] != 0
    finally:
        _remove(stack)


def test_make_up_gives_up_on_migrations_that_never_finish(tmp_path: Path) -> None:
    slow = tmp_path / "compose.slow-migrate.yaml"
    slow.write_text('services:\n  migrate:\n    command: ["sleep", "600"]\n')
    stack = _new_stack(files=[slow])
    try:
        stack.compose("pull", "--ignore-buildable", "--quiet", timeout=BUILD_TIMEOUT)
        started = time.monotonic()
        up = stack.make("up", "MIGRATE_TIMEOUT=5", timeout=BUILD_TIMEOUT + WAIT_TIMEOUT)
        assert up.returncode != 0, up.stdout + up.stderr
        assert "did not finish within 5 s" in up.stderr, up.stdout + up.stderr
        assert time.monotonic() - started < 300
    finally:
        _remove(stack)


def _remove(stack: Stack) -> None:
    """Take a project down whatever its profiles, and delete its data."""
    stack.compose(
        "--profile", "*", "down", "--volumes", "--remove-orphans", timeout=300, check=False
    )
    for image in (stack.image(name) for name in ("app", "mediamtx", "fake-cameras")):
        subprocess.run(["docker", "image", "rm", image], capture_output=True, check=False)


def _leftovers(stack: Stack) -> dict[str, list[str]]:
    """Every container, network and volume Docker still holds for the project."""
    label = f"label=com.docker.compose.project={stack.project}"
    found: dict[str, list[str]] = {}
    for kind, command, field_name in (
        ("containers", ["ps", "--all"], "Names"),
        ("networks", ["network", "ls"], "Name"),
        ("volumes", ["volume", "ls"], "Name"),
    ):
        found[kind] = subprocess.run(
            ["docker", *command, "--filter", label, "--format", f"{{{{.{field_name}}}}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    return found


def test_make_down_stops_profile_services_and_keeps_the_data_unless_told() -> None:
    # Triton's profile (gpu, on the CPU here when VISIONQUERY_TEST_TRITON=cpu) is not enabled
    # for `down` by anything but `--profile '*'`; a plain `down` would leave it holding the GPU.
    profiles = ["observability"]
    files: list[Path] = []
    if TRITON_MODE in {"gpu", "cpu"}:
        profiles.append("gpu")
        files = [NO_GPU] if TRITON_MODE == "cpu" else []
    stack = _new_stack(profiles=profiles, files=files)
    try:
        stack.compose("pull", "--ignore-buildable", "--quiet", timeout=BUILD_TIMEOUT)
        up = stack.make("up", timeout=BUILD_TIMEOUT + WAIT_TIMEOUT)
        assert up.returncode == 0, up.stdout + up.stderr + stack.diagnostics()
        assert _leftovers(stack)["containers"]

        # Without the profiles on the command line, as a developer who `up`ed with
        # COMPOSE_PROFILES in `.env` and later lost it from their shell would run it.
        bare = replace(stack, profiles=())
        down = bare.make("down", timeout=300)
        assert down.returncode == 0, down.stdout + down.stderr
        left = _leftovers(stack)
        assert left["containers"] == []
        assert left["networks"] == []
        assert left["volumes"], "make down must keep the data"

        wiped = bare.make("down", "VOLUMES=1", timeout=300)
        assert wiped.returncode == 0, wiped.stdout + wiped.stderr
        assert _leftovers(stack) == {"containers": [], "networks": [], "volumes": []}
    finally:
        _remove(stack)


def test_concurrent_projects_do_not_touch_each_others_images_or_containers() -> None:
    # Image tags are global to the daemon and Compose stamps the project into every image it
    # builds, so projects sharing a tag would each give it a new ID, and each one's next
    # `make up` would recreate its containers. Two projects build and come up at once, then go
    # up again at once, then one goes down while the other keeps running.
    first, second = _new_stack(), _new_stack()
    stacks = (first, second)
    try:
        for stack in stacks:
            stack.compose("pull", "--ignore-buildable", "--quiet", timeout=BUILD_TIMEOUT)

        def up(stack: Stack) -> subprocess.CompletedProcess[str]:
            return stack.make("up", timeout=BUILD_TIMEOUT + WAIT_TIMEOUT)

        def state(stack: Stack) -> tuple[dict[str, str], dict[str, str]]:
            containers = {name: c["ID"] for name, c in stack.containers().items()}
            images = {n: _image_id(stack.image(n)) for n in ("app", "mediamtx")}
            return containers, images

        with ThreadPoolExecutor(max_workers=2) as pool:
            for result in pool.map(up, stacks):
                assert result.returncode == 0, result.stdout + result.stderr
            before = [state(stack) for stack in stacks]
            for result in pool.map(up, stacks):
                assert result.returncode == 0, result.stdout + result.stderr
        assert [state(stack) for stack in stacks] == before
        # Each project has images of its own, not one tag between them.
        assert first.image("app") != second.image("app")

        down = first.make("down", "VOLUMES=1", timeout=300)
        assert down.returncode == 0, down.stdout + down.stderr
        assert _leftovers(first)["containers"] == []
        assert state(second) == before[1]
        _assert_healthy(second, {"postgres", "mediamtx"})
    finally:
        for stack in stacks:
            _remove(stack)


# --- The observability profile --------------------------------------------------------------

#: Every Prometheus job with a target in the stack without `gpu`.
OBSERVED_JOBS = {"prometheus", "grafana", "postgres", "mediamtx", "reconciler", "api"}


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


def test_prometheus_logs_no_failed_lookup_without_the_gpu_profile(observed_stack: Stack) -> None:
    # A job looking up `triton` without Triton would log a DNS error every 15 s.
    # Wait until Prometheus has looked every DNS job up again, which is a refresh.
    url = observed_stack.url(
        "prometheus", 9090, "/api/v1/query?query=sum(prometheus_sd_dns_lookups_total)"
    )

    def lookups() -> float:
        status, body = _get(url)
        assert status == 200, body
        result = json.loads(body)["data"]["result"]
        return float(result[0]["value"][1]) if result else 0.0

    first = lookups()
    jobs = len(OBSERVED_JOBS)  # each is looked up in DNS once per refresh

    def refreshed() -> tuple[float | None, str]:
        now = lookups()
        return (now, "") if now >= first + jobs else (None, f"lookups still at {now}")

    wait_for(refreshed, timeout=60)
    logs = observed_stack.compose("logs", "--no-color", "prometheus").stdout
    assert "triton" not in logs, logs


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
    profiles = ["gpu", "observability"]
    with running_stack(profiles=profiles, files=[NO_GPU] if TRITON_MODE == "cpu" else []) as stack:
        yield stack


def test_the_gpu_profile_adds_triton_healthy_with_an_empty_model_repository(
    triton_stack: Stack,
) -> None:
    _assert_healthy(triton_stack, {"postgres", "mediamtx", "triton"})
    assert _get(triton_stack.url("triton", 8000, "/v2/health/ready"))[0] == 200
    status, body = _get(triton_stack.url("triton", 8000, "/v2/repository/index"), data=b"{}")
    assert status == 200, body
    assert json.loads(body) == []


def test_prometheus_finds_triton_through_the_file_the_gpu_profile_writes(
    triton_stack: Stack,
) -> None:
    url = triton_stack.url("prometheus", 9090, "/api/v1/targets?state=active")

    def triton_up() -> tuple[str | None, str]:
        status, body = _get(url)
        if status != 200:
            return None, f"Prometheus answered {status}"
        health = {
            t["labels"]["job"]: t["health"] for t in json.loads(body)["data"]["activeTargets"]
        }
        if health.get("triton") != "up":
            return None, f"targets are {health}, triton is wanted up"
        return "up", ""

    wait_for(triton_up, timeout=90)


# --- The dev profile: fake cameras ------------------------------------------------------------

#: ffmpeg's static build, which the fake cameras' image copies its ffmpeg from; the tests use
#: its ffmpeg to make a sample and its ffprobe to read what MediaMTX serves.
FFMPEG_IMAGE = next(
    line.split()[1]
    for line in (ROOT / "docker" / "fake-cameras" / "Dockerfile").read_text().splitlines()
    if line.startswith("FROM mwader/static-ffmpeg:")
)
#: Samples made by the test, so that cameras loop real video files with no download. Made with
#: x264's defaults, which include B-frames, so that preparing them has something to remove.
#: Their picture is flat grey that brightens steadily over MARKER_PERIOD seconds and starts
#: over, so that the point of the loop a camera is at can be read from any single frame.
SAMPLE = "sample.mp4"
#: Another sample, a little longer, as MEVA's cameras' recordings are of one another's.
LONGER_SAMPLE = "longer-sample.mp4"
SAMPLE_SECONDS = {SAMPLE: 20.4, LONGER_SAMPLE: 21.0}
SAMPLE_SIZE = (320, 240)
SAMPLE_RATE = 10
#: What the cameras cut every sample to (VISIONQUERY_FAKE_CAMERA_DURATION), which is also the
#: period of the marker.
MARKER_PERIOD = 20
#: The luma range of video (16 to 235), which the marker spans.
LUMA_LOW, LUMA_RANGE = 16, 219
#: The cameras of the dev stack: two looping the same sample and one a longer one, two test
#: patterns, and one whose video was never fetched.
DEV_CAMERAS = (
    f"gate={SAMPLE},gate-again={SAMPLE},hall={LONGER_SAMPLE},"
    "lobby=synthetic,yard=synthetic,missing=not-fetched.mp4"
)
#: The cameras of DEV_CAMERAS that loop a sample, which are on one timeline.
TIMELINE_CAMERAS = ("gate", "gate-again", "hall")
#: The test pattern's size, which a camera without its video shows.
TEST_PATTERN_SIZE = (1280, 720)
SAMPLES_VARIABLE = "VISIONQUERY_TEST_SAMPLES"


def _make_samples(directory: Path) -> None:
    """Write the samples into `directory`, which the fake cameras then read."""
    directory.mkdir()
    # The fake cameras run as a user of their own; pytest's directories are private.
    directory.chmod(0o755)
    size = f"{SAMPLE_SIZE[0]}x{SAMPLE_SIZE[1]}"
    brightness = f"{LUMA_LOW}+{LUMA_RANGE}*mod(T,{MARKER_PERIOD})/{MARKER_PERIOD}"
    for name, seconds in SAMPLE_SECONDS.items():
        source = f"color=c=black:size={size}:rate={SAMPLE_RATE}:duration={seconds},"
        subprocess.run(
            ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
             "--volume", f"{directory}:/out", "--entrypoint", "/ffmpeg", FFMPEG_IMAGE,
             "-loglevel", "error", "-f", "lavfi",
             "-i", f"{source}geq=lum='{brightness}':cb=128:cr=128",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", f"/out/{name}"],
            check=True,
            capture_output=True,
            timeout=600,
        )  # fmt: skip
        (directory / name).chmod(0o644)


def _reading(stack: Stack, camera: str) -> str:
    """Where VisionQuery's processes read a camera: only they may (ADR 0002)."""
    password = quote(stack.secret("VISIONQUERY_MEDIAMTX_API_PASSWORD"), safe="")
    return f"rtsp://visionquery:{password}@mediamtx:8554/{camera}"


def _try_probe(stack: Stack, camera: str, seconds: float) -> tuple[dict[str, Any] | None, str]:
    """Read `seconds` of the camera from MediaMTX over RTSP, as a client on the project's
    network would (a container on it works on every host, unlike `--network host`, which
    Docker Desktop does not give the host's network), and say what its video is and how many
    frames were decoded; or None, and why not (a camera is unreachable for a moment when it
    switches from the test pattern to its sample)."""
    probe = subprocess.run(
        ["docker", "run", "--rm", "--network", f"{stack.project}_default",
         "--entrypoint", "/ffprobe",
         FFMPEG_IMAGE, "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
         "-count_frames", "-read_intervals", f"%+{seconds}",
         "-show_entries", "stream=codec_name,width,height,has_b_frames,nb_read_frames",
         "-of", "json", _reading(stack, camera)],
        capture_output=True,
        text=True,
        check=False,
        timeout=seconds + 60,
    )  # fmt: skip
    if probe.returncode != 0:
        return None, probe.stderr
    (stream,) = json.loads(probe.stdout)["streams"]
    return stream, ""


def _probe(stack: Stack, camera: str, seconds: float) -> dict[str, Any]:
    """Read the camera as `_try_probe` does, which must work."""
    stream, why = _try_probe(stack, camera, seconds)
    assert stream is not None, f"{camera}: {why}\n{stack.diagnostics()}"
    return stream


def _paths(stack: Stack) -> dict[str, dict[str, Any]]:
    """MediaMTX's paths, by name, as its API lists them."""
    url = stack.url("mediamtx", 9997, "/v3/paths/list")
    status, body = _get(url, ("visionquery", stack.secret("VISIONQUERY_MEDIAMTX_API_PASSWORD")))
    assert status == 200, body
    return {item["name"]: item for item in json.loads(body)["items"]}


def _wait_until_every_camera_is_ready(stack: Stack, cameras: set[str], timeout: float) -> None:
    def ready() -> tuple[set[str] | None, str]:
        paths = _paths(stack)
        ready = {name for name, path in paths.items() if path["ready"]}
        if ready != cameras:
            return None, f"ready paths are {sorted(ready)}, {sorted(cameras)} are wanted"
        return ready, ""

    wait_for(ready, timeout)


def _wait_until_looping_its_sample(stack: Stack, camera: str, width: int, timeout: float) -> None:
    """A camera shows the test pattern until its sample is prepared; the sample's width tells."""

    def looping() -> tuple[bool | None, str]:
        stream, why = _try_probe(stack, camera, 0.5)
        if stream is None:
            return None, f"{camera} cannot be read: {why}"
        if stream["width"] != width:
            return None, f"{camera} is {stream['width']}x{stream['height']}, not its sample yet"
        return True, ""

    wait_for(looping, timeout)


def _marker_offsets(stack: Stack, cameras: tuple[str, ...], seconds: float) -> dict[str, float]:
    """How far each camera's picture is ahead of the wall clock, in seconds modulo the marker
    period (negative: behind it by the stream's delay), read from the cameras at the same time.

    Every decoded frame is stamped with when it arrived, and its brightness says which second of
    the loop it is, so each frame tells how far ahead of the clock the loop is: unlike comparing
    one frame of each camera, that does not depend on when a connection happened to start, on
    which keyframe it began at, or on the delay of the stream, which is the same for every camera.
    """
    readers = {
        camera: subprocess.Popen(
            ["docker", "run", "--rm", "--network", f"{stack.project}_default",
             "--entrypoint", "/ffmpeg", FFMPEG_IMAGE,
             "-loglevel", "error", "-fflags", "nobuffer", "-rtsp_transport", "tcp",
             # One decoding thread: with one per core, the decoder holds back a picture per
             # thread, which would put every camera a machine-dependent second behind the clock.
             "-threads", "1",
             "-i", _reading(stack, camera), "-t", str(seconds), "-an",
             # One byte per frame: the brightness of the whole picture, as stored in the video.
             "-vf", "scale=1:1:flags=area,format=yuv420p,extractplanes=y",
             "-fps_mode", "passthrough",
             "-f", "rawvideo", "-flush_packets", "1", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        for camera in cameras
    }  # fmt: skip

    def read(camera: str) -> list[tuple[float, int]]:
        stdout = readers[camera].stdout
        assert stdout is not None
        frames: list[tuple[float, int]] = []
        while chunk := stdout.read(1):
            frames.append((time.time(), chunk[0]))
        return frames

    with ThreadPoolExecutor(max_workers=len(cameras)) as pool:
        frames = dict(zip(cameras, pool.map(read, cameras), strict=True))
    offsets: dict[str, float] = {}
    for camera, process in readers.items():
        assert process.stderr is not None
        process.wait(timeout=60)
        assert process.returncode == 0, f"{camera}: {process.stderr.read().decode()}"
        arrived = frames[camera]
        # At the camera's own rate: a loop that restarts with timestamps that do not move is
        # sent all at once, which is many times as many pictures as there is time for. (The few
        # of the keyframe a connection starts at arrive together, hence the margin.)
        expected = SAMPLE_RATE * (arrived[-1][0] - arrived[0][0])
        assert 0.7 * expected <= len(arrived) <= 1.3 * expected, (camera, len(arrived), expected)
        # Not the first seconds for the offset, which come in a burst from that keyframe.
        settled = [(t, y) for t, y in arrived if t > arrived[0][0] + seconds / 3]
        assert len(settled) >= 5, (camera, len(arrived))
        angles = [
            2
            * math.pi
            * ((((y - LUMA_LOW) / LUMA_RANGE) * MARKER_PERIOD - t) % MARKER_PERIOD)
            / MARKER_PERIOD
            for t, y in settled
        ]
        mean = math.atan2(sum(map(math.sin, angles)), sum(map(math.cos, angles)))
        offsets[camera] = mean / (2 * math.pi) * MARKER_PERIOD
    return offsets


#: How far a camera may be from the moment of its loop that the wall clock says: each ffmpeg
#: sends its first half second at once (`-re`), and keeps that lead less its own start-up.
WALL_CLOCK_WITHIN = 0.75


def _assert_in_step(stack: Stack, cameras: tuple[str, ...], within: float = 1.0) -> None:
    """The cameras show the same moment of their loops, to within `within` seconds of each
    other, and to within WALL_CLOCK_WITHIN of the moment the wall clock says."""
    # Longer than a loop, so that the cameras start over while they are read.
    offsets = _marker_offsets(stack, cameras, seconds=MARKER_PERIOD + 8)
    print(f"seconds ahead of the wall clock: { {c: round(o, 2) for c, o in offsets.items()} }")
    for camera, offset in offsets.items():
        assert abs(offset) <= WALL_CLOCK_WITHIN, (
            f"{camera} is {offset:+.2f} s from the wall clock: {offsets}"
        )
    first = cameras[0]
    for camera in cameras[1:]:
        gap = (offsets[camera] - offsets[first] + MARKER_PERIOD / 2) % MARKER_PERIOD
        gap -= MARKER_PERIOD / 2
        assert abs(gap) <= within, f"{camera} is {gap:+.2f} s from {first}: {offsets}"


@pytest.fixture(scope="module")
def dev_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    samples = tmp_path_factory.mktemp("dev") / "samples"
    _make_samples(samples)
    environment = {
        "VISIONQUERY_FAKE_CAMERAS": DEV_CAMERAS,
        "VISIONQUERY_FAKE_CAMERA_SAMPLES": str(samples),
        "VISIONQUERY_FAKE_CAMERA_DURATION": str(MARKER_PERIOD),
    }
    with running_stack(profiles=["dev"], environment=environment) as stack:
        yield stack


def _dev_cameras() -> set[str]:
    return {camera.name for camera in parse_cameras(DEV_CAMERAS)}


def test_the_dev_profile_adds_fake_cameras_healthy(dev_stack: Stack) -> None:
    _assert_healthy(dev_stack, {"postgres", "mediamtx", "reconciler", "fake-cameras"})
    registered = dev_stack.containers()["register-fake-cameras"]
    assert (registered["State"], registered["ExitCode"]) == ("exited", 0)


def test_each_fake_camera_is_a_registered_pushed_camera_publishing_as_itself(
    dev_stack: Stack,
) -> None:
    with _registry(dev_stack) as registry:
        cameras = registry.list()
    assert {(c.name, c.path) for c in cameras} == {(n, n) for n in _dev_cameras()}
    password = dev_stack.secret("VISIONQUERY_FAKE_CAMERA_PASSWORD")
    for camera in cameras:
        assert camera.publish_credentials is not None
        assert camera.publish_credentials.user == camera.name
        assert camera.publish_credentials.verify(password)


def _publish(stack: Stack, path: str, credentials: str = "") -> subprocess.CompletedProcess[str]:
    """Publish two seconds of a test pattern to `path` as a camera would, from the project's
    network; ffmpeg exits 0 only when MediaMTX took it."""
    return subprocess.run(
        ["docker", "run", "--rm", "--network", f"{stack.project}_default",
         "--entrypoint", "/ffmpeg", FFMPEG_IMAGE, "-v", "error", "-re",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-f", "rtsp", "-rtsp_transport", "tcp",
         f"rtsp://{credentials}mediamtx:8554/{path}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )  # fmt: skip


def test_mediamtx_refuses_an_unauthenticated_publish_and_read(dev_stack: Stack) -> None:
    # ADR 0002: Visual Data cannot leave the Deployment through MediaMTX, and nothing gets in
    # but a registered camera with its own credentials.
    _wait_until_every_camera_is_ready(dev_stack, _dev_cameras(), timeout=60)
    refused = subprocess.run(
        ["docker", "run", "--rm", "--network", f"{dev_stack.project}_default",
         "--entrypoint", "/ffprobe", FFMPEG_IMAGE, "-v", "error", "-rtsp_transport", "tcp",
         "-show_entries", "stream=codec_name", "rtsp://mediamtx:8554/lobby"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )  # fmt: skip
    assert refused.returncode != 0
    assert "401" in refused.stderr, refused.stderr
    assert _probe(dev_stack, "lobby", 1)["codec_name"] == "h264"

    password = quote(dev_stack.secret("VISIONQUERY_FAKE_CAMERA_PASSWORD"), safe="")
    unauthenticated = _publish(dev_stack, "lobby")
    assert unauthenticated.returncode != 0
    assert "401" in unauthenticated.stderr, unauthenticated.stderr
    # Not to a path the registry does not have, nor with another camera's credentials.
    assert _publish(dev_stack, "unregistered").returncode != 0
    assert _publish(dev_stack, "lobby", f"yard:{password}@").returncode != 0
    assert _publish(dev_stack, "unregistered", f"yard:{password}@").returncode != 0
    _assert_healthy(dev_stack, {"fake-cameras"})


def test_the_webrtc_live_view_is_refused_without_visionquerys_credentials(
    dev_stack: Stack,
) -> None:
    # A browser asks for a camera over WHEP, on the camera-facing WebRTC port.
    url = dev_stack.url("mediamtx", 8889, "/lobby/whep")

    def offer(credentials: tuple[str, str] | None = None) -> int:
        request = urllib.request.Request(url, data=b"v=0\r\n", method="POST")
        request.add_header("Content-Type", "application/sdp")
        if credentials is not None:
            token = base64.b64encode(":".join(credentials).encode()).decode()
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    assert offer() == 401
    assert offer(("lobby", dev_stack.secret("VISIONQUERY_FAKE_CAMERA_PASSWORD"))) == 401
    # With them MediaMTX goes on to the offer, which is deliberately not one it can answer.
    assert offer(("visionquery", dev_stack.secret("VISIONQUERY_MEDIAMTX_API_PASSWORD"))) == 400


def test_mediamtx_has_a_ready_path_for_exactly_the_configured_cameras(dev_stack: Stack) -> None:
    _wait_until_every_camera_is_ready(dev_stack, _dev_cameras(), timeout=30)
    paths = _paths(dev_stack)
    assert set(paths) == _dev_cameras()
    for name, path in paths.items():
        assert path["source"]["type"] == "rtspSession", name
        assert path["tracks"] == ["H264"], name


def test_each_fake_camera_is_readable_over_rtsp(dev_stack: Stack) -> None:
    for camera in sorted(_dev_cameras()):
        stream = _probe(dev_stack, camera, 2)
        assert stream["codec_name"] == "h264", camera
        assert int(stream["nb_read_frames"]) >= 5, (camera, stream)


def test_a_camera_loops_its_video_prepared_as_a_camera_sends_it(dev_stack: Stack) -> None:
    _wait_until_looping_its_sample(dev_stack, "gate", SAMPLE_SIZE[0], timeout=120)
    # Past the end of a loop, which the loop must go on from at the sample's own rate.
    seconds = MARKER_PERIOD + 5
    stream = _probe(dev_stack, "gate", seconds)
    assert (stream["width"], stream["height"]) == SAMPLE_SIZE
    assert int(stream["nb_read_frames"]) >= 0.8 * seconds * SAMPLE_RATE, stream
    # The sample had B-frames; a browser's WebRTC cannot play them, and no camera sends them.
    assert stream["has_b_frames"] == 0


def test_a_sample_at_a_fractional_frame_rate_is_prepared_with_keyframes_on_whole_seconds(
    tmp_path: Path,
) -> None:
    # 29.97 frames a second, as many cameras record: no picture after the first is on a whole
    # second, where every camera joins the timeline.
    tmp_path.chmod(0o777)

    def ffmpeg(tool: str, *arguments: str) -> str:
        return subprocess.run(
            ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
             "--volume", f"{tmp_path}:/w", "--entrypoint", tool, FFMPEG_IMAGE, *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        ).stdout  # fmt: skip

    ffmpeg("/ffmpeg", "-loglevel", "error", "-f", "lavfi",
           "-i", "testsrc2=duration=8:size=320x240:rate=30000/1001", "-c:v", "libx264",
           "/w/ntsc.mp4")  # fmt: skip
    ffmpeg(*prepare_command(Path("/w/ntsc.mp4"), Path("/w/prepared.mp4"), "/ffmpeg", 6))
    keyframes = ffmpeg(
        "/ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
        "-show_entries", "frame=pts_time", "-of", "csv=p=0", "/w/prepared.mp4",
    )  # fmt: skip
    assert [float(t.strip(",")) for t in keyframes.split()] == [0, 1, 2, 3, 4, 5]
    duration = ffmpeg(
        "/ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
        "/w/prepared.mp4",
    )  # fmt: skip
    assert float(duration) == 6


def test_cameras_looping_samples_are_in_step_whatever_their_length_and_start(
    dev_stack: Stack,
) -> None:
    # `gate` and `gate-again` loop one sample, which each prepared and started on its own;
    # `hall`'s is 0.6 s longer, which drifts a loop that is not cut to the duration.
    for camera in TIMELINE_CAMERAS:
        _wait_until_looping_its_sample(dev_stack, camera, SAMPLE_SIZE[0], timeout=120)
    _assert_in_step(dev_stack, TIMELINE_CAMERAS)
    # And again a loop later, which is where unequal lengths show.
    time.sleep(MARKER_PERIOD)
    _assert_in_step(dev_stack, TIMELINE_CAMERAS)


def test_a_camera_whose_video_was_never_fetched_shows_the_test_pattern_and_says_why(
    dev_stack: Stack,
) -> None:
    stream = _probe(dev_stack, "missing", 1)
    assert (stream["width"], stream["height"]) == TEST_PATTERN_SIZE
    logs = dev_stack.compose("logs", "--no-color", "fake-cameras").stdout
    assert "not-fetched.mp4" in logs
    assert "make fetch-samples" in logs


def test_every_camera_comes_back_by_itself_after_mediamtx_restarts(dev_stack: Stack) -> None:
    publisher = dev_stack.containers()["fake-cameras"]["ID"]
    dev_stack.compose("restart", "mediamtx")
    _wait_until_every_camera_is_ready(dev_stack, _dev_cameras(), timeout=60)
    # Each camera's ffmpeg started over: they all rejoin the timeline, none starts its loop over.
    # Read at once and for longer than a loop, to see the first time each one's loop ends.
    _assert_in_step(dev_stack, TIMELINE_CAMERAS)
    for camera in sorted(_dev_cameras()):
        assert int(_probe(dev_stack, camera, 1)["nb_read_frames"]) > 0, camera
    # The publisher reconnected; it was neither restarted nor recreated.
    assert dev_stack.containers()["fake-cameras"]["ID"] == publisher
    _assert_healthy(dev_stack, {"mediamtx", "fake-cameras"})


# --- The dev profile with the real samples ----------------------------------------------------

#: How long the test pattern runs before it loops, which is not on the timeline.
SYNTHETIC_DURATION = 60.0
#: The sample videos' width: MEVA's cameras record 1920x1080 or 1920x1072.
SAMPLE_WIDTH = 1920


@pytest.fixture(scope="module")
def sample_stack() -> Iterator[Stack]:
    if os.environ.get(SAMPLES_VARIABLE) != "1":
        pytest.skip(
            f"downloads the sample videos (about 400 MB, once) into samples/: set "
            f"{SAMPLES_VARIABLE}=1 to run it"
        )
    fetch = subprocess.run(
        ["make", "--no-print-directory", "fetch-samples"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=BUILD_TIMEOUT,
    )
    assert fetch.returncode == 0, fetch.stdout + fetch.stderr
    # The cameras .env.example configures, reading the repository's samples/.
    with running_stack(profiles=["dev"]) as stack:
        yield stack


def test_the_default_cameras_loop_the_fetched_samples(sample_stack: Stack) -> None:
    cameras = parse_cameras(_documented()["VISIONQUERY_FAKE_CAMERAS"])
    _wait_until_every_camera_is_ready(sample_stack, {c.name for c in cameras}, timeout=60)
    for camera in cameras:
        # Preparing a five-minute 1080p sample takes about a minute on 12 cores.
        _wait_until_looping_its_sample(sample_stack, camera.name, SAMPLE_WIDTH, timeout=900)
        stream = _probe(sample_stack, camera.name, 3)
        assert stream["has_b_frames"] == 0, camera
        assert int(stream["nb_read_frames"]) >= 30, (camera, stream)
    logs = sample_stack.compose("logs", "--no-color", "fake-cameras").stdout
    assert "make fetch-samples" not in logs


def test_the_default_samples_are_prepared_to_the_same_five_minutes(sample_stack: Stack) -> None:
    # The recordings are 300.0 s to 300.3 s long (and start with two pictures that are lost),
    # which would drift their loops apart.
    for camera in parse_cameras(_documented()["VISIONQUERY_FAKE_CAMERAS"]):
        _wait_until_looping_its_sample(sample_stack, camera.name, SAMPLE_WIDTH, timeout=900)
    listing = sample_stack.compose(
        "exec", "-T", "fake-cameras", "python3", "-c",
        "import pathlib; print(*sorted(p.name for p in pathlib.Path('/cache').glob('*.mp4')))",
    )  # fmt: skip
    prepared = listing.stdout.split()
    assert len(prepared) >= 4, listing.stdout  # three samples and the test pattern
    durations: dict[str, float] = {}
    for name in prepared:
        probe = subprocess.run(
            ["docker", "run", "--rm", "--volume", f"{sample_stack.project}_fake-cameras:/cache:ro",
             "--entrypoint", "/ffprobe", FFMPEG_IMAGE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=start_time,duration", "-of", "csv=p=0", f"/cache/{name}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )  # fmt: skip
        start, duration = map(float, probe.stdout.strip().split(","))
        # Whole seconds are keyframes only if the pictures start at 0.
        assert start == 0, (name, start)
        durations[name] = duration
    assert sorted(round(d, 2) for d in durations.values()) == [SYNTHETIC_DURATION, 300, 300, 300]
