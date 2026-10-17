"""`scripts/check_attribution.py` rejects agent attribution in commit messages and PR text.

CLAUDE.md forbids any of it; CI runs the script over every commit of a PR and its title and
body, and the commit-msg hook over each message as it is written. Role names of the workflow
(tester, pr-reviewer...) are not attribution and pass.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_attribution.py"

# Built from parts so that this file's own text never trips a check over the repository.
CO_AUTHORED = "Co-" + "Authored-By: Someone <noreply@example.com>"
SESSION = "Claude-" + "Session: https://example.com/session"
FOOTER = "Generated with " + "Claude Code"


def _check(
    *arguments: str, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _message(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(text)
    return path


def test_a_clean_message_passes(tmp_path: Path) -> None:
    message = _message(
        tmp_path,
        "feat(memory): add the camera registry\n\n"
        "Reviewed by the tester and the pr-reviewer; built by the senior-developer.\n\n"
        "Refs #12\n",
    )
    result = _check(str(message))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "line",
    [
        CO_AUTHORED,
        CO_AUTHORED.lower(),
        CO_AUTHORED.upper(),
        SESSION,
        SESSION.lower(),
        FOOTER,
        f"🤖 {FOOTER.upper()} (https://example.com)",
    ],
)
def test_attribution_anywhere_in_a_message_fails_naming_where(tmp_path: Path, line: str) -> None:
    message = _message(tmp_path, f"fix(api): answer 503 when the database is down\n\n{line}\n")
    result = _check(str(message))
    assert result.returncode == 1
    assert f"{message}:3" in result.stderr
    assert line in result.stderr


def test_git_comments_and_the_diff_below_the_scissors_are_not_the_message(tmp_path: Path) -> None:
    # `git commit -v` appends the diff below a scissors line, and git strips `#` comments.
    message = _message(
        tmp_path,
        "docs(repo): explain the stack\n\n"
        f"# {CO_AUTHORED}\n"
        "# ------------------------ >8 ------------------------\n"
        f"+{SESSION}\n",
    )
    result = _check(str(message))
    assert result.returncode == 0, result.stderr


def test_pr_text_is_read_from_the_environment(tmp_path: Path) -> None:
    environment = {
        "PATH": "/usr/bin:/bin",
        "PR_TITLE": "feat(web): show the Deployment's health",
        "PR_BODY": f"Closes #7\n\nSummary.\r\n\r\n{FOOTER}\r\n",
    }
    result = _check("--env", "PR_TITLE", "--env", "PR_BODY", env=environment)
    assert result.returncode == 1
    assert "PR_BODY:5" in result.stderr
    assert "PR_TITLE" not in result.stderr

    environment["PR_BODY"] = "Closes #7\n"
    assert _check("--env", "PR_TITLE", "--env", "PR_BODY", env=environment).returncode == 0


def test_an_unset_or_empty_variable_is_nothing_to_check() -> None:
    # A PR may have no body at all.
    result = _check("--env", "PR_BODY", env={"PATH": "/usr/bin:/bin", "PR_BODY": ""})
    assert result.returncode == 0, result.stderr
    assert _check("--env", "PR_BODY", env={"PATH": "/usr/bin:/bin"}).returncode == 0


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repository),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Operator",
            "GIT_AUTHOR_EMAIL": "operator@example.com",
            "GIT_COMMITTER_NAME": "Operator",
            "GIT_COMMITTER_EMAIL": "operator@example.com",
        },
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet", "--initial-branch=develop")
    _git(tmp_path, "commit", "--quiet", "--allow-empty", "--no-verify", "-m", "chore: start")
    return tmp_path


def _commit(repository: Path, message: str) -> str:
    _git(repository, "commit", "--quiet", "--allow-empty", "--no-verify", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def test_every_commit_in_a_range_is_checked_and_the_bad_one_named(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "feat(api): one\n\nRefs #1")
    bad = _commit(repository, f"fix(api): two\n\nRefs #1\n\n{SESSION}")
    _commit(repository, "test(api): three\n\nRefs #1")
    result = _check("--commits", f"{base}..HEAD", cwd=repository)
    assert result.returncode == 1
    assert f"commit {bad[:12]} (fix(api): two):5" in result.stderr
    assert "feat(api): one" not in result.stderr


def test_a_clean_range_passes(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "feat(api): one\n\nRefs #1")
    result = _check("--commits", f"{base}..HEAD", cwd=repository)
    assert result.returncode == 0, result.stderr


def test_a_range_git_cannot_resolve_is_an_error_not_a_pass(repository: Path) -> None:
    result = _check("--commits", "no-such-ref..HEAD", cwd=repository)
    assert result.returncode == 2
    assert "no-such-ref" in result.stderr


def test_nothing_to_check_is_a_usage_error() -> None:
    assert _check().returncode == 2
