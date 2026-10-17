"""The web app is TypeScript in strict mode, linted and tested, on pinned versions, by `make check`.

Running the linter, the type checker and the component tests is `make web`'s job; these check,
without Node, that it stays set up so that they mean something.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
EXACT = re.compile(r"^\d+\.\d+\.\d+$")


def _json(name: str) -> dict[str, Any]:
    return json.loads((WEB / name).read_text())


def test_every_dependency_is_pinned_exactly_and_locked() -> None:
    package = _json("package.json")
    pinned = package["dependencies"] | package["devDependencies"]
    assert {name: v for name, v in pinned.items() if not EXACT.match(v)} == {}
    lock = _json("package-lock.json")
    locked = lock["packages"][""]
    assert locked["dependencies"] | locked["devDependencies"] == pinned
    # `npm install <name>` keeps pinning exactly; npm refuses a Node outside `engines`.
    npmrc = (WEB / ".npmrc").read_text().splitlines()
    assert "save-exact=true" in npmrc
    assert "engine-strict=true" in npmrc


def test_node_24_is_the_one_named_everywhere() -> None:
    assert (WEB / ".nvmrc").read_text().strip() == "24"
    assert _json("package.json")["engines"]["node"] == "^24.0.0"


@pytest.mark.parametrize("config", ["tsconfig.app.json", "tsconfig.node.json"])
def test_typescript_is_strict(config: str) -> None:
    options = _json(config)["compilerOptions"]
    assert options["strict"] is True
    assert options["noUncheckedIndexedAccess"] is True
    assert options["exactOptionalPropertyTypes"] is True


def test_the_type_check_covers_the_app_and_its_tooling() -> None:
    references = [r["path"] for r in _json("tsconfig.json")["references"]]
    assert references == ["./tsconfig.app.json", "./tsconfig.node.json"]
    assert _json("tsconfig.app.json")["include"] == ["src"]
    assert set(_json("tsconfig.node.json")["include"]) == {"vite.config.ts", "eslint.config.js"}


def test_the_linter_allows_no_warnings_and_checks_types() -> None:
    scripts = _json("package.json")["scripts"]
    assert "--max-warnings 0" in scripts["lint"]
    config = (WEB / "eslint.config.js").read_text()
    assert "strictTypeChecked" in config
    assert "projectService: true" in config


def test_make_check_lints_type_checks_and_tests_the_web_app() -> None:
    dry = subprocess.run(
        ["make", "--dry-run", "--no-print-directory", "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for command in ("npm --prefix web run lint", "npm --prefix web run typecheck"):
        assert command in dry
    assert "npm --prefix web test" in dry
    scripts = _json("package.json")["scripts"]
    assert scripts["typecheck"] == "tsc --build"
    assert scripts["test"] == "vitest run"


def test_make_sync_installs_the_web_app_from_its_lockfile() -> None:
    dry = subprocess.run(
        ["make", "--dry-run", "--no-print-directory", "sync"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "npm --prefix web ci" in dry
