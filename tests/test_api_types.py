"""The web app's API types are generated from the api, and `make check` fails when they are stale.

Two files are committed in `web/src/api/`: `openapi.json`, the api's OpenAPI schema as
`visionquery-openapi` writes it, and `schema.d.ts`, the TypeScript openapi-typescript generates
from it. `make api-types` regenerates both; `make api-types-check` fails when either is not what
regenerating would give, and `make check` runs it.

Each test that runs the check points it at a copy in a temporary directory (`API_SCHEMA` and
`API_TYPES`), so the committed files are never touched. They need the web app's dependencies
(`make sync`), and are skipped, saying so, without them.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "web" / "src" / "api"
SCHEMA = API / "openapi.json"
TYPES = API / "schema.d.ts"
GENERATOR = ROOT / "web" / "node_modules" / ".bin" / "openapi-typescript"

needs_web = pytest.mark.skipif(
    not GENERATOR.exists(), reason="needs the web app's dependencies: run `make sync`"
)


def _make(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _copy(tmp_path: Path) -> tuple[Path, Path]:
    schema, types = tmp_path / SCHEMA.name, tmp_path / TYPES.name
    shutil.copy(SCHEMA, schema)
    shutil.copy(TYPES, types)
    return schema, types


def _check(schema: Path, types: Path) -> subprocess.CompletedProcess[str]:
    return _make("api-types-check", f"API_SCHEMA={schema}", f"API_TYPES={types}")


def test_make_check_runs_the_api_types_check() -> None:
    check = subprocess.run(
        ["make", "--dry-run", "--no-print-directory", "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"visionquery-openapi --check {SCHEMA.relative_to(ROOT)}" in check
    assert "--check" in next(line for line in check.splitlines() if "openapi-typescript" in line)


@needs_web
def test_the_committed_schema_and_types_are_current() -> None:
    result = _make("api-types-check")
    assert result.returncode == 0, result.stdout + result.stderr


@needs_web
def test_regenerating_gives_exactly_the_committed_files(tmp_path: Path) -> None:
    schema, types = tmp_path / "api" / SCHEMA.name, tmp_path / "api" / TYPES.name
    result = _make("api-types", f"API_SCHEMA={schema}", f"API_TYPES={types}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert schema.read_text() == SCHEMA.read_text()
    assert types.read_text() == TYPES.read_text()


@needs_web
def test_the_check_fails_when_the_schema_no_longer_matches_the_api(tmp_path: Path) -> None:
    # As after changing an endpoint without running `make api-types`.
    schema, types = _copy(tmp_path)
    stale = json.loads(schema.read_text())
    stale["paths"]["/health"]["get"]["summary"] = "an older summary"
    schema.write_text(json.dumps(stale, indent=2) + "\n")
    result = _check(schema, types)
    assert result.returncode != 0
    assert "make api-types" in result.stderr


@needs_web
def test_the_check_fails_when_the_types_no_longer_match_the_schema(tmp_path: Path) -> None:
    # As after editing the generated types by hand, or regenerating only the schema.
    schema, types = _copy(tmp_path)
    text = types.read_text()
    edited = text.replace('"healthy" | "unhealthy"', '"healthy" | "unhealthy" | "degraded"')
    assert edited != text
    types.write_text(edited)
    result = _check(schema, types)
    assert result.returncode != 0
    assert "make api-types" in result.stderr
    assert types.read_text() == edited, "the check must never rewrite the types"


@needs_web
def test_the_check_fails_when_the_types_are_missing(tmp_path: Path) -> None:
    schema, types = _copy(tmp_path)
    types.unlink()
    result = _check(schema, types)
    assert result.returncode != 0
    assert "make api-types" in result.stderr


def test_only_the_generated_types_are_marked_generated_for_the_language_statistics() -> None:
    # openapi.json is the api's schema, which people review; schema.d.ts is the generated one.
    def attribute(path: Path) -> str:
        done = subprocess.run(
            ["git", "check-attr", "linguist-generated", "--", str(path.relative_to(ROOT))],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.rsplit(": ", 1)[1].strip()

    assert attribute(TYPES) == "set"
    assert attribute(SCHEMA) == "unspecified"
