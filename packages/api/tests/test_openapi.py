"""`visionquery-openapi` writes the api's OpenAPI schema, and says when a copy of it is stale.

The web app's types are generated from the file it writes (web/src/api/), so a copy that no
longer matches the api must fail the check that `make check` runs.
"""

import json
import subprocess
import sys
from pathlib import Path

from visionquery.api.openapi import render

COMMAND = Path(sys.executable).parent / "visionquery-openapi"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(COMMAND), *args], capture_output=True, text=True, timeout=60, check=False
    )


def test_the_schema_describes_health_and_its_unhealthy_answer() -> None:
    schema = json.loads(render())
    responses = schema["paths"]["/health"]["get"]["responses"]
    health = {"$ref": "#/components/schemas/Health"}
    assert responses["200"]["content"]["application/json"]["schema"] == health
    assert responses["503"]["content"]["application/json"]["schema"] == health
    problem = schema["components"]["schemas"]["DatabaseStatus"]["properties"]["problem"]
    # The problem is one of a fixed set of categories, so the web app can tell them apart.
    assert {"$ref": "#/components/schemas/ConnectionProblem"} in problem["anyOf"]
    categories = schema["components"]["schemas"]["ConnectionProblem"]["enum"]
    assert "authentication failed" in categories


def test_the_schema_is_rendered_the_same_every_time_and_ends_with_a_newline() -> None:
    assert render() == render()
    assert render().endswith("}\n")


def test_writes_the_schema_and_then_finds_it_current(tmp_path: Path) -> None:
    path = tmp_path / "openapi.json"
    written = _run(str(path))
    assert written.returncode == 0, written.stderr
    assert path.read_text() == render()
    checked = _run("--check", str(path))
    assert (checked.returncode, checked.stderr) == (0, "")


def test_the_check_fails_when_the_copy_differs_from_the_api(tmp_path: Path) -> None:
    path = tmp_path / "openapi.json"
    schema = json.loads(render())
    del schema["paths"]["/health"]
    path.write_text(json.dumps(schema, indent=2) + "\n")
    checked = _run("--check", str(path))
    assert checked.returncode == 1
    assert str(path) in checked.stderr
    assert "make api-types" in checked.stderr
    assert path.read_text() != render(), "the check must never rewrite the file"


def test_the_check_fails_when_there_is_no_copy(tmp_path: Path) -> None:
    checked = _run("--check", str(tmp_path / "missing.json"))
    assert checked.returncode == 1
    assert "make api-types" in checked.stderr
