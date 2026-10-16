"""The runtime package is installed from its own source tree and imports cleanly."""

from pathlib import Path

import visionquery.runtime


def test_imports_from_its_own_source_tree() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "visionquery" / "runtime"
    assert Path(visionquery.runtime.__file__).resolve().parent == source
