# The checks CI runs, one target each; `make check` runs them all.
# Every target is a thin wrapper over a command listed in CLAUDE.md.

.PHONY: sync lint format typecheck contracts test test-stack check up down

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

# Starts the stack, and fails when the migrations did not succeed. `docker compose up --wait`
# returns while `migrate` is still running, so a failed migration would go unnoticed, and on
# a stack that is already up it fails because `migrate` ran again and exited, which is why
# `migrate` is run and waited for first, and it and every other one-shot job (restart: "no")
# is left out of the `--wait`. The schema is therefore up to date before any other service starts.
# Profiles and files come from the environment as usual:
#   make up COMPOSE_PROFILES=observability
#   make up COMPOSE_FILE=compose.yaml:compose.no-gpu.yaml COMPOSE_PROFILES=gpu
# COMPOSE is the command to run, which the stack tests replace to target a project of their own.
#
# `up` builds first, so that changed code is what runs: Compose's `pull_policy: missing` (see
# compose.yaml) builds only an absent image, which would leave a stale one running after an
# edit. Building is cheap on an unchanged tree because every layer is cached, and the image ID
# stays the same only when BuildKit adds no attestation, whose timestamp would otherwise give
# a new ID on every build and have Compose recreate migrate and mediamtx on every `up`.
# The variable is exported for every Compose call, including a `docker compose build` run by hand.
export BUILDX_NO_DEFAULT_ATTESTATIONS = 1
COMPOSE ?= docker compose

define LONG_RUNNING
import json, sys
services = json.load(sys.stdin)["services"]
print(*sorted(n for n, s in services.items() if s.get("restart", "no") != "no"))
endef
export LONG_RUNNING

up:
	$(COMPOSE) build
	$(COMPOSE) up --detach migrate
	@if ! $(COMPOSE) wait migrate >/dev/null 2>&1; then \
		echo "make up: the database migrations failed. migrate said:" >&2; \
		$(COMPOSE) logs --no-color --tail 20 migrate >&2; \
		exit 1; \
	fi
	$(COMPOSE) up --detach --wait $$($(COMPOSE) config --format json | python3 -c "$$LONG_RUNNING")

down:
	$(COMPOSE) down
