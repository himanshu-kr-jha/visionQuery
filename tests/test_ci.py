"""The CI workflow runs every check `make check` runs, and the stack tests, the way developers do.

Each job calls the same `make` target a developer runs, so CI and a local run cannot drift
apart: a target added to `make check` fails here until a job runs it. These tests read the
workflow; that it passes is shown by its runs on GitHub.
"""

import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "ci.yml"
#: Re-runs the commits job alone when a PR's title, body or base is edited.
EDITED = WORKFLOWS / "pr-edited.yml"
SETUP = ROOT / ".github" / "actions" / "setup" / "action.yml"
#: A full commit SHA, which unlike a tag cannot be moved to other code, and the version it is.
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _yaml(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text())


def _workflow() -> dict[Any, Any]:
    return _yaml(WORKFLOW)


def _jobs() -> dict[str, dict[str, Any]]:
    return _workflow()["jobs"]


def _all_workflows() -> dict[str, dict[Any, Any]]:
    return {path.name: _yaml(path) for path in sorted(WORKFLOWS.glob("*.yml"))}


def _steps() -> Iterator[dict[str, Any]]:
    for workflow in _all_workflows().values():
        for job in workflow["jobs"].values():
            yield from job["steps"]
    yield from _yaml(SETUP)["runs"]["steps"]


def _jobs_run_on_pull_request(action: str) -> set[str]:
    """The jobs, of every workflow, that a pull_request event with this action starts."""
    started: set[str] = set()
    for workflow in _all_workflows().values():
        pull_request = workflow[True].get("pull_request")
        if pull_request is not None and action in pull_request["types"]:
            started.update(workflow["jobs"])
    return started


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def _make_targets(job: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    for line in _runs(job).splitlines():
        if line.strip().startswith("make "):
            targets.update(w for w in line.split()[1:] if "=" not in w and not w.startswith("-"))
    return targets


def test_it_runs_on_prs_and_pushes_to_develop_and_main() -> None:
    # PyYAML reads the bare key `on` as True.
    triggers = _workflow()[True]
    assert triggers["pull_request"]["branches"] == ["develop", "main"]
    # An edit of the title, body or base is pr-edited.yml's, which runs the commits job alone.
    assert triggers["pull_request"]["types"] == ["opened", "synchronize", "reopened"]
    assert triggers["push"]["branches"] == ["develop", "main"]
    # No manual trigger: a run started by hand would replace the result of a required check on
    # the same SHA. A run is repeated with `gh run rerun`, which keeps its event.
    assert set(triggers) == {"pull_request", "push"}


def test_the_token_can_only_read() -> None:
    assert _workflow()["permissions"] == {"contents": "read"}
    # The commits job alone reads the PR (its live title, body and base), and only reads it.
    assert _jobs()["commits"]["permissions"] == {"contents": "read", "pull-requests": "read"}
    for name, job in _jobs().items():
        assert name == "commits" or "permissions" not in job, name


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
    assert uv["with"]["cache-python"] is True
    # Every job fills the cache alike, so one saves it rather than all racing to.
    assert uv["with"]["save-cache"] == "${{ github.job == 'lint' }}"
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


#: What an event's payload holds of a PR, frozen when the event was sent.
STALE_PAYLOAD = re.compile(r"github\.event\.pull_request\.(title|body|base\b|head)")


def test_the_commits_job_reads_the_pr_as_it_is_now_not_as_its_event_was() -> None:
    # `gh run rerun` and a late finisher reuse an old event's payload: a check computed from its
    # title and body could be green for text that has since been changed to carry attribution,
    # and be the latest `commits` result for the head. The job reads the PR when it runs.
    for path in (WORKFLOW, EDITED):
        job = _yaml(path)["jobs"]["commits"]
        assert not STALE_PAYLOAD.search(json.dumps(job)), path.name
        reads = [s for s in job["steps"] if "gh api" in s.get("run", "")]
        assert len(reads) == 1, path.name
        (read,) = reads
        assert "pulls/$PR_NUMBER" in read["run"], path.name
        assert read["env"]["GH_TOKEN"] == "${{ github.token }}", path.name
        assert read["env"]["REPO"] == "${{ github.repository }}", path.name
        assert read["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}", path.name
        for field in ("title", "body", "base.sha", "head.sha"):
            assert f".{field}" in read["run"], (path.name, field)


def test_a_changed_base_fails_the_commits_job() -> None:
    # Edits re-run only `commits`, so a PR moved to another base would keep its other results,
    # merged against the old base. The step is in both copies of the job (they must stay equal)
    # and does something only when the event says the base changed, which only an edit says.
    for path in (WORKFLOW, EDITED):
        steps = _yaml(path)["jobs"]["commits"]["steps"]
        (step,) = (s for s in steps if s.get("if") == "github.event.changes.base")
        assert "exit 1" in step["run"], path.name
        assert "close and reopen" in step["run"], path.name


def test_the_commits_job_checks_something_on_every_event_that_triggers_it() -> None:
    # A step guarded by an event never runs on another, so an event none of the steps accepts
    # would pass the (required) job having checked nothing.
    for path in (WORKFLOW, EDITED):
        workflow = _yaml(path)
        checks = [step for step in workflow["jobs"]["commits"]["steps"][2:] if "run" in step]
        assert checks
        for event in workflow[True]:
            guards = [step.get("if") for step in checks]
            assert any(
                guard is None or f"github.event_name == '{event}'" in guard for guard in guards
            ), (path.name, event)


def test_a_newer_push_to_a_pr_cancels_the_run_it_supersedes() -> None:
    concurrency = _workflow()["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_editing_a_pr_neither_cancels_nor_is_cancelled_by_the_run_of_a_push() -> None:
    # The edited run's group is its own, per PR: editing the title mid-run must not cancel a
    # full run for the same head (which would leave its checks cancelled), and a push cancels
    # the full run, not the edit's. A newer edit makes an older edit's run pointless.
    full = _workflow()["concurrency"]["group"]
    edited = _yaml(EDITED)["concurrency"]
    assert full == "ci-${{ github.event.pull_request.number || github.ref }}"
    assert edited["group"] == "pr-edited-${{ github.event.pull_request.number }}"
    assert edited["cancel-in-progress"] is True


def test_make_lint_lints_the_workflows_too() -> None:
    # actionlint catches what only GitHub would otherwise report, after a push: an expression
    # in a context that does not have it, an unknown input, a shell error in a `run:`.
    dry = subprocess.run(
        ["make", "--dry-run", "--no-print-directory", "lint"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "uv run actionlint" in dry


def test_editing_a_pr_re_runs_only_the_commits_job() -> None:
    # A title or body is all an edit can fix, and the commits job is all that reads them; the
    # rest of the suite, the 7-minute stack included, keeps its result for the head.
    assert _jobs_run_on_pull_request("edited") == {"commits"}
    assert _jobs_run_on_pull_request("synchronize") == set(_jobs())


def test_the_edited_run_is_the_ci_commits_job_itself_on_the_same_branches() -> None:
    # Its job id is its check's name: the same `commits` the rulesets require, so its result
    # replaces the full run's for that check, and it adds no check of its own.
    edited = _yaml(EDITED)
    assert edited[True] == {"pull_request": {"branches": ["develop", "main"], "types": ["edited"]}}
    assert edited["permissions"] == _workflow()["permissions"]
    assert edited["jobs"] == {"commits": _jobs()["commits"]}


def test_no_ci_job_is_skipped_by_a_condition() -> None:
    # A job skipped by its `if:` reports success to a required check: skipping the suite on an
    # edit that way would let an edit turn a red check green. Edits get a workflow of their own.
    for name, job in _jobs().items():
        assert "if" not in job, name


def test_no_workflow_reports_a_required_check_but_ci_and_the_edited_commits_job() -> None:
    # A same-named job elsewhere (a `pull_request_target`, `workflow_run` or `push` workflow with
    # a job `lint` or `stack`) would add another check run of that name for the head, and the
    # rulesets cannot tell workflows apart. The edited run's `commits` is the one intended.
    required = set(_jobs())
    for name, workflow in _all_workflows().items():
        if name == WORKFLOW.name:
            continue
        clashes = required & set(workflow["jobs"])
        assert clashes <= ({"commits"} if name == EDITED.name else set()), (name, clashes)
