"""The import contracts in `.importlinter` enforce the rules CLAUDE.md states.

Each test copies every package's source into a temporary tree, adds the import under
test, and runs `lint-imports` against that tree with the repository's own contracts.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".importlinter"
PACKAGES = ("memory", "tools", "perception", "linker", "recorder", "reasoner", "api")

NO_REACHING_UP = "Perception and the recorder never import the reasoner or the api"
ONLY_MEMORY_HOLDS_A_DRIVER = "Only memory imports a database driver"
REASONER_THROUGH_TOOLS = "The reasoner reaches the memory only through tools"


def _lint_imports(cwd: Path, pythonpath: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return subprocess.run(
        [str(Path(sys.executable).parent / "lint-imports"), "--config", str(CONFIG), "--no-cache"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _lint_with(tmp_path: Path, modules: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Lint a copy of the workspace in which each dotted module name holds the given source."""
    tree = tmp_path / "src"
    for package in PACKAGES:
        shutil.copytree(ROOT / "packages" / package / "src", tree, dirs_exist_ok=True)
    for name, source in modules.items():
        (tree / Path(*name.split("."))).with_suffix(".py").write_text(source)
    # PYTHONPATH comes before the editable installs on sys.path, so the copy is what is linted.
    return _lint_imports(tmp_path, pythonpath=tree)


def _broken(result: subprocess.CompletedProcess[str]) -> set[str]:
    return {
        line.removesuffix(" BROKEN").strip()
        for line in result.stdout.splitlines()
        if line.endswith(" BROKEN")
    }


def test_the_workspace_keeps_every_contract() -> None:
    result = _lint_imports(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contracts: 3 kept, 0 broken." in result.stdout


def test_an_unchanged_copy_keeps_every_contract(tmp_path: Path) -> None:
    result = _lint_with(tmp_path, {})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("importer", "imported", "contract"),
    [
        ("perception", "visionquery.reasoner", NO_REACHING_UP),
        ("perception", "visionquery.api", NO_REACHING_UP),
        ("recorder", "visionquery.reasoner", NO_REACHING_UP),
        ("recorder", "visionquery.api", NO_REACHING_UP),
        ("reasoner", "visionquery.memory", REASONER_THROUGH_TOOLS),
        *[(p, "psycopg", ONLY_MEMORY_HOLDS_A_DRIVER) for p in PACKAGES if p != "memory"],
        ("tools", "psycopg_pool", ONLY_MEMORY_HOLDS_A_DRIVER),
        ("tools", "psycopg2", ONLY_MEMORY_HOLDS_A_DRIVER),
        ("tools", "asyncpg", ONLY_MEMORY_HOLDS_A_DRIVER),
    ],
)
def test_a_forbidden_import_is_reported(
    tmp_path: Path, importer: str, imported: str, contract: str
) -> None:
    result = _lint_with(tmp_path, {f"visionquery.{importer}.violation": f"import {imported}\n"})
    assert result.returncode != 0, result.stdout + result.stderr
    assert _broken(result) == {contract}, result.stdout
    assert f"visionquery.{importer}.violation -> {imported}" in result.stdout


def test_the_reasoner_may_reach_the_memory_through_tools(tmp_path: Path) -> None:
    result = _lint_with(
        tmp_path,
        {
            "visionquery.tools.lookup": "import visionquery.memory\n",
            "visionquery.reasoner.ask": "import visionquery.tools.lookup\n",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_memory_may_import_the_database_driver(tmp_path: Path) -> None:
    result = _lint_with(tmp_path, {"visionquery.memory.connection": "import psycopg\n"})
    assert result.returncode == 0, result.stdout + result.stderr
