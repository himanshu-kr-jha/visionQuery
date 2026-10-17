"""`visionquery-openapi`: write the api's OpenAPI schema, or check that a written copy is current.

The web app's TypeScript types are generated from the copy in `web/src/api/` (`make api-types`),
and `make check` fails while that copy no longer matches the api (`--check`). It is a
development tool run from a checkout, not a Deployment's process, so it starts no runtime.

    visionquery-openapi web/src/api/openapi.json            write it
    visionquery-openapi --check web/src/api/openapi.json    exit 1 unless it is current
"""

import argparse
import json
from pathlib import Path

from visionquery.api.app import create_app

__all__ = ["main", "render"]


def render() -> str:
    """The schema as the file holds it: indented JSON in the api's order, ending in a newline."""
    return json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    """The `visionquery-openapi` command."""
    parser = argparse.ArgumentParser(
        prog="visionquery-openapi",
        description="Write the api's OpenAPI schema, or check that a written copy is current.",
    )
    parser.add_argument("path", type=Path, help="the schema file, such as web/src/api/openapi.json")
    parser.add_argument(
        "--check", action="store_true", help="exit 1, changing nothing, unless the file is current"
    )
    arguments = parser.parse_args()
    path: Path = arguments.path
    schema = render()
    if not arguments.check:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(schema, encoding="utf-8")
        return
    try:
        current = path.read_text(encoding="utf-8") == schema
    except FileNotFoundError:
        current = False
    if not current:
        raise SystemExit(
            f"{path} is out of date with the api: run `make api-types` and commit the result"
        )
