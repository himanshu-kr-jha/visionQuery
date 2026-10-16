"""The uv workspace holds exactly the packages CLAUDE.md names, each installable and locked."""

import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pyproject(path: Path) -> dict[str, Any]:
    return tomllib.loads((path / "pyproject.toml").read_text())


def _members() -> set[str]:
    members: list[str] = _pyproject(ROOT)["tool"]["uv"]["workspace"]["members"]
    return {p.parent.name for m in members for p in ROOT.glob(f"{m}/pyproject.toml")}


PACKAGES = _members()


def test_workspace_members_are_the_named_packages() -> None:
    # CLAUDE.md names these; a new package is added here deliberately, with its ADR.
    named = {"memory", "tools", "perception", "linker", "recorder", "reasoner", "api", "runtime"}
    assert named == PACKAGES, (
        f"missing: {sorted(named - PACKAGES)}, unnamed: {sorted(PACKAGES - named)}; a new "
        "package must be added here, to CLAUDE.md and to the contracts in .importlinter"
    )


@pytest.mark.parametrize("package", sorted(PACKAGES))
def test_package_is_a_src_layout_distribution(package: str) -> None:
    directory = ROOT / "packages" / package
    project = _pyproject(directory)["project"]
    assert project["name"] == f"visionquery-{package}"
    assert project["requires-python"] == _pyproject(ROOT)["project"]["requires-python"]
    assert (directory / "src" / "visionquery" / package / "__init__.py").is_file()
    assert (directory / "src" / "visionquery" / package / "py.typed").is_file()
    # `visionquery` is a namespace shared by every package, so none of them may own it.
    assert not (directory / "src" / "visionquery" / "__init__.py").exists()


def test_root_depends_on_every_package() -> None:
    dependencies: list[str] = _pyproject(ROOT)["project"]["dependencies"]
    assert set(dependencies) == {f"visionquery-{p}" for p in PACKAGES}


def test_python_version_is_the_same_everywhere() -> None:
    pinned = (ROOT / ".python-version").read_text().strip()
    major_minor = pinned.split(".")
    assert len(major_minor) == 2, (
        "pin the minor only; the patch is whichever uv-managed 3.14 is installed"
    )
    root = _pyproject(ROOT)
    assert root["project"]["requires-python"] == f"=={pinned}.*"
    assert root["tool"]["pyright"]["pythonVersion"] == pinned
    assert root["tool"]["ruff"]["target-version"] == f"py{''.join(major_minor)}"
    for package in PACKAGES:
        project = _pyproject(ROOT / "packages" / package)["project"]
        assert project["requires-python"] == f"=={pinned}.*", package


def test_the_interpreter_is_uv_managed_never_the_systems() -> None:
    assert _pyproject(ROOT)["tool"]["uv"]["python-preference"] == "only-managed"


def test_no_one_claims_the_patch_release_is_pinned() -> None:
    # Only the minor is pinned, so a machine may hold a different 3.14 patch than another.
    text = (ROOT / "pyproject.toml").read_text().lower()
    assert "same patch" not in text


def test_pyright_brings_its_own_node() -> None:
    dev: list[str] = _pyproject(ROOT)["dependency-groups"]["dev"]
    assert any(d.startswith("pyright[nodejs]") for d in dev)


def test_lockfile_is_committed_and_current() -> None:
    assert (ROOT / "uv.lock").is_file()
    result = subprocess.run(
        [os.environ.get("UV", "uv"), "lock", "--check", "--offline"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
