"""The reconciler package is installed from its own source tree and imports cleanly."""

from pathlib import Path

import visionquery.reconciler


def test_imports_from_its_own_source_tree() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "visionquery" / "reconciler"
    assert Path(visionquery.reconciler.__file__).resolve().parent == source
