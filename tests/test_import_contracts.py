"""The import contracts in `.importlinter` enforce the rules CLAUDE.md states.

Each test copies every package's source into a temporary tree, adds the import under
test, and runs `lint-imports` against that tree with the repository's own contracts.
"""

import configparser
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".importlinter"


def _workspace_packages() -> tuple[str, ...]:
    members: list[str] = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"][
        "workspace"
    ]["members"]
    return tuple(sorted(p.parent.name for m in members for p in ROOT.glob(f"{m}/pyproject.toml")))


PACKAGES = _workspace_packages()

NO_REACHING_UP = "Perception and the recorder never import the reasoner or the api"
ONLY_MEMORY_HOLDS_A_DRIVER = "Only memory imports a database driver"
REASONER_THROUGH_TOOLS = "The reasoner reaches the memory only through tools"
NO_REACHING_UP_ID = "no-reaching-up"
ONLY_MEMORY_HOLDS_A_DRIVER_ID = "only-memory-holds-a-driver"
REASONER_THROUGH_TOOLS_ID = "reasoner-through-tools"


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
        ("reasoner", "visionquery.linker", REASONER_THROUGH_TOOLS),
        ("reasoner", "visionquery.perception", REASONER_THROUGH_TOOLS),
        ("reasoner", "visionquery.recorder", REASONER_THROUGH_TOOLS),
        ("reasoner", "visionquery.api", REASONER_THROUGH_TOOLS),
        *[(p, "psycopg", ONLY_MEMORY_HOLDS_A_DRIVER) for p in PACKAGES if p != "memory"],
        ("tools", "psycopg_pool", ONLY_MEMORY_HOLDS_A_DRIVER),
        ("tools", "psycopg2", ONLY_MEMORY_HOLDS_A_DRIVER),
        ("tools", "asyncpg", ONLY_MEMORY_HOLDS_A_DRIVER),
        ("tools", "pg8000", ONLY_MEMORY_HOLDS_A_DRIVER),
        ("tools", "sqlalchemy", ONLY_MEMORY_HOLDS_A_DRIVER),
        ("tools", "pgvector", ONLY_MEMORY_HOLDS_A_DRIVER),
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


def test_a_package_that_imports_memory_does_not_count_as_importing_the_driver(
    tmp_path: Path,
) -> None:
    result = _lint_with(
        tmp_path,
        {
            "visionquery.memory.db": "import psycopg\n",
            "visionquery.tools.q": "import visionquery.memory.db\n",
            "visionquery.linker.link": "import visionquery.memory.db\n",
            "visionquery.api.routes": "import visionquery.tools.q\n",
            "visionquery.reasoner.ask": "import visionquery.tools.q\n",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contracts: 3 kept, 0 broken." in result.stdout


def test_the_reasoner_may_reach_the_linker_through_tools(tmp_path: Path) -> None:
    result = _lint_with(
        tmp_path,
        {
            "visionquery.linker.follow": "import visionquery.memory\n",
            "visionquery.tools.following": "import visionquery.linker.follow\n",
            "visionquery.reasoner.ask": "import visionquery.tools.following\n",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_direct_driver_import_is_still_reported_next_to_a_memory_import(tmp_path: Path) -> None:
    result = _lint_with(
        tmp_path,
        {
            "visionquery.memory.db": "import psycopg\n",
            "visionquery.tools.q": "import visionquery.memory.db\nimport psycopg\n",
        },
    )
    assert _broken(result) == {ONLY_MEMORY_HOLDS_A_DRIVER}, result.stdout
    assert "visionquery.tools.q -> psycopg" in result.stdout


def _config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(CONFIG)
    return config


def test_the_whole_visionquery_namespace_is_analysed() -> None:
    section = _config()["importlinter"]
    assert section.get("root_package") == "visionquery"
    assert "root_packages" not in section


def test_every_package_but_memory_is_checked_for_driver_imports() -> None:
    section = _config()[f"importlinter:contract:{ONLY_MEMORY_HOLDS_A_DRIVER_ID}"]
    checked = {m.strip().removeprefix("visionquery.") for m in section["source_modules"].split()}
    assert checked == set(PACKAGES) - {"memory"}


@pytest.mark.parametrize("contract", [ONLY_MEMORY_HOLDS_A_DRIVER_ID, REASONER_THROUGH_TOOLS_ID])
def test_the_contracts_that_forbid_a_path_see_through_the_memory_package(contract: str) -> None:
    # Both rules are about naming a module, not about what a module reaches: reasoner ->
    # tools -> memory -> psycopg is legal, so these must tolerate indirect imports.
    assert _config()[f"importlinter:contract:{contract}"].getboolean("allow_indirect_imports")


def test_no_reaching_up_follows_indirect_imports() -> None:
    section = _config()[f"importlinter:contract:{NO_REACHING_UP_ID}"]
    assert not section.getboolean("allow_indirect_imports", fallback=False)
