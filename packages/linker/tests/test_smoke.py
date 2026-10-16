"""The linker package is installed from its own source tree and imports cleanly."""

from pathlib import Path

import visionquery.linker


def test_imports_from_its_own_source_tree() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "visionquery" / "linker"
    assert Path(visionquery.linker.__file__).resolve().parent == source
