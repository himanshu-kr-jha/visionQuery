# The checks CI runs, one target each; `make check` runs them all.
# Every target is a thin wrapper over a command listed in CLAUDE.md.

# A recipe stops at its first failing command, and at a failing stage of a pipe.
SHELL := bash
.SHELLFLAGS := -euo pipefail -c

.PHONY: bootstrap build sync lint format typecheck contracts test test-stack check branch-rules up down fetch-samples
.PHONY: web web-lint web-typecheck web-test api-types api-types-check

# The web app (web/) needs Node 24 (web/.nvmrc) and installs from its lockfile.
WEB := npm --prefix web

# From a fresh clone to a development environment: checks the prerequisites (naming each that
# is missing or too old), writes .env from .env.example with generated passwords unless .env
# exists, and runs `make sync`. Without the project, so nothing is synced before the checks.
bootstrap:
	@command -v uv >/dev/null || { echo "make bootstrap: uv is not installed: install it (https://docs.astral.sh/uv/getting-started/installation/), open a new shell, and run make bootstrap again" >&2; exit 1; }
	uv run --no-project scripts/bootstrap.py

sync:
	uv sync --locked
	$(WEB) ci

lint:
	uv run ruff check
	uv run ruff format --check
	uv run actionlint

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

web: web-lint web-typecheck web-test

web-lint:
	$(WEB) run lint

web-typecheck:
	$(WEB) run typecheck

web-test:
	$(WEB) test

# The web app's API types come from the api: its OpenAPI schema is written to API_SCHEMA, and
# openapi-typescript generates API_TYPES from that. Both are committed; after changing the api,
# run `make api-types` and commit the result. `api-types-check` fails, changing nothing, when
# either file is not what regenerating would give.
API_SCHEMA ?= web/src/api/openapi.json
API_TYPES ?= web/src/api/schema.d.ts
OPENAPI_TYPESCRIPT := web/node_modules/.bin/openapi-typescript

api-types:
	uv run visionquery-openapi $(API_SCHEMA)
	$(OPENAPI_TYPESCRIPT) $(API_SCHEMA) --output $(API_TYPES)

api-types-check:
	uv run visionquery-openapi --check $(API_SCHEMA)
	$(OPENAPI_TYPESCRIPT) $(API_SCHEMA) --output $(API_TYPES) --check || { \
		echo "$(API_TYPES) is out of date with $(API_SCHEMA): run \`make api-types\` and commit the result" >&2; exit 1; }

check: lint typecheck contracts test web api-types-check

# Makes the develop and main rulesets require a PR and every job of .github/workflows/ci.yml,
# through the gh CLI of a repository admin. It refuses until each job has passed on develop;
# `uv run scripts/branch_rules.py` prints what it would send.
branch-rules:
	uv run scripts/branch_rules.py --apply

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
# `up` builds first, so that changed code is what runs: Compose's `pull_policy: never` (see
# compose.yaml) builds only an absent image, which would leave a stale one running after an
# edit. Building is cheap on an unchanged tree because every layer is cached, and the image ID
# stays the same only when BuildKit adds no attestation, whose timestamp would otherwise give
# a new ID on every build and have Compose recreate migrate and mediamtx on every `up`.
# The variable is exported for every Compose call, including a `docker compose build` run by hand.
export BUILDX_NO_DEFAULT_ATTESTATIONS = 1
COMPOSE ?= docker compose

# The one-shot jobs (restart: "no" in compose.yaml; tests/test_compose.py checks the list):
# `up --wait` fails on a service that exited, so they are left out of it. Everything else
# is started and waited for.
ONE_SHOTS ?= migrate triton-scrape
# Seconds `make up` gives the migrations to finish, and then every healthcheck.
MIGRATE_TIMEOUT ?= 300
WAIT_TIMEOUT ?= 300

# Builds the images of the enabled profiles, as `make up` does before starting anything.
build:
	$(COMPOSE) build

up: build
	$(COMPOSE) up --detach migrate
	timeout $(MIGRATE_TIMEOUT) $(COMPOSE) wait migrate >/dev/null || { status=$$?; \
		if [ $$status -eq 124 ]; then echo "make up: the database migrations did not finish within $(MIGRATE_TIMEOUT) s (MIGRATE_TIMEOUT). migrate said:" >&2; \
		else echo "make up: the database migrations failed ($(COMPOSE) wait migrate exited $$status, with the error above if it printed one). migrate said:" >&2; fi; \
		$(COMPOSE) logs --no-color --tail 20 migrate >&2; exit 1; }
	services="$$($(COMPOSE) config --services | grep --invert-match --line-regexp --fixed-strings $(addprefix --regexp ,$(ONE_SHOTS)) || true)"; \
		[ -n "$$services" ] || { echo "make up: no long-running service in the project" >&2; exit 1; }; \
		$(COMPOSE) up --detach --wait --wait-timeout $(WAIT_TIMEOUT) $$services

# Stops every service of the project, including those of a profile that is not enabled now
# (Triton, which would otherwise keep the GPU), and keeps its data; `make down VOLUMES=1`
# deletes the data too (the database, the metrics).
down:
	$(COMPOSE) --profile '*' down --remove-orphans $(if $(VOLUMES),--volumes)

# Downloads the sample videos samples/manifest.toml lists into samples/ (git ignores them),
# verifying each against its SHA-256 and skipping those already present. About 400 MB.
fetch-samples:
	uv run scripts/fetch_samples.py
