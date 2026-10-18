"""The Compose stack's definition, checked without starting anything.

`docker compose config` only reads files, so these need the `docker` CLI with its Compose
plugin but no running daemon. They are skipped, saying why, when the CLI is missing.

Each run reads `.env.example` instead of a developer's `.env`, so what is checked is what a
fresh clone gets, and the variables `.env.example` leaves empty (the secrets) are filled in.
"""

import fnmatch
import itertools
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from fake_cameras import SYNTHETIC, parse_cameras
from fetch_samples import MANIFEST, load_manifest
from visionquery.api.probe import DEADLINE_SECONDS
from visionquery.memory.roles import MONITORING, RUNTIME

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
NO_GPU = ROOT / "compose.no-gpu.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
PROMETHEUS = ROOT / "docker" / "prometheus" / "prometheus.yml"
MEDIAMTX = ROOT / "docker" / "mediamtx" / "mediamtx.yml"
GRAFANA_DATASOURCES = ROOT / "docker" / "grafana" / "provisioning" / "datasources"

PROFILES = ("dev", "gpu", "observability")
#: What `docker compose up` starts with no profile, on a machine without a GPU.
DEFAULT_SERVICES = {"postgres", "migrate", "mediamtx", "reconciler", "recorder", "api"}
#: What each profile adds.
PROFILE_SERVICES: dict[str, set[str]] = {
    "dev": {"fake-cameras", "register-fake-cameras"},
    "gpu": {"triton", "triton-scrape"},
    "observability": {"prometheus", "grafana", "postgres-exporter"},
}
#: Where each long-running service that is not a VisionQuery process serves its metrics.
#: A VisionQuery process serves them on `<service>:9100` (ADR 0012); Postgres has none of
#: its own, so its exporter is scraped instead.
METRICS: dict[str, str] = {
    "postgres": "postgres-exporter:9187",
    "postgres-exporter": "postgres-exporter:9187",
    "mediamtx": "mediamtx:9998",
    "triton": "triton:8002",
    "prometheus": "prometheus:9090",
    "grafana": "grafana:3000",
}
RUNTIME_METRICS_PORT = 9100
#: Long-running services Prometheus does not scrape, and why.
UNSCRAPED = {"fake-cameras": "a development tool, never part of a Deployment"}
#: The Compose project `docker compose config` names when not told another.
VISIONQUERY_PROJECT = "visionquery"

#: An exact tag, naming a version (`1.21.1`) or a dated build (`trixie-20261005-slim`) rather
#: than `latest` or a bare major, and the digest it resolved to.
PINNED = re.compile(r"^[\w./-]+:[\w.-]*(\d+\.\d+|\d{8})[\w.-]*@sha256:[0-9a-f]{64}$")
VARIABLE = re.compile(r"\$\{(?P<name>[A-Z][A-Z0-9_]*)")

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="needs the docker CLI with the Compose plugin"
)


def _example() -> dict[str, str]:
    """The variables `.env.example` documents, with the values it gives them."""
    variables: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            variables[name.strip()] = value.strip()
    return variables


def _secrets() -> list[str]:
    """The variables `.env.example` leaves empty: each Deployment chooses its own."""
    return sorted(name for name, value in _example().items() if not value)


def _environment(overrides: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """The caller's environment without anything that would change what Compose reads."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("VISIONQUERY_", "COMPOSE_", "DOCKER_DEFAULT_PLATFORM"))
    }
    env |= {name: f"test-{name.lower()}" for name in _secrets()}
    for name, value in (overrides or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _compose(
    *args: str,
    profiles: Iterable[str] = (),
    files: Iterable[Path] = (),
    env: Mapping[str, str | None] | None = None,
    env_file: Path = ENV_EXAMPLE,
    project: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "--project-directory", str(ROOT), "-f", str(COMPOSE)]
    if project is not None:
        command += ["--project-name", project]
    for file in files:
        command += ["-f", str(file)]
    command += ["--env-file", str(env_file)]
    for profile in profiles:
        command += ["--profile", profile]
    return subprocess.run(
        [*command, *args],
        cwd=ROOT,
        env=_environment(env),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _config(
    profiles: Iterable[str] = PROFILES,
    files: Iterable[Path] = (),
    env: Mapping[str, str | None] | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    result = _compose(
        "config", "--format", "json", profiles=profiles, files=files, env=env, project=project
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _services(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return config["services"]


def _long_running(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Services that stay up, as opposed to one-shot jobs such as `migrate`."""
    return {n: s for n, s in _services(config).items() if s.get("restart", "no") != "no"}


def _builds_the_visionquery_image(service: Mapping[str, Any]) -> bool:
    build = service.get("build")
    return build is not None and Path(build["context"]).resolve() == ROOT


def _is_visionquery_process(service: Mapping[str, Any]) -> bool:
    """Whether the service runs the image the root Dockerfile builds, whoever builds it."""
    return service.get("image") == f"{VISIONQUERY_PROJECT}-app:dev"


def _combinations() -> list[tuple[str, ...]]:
    return [c for r in range(len(PROFILES) + 1) for c in itertools.combinations(PROFILES, r)]


@pytest.mark.parametrize("profiles", _combinations(), ids=lambda c: "+".join(c) or "default")
def test_each_profile_combination_is_valid_and_starts_its_services(
    profiles: tuple[str, ...],
) -> None:
    result = _compose("config", "--services", profiles=profiles)
    assert result.returncode == 0, result.stderr
    expected = DEFAULT_SERVICES.union(*(PROFILE_SERVICES[p] for p in profiles))
    assert set(result.stdout.split()) == expected


def test_the_project_has_a_name_of_its_own() -> None:
    # Without one, the project is named after the checkout's directory, and two clones would
    # share containers and volumes.
    assert yaml.safe_load(COMPOSE.read_text())["name"] == "visionquery"


def test_triton_reserves_every_gpu() -> None:
    triton = _services(_config())["triton"]
    devices = triton["deploy"]["resources"]["reservations"]["devices"]
    assert devices == [{"capabilities": ["gpu"], "driver": "nvidia", "count": -1}]


def test_the_no_gpu_override_only_drops_tritons_gpu_reservation() -> None:
    with_gpu = _config()
    without = _config(files=[NO_GPU])
    assert "deploy" not in _services(without)["triton"]
    del _services(with_gpu)["triton"]["deploy"]
    assert without == with_gpu


@pytest.mark.parametrize("secret", _secrets())
def test_compose_refuses_to_run_without_each_secret(secret: str) -> None:
    result = _compose("config", "--quiet", profiles=PROFILES, env={secret: None})
    assert result.returncode != 0
    assert secret in result.stderr


def test_the_secrets_are_the_passwords() -> None:
    # Every credential is chosen per Deployment, and nothing else is left empty.
    assert _secrets() == [
        "VISIONQUERY_FAKE_CAMERA_PASSWORD",
        "VISIONQUERY_GRAFANA_ADMIN_PASSWORD",
        "VISIONQUERY_MEDIAMTX_API_PASSWORD",
        "VISIONQUERY_POSTGRES_MONITORING_PASSWORD",
        "VISIONQUERY_POSTGRES_PASSWORD",
        "VISIONQUERY_POSTGRES_RUNTIME_PASSWORD",
    ]


def test_env_example_documents_exactly_the_variables_compose_reads() -> None:
    # COMPOSE_PROJECT_NAME is Compose's own, set from the file's `name:` or `--project-name`.
    read = {m["name"] for file in (COMPOSE, NO_GPU) for m in VARIABLE.finditer(file.read_text())}
    read -= {"COMPOSE_PROJECT_NAME"}
    documented = set(_example())
    assert read - documented == set(), "read by Compose but missing from .env.example"
    assert documented - read == set(), "documented in .env.example but never read"


def test_compose_defaults_are_the_values_in_env_example(tmp_path: Path) -> None:
    # Then a `.env` that leaves a variable out gets what `.env.example` says it would.
    empty = tmp_path / "empty.env"
    empty.write_text("")
    result = _compose("config", "--format", "json", profiles=PROFILES, env_file=empty)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == _config()


def test_env_example_explains_every_variable() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    for previous, line in itertools.pairwise(["", *lines]):
        if line.strip() and not line.startswith("#"):
            assert previous.startswith("#"), f"{line!r} has no comment above it"


def _dockerfile_bases(service: Mapping[str, Any]) -> list[str]:
    build = service["build"]
    dockerfile = Path(build["context"]) / build.get("dockerfile", "Dockerfile")
    stages: set[str] = set()
    bases: list[str] = []
    for line in dockerfile.read_text().splitlines():
        words = line.split()
        if words[:1] == ["FROM"]:
            image = next(w for w in words[1:] if not w.startswith("--"))
            if image not in stages:
                bases.append(image)
            if len(words) >= 4 and words[-2].upper() == "AS":
                stages.add(words[-1])
        elif words[:1] == ["COPY"] and (source := re.search(r"--from=(\S+)", line)):
            if source[1] not in stages:
                bases.append(source[1])
    return bases


def test_every_image_is_pinned_to_a_version_and_its_digest() -> None:
    images: list[str] = []
    for name, service in _services(_config()).items():
        if "build" in service:
            images += _dockerfile_bases(service)
            # A built image exists only on this host, so it is never pulled from a registry.
            assert service.get("pull_policy") == "never", name
        elif not _is_visionquery_process(service):
            # A VisionQuery process runs the image migrate builds, whose bases are checked there.
            images.append(service["image"])
    assert images
    unpinned = [image for image in images if not PINNED.match(image)]
    assert unpinned == []


def test_up_never_pulls_a_built_image_and_never_rebuilds_it() -> None:
    # `never` builds an image that is absent (Compose builds rather than fails), never pulls
    # it, and does not rebuild one that is present. A rebuild gives a new image ID even when no
    # layer changed, and Compose recreates a container whose image ID changed, which drops
    # live streams; so a bare `up` must not rebuild, and `make up` builds first, without
    # attestations, which keeps the ID stable (tests/test_stack.py checks that `make up`
    # recreates nothing).
    built = {n: s for n, s in _services(_config()).items() if "build" in s}
    assert set(built) >= {"migrate", "mediamtx"}
    assert {name: s.get("pull_policy") for name, s in built.items()} == dict.fromkeys(
        built, "never"
    )


def test_built_images_are_named_for_their_project() -> None:
    # Image tags are global to the Docker daemon, while Compose stamps the project's name into
    # the labels of every image it builds. A fixed tag shared by two projects (two clones, a
    # stack test beside a developer's stack) would therefore point at a different image ID
    # after each project's build, and each project's next `make up` would recreate its
    # containers and drop live streams.
    first = _services(_config(project="vqtest-first"))
    second = _services(_config(project="vqtest-second"))
    built = [name for name, service in first.items() if "build" in service]
    assert built
    for name in built:
        assert first[name]["image"].startswith("vqtest-first"), name
        assert second[name]["image"].startswith("vqtest-second"), name
    default = _services(_config())
    assert all(default[name]["image"].startswith("visionquery") for name in built)


def test_every_visionquery_process_runs_the_one_image() -> None:
    # One build and one set of layers per project: every service built from the root
    # Dockerfile has the same image name.
    services = _services(_config())
    images = {s["image"] for s in services.values() if _builds_the_visionquery_image(s)}
    assert images == {f"{VISIONQUERY_PROJECT}-app:dev"}
    assert {n for n, s in services.items() if _is_visionquery_process(s)} >= {"migrate", "api"}


def test_exactly_one_service_builds_the_visionquery_image() -> None:
    # Compose stamps the building service's name into the image's labels, so two services
    # building one tag would race, each `make up` would leave a different image ID behind, and
    # every VisionQuery container would be recreated on every `make up`. One service builds it
    # (migrate, which every other one waits for) and the others only run it.
    services = _services(_config())
    assert [n for n, s in services.items() if _builds_the_visionquery_image(s)] == ["migrate"]
    for name, service in services.items():
        if _is_visionquery_process(service):
            assert service.get("pull_policy") == "never", name


def test_dockerfiles_pin_the_frontend_that_copy_parents_needs() -> None:
    # `COPY --parents` arrived in Dockerfile frontend 1.20, and the frontend is whatever the
    # build resolves unless the first line says which, so the line is required and pinned.
    dockerfiles = [ROOT / "Dockerfile", *(ROOT / "docker").glob("*/Dockerfile")]
    needing = [d for d in dockerfiles if "--parents" in d.read_text()]
    assert ROOT / "Dockerfile" in needing
    for dockerfile in needing:
        first = dockerfile.read_text().splitlines()[0]
        found = re.fullmatch(
            r"# syntax=docker/dockerfile:1\.(\d+)(?:\.\d+)?@sha256:[0-9a-f]{64}", first
        )
        assert found, f"{dockerfile}: {first!r}"
        assert int(found[1]) >= 20, f"{dockerfile}: {first!r}"


def test_the_runtime_image_has_certificates_and_zone_data_and_no_phantom_home() -> None:
    # The remote Reasoner is reached over TLS (ADR 0002); the root filesystem is read-only
    # and no home exists, so the working directory must be one that does.
    dockerfile = (ROOT / "Dockerfile").read_text()
    runtime = dockerfile[dockerfile.index("AS runtime") :]
    assert "ca-certificates" in runtime
    assert "tzdata" in runtime
    assert "/nonexistent" not in dockerfile
    assert re.search(r"^USER visionquery", runtime, re.M)
    assert re.search(r"^WORKDIR /$", runtime, re.M)


def test_every_service_built_from_our_image_waits_for_the_migrations_to_succeed() -> None:
    # Otherwise a long-running process could start against a schema that is not up to date,
    # or that a failed migration left behind.
    for name, service in _services(_config()).items():
        if name == "migrate" or not _is_visionquery_process(service):
            continue
        condition = service.get("depends_on", {}).get("migrate", {}).get("condition")
        assert condition == "service_completed_successfully", name


#: The variable holding the password of each role a service connects to Postgres as.
PASSWORD_OF = {
    "visionquery": "VISIONQUERY_POSTGRES_PASSWORD",
    RUNTIME: "VISIONQUERY_POSTGRES_RUNTIME_PASSWORD",
    MONITORING: "VISIONQUERY_POSTGRES_MONITORING_PASSWORD",
}


def _connecting(services: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The services that connect to the memory with a URL, as VisionQuery processes do."""
    return {
        n: s for n, s in services.items() if "VISIONQUERY_DATABASE__URL" in s.get("environment", {})
    }


def _user(service: Mapping[str, Any]) -> str:
    url = service["environment"]["VISIONQUERY_DATABASE__URL"]
    return url.removeprefix("postgresql://").split("@", 1)[0]


def test_the_database_password_is_not_part_of_the_url() -> None:
    # A password with `@`, `/`, `:` or `#` in it would break a URL; libpq reads it from
    # PGPASSWORD instead, so any password works.
    passwords = {variable: f"p@ss/w:rd#{n}" for n, variable in enumerate(PASSWORD_OF.values())}
    services = _services(_config(env=passwords))
    connecting = _connecting(services)
    assert set(connecting) >= {"migrate", "api"}
    for name, service in connecting.items():
        environment = service["environment"]
        password = passwords[PASSWORD_OF[_user(service)]]
        assert ":" not in _user(service), name
        assert password not in environment["VISIONQUERY_DATABASE__URL"], name
        assert environment["PGPASSWORD"] == password, name


def test_only_migrate_connects_as_the_superuser() -> None:
    # Every other VisionQuery process, today's and those later milestones add, connects as the
    # runtime role, which reads and writes the memory's rows and nothing more (ADR 0016).
    config = _config()
    users = {name: _user(service) for name, service in _connecting(_services(config)).items()}
    assert users.pop("migrate") == "visionquery"
    assert users
    assert set(users.values()) == {RUNTIME}
    for name, service in _long_running(config).items():
        if _is_visionquery_process(service):
            assert name in users, f"{name} runs our image and does not say how it connects"


def test_only_postgres_and_migrate_are_given_the_superusers_password() -> None:
    password = "the-superusers-own-password"
    config = _config(env={"VISIONQUERY_POSTGRES_PASSWORD": password})
    given = {
        name
        for name, service in _services(config).items()
        if password in json.dumps(service.get("environment", {}))
    }
    assert given == {"postgres", "migrate"}


def test_migrate_gives_the_runtime_and_monitoring_roles_their_passwords() -> None:
    environment = _services(_config())["migrate"]["environment"]
    runtime = "test-visionquery_postgres_runtime_password"
    monitoring = "test-visionquery_postgres_monitoring_password"
    assert environment["VISIONQUERY_RUNTIME_PASSWORD"] == runtime
    assert environment["VISIONQUERY_MONITORING_PASSWORD"] == monitoring


def test_the_postgres_exporter_connects_as_the_monitoring_role_once_it_can_log_in() -> None:
    exporter = _services(_config())["postgres-exporter"]
    environment = exporter["environment"]
    assert environment["DATA_SOURCE_USER"] == MONITORING
    assert environment["DATA_SOURCE_PASS"] == "test-visionquery_postgres_monitoring_password"
    # migrate gives the role its login.
    condition = exporter["depends_on"]["migrate"]["condition"]
    assert condition == "service_completed_successfully"


def test_the_api_serves_on_its_own_port_and_is_healthy_only_when_health_says_so() -> None:
    api = _services(_config())["api"]
    assert api["command"] == ["visionquery-api"]
    [port] = api["ports"]
    assert (port["target"], port["published"]) == (8080, "8080")
    # The image has no curl or wget: the healthcheck asks /health with the image's own Python,
    # which fails on the 503 the api answers while the database cannot be reached.
    test = " ".join(api["healthcheck"]["test"])
    assert "http://127.0.0.1:8080/health" in test


def test_the_api_healthcheck_waits_longer_than_the_api_waits_for_the_database() -> None:
    # /health can take the probe's whole deadline; a healthcheck that gave up sooner would
    # mark an api unhealthy for a database that is merely slow to answer.
    healthcheck = _services(_config())["api"]["healthcheck"]
    found = re.search(r"urlopen\([^)]*timeout=(\d+)\)", " ".join(healthcheck["test"]))
    assert found, healthcheck["test"]
    asks = int(found.group(1))
    assert asks >= DEADLINE_SECONDS + 1
    assert healthcheck["timeout"] == f"{asks + 1}s"  # Docker's own limit outlasts the request


def test_the_api_listens_on_every_interface_of_its_container() -> None:
    # Its default is loopback, which a published port cannot reach from outside the container;
    # who can reach the published port is VISIONQUERY_BIND_ADDRESS's business.
    api = _services(_config())["api"]
    assert api["environment"]["VISIONQUERY_HTTP_HOST"] == "0.0.0.0"
    [port] = api["ports"]
    assert port["host_ip"] == "127.0.0.1"


def _published(config: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    return [
        (name, port.get("host_ip", ""), str(port["published"]), port.get("protocol", "tcp"))
        for name, service in _services(config).items()
        for port in service.get("ports", [])
    ]


def test_no_two_services_publish_the_same_host_port() -> None:
    published = _published(_config())
    seen: dict[tuple[str, str, str], str] = {}
    for name, host_ip, port, protocol in published:
        key = (host_ip, port, protocol)
        assert key not in seen, f"{name} and {seen[key]} both publish {protocol}/{port}"
        seen[key] = name


CAMERA_FACING = {
    ("mediamtx", "8554", "tcp"),
    ("mediamtx", "8889", "tcp"),
    ("mediamtx", "8189", "udp"),
}


def _bound(config: Mapping[str, Any]) -> dict[tuple[str, str, str], str]:
    """Each published port as (service, container port, protocol), and its host address."""
    return {
        (name, str(port["target"]), port.get("protocol", "tcp")): port.get("host_ip", "")
        for name, service in _services(config).items()
        for port in service.get("ports", [])
    }


def test_ports_are_published_on_loopback_unless_told_otherwise() -> None:
    bound = _bound(_config())
    assert bound
    assert set(bound.values()) == {"127.0.0.1"}


def test_widening_a_bind_address_never_widens_the_others() -> None:
    # Cameras elsewhere need MediaMTX's camera-facing ports, not Postgres, Prometheus (which has
    # no authentication), Grafana, Triton or MediaMTX's control API.
    cameras = _bound(_config(env={"VISIONQUERY_MEDIAMTX_BIND_ADDRESS": "0.0.0.0"}))
    assert {key for key, host_ip in cameras.items() if host_ip == "0.0.0.0"} == CAMERA_FACING
    assert {host_ip for key, host_ip in cameras.items() if key not in CAMERA_FACING} == {
        "127.0.0.1"
    }
    rest = _bound(_config(env={"VISIONQUERY_BIND_ADDRESS": "0.0.0.0"}))
    assert {key for key, host_ip in rest.items() if host_ip == "127.0.0.1"} == CAMERA_FACING
    assert {host_ip for key, host_ip in rest.items() if key not in CAMERA_FACING} == {"0.0.0.0"}


def test_every_bind_address_defaults_to_loopback_and_is_documented() -> None:
    text = COMPOSE.read_text()
    addresses = {n for n in _example() if n.endswith("_BIND_ADDRESS")}
    assert addresses == {"VISIONQUERY_BIND_ADDRESS", "VISIONQUERY_MEDIAMTX_BIND_ADDRESS"}
    assert {_example()[n] for n in addresses} == {"127.0.0.1"}
    published = re.findall(r'^\s+- "(\$\{[^}]*\}):', text, re.MULTILINE)
    assert published
    for host in published:
        assert host in {f"${{{n}:-127.0.0.1}}" for n in addresses}, host


def test_the_mediamtx_bind_address_says_who_may_publish_and_read_and_warns_of_the_clear() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    at = lines.index("VISIONQUERY_MEDIAMTX_BIND_ADDRESS=127.0.0.1")
    comment = "\n".join(itertools.takewhile(lambda line: line.startswith("#"), lines[at - 1 :: -1]))
    assert "own credentials" in comment
    assert "only VisionQuery" in comment
    # RTSP sends them, and the video, unencrypted.
    assert "WARNING" in comment
    assert "in the clear" in comment


def test_webrtc_media_is_announced_at_the_hosts_and_port_the_browser_reaches() -> None:
    chosen = {
        "VISIONQUERY_MEDIAMTX_WEBRTC_HOSTS": "192.0.2.7,vq.example",
        "VISIONQUERY_MEDIAMTX_WEBRTC_UDP_PORT": "47123",
    }
    config = _config(env=chosen)
    environment = _services(config)["mediamtx"]["environment"]
    assert environment["MTX_WEBRTCADDITIONALHOSTS"] == "192.0.2.7,vq.example"
    assert environment["MTX_WEBRTCLOCALUDPADDRESS"] == ":47123"
    [udp] = [p for p in _services(config)["mediamtx"]["ports"] if p["protocol"] == "udp"]
    assert (udp["published"], udp["target"]) == ("47123", 47123)
    default = _services(_config())["mediamtx"]["environment"]
    assert default["MTX_WEBRTCADDITIONALHOSTS"] == "127.0.0.1"
    assert default["MTX_WEBRTCLOCALUDPADDRESS"] == ":8189"


def test_every_published_port_has_a_variable_of_its_own() -> None:
    # Then a clash with something already on the host is fixed in `.env`, and the stack tests
    # can ask for ephemeral ports so they never clash with a running stack.
    variables = sorted(n for n in _example() if n.endswith("_PORT"))
    chosen = {name: str(40000 + index) for index, name in enumerate(variables)}
    published = _published(_config(env=chosen))
    assert sorted(port for _, _, port, _ in published) == sorted(chosen.values())


def test_every_long_running_service_has_a_healthcheck() -> None:
    config = _config()
    missing = [n for n, s in _long_running(config).items() if not s.get("healthcheck")]
    assert missing == []


def test_migrate_runs_once_after_postgres_is_healthy() -> None:
    services = _services(_config())
    migrate = services["migrate"]
    assert migrate["restart"] == "no"
    assert migrate["command"] == ["visionquery-migrate"]
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_data_lives_in_named_volumes_and_configuration_is_read_only() -> None:
    config = _config()
    for name, service in _services(config).items():
        for mount in service.get("volumes", []):
            if mount["type"] == "volume":
                assert mount["source"] in config["volumes"], f"{name} has an anonymous volume"
            else:
                assert mount["type"] == "bind", name
                assert Path(mount["source"]).resolve().is_relative_to(ROOT), name
                assert mount.get("read_only") is True, f"{name} can write to {mount['source']}"


def _written_targets() -> set[str]:
    """The targets the gpu profile's `triton-scrape` service writes for Prometheus to read."""
    command = " ".join(_services(_config())["triton-scrape"]["command"])
    return set(re.findall(r"[\w-]+:\d+", command))


def _scraped() -> set[str]:
    """Every `host:port` Prometheus scrapes: looked up in DNS, or in a file a profile writes."""
    targets: set[str] = set()
    for job in yaml.safe_load(PROMETHEUS.read_text())["scrape_configs"]:
        assert job.get("metrics_path", "/metrics") == "/metrics", job["job_name"]
        if "file_sd_configs" in job:
            targets |= _written_targets()
        for dns in job.get("dns_sd_configs", []):
            assert dns["type"] == "A", job["job_name"]
            assert all(name.endswith(".") for name in dns["names"]), job["job_name"]
            targets.update(f"{name.rstrip('.')}:{dns['port']}" for name in dns["names"])
    return targets


def test_prometheus_scrapes_every_long_running_service() -> None:
    scraped = _scraped()
    for name, service in _long_running(_config()).items():
        if name in UNSCRAPED:
            continue
        if _is_visionquery_process(service):
            expected = f"{name}:{RUNTIME_METRICS_PORT}"
        else:
            assert name in METRICS, f"say where {name} serves its metrics"
            expected = METRICS[name]
        assert expected in scraped, f"Prometheus does not scrape {name} at {expected}"


def _runtime_environment() -> list[str]:
    """The variables compose.yaml says every VisionQuery process is given."""
    declared = yaml.safe_load(COMPOSE.read_text())["x-runtime-environment"]
    assert "VISIONQUERY_METRICS_HOST" in declared
    return list(declared)


def test_every_visionquery_process_serves_its_metrics_beyond_its_own_container() -> None:
    # A process serves /metrics on loopback unless told otherwise, which Prometheus, in a
    # container of its own, cannot reach. Nothing is published on the host.
    processes = {n: s for n, s in _services(_config()).items() if _is_visionquery_process(s)}
    assert processes
    for name, service in processes.items():
        for variable in _runtime_environment():
            assert variable in service["environment"], f"{name} lacks {variable}"
        assert service["environment"]["VISIONQUERY_METRICS_HOST"] == "0.0.0.0", name
        published = {str(port.get("target")) for port in service.get("ports", [])}
        assert str(RUNTIME_METRICS_PORT) not in published, name


def test_triton_is_scraped_only_through_a_file_the_gpu_profile_writes() -> None:
    # Looked up in DNS without the gpu profile, its name would not resolve and Prometheus would
    # log an error every 15 s; and the file must be where Prometheus reads it.
    config = _config()
    jobs = {j["job_name"]: j for j in yaml.safe_load(PROMETHEUS.read_text())["scrape_configs"]}
    assert "dns_sd_configs" not in jobs["triton"]
    (file_sd,) = jobs["triton"]["file_sd_configs"]
    (pattern,) = file_sd["files"]
    prometheus = {m["target"]: m for m in _services(config)["prometheus"]["volumes"]}
    directory = str(Path(pattern).parent)
    assert prometheus[directory]["source"] == "scrape-targets"
    assert prometheus[directory]["read_only"] is True
    depends = _services(config)["prometheus"]["depends_on"]["triton-scrape"]
    assert depends == {"condition": "service_completed_successfully", "required": False}
    writer = _services(config)["triton-scrape"]
    assert [m["source"] for m in writer["volumes"]] == ["scrape-targets"]
    assert writer["profiles"] == ["gpu"]
    assert f"{writer['volumes'][0]['target']}/triton.yml" in " ".join(writer["command"])
    assert fnmatch.fnmatch("triton.yml", Path(pattern).name)


def test_every_target_is_found_through_dns_or_a_file() -> None:
    # A statically listed target that is not running shows as down; a name that does not
    # resolve yields no target at all, so the stack without `gpu` still has every target up,
    # and every replica of a scaled service is scraped.
    jobs = yaml.safe_load(PROMETHEUS.read_text())["scrape_configs"]
    assert [job["job_name"] for job in jobs if "static_configs" in job] == []


def test_grafana_reads_prometheus() -> None:
    files = list(GRAFANA_DATASOURCES.glob("*.yaml"))
    assert len(files) == 1
    sources = yaml.safe_load(files[0].read_text())["datasources"]
    assert [(s["type"], s["url"], s.get("isDefault")) for s in sources] == [
        ("prometheus", "http://prometheus:9090", True)
    ]


def test_mediamtx_serves_its_api_and_metrics_and_records_nothing() -> None:
    config = yaml.safe_load(MEDIAMTX.read_text())
    assert config["api"] is True
    assert config["metrics"] is True
    assert config["rtsp"] is True
    assert config["webrtc"] is True
    # The recorder records Footage (ADR 0007); MediaMTX recording too would double the disk.
    assert config["pathDefaults"]["record"] is False
    assert config["playback"] is False
    # Only what cameras and the live view use is open.
    assert [config[p] for p in ("rtmp", "srt", "hls", "moq", "pprof")] == [False] * 5


def _mediamtx_users() -> list[dict[str, Any]]:
    return yaml.safe_load(MEDIAMTX.read_text())["authInternalUsers"]


def test_mediamtx_lets_nobody_publish_or_read_but_whom_the_reconciler_and_visionquery_say() -> None:
    # ADR 0002: Visual Data never leaves the Deployment through MediaMTX. A camera publishes
    # with its own credentials, which the reconciler adds; only VisionQuery's own user reads.
    config = yaml.safe_load(MEDIAMTX.read_text())
    assert config["authMethod"] == "internal"
    granted = {
        (user["user"], permission["action"])
        for user in _mediamtx_users()
        for permission in user["permissions"]
    }
    assert {action for _, action in granted if action in {"publish", "playback"}} == set()
    assert {user for user, action in granted if action == "read"} == {"visionquery"}
    # No path but those the reconciler adds: publishing anywhere else is refused.
    assert config["paths"] in ({}, None)
    # A hashed password can be checked only against Basic credentials.
    assert config["rtspAuthMethods"] == ["basic"]


def test_only_loopback_and_prometheus_reach_mediamtx_without_a_password() -> None:
    anonymous = [user for user in _mediamtx_users() if user["user"] == "any"]
    assert {(tuple(u["ips"]), p["action"]) for u in anonymous for p in u["permissions"]} == {
        (("127.0.0.1", "::1"), "api"),
        ((), "metrics"),
    }


def test_the_visionquery_users_password_is_the_api_password_compose_sets() -> None:
    (index,) = [i for i, user in enumerate(_mediamtx_users()) if user["user"] == "visionquery"]
    services = _services(_config(env={"VISIONQUERY_MEDIAMTX_API_PASSWORD": "chosen"}))
    environment = services["mediamtx"]["environment"]
    assert environment[f"MTX_AUTHINTERNALUSERS_{index}_PASS"] == "chosen"
    assert [k for k in environment if k.startswith("MTX_AUTHINTERNALUSERS_")] == [
        f"MTX_AUTHINTERNALUSERS_{index}_PASS"
    ]


def test_the_reconciler_drives_mediamtx_as_visionquery_once_both_are_up() -> None:
    config = _config(
        env={"VISIONQUERY_MEDIAMTX_API_PASSWORD": "chosen", "VISIONQUERY_RECONCILE_INTERVAL": "2"}
    )
    reconciler = _services(config)["reconciler"]
    assert reconciler["command"] == ["visionquery-reconciler"]
    environment = reconciler["environment"]
    assert environment["VISIONQUERY_MEDIAMTX__URL"] == "http://mediamtx:9997"
    assert environment["VISIONQUERY_MEDIAMTX__PASSWORD"] == "chosen"
    assert "VISIONQUERY_MEDIAMTX__USER" not in environment
    assert environment["VISIONQUERY_RECONCILE_INTERVAL"] == "2"
    assert _example()["VISIONQUERY_RECONCILE_INTERVAL"] == "5"
    assert reconciler["depends_on"]["mediamtx"]["condition"] == "service_healthy"
    assert reconciler["healthcheck"]["test"] == ["CMD", "visionquery-reconciler", "health"]
    assert "ports" not in reconciler


def test_the_recorder_reads_mediamtx_as_visionquery_into_a_footage_volume_of_its_own() -> None:
    config = _config(
        env={"VISIONQUERY_MEDIAMTX_API_PASSWORD": "chosen", "VISIONQUERY_SEGMENT_SECONDS": "60"}
    )
    recorder = _services(config)["recorder"]
    assert recorder["command"] == ["visionquery-recorder"]
    environment = recorder["environment"]
    assert environment["VISIONQUERY_MEDIAMTX__RTSP_URL"] == "rtsp://mediamtx:8554"
    assert environment["VISIONQUERY_MEDIAMTX__PASSWORD"] == "chosen"
    assert "VISIONQUERY_MEDIAMTX__USER" not in environment
    assert environment["VISIONQUERY_SEGMENT_SECONDS"] == "60"
    assert _example()["VISIONQUERY_SEGMENT_SECONDS"] == "300"
    assert environment["VISIONQUERY_FOOTAGE_DIR"] == "/footage"
    (mount,) = recorder["volumes"]
    assert (mount["type"], mount["source"], mount["target"]) == ("volume", "footage", "/footage")
    assert not mount.get("read_only")
    # Footage is Visual Data (ADR 0002): no other service mounts it, and nothing is published.
    mounting = [
        name
        for name, service in _services(config).items()
        if any(m.get("source") == "footage" for m in service.get("volumes", []))
    ]
    assert mounting == ["recorder"]
    assert "ports" not in recorder
    assert recorder["depends_on"]["mediamtx"]["condition"] == "service_healthy"
    assert recorder["healthcheck"]["test"] == ["CMD", "visionquery-recorder", "health"]


def test_the_image_ships_the_ffmpeg_the_fake_cameras_run_and_a_footage_directory() -> None:
    """One ffmpeg, pinned once in each Dockerfile, so the recorder's tests (which take it from
    the root Dockerfile) and the fake cameras exercise the same build."""
    stage = re.compile(r"^FROM (\S+) AS ffmpeg$", re.M)
    dockerfile = (ROOT / "Dockerfile").read_text()
    fake_cameras = (ROOT / "docker" / "fake-cameras" / "Dockerfile").read_text()
    (ours,) = stage.findall(dockerfile)
    assert stage.findall(fake_cameras) == [ours]
    runtime = dockerfile[dockerfile.index("AS runtime") :]
    assert "COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg" in runtime
    # A named volume takes the ownership of the directory it is first mounted on.
    assert re.search(r"mkdir /footage\s*\\?\s*&& chown visionquery:visionquery /footage", runtime)


def test_the_fake_cameras_are_registered_and_reconciled_before_they_publish() -> None:
    services = _services(_config(profiles=["dev"], env={"VISIONQUERY_FAKE_CAMERA_PASSWORD": "p"}))
    register, cameras = services["register-fake-cameras"], services["fake-cameras"]
    assert register["profiles"] == ["dev"]
    assert register["restart"] == "no"
    assert register["command"] == ["python", "/scripts/register_fake_cameras.py"]
    mounts = {Path(m["source"]): (m["target"], m["read_only"]) for m in register["volumes"]}
    assert mounts == {
        ROOT / "scripts" / "register_fake_cameras.py": ("/scripts/register_fake_cameras.py", True),
        ROOT / "docker" / "fake-cameras" / "fake_cameras.py": ("/scripts/fake_cameras.py", True),
    }
    # The same cameras, with the same password, as the fake cameras publish with.
    registered = register["environment"]
    assert registered["VISIONQUERY_FAKE_CAMERAS"] == cameras["environment"]["FAKE_CAMERAS"]
    assert registered["VISIONQUERY_FAKE_CAMERA_PASSWORD"] == "p"
    assert cameras["environment"]["FAKE_CAMERAS_PASSWORD"] == "p"
    after = cameras["depends_on"]
    assert after["register-fake-cameras"]["condition"] == "service_completed_successfully"
    assert after["reconciler"]["condition"] == "service_healthy"


def _make_dry_run(*arguments: str) -> list[str]:
    return subprocess.run(
        ["make", "--dry-run", "--no-print-directory", *arguments, "COMPOSE=compose-under-test"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def test_make_up_builds_first_with_attestations_off() -> None:
    # `pull_policy: never` alone would leave a stale image running after an edit. `make up`
    # therefore builds first; BuildKit's attestations are off for every Compose call, because
    # they carry a timestamp that gives each build a new image ID and so recreates containers.
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^export BUILDX_NO_DEFAULT_ATTESTATIONS\s*[:?]?=\s*1\s*$", makefile, re.M)
    commands = [line for line in _make_dry_run("up") if "compose-under-test " in line]
    assert commands[0] == "compose-under-test build"
    assert commands[1].startswith("compose-under-test up --detach migrate")


def test_make_recipes_stop_at_the_first_failure_and_need_no_host_python() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(
        r"^SHELL\s*:?=\s*/usr/bin/env bash\s*$|^SHELL\s*:?=\s*bash\s*$", makefile, re.M
    )
    assert re.search(r"^\.SHELLFLAGS\s*:?=\s*-euo pipefail -c\s*$", makefile, re.M)
    assert "python" not in makefile


def test_make_up_starts_exactly_the_long_running_services() -> None:
    # The one-shot jobs are left out of `up --wait`, which would otherwise fail on them. The
    # Makefile lists them, and this checks the list against compose.yaml.
    config = _config()
    one_shots = {n for n, s in _services(config).items() if s.get("restart", "no") == "no"}
    makefile = (ROOT / "Makefile").read_text()
    listed = re.search(r"^ONE_SHOTS\s*\??=\s*(.*)$", makefile, re.M)
    assert listed
    assert set(listed[1].split()) == one_shots


def test_make_up_bounds_every_wait() -> None:
    commands = _make_dry_run("up")
    wait = next(c for c in commands if " wait migrate" in c)
    assert wait.startswith("timeout "), wait
    up = next(c for c in commands if "--wait" in c)
    assert "--wait-timeout" in up, up
    commands = _make_dry_run("up", "WAIT_TIMEOUT=7", "MIGRATE_TIMEOUT=3")
    assert any(c.startswith("timeout 3 ") for c in commands)
    assert any("--wait-timeout 7" in c for c in commands)


def test_make_down_stops_profile_services_and_keeps_data_unless_told() -> None:
    # Without `--profile '*'`, `down` leaves a service whose profile is not enabled running
    # (Triton, holding the GPU).
    (down,) = [c for c in _make_dry_run("down") if "compose-under-test" in c]
    assert "--profile '*' down" in down
    assert "--volumes" not in down
    (wiped,) = [c for c in _make_dry_run("down", "VOLUMES=1") if "compose-under-test" in c]
    assert "--profile '*' down" in wiped
    assert "--volumes" in wiped


# --- The dev profile: fake cameras ------------------------------------------------------------


def _fake_cameras(env: Mapping[str, str | None] | None = None) -> dict[str, Any]:
    return _services(_config(profiles=["dev"], env=env))["fake-cameras"]


def test_only_the_dev_profile_runs_fake_cameras() -> None:
    assert _services(_config())["fake-cameras"]["profiles"] == ["dev"]


def test_fake_cameras_publish_into_mediamtx_once_it_is_healthy() -> None:
    cameras = _fake_cameras()
    assert cameras["environment"]["FAKE_CAMERAS_RTSP"] == "rtsp://mediamtx:8554"
    assert cameras["depends_on"]["mediamtx"]["condition"] == "service_healthy"
    # They publish into MediaMTX, so nothing of theirs is published on the host.
    assert "ports" not in cameras


def test_the_cameras_and_their_videos_come_from_the_environment() -> None:
    chosen = "gate=walk.mp4,lobby=synthetic,yard=walk.mp4"
    cameras = _fake_cameras(env={"VISIONQUERY_FAKE_CAMERAS": chosen})
    assert cameras["environment"]["FAKE_CAMERAS"] == chosen


def test_every_sample_is_cut_to_the_same_duration_the_environment_names() -> None:
    assert _fake_cameras()["environment"]["FAKE_CAMERAS_DURATION"] == "300"
    chosen = _fake_cameras(env={"VISIONQUERY_FAKE_CAMERA_DURATION": "20"})
    assert chosen["environment"]["FAKE_CAMERAS_DURATION"] == "20"
    assert _example()["VISIONQUERY_FAKE_CAMERA_DURATION"] == "300"


def _mount(service: Mapping[str, Any], target: str) -> dict[str, Any]:
    (mount,) = [m for m in service["volumes"] if m["target"] == target]
    return mount


def test_the_samples_are_read_from_a_directory_the_environment_names() -> None:
    default = _mount(_fake_cameras(), "/samples")
    assert (default["type"], Path(default["source"]), default["read_only"]) == (
        "bind",
        ROOT / "samples",
        True,
    )
    elsewhere = _mount(
        _fake_cameras(env={"VISIONQUERY_FAKE_CAMERA_SAMPLES": "/srv/videos"}), "/samples"
    )
    assert elsewhere["source"] == "/srv/videos"


def test_prepared_samples_are_kept_in_a_volume_of_their_own() -> None:
    cache = _mount(_fake_cameras(), "/cache")
    assert (cache["type"], cache["source"]) == ("volume", "fake-cameras")


def test_fake_cameras_are_healthy_only_while_every_camera_publishes() -> None:
    assert _fake_cameras()["healthcheck"]["test"] == ["CMD", "fake-cameras", "health"]


def test_the_default_cameras_loop_the_fetched_samples() -> None:
    cameras = parse_cameras(_example()["VISIONQUERY_FAKE_CAMERAS"])
    samples = {sample.file for sample in load_manifest(MANIFEST)}
    assert len(cameras) >= 3
    assert {c.source for c in cameras} - {SYNTHETIC} == samples


def test_make_fetch_samples_runs_the_script_with_uv() -> None:
    (command,) = [c for c in _make_dry_run("fetch-samples") if "fetch_samples" in c]
    assert command == "uv run scripts/fetch_samples.py"
