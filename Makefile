# The checks CI runs, one target each; `make check` runs them all.
# Every target is a thin wrapper over a command listed in CLAUDE.md.

.PHONY: sync lint format typecheck contracts test test-stack check

sync:
	uv sync --locked

lint:
	uv run ruff check
	uv run ruff format --check

format:
	uv run ruff check --fix
	uv run ruff format

typecheck:
	uv run pyright

contracts:
	uv run lint-imports

test:
	uv run pytest

# Brings the Compose stack up for real and takes it down again; needs a running Docker.
# Not part of `check`. Add VISIONQUERY_TEST_TRITON=gpu (or =cpu on a machine without a
# GPU) to include Triton, whose image is about 9 GB to download and 25 GB on disk.
test-stack:
	uv run pytest -m stack

check: lint typecheck contracts test
