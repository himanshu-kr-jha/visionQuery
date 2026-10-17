"""pre-commit runs ruff, ruff format and the commit message checks before git records a commit.

Each test runs the hooks through pre-commit itself, as `git commit` would once a developer has
opted in with `uv run pre-commit install`, without installing anything into `.git/hooks`.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".pre-commit-config.yaml"
PRE_COMMIT = Path(sys.executable).parent / "pre-commit"


def _config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text())


def _hooks() -> dict[str, dict[str, Any]]:
    return {hook["id"]: hook for repo in _config()["repos"] for hook in repo["hooks"]}


def _pre_commit(*arguments: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PRE_COMMIT), "run", "--config", str(CONFIG), *arguments],
        cwd=cwd,
        # From another repository, `uv run` finds this workspace through UV_PROJECT.
        env=os.environ | {"UV_PROJECT": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def test_installing_sets_up_both_the_pre_commit_and_the_commit_msg_hooks() -> None:
    assert set(_config()["default_install_hook_types"]) == {"pre-commit", "commit-msg"}


def test_every_hook_runs_the_locked_tools_through_uv() -> None:
    # One version of every tool, the one uv.lock pins, for the hooks, `make check` and CI.
    for identifier, hook in _hooks().items():
        assert hook["entry"].startswith("uv run --frozen "), identifier


def _commit_message(tmp_path: Path, text: str) -> subprocess.CompletedProcess[str]:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(text)
    # Naming the file keeps pre-commit from stashing this working tree's unstaged changes, as
    # it does when run bare, and from refusing while its configuration is being edited.
    return _pre_commit(
        "--hook-stage", "commit-msg", "--commit-msg-filename", str(message),
        "--files", str(message),
    )  # fmt: skip


def test_a_conventional_message_without_attribution_is_accepted(tmp_path: Path) -> None:
    result = _commit_message(tmp_path, "feat(memory): add the camera registry\n\nRefs #8\n")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_message_that_is_not_a_conventional_commit_is_refused(tmp_path: Path) -> None:
    result = _commit_message(tmp_path, "Add the camera registry\n\nRefs #8\n")
    assert result.returncode != 0
    assert "type(scope): subject" in result.stdout


def test_a_message_carrying_attribution_is_refused(tmp_path: Path) -> None:
    trailer = "Co-" + "Authored-By: Someone <noreply@example.com>"
    result = _commit_message(tmp_path, f"feat(memory): add it\n\nRefs #8\n\n{trailer}\n")
    assert result.returncode != 0
    assert trailer in result.stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A scratch repository with a module staged that is neither lint-clean nor formatted.

    pre-commit tells that a hook changed a file from git, so the file must be in a repository,
    and a scratch one keeps this one untouched.
    """
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "module.py").write_text("import os\nvalue = {  'a':1 }\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "module.py"], check=True)
    return tmp_path


def test_ruff_refuses_a_lint_error(repository: Path) -> None:
    result = _pre_commit("ruff-check", "--files", "module.py", cwd=repository)
    assert result.returncode != 0
    assert "F401" in result.stdout


def test_ruff_format_reformats_and_refuses_an_unformatted_file(repository: Path) -> None:
    result = _pre_commit("ruff-format", "--files", "module.py", cwd=repository)
    assert result.returncode != 0
    assert "files were modified by this hook" in result.stdout
    assert (repository / "module.py").read_text() == 'import os\n\nvalue = {"a": 1}\n'
    subprocess.run(["git", "-C", str(repository), "add", "module.py"], check=True)
    assert _pre_commit("ruff-format", "--files", "module.py", cwd=repository).returncode == 0
