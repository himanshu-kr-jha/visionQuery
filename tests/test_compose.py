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

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
NO_GPU = ROOT / "compose.no-gpu.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
PROMETHEUS = ROOT / "docker" / "prometheus" / "prometheus.yml"
MEDIAMTX = ROOT / "docker" / "mediamtx" / "mediamtx.yml"
GRAFANA_DATASOURCES = ROOT / "docker" / "grafana" / "provisioning" / "datasources"

PROFILES = ("dev", "gpu", "observability")
#: What `docker compose up` starts with no profile, on a machine without a GPU.
DEFAULT_SERVICES = {"postgres", "migrate", "mediamtx"}
#: What each profile adds. `dev` is where the fake camera publisher (#6) arrives.
PROFILE_SERVICES: dict[str, set[str]] = {
    "dev": set(),
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
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "--project-directory", str(ROOT), "-f", str(COMPOSE)]
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
) -> dict[str, Any]:
    result = _compose("config", "--format", "json", profiles=profiles, files=files, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _services(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return config["services"]


def _long_running(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Services that stay up, as opposed to one-shot jobs such as `migrate`."""
    return {n: s for n, s in _services(config).items() if s.get("restart", "no") != "no"}


def _is_visionquery_process(service: Mapping[str, Any]) -> bool:
    build = service.get("build")
    return build is not None and Path(build["context"]).resolve() == ROOT


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
        "VISIONQUERY_GRAFANA_ADMIN_PASSWORD",
        "VISIONQUERY_MEDIAMTX_API_PASSWORD",
        "VISIONQUERY_POSTGRES_PASSWORD",
    ]


def test_env_example_documents_exactly_the_variables_compose_reads() -> None:
    read = {m["name"] for file in (COMPOSE, NO_GPU) for m in VARIABLE.finditer(file.read_text())}
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
            # A built image is named for this project, so it is never pulled from a registry.
            assert service.get("pull_policy") == "missing", name
        else:
            images.append(service["image"])
    assert images
    unpinned = [image for image in images if not PINNED.match(image)]
    assert unpinned == []


def test_up_builds_an_image_only_when_it_is_missing() -> None:
    # A rebuild gives a new image ID even when no layer changed, and Compose recreates a
    # container whose image ID changed, which drops live streams. So a bare `up` must not
    # rebuild; `make up` builds first, without attestations, which keeps the ID stable
    # (the next test, and tests/test_stack.py, which checks that `make up` recreates nothing).
    built = {n: s for n, s in _services(_config()).items() if "build" in s}
    assert set(built) >= {"migrate", "mediamtx"}
    assert {name: s.get("pull_policy") for name, s in built.items()} == dict.fromkeys(
        built, "missing"
    )


def test_every_service_built_from_our_image_waits_for_the_migrations_to_succeed() -> None:
    # Otherwise a long-running process could start against a schema that is not up to date,
    # or that a failed migration left behind.
    for name, service in _services(_config()).items():
        if name == "migrate" or not _is_visionquery_process(service):
            continue
        condition = service.get("depends_on", {}).get("migrate", {}).get("condition")
        assert condition == "service_completed_successfully", name


def test_the_database_password_is_not_part_of_the_url() -> None:
    # A password with `@`, `/`, `:` or `#` in it would break a URL; libpq reads it from
    # PGPASSWORD instead, so any password works.
    password = "p@ss/w:rd#1"
    migrate = _services(_config(env={"VISIONQUERY_POSTGRES_PASSWORD": password}))["migrate"]
    environment = migrate["environment"]
    assert password not in environment["VISIONQUERY_DATABASE__URL"]
    assert "@" in environment["VISIONQUERY_DATABASE__URL"]
    assert "visionquery:" not in environment["VISIONQUERY_DATABASE__URL"]
    assert environment["PGPASSWORD"] == password


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


def test_ports_are_published_on_loopback_unless_told_otherwise() -> None:
    published = _published(_config())
    assert published
    assert {host_ip for _, host_ip, _, _ in published} == {"127.0.0.1"}
    moved = _published(_config(env={"VISIONQUERY_BIND_ADDRESS": "0.0.0.0"}))
    assert {host_ip for _, host_ip, _, _ in moved} == {"0.0.0.0"}


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
        if _is_visionquery_process(service):
            expected = f"{name}:{RUNTIME_METRICS_PORT}"
        else:
            assert name in METRICS, f"say where {name} serves its metrics"
            expected = METRICS[name]
        assert expected in scraped, f"Prometheus does not scrape {name} at {expected}"


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


def test_make_up_builds_first_with_attestations_off() -> None:
    # `pull_policy: missing` alone would leave a stale image running after an edit. `make up`
    # therefore builds first; BuildKit's attestations are off for every Compose call, because
    # they carry a timestamp that gives each build a new image ID and so recreates containers.
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^export BUILDX_NO_DEFAULT_ATTESTATIONS\s*[:?]?=\s*1\s*$", makefile, re.M)
    dry_run = subprocess.run(
        ["make", "--dry-run", "--no-print-directory", "up", "COMPOSE=compose-under-test"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    commands = [line for line in dry_run if line.startswith("compose-under-test ")]
    assert commands[0] == "compose-under-test build"
    assert commands[1].startswith("compose-under-test up --detach migrate")
