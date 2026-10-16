"""The recorder package is installed from its own source tree and imports cleanly."""

from pathlib import Path

import visionquery.recorder


def test_imports_from_its_own_source_tree() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "visionquery" / "recorder"
    assert Path(visionquery.recorder.__file__).resolve().parent == source
