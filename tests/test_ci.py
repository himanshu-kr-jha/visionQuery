"""The CI workflow runs every check `make check` runs, and the stack tests, the way developers do.

Each job calls the same `make` target a developer runs, so CI and a local run cannot drift
apart: a target added to `make check` fails here until a job runs it. These tests read the
workflow; that it passes is shown by its runs on GitHub.
"""

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SETUP = ROOT / ".github" / "actions" / "setup" / "action.yml"
#: A full commit SHA, which unlike a tag cannot be moved to other code, and the version it is.
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _yaml(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text())


def _workflow() -> dict[Any, Any]:
    return _yaml(WORKFLOW)


def _jobs() -> dict[str, dict[str, Any]]:
    return _workflow()["jobs"]


def _steps() -> Iterator[dict[str, Any]]:
    for job in _jobs().values():
        yield from job["steps"]
    yield from _yaml(SETUP)["runs"]["steps"]


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def _make_targets(job: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    for line in _runs(job).splitlines():
        if line.strip().startswith("make "):
            targets.update(w for w in line.split()[1:] if "=" not in w and not w.startswith("-"))
    return targets


def test_it_runs_on_prs_and_pushes_to_develop_and_main_and_by_hand() -> None:
    # PyYAML reads the bare key `on` as True.
    triggers = _workflow()[True]
    assert triggers["pull_request"]["branches"] == ["develop", "main"]
    assert triggers["push"]["branches"] == ["develop", "main"]
    assert "workflow_dispatch" in triggers


def test_the_token_can_only_read() -> None:
    assert _workflow()["permissions"] == {"contents": "read"}


def test_every_action_is_pinned_to_a_commit_with_its_version_named() -> None:
    uses = [step["uses"] for step in _steps() if "uses" in step]
    assert uses
    for action in uses:
        assert action.startswith("./") or PINNED.match(action), action
    text = WORKFLOW.read_text() + SETUP.read_text()
    for line in text.splitlines():
        if re.search(r"uses: \w", line):
            assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line


def test_every_check_make_check_runs_has_a_job_and_so_does_the_stack() -> None:
    dry = subprocess.run(
        ["make", "--no-print-directory", "--print-data-base", "--question", "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    prerequisites = re.search(r"^check: (.*)$", dry, re.MULTILINE)
    assert prerequisites is not None
    in_check = set(prerequisites.group(1).split())
    run = {target for job in _jobs().values() for target in _make_targets(job)}
    assert in_check | {"test-stack"} <= run, in_check - run


def test_every_job_sets_up_the_same_way_and_has_a_timeout() -> None:
    for name, job in _jobs().items():
        assert job["runs-on"] == "ubuntu-24.04", name
        assert 0 < job["timeout-minutes"] <= 60, name
        assert job["steps"][0]["uses"].startswith("actions/checkout@"), name
        assert job["steps"][1]["uses"] == "./.github/actions/setup", name


def test_python_comes_from_uv_at_the_version_the_image_is_built_with() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    match = re.search(r"ghcr\.io/astral-sh/uv:([\d.]+)@", dockerfile)
    assert match is not None
    uv = next(s for s in _yaml(SETUP)["runs"]["steps"] if "astral-sh/setup-uv" in s.get("uses", ""))
    assert uv["with"]["version"] == match.group(1)
    assert uv["with"]["enable-cache"] is True
    assert "uv.lock" in uv["with"]["cache-dependency-glob"]
    # uv installs the Python .python-version pins (python-preference = "only-managed").
    assert "python-version" not in uv["with"]
    assert "uv sync --locked" in SETUP.read_text()


def test_node_is_the_version_web_nvmrc_names_and_npm_is_cached() -> None:
    node = next(
        s for s in _yaml(SETUP)["runs"]["steps"] if "actions/setup-node" in s.get("uses", "")
    )
    assert node["with"]["node-version-file"] == "web/.nvmrc"
    assert node["with"]["cache"] == "npm"
    assert node["with"]["cache-dependency-path"] == "web/package-lock.json"
    assert "make sync" in SETUP.read_text()


def test_the_python_tests_run_against_a_postgres_service_of_the_tests_own_image() -> None:
    job = _jobs()["test"]
    service = job["services"]["postgres"]
    conftest = (ROOT / "packages" / "conftest.py").read_text()
    image = re.search(r'^IMAGE = "(.+)"$', conftest, re.MULTILINE)
    password = re.search(r'^PASSWORD = "(.+)"$', conftest, re.MULTILINE)
    assert image is not None
    assert password is not None
    assert service["image"] == image.group(1)
    assert service["env"]["POSTGRES_PASSWORD"] == password.group(1)
    # Ready only once it listens on TCP: the image first initialises on a socket alone.
    assert "pg_isready --host 127.0.0.1" in service["options"]
    (url,) = (s["env"]["VISIONQUERY_TEST_DATABASE_URL"] for s in job["steps"] if "env" in s)
    assert url.startswith(f"postgresql://postgres:{password.group(1)}@127.0.0.1:")
    assert "job.services.postgres.ports['5432']" in url


def test_the_web_app_is_installed_wherever_a_check_needs_it() -> None:
    # The API types tests in `make test` need web/node_modules, and so does `make web`.
    for name in ("test", "web"):
        assert _jobs()[name]["steps"][1]["with"]["web"] is True, name


def test_pr_text_reaches_the_shell_only_through_the_environment() -> None:
    # Inlining a PR's title or body into a script would let it inject shell commands.
    for step in _steps():
        assert "github.event.pull_request" not in step.get("run", ""), step


def test_the_commits_job_checks_attribution_and_the_title_of_a_pr() -> None:
    job = _jobs()["commits"]
    runs = _runs(job)
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    assert "scripts/check_attribution.py --commits" in runs
    assert "--env PR_TITLE --env PR_BODY" in runs
    assert "scripts/check_commit_message.py --env PR_TITLE" in runs
    environment = {k: v for step in job["steps"] for k, v in step.get("env", {}).items()}
    assert environment["PR_TITLE"] == "${{ github.event.pull_request.title }}"
    assert environment["PR_BODY"] == "${{ github.event.pull_request.body }}"


def test_a_newer_push_to_a_pr_cancels_the_run_it_supersedes() -> None:
    concurrency = _workflow()["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
