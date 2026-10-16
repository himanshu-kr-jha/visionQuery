# The checks CI runs, one target each; `make check` runs them all.
# Every target is a thin wrapper over a command listed in CLAUDE.md.

.PHONY: sync lint format typecheck contracts test check

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

check: lint typecheck contracts test
