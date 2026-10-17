"""`scripts/branch_rules.py` makes the develop and main rulesets require every CI check.

Without `--apply` it prints the two rulesets it would send. With it, it refuses unless every
check has already passed on develop's latest commit (GitHub can only require a check it has
seen, and requiring one that never reports would block every merge), then replaces each
ruleset through `gh api`. Here `gh` is a fake that answers from fixtures and records what it is
asked, so the live rulesets are never touched.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "branch_rules.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
#: The GitHub Actions app: only a check reported by a workflow satisfies the requirement.
GITHUB_ACTIONS = 15368
RULESETS = {"protect-develop": 25268539, "protect-main": 25268531}


def _checks() -> set[str]:
    jobs: dict[str, dict[str, Any]] = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    return {job.get("name", identifier) for identifier, job in jobs.items()}


def _run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _payloads() -> dict[str, dict[str, Any]]:
    result = _run()
    assert result.returncode == 0, result.stderr
    return {ruleset["name"]: ruleset for ruleset in json.loads(result.stdout)}


def _rules(ruleset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["type"]: rule.get("parameters", {}) for rule in ruleset["rules"]}


def test_there_is_one_active_ruleset_for_develop_and_one_for_main() -> None:
    payloads = _payloads()
    assert set(payloads) == set(RULESETS)
    for name, branch in (("protect-develop", "develop"), ("protect-main", "main")):
        ruleset = payloads[name]
        assert ruleset["target"] == "branch"
        assert ruleset["enforcement"] == "active"
        assert ruleset["conditions"] == {
            "ref_name": {"include": [f"refs/heads/{branch}"], "exclude": []}
        }
        assert ruleset["bypass_actors"] == []


@pytest.mark.parametrize("name", sorted(RULESETS))
def test_a_pr_is_required_and_force_pushes_and_deletion_are_forbidden(name: str) -> None:
    rules = _rules(_payloads()[name])
    assert {"deletion", "non_fast_forward", "pull_request"} <= set(rules)
    pull_request = rules["pull_request"]
    assert pull_request["required_review_thread_resolution"] is True
    assert pull_request["dismiss_stale_reviews_on_push"] is True
    # Features are squashed into develop; a release is merged into main and back into develop.
    assert set(pull_request["allowed_merge_methods"]) == {"merge", "squash", "rebase"}


@pytest.mark.parametrize("name", sorted(RULESETS))
def test_every_ci_job_is_a_required_check_reported_by_github_actions(name: str) -> None:
    required = _rules(_payloads()[name])["required_status_checks"]
    checks = required["required_status_checks"]
    assert {check["context"] for check in checks} == _checks()
    assert {"lint", "typecheck", "contracts", "test", "web", "stack", "commits"} <= _checks()
    assert all(check["integration_id"] == GITHUB_ACTIONS for check in checks)
    # A PR must have passed against the base it will be merged into (see CLAUDE.md).
    assert required["strict_required_status_checks_policy"] is True
    assert required["do_not_enforce_on_create"] is False


FAKE_GH = """\
import json, os, sys
arguments = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    log.write(json.dumps({"arguments": arguments, "stdin": stdin}) + "\\n")
path = next(a for a in arguments if a.startswith("repos/"))
if path.endswith("/rulesets"):
    rulesets = json.loads(os.environ["FAKE_RULESETS"])
    print(json.dumps([{"id": i, "name": n} for n, i in rulesets.items()]))
elif "/check-runs" in path:
    print(os.environ["FAKE_CHECK_RUNS"])
"""


def _fake_gh(tmp_path: Path, check_runs: list[dict[str, str]]) -> tuple[dict[str, str], Path]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(parents=True)
    gh = bin_directory / "gh"
    gh.write_text(f"#!{sys.executable}\n{FAKE_GH}")
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    log.touch()
    environment = os.environ | {
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_LOG": str(log),
        "FAKE_RULESETS": json.dumps(RULESETS),
        "FAKE_CHECK_RUNS": json.dumps({"total_count": len(check_runs), "check_runs": check_runs}),
    }
    return environment, log


def _calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_applying_replaces_both_rulesets_once_every_check_has_passed_on_develop(
    tmp_path: Path,
) -> None:
    passed = [{"name": c, "conclusion": "success"} for c in sorted(_checks())]
    environment, log = _fake_gh(tmp_path, passed)
    result = _run("--apply", env=environment)
    assert result.returncode == 0, result.stderr
    puts = [c for c in _calls(log) if "PUT" in c["arguments"]]
    assert len(puts) == 2
    payloads = _payloads()
    for put in puts:
        path = next(a for a in put["arguments"] if a.startswith("repos/"))
        name = next(
            n for n, i in RULESETS.items() if path == f"repos/{{owner}}/{{repo}}/rulesets/{i}"
        )
        assert json.loads(put["stdin"]) == payloads[name]
        assert put["arguments"][put["arguments"].index("--input") + 1] == "-"


@pytest.mark.parametrize("missing", ["stack", "commits"])
def test_applying_refuses_while_a_check_has_not_passed_on_develop(
    tmp_path: Path, missing: str
) -> None:
    runs = [{"name": c, "conclusion": "success"} for c in sorted(_checks()) if c != missing]
    for conclusion in ("absent", "failure"):
        if conclusion == "failure":
            runs.append({"name": missing, "conclusion": "failure"})
        environment, log = _fake_gh(tmp_path / conclusion, runs)
        result = _run("--apply", env=environment)
        assert result.returncode == 1
        assert missing in result.stderr
        assert not [c for c in _calls(log) if "PUT" in c["arguments"]]


def test_make_branch_rules_applies_them() -> None:
    dry = subprocess.run(
        ["make", "--dry-run", "--no-print-directory", "branch-rules"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.stdout.splitlines() == ["uv run python scripts/branch_rules.py --apply"]
