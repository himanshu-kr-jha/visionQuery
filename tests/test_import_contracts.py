"""The import contracts in `.importlinter` enforce the rules CLAUDE.md states.

Each test copies every package's source into a temporary tree, adds the import under
test, and runs `lint-imports` against that tree with the repository's own contracts.
"""

import configparser
import os
import re
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
API_THROUGH_TOOLS = "The api reaches the memory only through tools, except its health probe"
RUNTIME_IS_A_FOUNDATION = "The runtime imports no other VisionQuery package"
NO_REACHING_UP_ID = "no-reaching-up"
ONLY_MEMORY_HOLDS_A_DRIVER_ID = "only-memory-holds-a-driver"
REASONER_THROUGH_TOOLS_ID = "reasoner-through-tools"
API_THROUGH_TOOLS_ID = "api-through-tools"
RUNTIME_IS_A_FOUNDATION_ID = "runtime-is-a-foundation"


def _lint_imports(cwd: Path, pythonpath: Path | None = None) -> subprocess.CompletedProcess[str]:
    # The caller's PYTHONPATH must never decide which tree is analysed.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
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
        re.sub(r" BROKEN( \(.*\))?$", "", line).strip()
        for line in result.stdout.splitlines()
        if re.search(r" BROKEN( \(.*\))?$", line)
    }


def test_the_workspace_keeps_every_contract() -> None:
    result = _lint_imports(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contracts: 5 kept, 0 broken." in result.stdout


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
        ("api", "visionquery.memory", API_THROUGH_TOOLS),
        ("api", "visionquery.memory.migrate", API_THROUGH_TOOLS),
        ("api", "visionquery.linker", API_THROUGH_TOOLS),
        *[(p, "psycopg", ONLY_MEMORY_HOLDS_A_DRIVER) for p in PACKAGES if p != "memory"],
        *[
            ("runtime", f"visionquery.{p}", RUNTIME_IS_A_FOUNDATION)
            for p in PACKAGES
            if p != "runtime"
        ],
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


def test_the_api_may_reach_the_memory_through_tools(tmp_path: Path) -> None:
    result = _lint_with(
        tmp_path,
        {
            "visionquery.tools.lookup": "import visionquery.memory\n",
            "visionquery.linker.follow": "import visionquery.memory\n",
            "visionquery.tools.following": "import visionquery.linker.follow\n",
            "visionquery.api.routes": (
                "import visionquery.tools.lookup\nimport visionquery.tools.following\n"
            ),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_api_s_health_probe_may_import_the_memory_health_check(tmp_path: Path) -> None:
    probe = ROOT / "packages/api/src/visionquery/api/probe.py"
    source = probe.read_text() + "\nimport visionquery.memory.health\n"
    result = _lint_with(tmp_path, {"visionquery.api.probe": source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_another_api_module_importing_the_memory_health_check_is_reported(tmp_path: Path) -> None:
    # The exception names the modules that have it; a new route is not one of them.
    result = _lint_with(tmp_path, {"visionquery.api.routes": "import visionquery.memory.health\n"})
    assert _broken(result) == {API_THROUGH_TOOLS}, result.stdout
    assert "visionquery.api.routes -> visionquery.memory.health" in result.stdout


def test_the_api_s_exception_is_no_wider_than_the_probe_and_settings() -> None:
    ignored = {
        line.strip()
        for line in _config()[f"importlinter:contract:{API_THROUGH_TOOLS_ID}"]["ignore_imports"]
        .strip()
        .splitlines()
    }
    assert ignored == {
        "visionquery.api.app -> visionquery.memory.connection",
        "visionquery.api.app -> visionquery.memory.health",
        "visionquery.api.probe -> visionquery.memory.health",
        "visionquery.api.serve -> visionquery.memory.health",
        "visionquery.api.serve -> visionquery.memory.settings",
    }


def test_every_package_may_import_the_runtime(tmp_path: Path) -> None:
    result = _lint_with(
        tmp_path,
        {f"visionquery.{p}.starts": "import visionquery.runtime\n" for p in PACKAGES},
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
    assert "Contracts: 5 kept, 0 broken." in result.stdout


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


@pytest.mark.parametrize(
    "contract", [ONLY_MEMORY_HOLDS_A_DRIVER_ID, REASONER_THROUGH_TOOLS_ID, API_THROUGH_TOOLS_ID]
)
def test_the_contracts_that_forbid_a_path_see_through_the_memory_package(contract: str) -> None:
    # Both rules are about naming a module, not about what a module reaches: reasoner ->
    # tools -> memory -> psycopg is legal, so these must tolerate indirect imports.
    assert _config()[f"importlinter:contract:{contract}"].getboolean("allow_indirect_imports")


def test_no_reaching_up_follows_indirect_imports() -> None:
    section = _config()[f"importlinter:contract:{NO_REACHING_UP_ID}"]
    assert not section.getboolean("allow_indirect_imports", fallback=False)


def test_the_callers_pythonpath_does_not_change_what_is_analysed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "src"
    (tree / "visionquery" / "perception").mkdir(parents=True)
    (tree / "visionquery" / "perception" / "violation.py").write_text("import visionquery.api\n")
    monkeypatch.setenv("PYTHONPATH", str(tree))
    result = _lint_imports(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contracts: 5 kept, 0 broken." in result.stdout


def _modules(contract: str, key: str) -> set[str]:
    section = _config()[f"importlinter:contract:{contract}"]
    return {m.strip().removeprefix("visionquery.") for m in section[key].split()}


def test_the_reasoner_is_forbidden_every_package_but_itself_and_tools() -> None:
    forbidden = _modules(REASONER_THROUGH_TOOLS_ID, "forbidden_modules")
    expected = set(PACKAGES) - {"reasoner", "tools", "runtime"}
    assert forbidden == expected, (
        f"ADR 0009: the reasoner may reach only tools (and the runtime every process starts "
        f"with). Add new workspace packages to "
        f"forbidden_modules of [{REASONER_THROUGH_TOOLS_ID}] in .importlinter "
        f"(missing: {sorted(expected - forbidden)}, unknown: {sorted(forbidden - expected)})."
    )


def test_the_runtime_is_forbidden_every_other_package() -> None:
    sources = _modules(RUNTIME_IS_A_FOUNDATION_ID, "source_modules")
    forbidden = _modules(RUNTIME_IS_A_FOUNDATION_ID, "forbidden_modules")
    expected = set(PACKAGES) - {"runtime"}
    assert sources == {"runtime"}
    assert forbidden == expected, (
        f"Every package may import the runtime, so it may import none of them. Add new "
        f"workspace packages to forbidden_modules of [{RUNTIME_IS_A_FOUNDATION_ID}] in "
        f".importlinter (missing: {sorted(expected - forbidden)}, "
        f"unknown: {sorted(forbidden - expected)})."
    )
    section = _config()[f"importlinter:contract:{RUNTIME_IS_A_FOUNDATION_ID}"]
    assert not section.getboolean("allow_indirect_imports", fallback=False)


def test_perception_and_recorder_contract_matches_claude_md() -> None:
    # CLAUDE.md: perception and recorder never import reasoner or api. A new package is not
    # added to either side by default; if it asks or serves questions (like reasoner and api),
    # add it to forbidden_modules here and in .importlinter, and update CLAUDE.md.
    sources = _modules(NO_REACHING_UP_ID, "source_modules")
    forbidden = _modules(NO_REACHING_UP_ID, "forbidden_modules")
    assert sources == {"perception", "recorder"}, (
        "CLAUDE.md names only perception and recorder as sources of [no-reaching-up]; "
        "changing that is a CLAUDE.md change."
    )
    assert forbidden == {"reasoner", "api"}, (
        "CLAUDE.md names reasoner and api as forbidden for perception and recorder; "
        "a new package that asks or serves questions must be added to forbidden_modules of "
        f"[{NO_REACHING_UP_ID}] in .importlinter and to this test."
    )
    assert sources | forbidden <= set(PACKAGES)
