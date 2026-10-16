"""The api package is installed from its own source tree and imports cleanly."""

from pathlib import Path

import visionquery.api


def test_imports_from_its_own_source_tree() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "visionquery" / "api"
    assert Path(visionquery.api.__file__).resolve().parent == source
