"""The import contracts in `.importlinter` enforce the rules CLAUDE.md states.

Each test copies every package's source into a temporary tree, adds the import under
test, and runs `lint-imports` against that tree with the repository's own contracts.
"""

import ast
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

NO_REACHING_UP = (
    "Perception, the recorder and the reconciler import only the memory and the runtime"
)
ONLY_MEMORY_HOLDS_A_DRIVER = "Only memory imports a database driver"
REASONER_THROUGH_TOOLS = "The reasoner reaches the memory only through tools"
API_THROUGH_TOOLS = "The api reaches the memory only through tools, except its health probe"
RUNTIME_IS_A_FOUNDATION = "The runtime imports no other VisionQuery package"
LAYERS = "Each package imports only the layers below its own"
BESIDE_THE_LAYERS = "Nothing imports perception, the recorder or the reconciler"
PERCEPTION_AND_RECORDER_INDEPENDENT = (
    "Perception, the recorder and the reconciler import none of the others"
)
NO_REACHING_UP_ID = "no-reaching-up"
ONLY_MEMORY_HOLDS_A_DRIVER_ID = "only-memory-holds-a-driver"
REASONER_THROUGH_TOOLS_ID = "reasoner-through-tools"
API_THROUGH_TOOLS_ID = "api-through-tools"
RUNTIME_IS_A_FOUNDATION_ID = "runtime-is-a-foundation"
LAYERS_ID = "layers"
BESIDE_THE_LAYERS_ID = "nothing-imports-perception-or-the-recorder"
PERCEPTION_AND_RECORDER_INDEPENDENT_ID = "perception-and-recorder-independent"
KEPT = "Contracts: 8 kept, 0 broken."

# The layering CLAUDE.md states, highest first: a package may import the layers below its
# own, never one above it nor its sibling. Perception, the recorder and the reconciler sit beside them.
LAYERING = (("api", "reasoner"), ("tools",), ("linker",), ("memory",), ("runtime",))
BESIDE = ("perception", "recorder", "reconciler")


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
    assert KEPT in result.stdout


def test_an_unchanged_copy_keeps_every_contract(tmp_path: Path) -> None:
    result = _lint_with(tmp_path, {})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("importer", "imported", "contracts"),
    [
        *[
            (p, f"visionquery.{up}", {NO_REACHING_UP})
            for p in BESIDE
            for up in ("tools", "linker", "reasoner", "api")
        ],
        ("perception", "visionquery.recorder", {PERCEPTION_AND_RECORDER_INDEPENDENT}),
        ("recorder", "visionquery.perception", {PERCEPTION_AND_RECORDER_INDEPENDENT}),
        ("reconciler", "visionquery.perception", {PERCEPTION_AND_RECORDER_INDEPENDENT}),
        ("reconciler", "visionquery.recorder", {PERCEPTION_AND_RECORDER_INDEPENDENT}),
        ("perception", "visionquery.reconciler", {PERCEPTION_AND_RECORDER_INDEPENDENT}),
        ("recorder", "visionquery.reconciler", {PERCEPTION_AND_RECORDER_INDEPENDENT}),
        ("reasoner", "visionquery.memory", {REASONER_THROUGH_TOOLS}),
        ("reasoner", "visionquery.linker", {REASONER_THROUGH_TOOLS}),
        ("reasoner", "visionquery.perception", {REASONER_THROUGH_TOOLS, BESIDE_THE_LAYERS}),
        ("reasoner", "visionquery.recorder", {REASONER_THROUGH_TOOLS, BESIDE_THE_LAYERS}),
        ("reasoner", "visionquery.reconciler", {REASONER_THROUGH_TOOLS, BESIDE_THE_LAYERS}),
        ("reasoner", "visionquery.api", {REASONER_THROUGH_TOOLS, LAYERS}),
        ("api", "visionquery.memory", {API_THROUGH_TOOLS}),
        ("api", "visionquery.memory.migrate", {API_THROUGH_TOOLS}),
        ("api", "visionquery.linker", {API_THROUGH_TOOLS}),
        ("api", "visionquery.reasoner", {LAYERS}),
        ("api", "visionquery.recorder", {BESIDE_THE_LAYERS}),
        ("tools", "visionquery.api", {LAYERS}),
        ("tools", "visionquery.reasoner", {LAYERS}),
        ("tools", "visionquery.perception", {BESIDE_THE_LAYERS}),
        ("linker", "visionquery.tools", {LAYERS}),
        ("linker", "visionquery.api", {LAYERS}),
        ("linker", "visionquery.reasoner", {LAYERS}),
        ("linker", "visionquery.recorder", {BESIDE_THE_LAYERS}),
        *[
            ("memory", f"visionquery.{up}", {LAYERS})
            for up in ("linker", "tools", "reasoner", "api")
        ],
        *[("memory", f"visionquery.{p}", {BESIDE_THE_LAYERS}) for p in BESIDE],
        *[(p, "psycopg", {ONLY_MEMORY_HOLDS_A_DRIVER}) for p in PACKAGES if p != "memory"],
        *[
            (
                "runtime",
                f"visionquery.{p}",
                # The layers and [nothing-imports-perception-or-the-recorder] imply this
                # contract; it stays as the foundation's own, plainly named rule.
                {RUNTIME_IS_A_FOUNDATION, BESIDE_THE_LAYERS if p in BESIDE else LAYERS},
            )
            for p in PACKAGES
            if p != "runtime"
        ],
        ("tools", "psycopg_pool", {ONLY_MEMORY_HOLDS_A_DRIVER}),
        ("tools", "psycopg2", {ONLY_MEMORY_HOLDS_A_DRIVER}),
        ("tools", "asyncpg", {ONLY_MEMORY_HOLDS_A_DRIVER}),
        ("tools", "pg8000", {ONLY_MEMORY_HOLDS_A_DRIVER}),
        ("tools", "sqlalchemy", {ONLY_MEMORY_HOLDS_A_DRIVER}),
        ("tools", "pgvector", {ONLY_MEMORY_HOLDS_A_DRIVER}),
    ],
)
def test_a_forbidden_import_is_reported(
    tmp_path: Path, importer: str, imported: str, contracts: set[str]
) -> None:
    result = _lint_with(tmp_path, {f"visionquery.{importer}.violation": f"import {imported}\n"})
    assert result.returncode != 0, result.stdout + result.stderr
    assert _broken(result) == contracts, result.stdout
    assert f"visionquery.{importer}.violation -> {imported}" in result.stdout


@pytest.mark.parametrize(
    ("importer", "imported"),
    [("tools", "api"), ("tools", "reasoner"), ("linker", "tools"), ("memory", "tools")],
)
def test_reaching_up_a_layer_from_deep_inside_a_package_is_reported(
    tmp_path: Path, importer: str, imported: str
) -> None:
    # The gap the layers close: the forbidden contracts tolerate indirect imports, so
    # tools -> api was reported by none of them, and nothing stopped the cycle it makes.
    result = _lint_with(
        tmp_path,
        {
            f"visionquery.{imported}.top": "import visionquery.runtime\n",
            f"visionquery.{importer}.middle": f"import visionquery.{imported}.top\n",
            f"visionquery.{importer}.bottom": f"import visionquery.{importer}.middle\n",
        },
    )
    assert _broken(result) == {LAYERS}, result.stdout
    assert f"visionquery.{importer}.middle -> visionquery.{imported}.top" in result.stdout


def test_every_intended_path_down_the_layers_is_kept(tmp_path: Path) -> None:
    result = _lint_with(
        tmp_path,
        {
            "visionquery.runtime.base": "import logging\n",
            "visionquery.memory.store": "import visionquery.runtime.base\n",
            "visionquery.linker.follow": "import visionquery.memory.store\n",
            "visionquery.tools.lookup": "import visionquery.memory.store\n",
            "visionquery.tools.following": "import visionquery.linker.follow\n",
            "visionquery.api.routes": (
                "import visionquery.tools.lookup\nimport visionquery.tools.following\n"
            ),
            "visionquery.reasoner.ask": (
                "import visionquery.tools.lookup\nimport visionquery.tools.following\n"
            ),
            "visionquery.perception.worker": "import visionquery.memory.store\n",
            "visionquery.recorder.segments": "import visionquery.memory.store\n",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert KEPT in result.stdout


def test_a_new_package_left_out_of_the_layers_is_reported(tmp_path: Path) -> None:
    # The layers are exhaustive: a new package must be placed in a layer, or beside them.
    result = _lint_with(tmp_path, {"visionquery.newcomer": "import visionquery.runtime\n"})
    assert _broken(result) == {LAYERS}, result.stdout
    assert "visionquery.newcomer" in result.stdout


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


def test_a_new_api_module_opening_connections_is_reported(tmp_path: Path) -> None:
    # The app takes its dependencies as arguments; it never opens a connection itself.
    result = _lint_with(
        tmp_path, {"visionquery.api.routes": "import visionquery.memory.connection\n"}
    )
    assert _broken(result) == {API_THROUGH_TOOLS}, result.stdout
    assert "visionquery.api.routes -> visionquery.memory.connection" in result.stdout


def test_the_api_app_imports_nothing_from_the_memory_itself() -> None:
    # Read from the source, not linted in a copy: a copy that replaces an existing module is
    # not reliably the one lint-imports analyses. `make contracts` lints the real tree.
    source = (ROOT / "packages/api/src/visionquery/api/app.py").read_text()
    imported = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom | ast.Import)
    ]
    names = [
        n
        for node in imported
        for n in (
            [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else [a.name for a in node.names]
        )
    ]
    assert names
    assert not [n for n in names if n.startswith("visionquery.memory")], names


def test_another_api_module_importing_the_memory_health_check_is_reported(tmp_path: Path) -> None:
    # The exception names the modules that have it; a new route is not one of them.
    result = _lint_with(tmp_path, {"visionquery.api.routes": "import visionquery.memory.health\n"})
    assert _broken(result) == {API_THROUGH_TOOLS}, result.stdout
    assert "visionquery.api.routes -> visionquery.memory.health" in result.stdout


def test_the_api_s_exception_is_no_wider_than_the_probe_and_the_composition_root() -> None:
    ignored = {
        line.strip()
        for line in _config()[f"importlinter:contract:{API_THROUGH_TOOLS_ID}"]["ignore_imports"]
        .strip()
        .splitlines()
    }
    assert ignored == {
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
    assert KEPT in result.stdout


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
    assert KEPT in result.stdout


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
    # CLAUDE.md: perception, recorder and reconciler import only the memory and the runtime.
    sources = _modules(NO_REACHING_UP_ID, "source_modules")
    forbidden = _modules(NO_REACHING_UP_ID, "forbidden_modules")
    expected = set(PACKAGES) - set(BESIDE) - {"memory", "runtime"}
    assert sources == set(BESIDE), (
        f"CLAUDE.md names only perception, recorder and reconciler as sources of [{NO_REACHING_UP_ID}]; "
        "changing that is a CLAUDE.md change."
    )
    assert forbidden == expected, (
        "Perception, the recorder and the reconciler may import only the memory and the runtime. Add new "
        f"workspace packages to forbidden_modules of [{NO_REACHING_UP_ID}] in .importlinter "
        f"(missing: {sorted(expected - forbidden)}, unknown: {sorted(forbidden - expected)})."
    )


def _layers() -> tuple[tuple[str, ...], ...]:
    section = _config()[f"importlinter:contract:{LAYERS_ID}"]
    assert section["type"] == "layers"
    return tuple(
        tuple(m.strip() for m in line.split("|")) for line in section["layers"].strip().splitlines()
    )


def test_the_layers_are_the_ones_claude_md_states() -> None:
    # api | reasoner > tools > linker > memory > runtime; `|` keeps api and reasoner
    # independent of each other. Changing the order is a CLAUDE.md change.
    section = _config()[f"importlinter:contract:{LAYERS_ID}"]
    assert section["containers"].strip() == "visionquery"
    assert _layers() == LAYERING
    assert section.getboolean("exhaustive")
    assert set(section["exhaustive_ignores"].split()) == set(BESIDE)


def test_every_package_sits_in_a_layer_or_beside_them() -> None:
    placed = [p for layer in _layers() for p in layer]
    assert len(placed) == len(set(placed))
    assert set(placed) | set(BESIDE) == set(PACKAGES)
    assert not set(placed) & set(BESIDE)


def test_nothing_imports_the_packages_beside_the_layers() -> None:
    assert _modules(BESIDE_THE_LAYERS_ID, "source_modules") == set(PACKAGES) - set(BESIDE)
    assert _modules(BESIDE_THE_LAYERS_ID, "forbidden_modules") == set(BESIDE)
    assert _modules(PERCEPTION_AND_RECORDER_INDEPENDENT_ID, "modules") == set(BESIDE)
