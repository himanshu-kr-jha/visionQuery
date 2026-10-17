"""The setup guide: README's prerequisites and commands, and CLAUDE.md's Commands table.

The README takes a fresh clone to a running stack with fake cameras in a few commands, which the
`setup-guide` CI job runs as written (tests/test_ci.py), so they cannot rot. These tests keep
what the guide states in step with what the code needs: the minimum versions `make bootstrap`
checks, the Node.js major `web/.nvmrc` pins, the camera and port `.env.example` sets, and the
make targets and tests that CLAUDE.md's Commands table names.
"""

import copy
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from bootstrap import MINIMUM_BUILDX, MINIMUM_COMPOSE, MINIMUM_DOCKER

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CLAUDE = ROOT / "CLAUDE.md"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SETUP = ROOT / ".github" / "actions" / "setup" / "action.yml"
NODE = (ROOT / "web" / ".nvmrc").read_text().strip()


def _section(text: str, heading: str) -> str:
    """The body of the Markdown section under `heading`, up to the next heading of its level."""
    level = heading.split(" ", 1)[0]
    start = text.index(f"\n{heading}\n")
    end = re.search(rf"^{level} ", text[start + len(heading) + 2 :], re.MULTILINE)
    return text[start : start + len(heading) + 2 + end.start()] if end else text[start:]


def prerequisites() -> str:
    return _section(README.read_text(), "### Prerequisites")


def quick_start() -> list[str]:
    """The commands of the README's block that goes from a fresh clone to a stack and back, one
    per line, without its comments."""
    section = _section(README.read_text(), "### From a fresh clone")
    blocks = re.findall(r"^```sh\n(.*?)^```$", section, re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, "the section has one block of commands"
    return [line for line in blocks[0].splitlines() if line.strip() and not line.startswith("#")]


def _shown(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version))


def _example() -> dict[str, str]:
    text = (ROOT / ".env.example").read_text()
    return dict(line.split("=", 1) for line in text.splitlines() if line and line[0] != "#")


# --- Prerequisites ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requirement",
    [
        f"Docker Engine {_shown(MINIMUM_DOCKER)}",
        f"Compose plugin {_shown(MINIMUM_COMPOSE)}",
        f"buildx plugin {_shown(MINIMUM_BUILDX)}",
        "[uv](https://docs.astral.sh/uv/)",
        f"Node.js {NODE}",
        "npm",
        "NVIDIA driver",
        "NVIDIA Container Toolkit",
        "GNU Make",
        "curl",
        "git",
    ],
)
def test_the_readme_lists_each_prerequisite_at_the_version_that_is_checked(
    requirement: str,
) -> None:
    assert requirement in prerequisites()


def test_the_readme_says_how_to_install_uv_and_node_and_to_use_docker_without_sudo() -> None:
    text = prerequisites()
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in text
    assert f"nvm install {NODE}" in text
    assert "https://docs.docker.com/engine/install/linux-postinstall/" in text
    assert "nvidia-ctk runtime configure --runtime=docker" in text


# --- From a fresh clone -----------------------------------------------------------------------


def test_the_quick_start_clones_bootstraps_starts_checks_and_stops() -> None:
    commands = quick_start()
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    clone, cd, bootstrap, up, *checks, down = commands
    assert clone == "git clone https://github.com/himanshu-kr-jha/visionQuery.git"
    if url.startswith("https://github.com/"):
        assert clone.split()[-1].removesuffix(".git") == url.removesuffix(".git")
    assert cd == "cd visionQuery"
    assert bootstrap == "make bootstrap"
    # The dev profile's fake cameras need no download: a camera whose sample is not fetched
    # shows a test pattern.
    assert up == "make up COMPOSE_PROFILES=dev"
    assert down == "make down VOLUMES=1"
    assert len(checks) == 2


def test_the_quick_start_checks_the_health_the_api_publishes() -> None:
    (health,) = [c for c in quick_start() if "/health" in c]
    port = _example()["VISIONQUERY_API_PORT"]
    # --fail-with-body: exits non-zero on the 503 of an api that cannot reach the database,
    # printing the problem it names.
    assert health == (
        f"curl --silent --show-error --fail-with-body --max-time 10 http://127.0.0.1:{port}/health"
    )


def test_the_quick_start_reads_a_frame_from_a_camera_the_example_lists() -> None:
    (read,) = [c for c in quick_start() if "rtsp://" in c]
    camera = re.search(r"rtsp://mediamtx:8554/([\w-]+)", read)
    assert camera is not None
    cameras = [pair.split("=")[0] for pair in _example()["VISIONQUERY_FAKE_CAMERAS"].split(",")]
    assert camera.group(1) in cameras
    # From inside the project's network, with the ffmpeg the fake cameras already have, so the
    # check needs nothing installed on the host; one decoded frame proves the camera plays.
    assert read.startswith("docker compose --profile dev exec fake-cameras ffmpeg ")
    assert "-frames:v 1 -f null -" in read
    # A camera that does not answer must fail the check, not hang it: the socket timeout is
    # in microseconds.
    assert re.search(r"-timeout \d+ ", read)


def test_every_make_target_the_readme_runs_exists() -> None:
    for command in quick_start():
        if command.startswith("make "):
            target = command.split()[1]
            dry = subprocess.run(
                ["make", "--dry-run", "--no-print-directory", target],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            assert dry.returncode == 0, dry.stderr


# --- The CI job that follows them -------------------------------------------------------------


def _yaml(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text())


def _job() -> dict[str, Any]:
    return _yaml(WORKFLOW)["jobs"]["setup-guide"]


def test_the_setup_guide_job_runs_the_readme_commands_as_written() -> None:
    # The README's commands from a fresh clone, on a clean runner, so that they cannot rot.
    # The checkout is the clone; every other command is a step of its own, in order, with
    # nothing else run between them but diagnostics after a failure.
    job = _job()
    clone, cd, *commands = quick_start()
    assert clone.startswith("git clone ")
    assert cd.startswith("cd ")
    assert job["steps"][0]["uses"].startswith("actions/checkout@")
    runs = [(step["run"].strip(), step.get("if")) for step in job["steps"] if "run" in step]
    followed = [run for run, condition in runs if condition in (None, "always()")]
    assert followed == commands
    for run, condition in runs:
        assert condition in (None, "always()", "failure()"), run
    # The stack is taken down even when a step before failed.
    assert runs[-1] == (commands[-1], "always()")


#: What a step that runs a command may carry: nothing that changes how it runs.
RUN_KEYS = {"name", "run", "if"}


def _job_surprises(job: dict[str, Any]) -> list[str]:
    """What a job has beyond the checkout, the setup action and the README's commands."""
    surprises = [key for key in ("env", "defaults", "container", "services") if key in job]
    steps = job["steps"]
    uses = [step["uses"] for step in steps if "uses" in step]
    if len(uses) != 2 or "uses" not in steps[0] or "uses" not in steps[1]:
        surprises.append(f"uses steps: {uses}")
    for step in steps:
        if "run" in step:
            surprises += [f"{key} on {step['run']!r}" for key in step if key not in RUN_KEYS]
    return surprises


def _mutations() -> dict[str, dict[str, Any]]:
    jobs = {
        name: copy.deepcopy(_job())
        for name in ("env", "defaults", "container", "uses", "step", "dir")
    }
    jobs["env"]["env"] = {"X": "1"}
    jobs["defaults"]["defaults"] = {"run": {"shell": "sh"}}
    jobs["container"]["container"] = "ubuntu"
    jobs["uses"]["steps"].insert(2, {"uses": "actions/cache@v4"})
    jobs["step"]["steps"][2]["env"] = {"X": "1"}
    jobs["dir"]["steps"][-2]["working-directory"] = "web"
    return jobs


def test_the_setup_guide_job_has_nothing_but_the_readme_commands_around_it() -> None:
    assert _job_surprises(_job()) == []


@pytest.mark.parametrize("name", list(_mutations()))
def test_a_setup_guide_job_that_changes_how_the_commands_run_is_noticed(name: str) -> None:
    assert _job_surprises(_mutations()[name]), name


def test_the_setup_guide_job_starts_from_the_prerequisites_alone() -> None:
    # The runner gets uv and Node as a developer installs them, and nothing more: installing
    # the workspace and writing .env is what `make bootstrap` has to do.
    assert _job()["steps"][1] == {
        "uses": "./.github/actions/setup",
        "with": {"web": True, "sync": False},
    }
    assert _yaml(SETUP)["inputs"]["sync"]["default"] == "true"
    for step in _yaml(SETUP)["runs"]["steps"]:
        if "run" in step:
            assert "inputs.sync == 'true'" in step["if"], step


# --- CLAUDE.md's Commands ---------------------------------------------------------------------


def _commands_table() -> dict[str, str]:
    """CLAUDE.md's Commands table: what each row is for, by its command."""
    section = _section(CLAUDE.read_text(), "## Commands")
    rows = re.findall(r"^\| (.+?) \| (.+?) \|$", section, re.MULTILINE)
    return {command: what for what, command in rows if what != "What"}


def _commands() -> str:
    return "\n".join(_commands_table())


@pytest.mark.parametrize(
    ("purpose", "command"),
    [
        ("bootstrap", "`make bootstrap`"),
        ("build", "`make build`"),
        ("lint", "`make lint`"),
        ("type check", "`make typecheck`"),
        ("contracts", "`make contracts`"),
        ("all tests", "`make test`"),
        ("a single test", "`uv run pytest tests/"),
        ("migrations", "uv run visionquery-migrate`"),
        ("running the stack", "`make up`"),
    ],
)
def test_claude_md_lists_each_command(purpose: str, command: str) -> None:
    assert command in _commands(), purpose


def test_every_make_target_claude_md_names_exists() -> None:
    targets = {
        word
        for command in _commands_table()
        for match in re.finditer(r"`make ([^`]*)`", command)
        for word in match.group(1).split()
        if "=" not in word
    }
    assert "bootstrap" in targets
    for target in targets:
        dry = subprocess.run(
            ["make", "--dry-run", "--no-print-directory", target],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert dry.returncode == 0, f"make {target}: {dry.stderr}"


def test_the_single_test_claude_md_shows_exists() -> None:
    (single,) = [c for c, what in _commands_table().items() if what == "A single test"]
    match = re.search(r"`uv run pytest ([\w/.]+)::(\w+)`", single)
    assert match is not None, single
    path, name = match.groups()
    assert re.search(rf"^def {name}\(", (ROOT / path).read_text(), re.MULTILINE)
