"""The memory package is installed from its own source tree and imports cleanly."""

from pathlib import Path

import visionquery.memory


def test_imports_from_its_own_source_tree() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "visionquery" / "memory"
    assert Path(visionquery.memory.__file__).resolve().parent == source
