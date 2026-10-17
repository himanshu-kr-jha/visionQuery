"""The develop and main rulesets: a PR, every CI check, no force pushes and no deletion.

    uv run scripts/branch_rules.py            print the rulesets it would send
    uv run scripts/branch_rules.py --apply    send them (`make branch-rules`)

The required checks are the jobs of .github/workflows/ci.yml, so a job added there is required
as soon as this is applied again. GitHub can only require a check it has seen, and a required
check that never reports blocks every merge, so `--apply` refuses, changing nothing, unless
each check has passed on develop's latest commit. It replaces each ruleset, through the `gh`
CLI of whoever runs it (which needs admin rights on the repository). What it manages (the
`deletion`, `non_fast_forward`, `pull_request` and `required_status_checks` rules, with the
parameters it sets) it resets to the values here; what it does not manage it keeps as it is
live: the other `pull_request` parameters, such as
`require_extra_approval_for_unattributed_changes`, the bypass actors, and any other rule type.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
#: `gh api` fills in the repository of the current directory.
REPOSITORY = "repos/{owner}/{repo}"
RULESETS = {"protect-develop": "develop", "protect-main": "main"}
#: The GitHub Actions app, so that only a workflow's check satisfies a requirement.
GITHUB_ACTIONS = 15368


def required_checks() -> list[str]:
    jobs: dict[str, dict[str, Any]] = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    return sorted(job.get("name", identifier) for identifier, job in jobs.items())


def ruleset(name: str, branch: str, checks: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": [f"refs/heads/{branch}"], "exclude": []}},
        "bypass_actors": [],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                    "required_reviewers": [],
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": True,
                    # Features are squashed into develop; a release is merged into main and
                    # back into develop.
                    "allowed_merge_methods": ["merge", "squash", "rebase"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    # A PR must pass against the base it is merged into, so two PRs that each
                    # pass alone cannot together break the branch.
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [
                        {"context": check, "integration_id": GITHUB_ACTIONS} for check in checks
                    ],
                },
            },
        ],
    }


def rulesets() -> list[dict[str, Any]]:
    checks = required_checks()
    return [ruleset(name, branch, checks) for name, branch in RULESETS.items()]


def _gh(*arguments: str, stdin: str | None = None) -> Any:
    result = subprocess.run(
        ["gh", "api", *arguments], input=stdin, capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def _not_passed_on_develop(checks: list[str]) -> list[str]:
    runs = _gh(f"{REPOSITORY}/commits/develop/check-runs?per_page=100")["check_runs"]
    passed = {run["name"] for run in runs if run["conclusion"] == "success"}
    return [check for check in checks if check not in passed]


def _keeping_live_parameters(payload: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """The payload, with what the live ruleset has that it does not manage kept as it is."""
    kept: dict[str, Any] = {}
    for rule in live["rules"]:
        if rule["type"] == "pull_request":
            kept = rule.get("parameters", {})
    managed = {rule["type"] for rule in payload["rules"]}
    for rule in payload["rules"]:
        if rule["type"] == "pull_request":
            rule["parameters"] = kept | rule["parameters"]
    payload["rules"] += [rule for rule in live["rules"] if rule["type"] not in managed]
    payload["bypass_actors"] = live.get("bypass_actors", payload["bypass_actors"])
    return payload


def apply() -> int:
    checks = required_checks()
    if missing := _not_passed_on_develop(checks):
        print(
            f"Not applied: {', '.join(missing)} has not passed on develop's latest commit, and "
            "a required check that never reports would block every merge. Merge the workflow "
            "into develop, wait for its run to pass, and apply again.",
            file=sys.stderr,
        )
        return 1
    existing = {r["name"]: r["id"] for r in _gh(f"{REPOSITORY}/rulesets")}
    if absent := [name for name in RULESETS if name not in existing]:
        print(
            f"Not applied: no ruleset named {', '.join(absent)} exists in the repository. "
            "Create it (Settings, Rules, Rulesets) and apply again.",
            file=sys.stderr,
        )
        return 1
    for payload in rulesets():
        identifier = existing[payload["name"]]
        payload = _keeping_live_parameters(payload, _gh(f"{REPOSITORY}/rulesets/{identifier}"))
        _gh(
            "--method", "PUT", f"{REPOSITORY}/rulesets/{identifier}", "--input", "-",
            stdin=json.dumps(payload),
        )  # fmt: skip
        print(f"{payload['name']}: requires a PR and {', '.join(checks)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="The develop and main rulesets.")
    parser.add_argument("--apply", action="store_true", help="send them through `gh api`")
    if parser.parse_args().apply:
        return apply()
    print(json.dumps(rulesets(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
