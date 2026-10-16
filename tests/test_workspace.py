"""The uv workspace holds exactly the packages CLAUDE.md names, each installable and locked."""

import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {"memory", "tools", "perception", "linker", "recorder", "reasoner", "api"}


def _pyproject(path: Path) -> dict[str, Any]:
    return tomllib.loads((path / "pyproject.toml").read_text())


def test_workspace_members_are_the_named_packages() -> None:
    members: list[str] = _pyproject(ROOT)["tool"]["uv"]["workspace"]["members"]
    found = {p.parent.name for m in members for p in ROOT.glob(f"{m}/pyproject.toml")}
    assert found == PACKAGES


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


def test_python_pin_matches_requires_python() -> None:
    pinned = (ROOT / ".python-version").read_text().strip()
    assert _pyproject(ROOT)["project"]["requires-python"] == f"=={pinned}.*"


def test_lockfile_is_committed_and_current() -> None:
    assert (ROOT / "uv.lock").is_file()
    result = subprocess.run(
        ["uv", "lock", "--check", "--offline"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
