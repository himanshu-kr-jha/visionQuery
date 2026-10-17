"""`make bootstrap`: one command from a fresh clone to a development environment.

It checks every prerequisite the README lists and names each one that is missing or too old,
with how to get it, before changing anything; then writes `.env` from `.env.example` with a
generated password for every secret it leaves empty (never touching an `.env` that exists), and
installs the workspace and the web app from their lockfiles (`make sync`).

Here every tool it runs (docker, node, npm, make) is a fake on a PATH of its own that answers as
the version under test would, and the repository is a temporary copy of the files it reads, so
nothing real is checked, installed or written.
"""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from bootstrap import MINIMUM_BUILDX, MINIMUM_COMPOSE, MINIMUM_DOCKER, parse_version

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap.py"
EXAMPLE = ROOT / ".env.example"
NODE = int((ROOT / "web" / ".nvmrc").read_text().strip())
#: What a secret is generated as: `openssl rand -hex 24`, as `.env.example` suggests.
SECRET = re.compile(r"[0-9a-f]{48}")

#: The fake docker: each answer comes from an environment variable, and an empty one means
#: that part (the daemon, a plugin) is missing.
FAKE_DOCKER = """\
import os, sys
arguments = sys.argv[1:]
def answer(variable):
    value = os.environ[variable]
    if not value:
        print(f"docker: '{' '.join(arguments)}' failed: {variable} is unset", file=sys.stderr)
        sys.exit(1)
    print(value)
if arguments[:1] == ["version"]:
    answer("FAKE_DOCKER_SERVER")
elif arguments[:3] == ["compose", "version", "--short"]:
    answer("FAKE_COMPOSE")
elif arguments[:2] == ["buildx", "version"]:
    answer("FAKE_BUILDX")
else:
    sys.exit(f"the fake docker was not expecting {arguments}")
"""
FAKE_NODE = """\
import os
print(os.environ["FAKE_NODE"])
"""
FAKE_NPM = """\
print("11.6.2")
"""
#: The fake make records how it was called and fails when told to.
FAKE_MAKE = """\
import os, sys
with open(os.environ["FAKE_MAKE_LOG"], "a") as log:
    log.write(f"{os.getcwd()} {' '.join(sys.argv[1:])}\\n")
sys.exit(int(os.environ.get("FAKE_MAKE_STATUS", "0")))
"""
FAKES = {"docker": FAKE_DOCKER, "node": FAKE_NODE, "npm": FAKE_NPM, "make": FAKE_MAKE}

#: Tools as a development machine that meets every prerequisite has them.
GOOD = {
    "FAKE_DOCKER_SERVER": "29.6.2",
    "FAKE_COMPOSE": "5.1.3",
    "FAKE_BUILDX": "github.com/docker/buildx v0.33.0 f7897eba028583e0071642db3c011e860444f8cf",
    "FAKE_NODE": f"v{NODE}.14.1",
}


class Machine:
    """A temporary copy of the repository's inputs, and a PATH holding only the fake tools."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "clone"
        (self.root / "web").mkdir(parents=True)
        (self.root / ".env.example").write_text(EXAMPLE.read_text())
        (self.root / "web" / ".nvmrc").write_text(f"{NODE}\n")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.make_log = tmp_path / "make.log"
        self.environment = dict(GOOD)
        for tool, source in FAKES.items():
            self.install(tool, source)

    def install(self, tool: str, source: str) -> None:
        path = self.bin / tool
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)

    def remove(self, tool: str) -> None:
        (self.bin / tool).unlink()

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    def makes(self) -> list[str]:
        return self.make_log.read_text().splitlines() if self.make_log.exists() else []

    def bootstrap(self) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": str(self.bin),
            "FAKE_MAKE_LOG": str(self.make_log),
            **self.environment,
        }
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return Machine(tmp_path)


def _variables(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#")
    )


def _secrets() -> list[str]:
    return [name for name, value in _variables(EXAMPLE.read_text()).items() if value == ""]


# --- A machine with every prerequisite -------------------------------------------------------


def test_a_fresh_clone_gets_an_env_with_every_secret_generated_and_the_rest_as_the_example(
    machine: Machine,
) -> None:
    result = machine.bootstrap()
    assert result.returncode == 0, result.stderr
    written = _variables(machine.env_file.read_text())
    example = _variables(EXAMPLE.read_text())
    assert _secrets(), "the example has secrets to generate"
    assert set(written) == set(example)
    for name, value in example.items():
        if value:
            assert written[name] == value, name
        else:
            assert SECRET.fullmatch(written[name]), name
    # Each secret is a password of its own.
    assert len({written[name] for name in _secrets()}) == len(_secrets())
    # The comments that explain each variable are kept.
    comments = [line for line in EXAMPLE.read_text().splitlines() if line.startswith("#")]
    assert [
        line for line in machine.env_file.read_text().splitlines() if line.startswith("#")
    ] == comments
    # The secrets are readable by their owner alone.
    assert stat.S_IMODE(machine.env_file.stat().st_mode) == 0o600
    assert "generated" in result.stdout


def test_it_installs_the_workspace_and_the_web_app_from_the_lockfiles(machine: Machine) -> None:
    result = machine.bootstrap()
    assert result.returncode == 0, result.stderr
    assert machine.makes() == [f"{machine.root} sync"]


def test_it_says_what_to_run_next(machine: Machine) -> None:
    result = machine.bootstrap()
    assert result.returncode == 0, result.stderr
    assert "make up" in result.stdout


def test_it_never_overwrites_an_existing_env(machine: Machine) -> None:
    mine = EXAMPLE.read_text()
    for name in _secrets():
        mine = mine.replace(f"\n{name}=\n", f"\n{name}=chosen-by-me\n")
    mine += "MY_OWN=setting\n"
    machine.env_file.write_text(mine)
    result = machine.bootstrap()
    assert result.returncode == 0, result.stderr
    assert machine.env_file.read_text() == mine
    assert "kept" in result.stdout
    assert machine.makes() == [f"{machine.root} sync"]


def test_running_it_again_changes_nothing(machine: Machine) -> None:
    assert machine.bootstrap().returncode == 0
    first = machine.env_file.read_text()
    assert machine.bootstrap().returncode == 0
    assert machine.env_file.read_text() == first


def test_an_existing_env_that_leaves_a_secret_empty_is_named_and_left_alone(
    machine: Machine,
) -> None:
    # Compose would refuse to start; say so now, naming each, rather than at `make up`.
    machine.env_file.write_text(EXAMPLE.read_text())
    result = machine.bootstrap()
    assert result.returncode == 1
    for name in _secrets():
        assert name in result.stderr
    assert machine.env_file.read_text() == EXAMPLE.read_text()


def test_a_secret_missing_from_an_existing_env_is_named(machine: Machine) -> None:
    # As when .env.example gains a secret after .env was made.
    (missing, *_) = _secrets()
    kept = [line for line in EXAMPLE.read_text().splitlines() if not line.startswith(f"{missing}=")]
    filled = "\n".join(line + "x" if line.endswith("=") else line for line in kept) + "\n"
    machine.env_file.write_text(filled)
    result = machine.bootstrap()
    assert result.returncode == 1
    assert missing in result.stderr


def test_a_failing_sync_fails_the_bootstrap(machine: Machine) -> None:
    machine.environment["FAKE_MAKE_STATUS"] = "2"
    result = machine.bootstrap()
    assert result.returncode == 1
    assert "make sync" in result.stderr


# --- Prerequisites: each one named, with how to get it, before anything changes -------------


def _refused(machine: Machine, *expected: str) -> str:
    result = machine.bootstrap()
    assert result.returncode == 1, result.stdout
    for text in expected:
        assert text in result.stderr, result.stderr
    # Nothing was installed or written.
    assert machine.makes() == []
    assert not machine.env_file.exists()
    return result.stderr


def test_without_docker_it_says_to_install_docker_engine(machine: Machine) -> None:
    machine.remove("docker")
    _refused(machine, "Docker", "https://docs.docker.com/engine/install/")


def test_an_unreachable_docker_daemon_is_named(machine: Machine) -> None:
    machine.environment["FAKE_DOCKER_SERVER"] = ""
    _refused(machine, "daemon")


def test_an_old_docker_engine_is_named_with_the_minimum(machine: Machine) -> None:
    machine.environment["FAKE_DOCKER_SERVER"] = "24.0.7"
    _refused(machine, "24.0.7", f"Docker Engine {MINIMUM_DOCKER[0]}")


def test_a_missing_compose_plugin_is_named(machine: Machine) -> None:
    machine.environment["FAKE_COMPOSE"] = ""
    _refused(machine, "Compose", "https://docs.docker.com/compose/install/linux/")


def test_an_old_compose_is_named_with_the_minimum(machine: Machine) -> None:
    machine.environment["FAKE_COMPOSE"] = "v2.29.7"
    _refused(machine, "2.29.7", "Compose 2.30")


def test_a_missing_or_old_buildx_is_named(machine: Machine) -> None:
    machine.environment["FAKE_BUILDX"] = ""
    _refused(machine, "buildx")
    machine.environment["FAKE_BUILDX"] = "github.com/docker/buildx v0.16.2 abcdef"
    _refused(machine, "0.16.2", "buildx 0.17")


def test_without_node_it_names_the_version_web_nvmrc_pins(machine: Machine) -> None:
    machine.remove("node")
    _refused(machine, f"Node.js {NODE}", "web/.nvmrc")


def test_another_major_of_node_is_refused(machine: Machine) -> None:
    # npm refuses every other major (engines, engine-strict), newer ones included.
    for version in (f"v{NODE - 2}.11.0", f"v{NODE + 1}.0.0"):
        machine.environment["FAKE_NODE"] = version
        _refused(machine, version.removeprefix("v"), f"Node.js {NODE}")


def test_without_npm_it_says_so(machine: Machine) -> None:
    machine.remove("npm")
    _refused(machine, "npm")


def test_every_missing_prerequisite_is_named_at_once(machine: Machine) -> None:
    machine.remove("docker")
    machine.remove("node")
    _refused(machine, "Docker", f"Node.js {NODE}")


# --- Versions as the tools print them ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("29.6.2", (29, 6, 2)),
        ("25.0.3+dfsg1", (25, 0, 3)),
        ("28.0.0-rc.1", (28, 0, 0)),
        ("v2.30.0", (2, 30, 0)),
        ("2.39.4-desktop.1", (2, 39, 4)),
        ("github.com/docker/buildx v0.17.1 abc", (0, 17, 1)),
        ("github.com/docker/buildx 0.12.1+azure-1 abc", (0, 12, 1)),
        ("v24.14.1", (24, 14, 1)),
        ("25.0", (25, 0)),
    ],
)
def test_versions_are_read_as_each_tool_prints_them(text: str, version: tuple[int, ...]) -> None:
    assert parse_version(text) == version


def test_nonsense_is_no_version() -> None:
    assert parse_version("Docker version unknown") is None


def test_the_minimums_are_what_the_makefile_and_the_dockerfile_need() -> None:
    # `make up` needs `docker compose wait` (Compose 2.30), and the Dockerfile's `COPY --parents`
    # needs the frontend it pins, which Docker Engine 25's BuildKit fetches.
    assert MINIMUM_DOCKER == (25, 0)
    assert MINIMUM_COMPOSE == (2, 30)
    # Compose refuses to build with an older buildx.
    assert MINIMUM_BUILDX == (0, 17)


# --- The make target --------------------------------------------------------------------------


def test_make_bootstrap_runs_the_script_with_uv_only() -> None:
    dry = subprocess.run(
        ["make", "--dry-run", "--no-print-directory", "bootstrap"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # No project: running it must not sync, or update, the workspace before checking anything.
    assert "uv run --no-project scripts/bootstrap.py" in dry


def test_make_bootstrap_without_uv_says_how_to_install_it(tmp_path: Path) -> None:
    # Only what make and its shell need: no uv.
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for tool in ("bash", "make", "env"):
        found = subprocess.run(
            ["bash", "-c", f"command -v {tool}"], capture_output=True, text=True, check=True
        )
        (bin_directory / tool).symlink_to(found.stdout.strip())
    result = subprocess.run(
        ["make", "--no-print-directory", "bootstrap"],
        cwd=ROOT,
        env={"PATH": str(bin_directory), "HOME": os.environ.get("HOME", str(tmp_path))},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "uv" in result.stderr
    assert "https://docs.astral.sh/uv/" in result.stderr
