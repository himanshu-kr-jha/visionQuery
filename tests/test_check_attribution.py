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
#: The footer Claude Code appends to a PR description, and other ways to spell it.
REAL_FOOTER = "🤖 Generated with [" + "Claude Code](https://claude.com/claude-code)"
VARIANTS = [
    REAL_FOOTER,
    "Generated  with  " + "Claude  Code",
    "Generated\twith\t" + "Claude\tCode",
    "Generated-with-" + "Claude-Code",
    "Generated with [claude" + "code]",
    "GENERATED WITH [" + "CLAUDE CODE]",
]

#: Other ways the markers appear: a bare session URL, the agent's no-reply address, the trailer
#: and the session marker written with spaces, and emphasis around the tool name in the footer.
OTHER_MARKERS = [
    "See https://claude" + ".ai/code/session_01ABC for details",
    "claude" + ".ai/code/session_01ABC",
    "Someone <noreply" + "@anthropic.com>",
    "by noreply" + "@ANTHROPIC.COM",
    "Co " + "authored by: Someone",
    "co" + "authored" + " by Someone",
    "Co-" + "Authored" + " By Someone",
    "Claude " + "Session: abc",
    "claude  " + "session",
    "Generated with **" + "Claude Code**",
    "Generated with _" + "Claude Code_",
    "Generated with `" + "Claude Code`",
    "Generated with [**" + "Claude Code**](https://example.com)",
    "Generated with *" + "Claude* *Code*",
]


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
        *VARIANTS,
    ],
)
def test_attribution_anywhere_in_a_message_fails_naming_where(tmp_path: Path, line: str) -> None:
    message = _message(tmp_path, f"fix(api): answer 503 when the database is down\n\n{line}\n")
    result = _check(str(message))
    assert result.returncode == 1
    assert f"{message}:3" in result.stderr
    assert line in result.stderr


def test_the_footer_split_across_a_line_break_fails_naming_the_line_it_starts_on(
    tmp_path: Path,
) -> None:
    message = _message(tmp_path, "fix(api): one\n\nGenerated with\n" + "Claude Code\n")
    result = _check(str(message))
    assert result.returncode == 1
    assert f"{message}:3" in result.stderr


def test_the_words_claude_code_in_ordinary_prose_are_not_attribution(tmp_path: Path) -> None:
    message = _message(
        tmp_path,
        "docs(agents): describe the Claude " + "Code agents\n\n"
        "The files were generated with the project's script, with code from the tester.\n",
    )
    result = _check(str(message))
    assert result.returncode == 0, result.stderr


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


@pytest.mark.parametrize("footer", VARIANTS)
def test_every_spelling_of_the_footer_in_pr_text_fails(footer: str) -> None:
    environment = {"PATH": "/usr/bin:/bin", "PR_BODY": f"Closes #8\n\nSummary.\n\n{footer}\n"}
    result = _check("--env", "PR_BODY", env=environment)
    assert result.returncode == 1
    assert "PR_BODY:5" in result.stderr


def test_a_footer_split_across_a_line_break_in_pr_text_fails() -> None:
    body = "Summary.\r\n\r\nGenerated\r\nwith " + "Claude\r\nCode\r\n"
    result = _check("--env", "PR_BODY", env={"PATH": "/usr/bin:/bin", "PR_BODY": body})
    assert result.returncode == 1
    assert "PR_BODY:3" in result.stderr


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


@pytest.mark.parametrize("footer", [REAL_FOOTER, "Generated\nwith " + "Claude\nCode"])
def test_the_footer_in_a_commit_of_a_range_fails(repository: Path, footer: str) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    bad = _commit(repository, f"fix(api): two\n\nRefs #1\n\n{footer}")
    result = _check("--commits", f"{base}..HEAD", cwd=repository)
    assert result.returncode == 1
    assert f"commit {bad[:12]} (fix(api): two):5" in result.stderr


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


@pytest.mark.parametrize("marker", OTHER_MARKERS)
def test_other_spellings_of_the_markers_fail_in_pr_text(marker: str) -> None:
    environment = {"PATH": "/usr/bin:/bin", "PR_BODY": f"Closes #8\n\nSummary.\n\n{marker}\n"}
    result = _check("--env", "PR_BODY", env=environment)
    assert result.returncode == 1, marker
    assert "PR_BODY:5" in result.stderr


@pytest.mark.parametrize("marker", OTHER_MARKERS)
def test_other_spellings_of_the_markers_fail_in_a_commit_message(
    tmp_path: Path, marker: str
) -> None:
    message = _message(tmp_path, f"fix(api): one\n\n{marker}\n")
    assert _check(str(message)).returncode == 1, marker


@pytest.mark.parametrize(
    "prose",
    [
        "Use a claude " + "model for the Reasoner",
        "The session cookie is signed; Claude Code agents are described in CLAUDE.md",
        "Written by the senior-developer, authored in the worktree",
        "Send mail to noreply@example.com",
    ],
)
def test_prose_near_the_markers_still_passes(tmp_path: Path, prose: str) -> None:
    message = _message(tmp_path, f"docs(agents): describe the team\n\n{prose}\n")
    result = _check(str(message))
    assert result.returncode == 0, result.stderr
