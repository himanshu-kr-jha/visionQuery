"""Pyright runs in strict mode over the whole workspace."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyright(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(Path(sys.executable).parent / "pyright"), "--project", str(ROOT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_an_unannotated_function_is_an_error(tmp_path: Path) -> None:
    loose = tmp_path / "loose.py"
    loose.write_text("def identity(value):\n    return value\n")
    result = _pyright(str(loose))
    assert result.returncode != 0, result.stdout
    assert "reportMissingParameterType" in result.stdout
