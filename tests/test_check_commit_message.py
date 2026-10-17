"""`scripts/check_commit_message.py` enforces the Conventional Commits CLAUDE.md asks for.

`type(scope): subject`, with the types CLAUDE.md lists and a scope that is a package, `web`, or
one of the few repo-wide scopes it names; the scope may be left out. The commit-msg hook runs it
on every message, and CI on every PR title, which becomes the squash commit's subject.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_commit_message.py"
CLAUDE = (ROOT / "CLAUDE.md").read_text()


def _check_text(tmp_path: Path, text: str) -> subprocess.CompletedProcess[str]:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(text)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(message)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


SCOPES = {"agents", "ci", "repo", "docs"}
PACKAGES = {p.parent.name for p in (ROOT / "packages").glob("*/pyproject.toml")}


def test_claude_md_names_the_types_and_repo_scopes_the_check_allows() -> None:
    sentence = next(line for line in CLAUDE.splitlines() if line.startswith("**Commits** follow"))
    types = sentence.split("with types", 1)[1].split(";", 1)[0]
    assert set(re.findall(r"`([a-z]+)`", types)) == {
        "feat", "fix", "test", "refactor", "docs", "chore", "ci", "build", "perf",
    }  # fmt: skip
    repo_scopes = sentence.split(") or", 1)[1]
    assert set(re.findall(r"`([a-z]+)`", repo_scopes)) == SCOPES


@pytest.mark.parametrize(
    "type_", ["feat", "fix", "test", "refactor", "docs", "chore", "ci", "build", "perf"]
)
def test_every_documented_type_passes(tmp_path: Path, type_: str) -> None:
    result = _check_text(tmp_path, f"{type_}(memory): do the thing\n\nRefs #8\n")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scope", sorted(PACKAGES | {"web"} | SCOPES))
def test_every_package_web_and_repo_scope_passes(tmp_path: Path, scope: str) -> None:
    assert _check_text(tmp_path, f"feat({scope}): do the thing\n").returncode == 0


@pytest.mark.parametrize(
    "subject",
    [
        "docs: explain the plan",
        "feat(api)!: answer every Question with Evidence",
        "Merge branch 'develop' into feature/8-ci",
        'Revert "feat(api): add a route"',
        "fixup! feat(api): add a route",
        "squash! feat(api): add a route",
    ],
)
def test_no_scope_breaking_changes_and_what_git_writes_itself_pass(
    tmp_path: Path, subject: str
) -> None:
    result = _check_text(tmp_path, f"{subject}\n")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "subject",
    [
        "add the camera registry",
        "feature(memory): add the camera registry",
        "style(memory): reformat",
        "Feat(memory): add the camera registry",
        "feat(cameras): add the camera registry",
        "feat(Memory): add the camera registry",
        "feat(): add the camera registry",
        "feat(memory) add the camera registry",
        "feat(memory):add the camera registry",
        "feat(memory): ",
        "",
    ],
)
def test_anything_else_fails_saying_what_is_expected(tmp_path: Path, subject: str) -> None:
    result = _check_text(tmp_path, f"{subject}\n\nRefs #8\n")
    assert result.returncode == 1
    assert "type(scope): subject" in result.stderr
    assert "memory" in result.stderr
    assert "perf" in result.stderr


def test_the_body_is_separated_from_the_subject_by_a_blank_line(tmp_path: Path) -> None:
    result = _check_text(tmp_path, "feat(memory): add it\nRefs #8\n")
    assert result.returncode == 1
    assert "blank line" in result.stderr


def test_git_comments_before_the_subject_are_ignored(tmp_path: Path) -> None:
    text = "# Please enter the commit message\nfeat(memory): add it\n# On branch x\n"
    assert _check_text(tmp_path, text).returncode == 0


def test_a_pr_title_is_checked_from_the_environment() -> None:
    def check(title: str) -> int:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--env", "PR_TITLE"],
            env={"PATH": "/usr/bin:/bin", "PR_TITLE": title},
            capture_output=True,
            check=False,
            timeout=60,
        ).returncode

    assert check("ci: run every check on each PR") == 0
    assert check("CI workflow, pre-commit and branch rules") == 1
