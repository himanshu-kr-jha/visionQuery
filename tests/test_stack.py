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
import os
import secrets
import shlex
import shutil
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
        """The image Compose builds for `app` (every VisionQuery process) or `mediamtx`."""
        tag = {"app": "dev", "mediamtx": "1.21.1"}[name]
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


def test_the_default_stack_starts_postgres_and_mediamtx_healthy(default_stack: Stack) -> None:
    containers = default_stack.containers()
    assert set(containers) == {"postgres", "migrate", "mediamtx"}
    _assert_healthy(default_stack, {"postgres", "mediamtx"})


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
    for image in (stack.image("app"), stack.image("mediamtx")):
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
