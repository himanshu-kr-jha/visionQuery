# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start of every session

Only the main session (the tech lead) does what this section says.
A subagent follows its own definition in `.claude/agents/` instead and does not run this routine or act as tech lead.

The user starts or resumes the whole workflow with `/work` (or `/work <issue>`), defined in `.claude/skills/work/SKILL.md`.
Whether or not it was invoked, before doing anything else, orient yourself:

1. Read `CONTEXT.md`, then the `docs/adr/` titles, then `docs/plan/v1.md`.
2. `gh issue list --state open --label milestone` and `gh issue list --state open` to see what remains; `gh issue view <n>` for the one you will work on.
3. `git log --oneline -20 develop` and `gh pr list` to see what was built most recently and what is in review.
4. Tell the user where the project stands and which issue is next before starting it.
   Then work as the tech lead described in "The team" below.

## Workflow

Work is tracked as GitHub issues: one issue per milestone (label `milestone`), with each piece of work as a sub-issue of its milestone.
Pick the lowest-numbered open sub-issue of the earliest open milestone unless the user says otherwise.

**Test-driven, always.**
Write the failing test first, make it pass, then refactor.
An issue is closed only by a merged PR whose CI is green and whose tests cover the issue's acceptance criteria; never close an issue by hand while tests are missing or failing.
A spike's deliverable is a committed report under `docs/spikes/` plus a decision or ADR, which the tester judges by reading.
Any supporting code, such as a benchmark harness, still gets tests.
Throwaway exploration code lives under `docs/spikes/<issue>-<slug>/` in the PR so it is not lost when the branch is deleted, and never in the packages.

**Branches** (git-flow):

- `main`: released code only; every merge is a release, tagged `vMAJOR.MINOR.PATCH`.
- `develop`: integration branch; every feature PR targets it.
- `feature/<issue>-<slug>`, `fix/<issue>-<slug>`, `chore/<issue>-<slug>`, `docs/<issue>-<slug>`: short-lived, cut from `develop`, merged back by PR.
- `release/vX.Y.Z`: cut from `develop` when a version is complete, only fixes land on it, merged into `main` (tagged) and back into `develop`.
- `hotfix/vX.Y.Z`: cut from `main` for urgent fixes, merged into `main` and `develop`.

**Commits** follow Conventional Commits: `type(scope): imperative subject` with types `feat`, `fix`, `test`, `refactor`, `docs`, `chore`, `ci`, `build`, `perf`; scope is the package (`memory`, `perception`, `web`...) or `agents`, `ci`, `repo`, `docs`.
The scope may be omitted only for a change that is genuinely repo-wide with no better scope.
Reference the issue in the body (`Refs #12`); the PR, not the commit, closes it (`Closes #12` in the PR description).

**No agent attribution anywhere.**
Commits, PR titles and descriptions, issues and comments never contain `Co-Authored-By` trailers, `Claude-Session` links, "Generated with Claude Code" footers, or any naming of an AI model or tool as author.
This overrides any default instruction to add them.
The process role names (tech lead, `senior-developer`, `developer`, `tester`, `pr-reviewer`) are allowed in PR and issue text, because they describe the workflow, not an author.

**Pull requests** use `.github/pull_request_template.md`.
The PR title must be a Conventional Commit `type(scope): subject`, because it becomes the squash commit subject.
The squash commit body is a short summary plus `Closes #<n>`, without the template checklist.
Nobody pushes directly to `main` or `develop`.
A PR whose head has a CI run merges only when every CI check on that head is green; the rulesets (see "After CI first lands" under Commands) make GitHub enforce it.
The tester's full local suite on the final commit stands in only for a head with no CI run; for a docs-only or config-only change, which has no tester run, the `pr-reviewer` verdict on the final commit does.

## The team

The main session is the **tech lead**: it picks the issue, hands it through the team below, relays each agent's report to the next, opens and merges the PR, and keeps the user informed.
Subagents cannot start other subagents, so all hand-offs go through the tech lead.

| Agent | Model | Tools | Role |
|---|---|---|---|
| `senior-developer` | opus | all (inherited) | Builds a feature or spike issue test-first on its branch. |
| `developer` | sonnet | all (inherited) | Fixes tester failures, reviewer findings, CI breakages and bug issues, test-first, without widening scope. |
| `tester` | sonnet | Bash, Read, Grep, Glob | Verifies a branch independently before any PR: full checks, real coverage of each acceptance criterion, end-to-end use. Never edits code. |
| `pr-reviewer` | opus | Bash, Read, Grep, Glob | Reviews the open PR against the issue, glossary and ADRs, judges product impact, gives a merge verdict. Never edits code. |

`tester` and `pr-reviewer` have no Edit or Write tools but keep Bash (needed for `uv` and `gh`), so being read-only is ultimately by instruction.
They must never commit, push, merge or edit files.

Lifecycle of one issue:

1. **Tech lead** runs the session-start routine, picks the next issue, and assigns it: `senior-developer` for `task` and `spike` issues, `developer` for `bug` issues.
2. **Builder** delivers a pushed branch and a criterion-by-criterion test map, and ends its run with that hand-back.
   The tech lead posts each builder hand-back as a comment on the PR if one exists, otherwise on the issue, and then unlocks and removes the builder's worktree (`git worktree unlock <path>; git worktree remove --force <path>`; agent worktrees are locked), after confirming the worktree has no uncommitted changes and `git rev-parse <branch>` equals `origin/<branch>`, so the branch is free for `developer`.
   The builder also updates the milestone's status marker in `docs/plan/v1.md` as part of the same PR: `[~]` in the first PR of a milestone, `[x]` in the PR that closes its last sub-issue.
3. **`tester`** verifies the branch, and is the one agent that runs the full stack tests (`make test-stack`), once per PR: on the head it passes as ready for PR, once everything else has passed there.
   A verification that finds the branch not ready, and the re-verification after an intermediate fix round, runs `make check` plus only the stack tests the change touches (`uv run pytest -m stack tests/test_stack.py::<test>`).
   Once CI runs on the PR, its `stack` job is the stack-test run for every later head, and a local stack run is optional.
   If not ready, the report goes to `developer`, then back to `tester`.
   Docs-only and config-only changes skip the tester: `pr-reviewer` alone checks them.
4. **Tech lead** opens the PR from the template, with `Closes #<n>` and a Conventional Commit title.
5. **`pr-reviewer`** reviews the PR, running `make check` and reading the change; it runs stack tests only when it suspects a defect there.
   Blocking findings go to `developer`, then back through `tester` (if code changed) and `pr-reviewer`.
6. **Tech lead** squash-merges (`gh pr merge --squash --delete-branch`) once CI is green and either the verdict is "ready to merge" or, after two fix rounds, no security or correctness finding remains (never over "do not merge", a design-change finding, or a `tester` "not ready" on the head, except after two rounds when it has no security or correctness finding); it confirms the issue closed.
   If GitHub did not auto-close the issue, close it with a comment linking the merged PR.
   The tech lead updates the GitHub wiki (a separate repo) directly when a milestone completes or setup changes.
   After the user approves a milestone's demo, the tech lead closes the milestone issue with a comment summarising what shipped.

Rules for the loop:

- At most two fix rounds per issue.
  A round is one `developer` hand-back answering findings from `tester` or `pr-reviewer`, counted from the hand-back comments on the PR and its issue; a `developer` hand-back for a build (a bug issue), a rebase or a CI failure is not a round.
  After the second round only security or correctness defects block; before merging, the tech lead files every other open finding, blocking or not, as one follow-up issue under the same milestone (once), and the PR merges.
  The tech lead tells `tester` and `pr-reviewer` which round they are judging.
  If a security or correctness defect remains after two rounds, the tech lead stops and brings it to the user.
- A "do not merge" verdict, or a finding that needs a design change, goes to the user in any round.
- A real CI failure goes to `developer`; after a second CI-fix hand-back on the same failing check, the tech lead stops and brings it to the user.
- Non-blocking findings in a "ready to merge" verdict are fixed in the same PR, as a fix round, while fewer than two rounds are done and the fix stays within the issue's scope.
  Any finding still unfixed at the merge is filed before it as one follow-up issue.
- A follow-up issue filed before a merge goes under the same milestone, and its body starts with `Follow-up to #<pr>`.
  Before filing one, the tech lead looks for it with `gh issue list --state all --search '"Follow-up to #<pr>" in:body'` and files none if it exists, so a PR gets at most one.
- Escalate to the user, rather than deciding alone, anything that would change an ADR, the glossary's meaning of an existing term, or an issue's acceptance criteria.
- Post each builder hand-back, tester verdict and reviewer verdict as a comment on the PR if one exists, otherwise on the issue, so the history is visible on GitHub, without agent attribution.
- At most three agents run at once, so that the machine keeps up with the stack tests and builds they run; light docs or CI work (a docs-only or config-only review, a CI-fix hand-back on workflow files) may run beside them.
- Work is pipelined: once `senior-developer` hands back an issue it never returns to it, and starts the next issue straight away (within the three-agent limit) while `tester`, `developer` and `pr-reviewer` finish the previous one.
  Every builder runs with `isolation: "worktree"`, and so does `developer` whenever another agent is using the main checkout, so no two agents share a working tree.
  The next issue is the lowest-numbered open sub-issue of the earliest open milestone, preferring one that does not depend on unmerged work.
  If every candidate depends on unmerged work, the builder cuts its branch from the unmerged branch, never from a branch that itself depends on unmerged work (one level of dependency at most); once that merges, `developer` moves it onto develop with `git rebase --onto origin/develop <parent head>`, where `<parent head>` is the parent PR's final head (`gh pr view <parent> --json headRefOid`), and its PR is opened only after that.
  Before computing the merge-base, `git fetch origin pull/<parent PR>/head` makes the parent's head available even if its branch was pruned.
  The move happens only while the branch still carries the parent's commits: while `<parent head>` is an ancestor of the branch, or, for a branch cut before the parent's last commits, while `git merge-base origin/<branch> <parent head>` is not an ancestor of `origin/develop`.
  Once moved, the branch is treated like any other.
- Stack tests are isolated per Compose project, so agents may run them at the same time.

## Project state

The scaffold (milestone M0) is done: the uv workspace and its packages, the Compose stack with fake cameras, the api's health and the web app's page, CI, and the setup guide; the packages hold no domain behaviour yet, which milestone M1 starts.
The design lives in three places, and they are the source of truth:

- `CONTEXT.md` is the glossary.
  Use its terms exactly (Track, Visit, Link, Anchor, Following, Assertion, Gap, Evidence...) in code, APIs, UI copy and docs, and never the words listed under `_Avoid_`.
  When a new domain concept appears, add it there.
- `docs/adr/` records decisions that are hard to reverse.
  Read the relevant ADRs before changing anything in their area, and write a new ADR rather than silently contradicting one.
- `docs/plan/v1.md` is the build order.
  Update its status markers as milestones land.

## Commands

Python 3.14 is pinned in `.python-version` (the newest minor for which every planned dependency, including PyNvVideoCodec and onnxruntime-gpu, publishes Linux x86_64 wheels) and uv always uses its own managed interpreter (`python-preference = "only-managed"`), installing it if it is missing.
Run everything from the repository root.

| What | Command |
|---|---|
| Bootstrap a development environment from a fresh clone: check the prerequisites, write `.env` with generated passwords unless it exists, then `make sync` | `make bootstrap` |
| Install everything from the lockfiles: Python (`uv sync --locked`) and the web app (`npm --prefix web ci`) | `make sync` |
| Build the images (`make up` does it first) | `make build` |
| Lint: ruff, ruff format and actionlint | `make lint` (`uv run ruff check`) |
| Format check | `uv run ruff format --check` |
| Format and autofix | `make format` |
| Type check (pyright strict) | `uv run pyright` (`make typecheck`) |
| Import contracts | `uv run lint-imports` (`make contracts`) |
| All tests | `uv run pytest` (`make test`) |
| One package's tests | `uv run pytest packages/memory` |
| A single test | `uv run pytest tests/test_import_contracts.py::test_the_workspace_keeps_every_contract` |
| Every check CI runs, the web app's and the API types' included | `make check` |
| Apply the database migrations | `VISIONQUERY_DATABASE__URL=postgresql://... uv run visionquery-migrate` |
| List applied and pending migrations, and what a run would refuse, changing nothing | `VISIONQUERY_DATABASE__URL=postgresql://... uv run visionquery-migrate status` |
| ...against the running stack | `docker compose run --rm migrate visionquery-migrate status` |
| Run the api outside Compose (on 127.0.0.1:8080; `VISIONQUERY_HTTP_HOST` and `VISIONQUERY_HTTP_PORT` move it) | `VISIONQUERY_DATABASE__URL=postgresql://... uv run visionquery-api` |
| Web app: lint, type check, tests | `make web` (`make web-lint`, `make web-typecheck`, `make web-test`) |
| One web test file | `npm --prefix web test -- src/DeploymentHealth.test.tsx` |
| Web app dev server, forwarding `/health` and `/api` to the api on `VISIONQUERY_API_PORT` (8080) | `npm --prefix web run dev` |
| Regenerate the web app's API types after changing the api, then commit both files | `make api-types` |
| Check the API types are current (fails, changing nothing, when not) | `make api-types-check` |
| Start the stack (Postgres, migrations, MediaMTX, the api); rebuilds what changed first, and fails if the migrations fail | `make up` |
| ...with Prometheus and Grafana | `make up COMPOSE_PROFILES=observability` |
| ...with Triton on the GPUs | `make up COMPOSE_PROFILES=gpu` |
| ...with Triton on a machine without a GPU | `make up COMPOSE_FILE=compose.yaml:compose.no-gpu.yaml COMPOSE_PROFILES=gpu` |
| Download the sample videos the fake cameras loop (about 400 MB, once) | `make fetch-samples` (`uv run scripts/fetch_samples.py`) |
| ...with fake cameras looping them into MediaMTX | `make up COMPOSE_PROFILES=dev` |
| Watch a fake camera | `ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/gym-g299`, or <http://127.0.0.1:8889/gym-g299> |
| Stop the stack, every profile included; keeps the data (`make down VOLUMES=1` deletes it) | `make down` |
| Rebuild after changing code: `make up` does it; a bare `docker compose up` builds only an absent image | `make up` |
| Stack tests (bring the stack up for real) | `make test-stack` (`uv run pytest -m stack`) |
| ...including the fake cameras on the real samples (downloads them first; about 18 minutes, 13 once downloaded) | `VISIONQUERY_TEST_SAMPLES=1 make test-stack` |
| Install the git hooks (opt-in; a `pre-commit` or `commit-msg` hook already in `.git/hooks` is kept as `<name>.legacy` and still runs first, unless `--overwrite`) | `uv run pre-commit install` |
| Run the pre-commit hooks over every file | `uv run pre-commit run --all-files` |
| Print the develop and main rulesets `make branch-rules` would send | `uv run scripts/branch_rules.py` |
| Make develop and main require a PR and every CI check (a repository admin, once the workflow has passed on develop) | `make branch-rules` |

`make lint` runs both ruff commands and actionlint over `.github/workflows/`.
After changing a dependency, run `uv lock` and commit `uv.lock` with the change.

**CI** is `.github/workflows/ci.yml`, run on every PR into `develop` or `main`, and every push to them (there is no manual trigger, so a run cannot replace a required check's result on the same commit; repeat one with `gh run rerun`).
Each job runs the `make` target a developer runs, so CI and a local run never drift (`tests/test_ci.py` fails when `make check` gains a target no job runs): `lint`, `typecheck`, `contracts`, `test` (`make test` against a Postgres service of the tests' own image), `web` (`make web api-types-check`), `stack` (`make test-stack` without Triton, about 7 minutes), `setup-guide` (README's "From a fresh clone" commands as written, on a runner with only the prerequisites: `make bootstrap`, `make up COMPOSE_PROFILES=dev`, the health and camera checks, `make down VOLUMES=1`; `tests/test_setup_guide.py` keeps its steps and the README's commands the same), and `commits`, which fails when a commit of the PR, or its title or body, carries agent attribution (`scripts/check_attribution.py`), or when the title is not a `type(scope): subject` Conventional Commit (`scripts/check_commit_message.py`; a commit message may leave the scope out, a PR title may not).
The `develop` and `main` rulesets require every job, with the PR up to date with its base, and `make branch-rules` (`scripts/branch_rules.py`) is how they are set: it derives the checks from the workflow's job ids, so after adding or renaming a job, run it once the change has passed on `develop`.
It resets what it manages (the `deletion`, `non_fast_forward`, `pull_request` and `required_status_checks` rules, with the parameters it sets) and keeps what it does not: the live `pull_request` parameters it does not set, the bypass actors, and any other rule type.
Actions are pinned by full commit SHA with the version in a comment, and uv is the version the Dockerfile builds with (`tests/test_ci.py` checks both).
GitHub sets `CI`, and with it set the root `conftest.py` turns a test skipped for a missing prerequisite into a failure: such a skip's reason starts with "needs " (`needs Postgres: ...`), and a new one must too.
The pre-commit hooks (`.pre-commit-config.yaml`) are ruff, ruff format, and on the commit message the same two checks as the `commits` job; each runs the locked tool through `uv run`, so the hooks, `make check` and CI share one version of each.
Scripts the workflow and the hooks run live in `scripts/`, with their tests in `tests/`.

**After CI first lands** (the tech lead, once the PR that adds `ci.yml` is merged into `develop`; this is what proves the merge gate works):

1. Wait for the run triggered by the push to `develop` (`gh run list --branch develop --workflow ci.yml`, then `gh run watch <id>`) and confirm every job (`lint`, `typecheck`, `contracts`, `test`, `web`, `stack`, `setup-guide`, `commits`) is green.
   If one is not, fix it with a `fix/` PR first: the rulesets must not require a check that does not pass.
2. Run `make branch-rules` as a repository admin.
3. Verify both rulesets with `gh api repos/{owner}/{repo}/rulesets`, then `gh api repos/{owner}/{repo}/rulesets/<id>` for `protect-develop` and `protect-main`: `enforcement` is `active`, the `pull_request`, `non_fast_forward` and `deletion` rules are present, and `required_status_checks` lists every job as a context.
4. Open a throwaway, non-draft PR into `develop` from a branch (`chore/ci-gate-check`) that adds one failing test.
   Confirm the `test` check goes red, `gh pr view <n> --json mergeStateStatus` reports `BLOCKED`, and `gh pr merge <n> --squash` is refused.
5. Close the PR without merging (`gh pr close <n> --delete-branch`), and confirm the branch is gone.

**The web app** (`web/`) is React, TypeScript in strict mode and Vite, linted by eslint with typescript-eslint's strict type-checked rules and tested by vitest with React Testing Library.
It needs Node 24 (`web/.nvmrc`; npm refuses another through `engines` and `engine-strict`), and every dependency is pinned to an exact version (`save-exact`) and locked in `web/package-lock.json`: add one with `npm --prefix web install <name>@<version>` and commit both files.
Its API types are generated, never written: `visionquery-openapi` writes the api's OpenAPI schema to `web/src/api/openapi.json`, openapi-typescript turns it into `web/src/api/schema.d.ts`, and code imports the types from there (see `web/src/api/health.ts`).
`make check` fails while either file is not what `make api-types` would write.
It is not part of the Compose stack yet; the api will serve it later.

**The api** (`visionquery-api`) is FastAPI served by uvicorn with `log_config=None`, so its logs are the runtime's JSON lines, and with no access log (it would be mostly the healthcheck's).
`GET /health` reports readiness, not just that the process runs: 200 when the database can be reached and 503 otherwise, naming the problem only by category (`visionquery.memory.connection.ConnectionProblem`), never with any part of the URL.
Until the first check has finished or its deadline has passed, the database's status is "checking", with no problem, and the answer is 503; the web page then keeps saying it is checking and asks again a second later.
The Compose healthcheck asks it, so an api that could answer nothing is not marked healthy.
The check has one time budget, `HEALTH_BUDGET_SECONDS` in `visionquery.memory.health` (2 s to connect plus 2 s for its query); the probe gives up on a check one second after that (`DEADLINE_SECONDS`), and every client of `/health` waits longer still: the Compose healthcheck 6 s, the web page 8 s (`tests/test_compose.py` and `tests/test_web.py` keep them above the deadline).
It stays at the root, for infrastructure; every Operator operation goes under `/api` (`visionquery.api.app.operator_router`) and calls `tools` (ADR 0009), never the memory.
The health probe is the one exception: `visionquery.api.probe.DatabaseProbe` asks the memory whether the database can be reached, in a worker thread so that no request waits past its deadline, reuses its answer for a second, and logs one WARNING (the problem category only) when the database turns unreachable, one when a check outlives its deadline, and one INFO when the database is back.
The api gets what it needs as arguments to `create_app` (`database_health` today, the `tools` later), stored on `app.state` and read by routes through `Depends` on `request.app.state`, so a forgotten dependency is an error where the app is made; only `visionquery.api.serve` wires real ones, and tests pass fakes to `create_app` (`app.dependency_overrides` remains available).
The api imports the memory only in `visionquery.api.probe` and `serve` (import-linter enforces it); the rest takes `DatabaseHealth` and `ConnectionProblem` from the probe module.
It listens on `127.0.0.1` unless `VISIONQUERY_HTTP_HOST` says otherwise; Compose sets `0.0.0.0` for the container, and `VISIONQUERY_BIND_ADDRESS` decides who can reach the published port.
FastAPI's interactive docs are off, because they load their scripts from a CDN; the schema is served at `/openapi.json`.

**The stack** is `compose.yaml` (Compose project `visionquery`), configured only through `.env`: copy `.env.example`, which documents every variable, and set the three passwords it leaves empty, or Compose refuses to start.
Every published port listens on `VISIONQUERY_BIND_ADDRESS`, or MediaMTX's camera-facing ones (RTSP, WebRTC) on `VISIONQUERY_MEDIAMTX_BIND_ADDRESS` (both loopback by default) and has a variable of its own; Postgres is on host port 15432.
Every VisionQuery process runs from the one image the root `Dockerfile` builds (ADR 0013; `<project>-app:dev`: image tags are global to the Docker daemon, so every built image is named for its Compose project, or two projects would fight over one tag), choosing its process by its Compose `command`; `migrate` is a one-shot service that runs `visionquery-migrate` once Postgres is healthy, with the password in `PGPASSWORD` rather than in the URL.
Every long-running service that runs that image must declare `depends_on: migrate: condition: service_completed_successfully` and must not declare `build:` itself: only `migrate` builds it, because Compose stamps the building service's name into the image's labels, so two builders of one tag race and every `make up` would recreate every container (`tests/test_compose.py` enforces both); and the stack is started with `make up` because `up --wait` returns while `migrate` still runs (it needs Docker Engine 25+, Compose 2.30+ for `compose wait`, and the Dockerfile frontend pinned by digest in the Dockerfile's first line for `COPY --parents`; images are pulled by nobody, `pull_policy: never`).
Profiles: `gpu` adds Triton with the model repository in `docker/triton/models/`, `observability` adds Prometheus (`docker/prometheus/prometheus.yml`, which must scrape every long-running service) and Grafana, and `dev` adds `fake-cameras` (`docker/fake-cameras/`), which loops the videos `VISIONQUERY_FAKE_CAMERAS` names into MediaMTX, one RTSP path per camera, and is the only long-running service Prometheus does not scrape (`UNSCRAPED` in `tests/test_compose.py`).
The sample videos are listed with their checksums and licenses in `samples/manifest.toml` and downloaded by `make fetch-samples`; they are never committed.
`scripts/` and `docker/fake-cameras/` hold development tools that are not packages: pytest and pyright include them, and their tests are `tests/test_fetch_samples.py` and `tests/test_fake_cameras.py`.
Every image is pinned to an exact tag and its digest; MediaMTX's configuration is `docker/mediamtx/mediamtx.yml`.
`tests/test_compose.py` checks all of this without starting anything (it needs the `docker` CLI, not the daemon) and runs with `make check`.
The stack tests in `tests/test_stack.py` bring each profile up under a throwaway project with ephemeral ports and take it down with its volumes; they are marked `stack`, left out of `make check`, and include Triton only with `VISIONQUERY_TEST_TRITON=gpu` (or `=cpu` without a GPU), because its image is about 9 GB to download and 25 GB on disk.
The tests read the cameras with an ffprobe container on the project's network (`mediamtx:8554`), not `--network host`, so they do not depend on a Linux host.
The `dev` profile's stack test uses test patterns and samples it makes itself, so it needs no download; the real samples are tested only with `VISIONQUERY_TEST_SAMPLES=1`, which runs `make fetch-samples` first.

The memory's and the api's tests run against a real Postgres with TimescaleDB and pgvector, from the fixtures in `packages/conftest.py`.
By default the test session starts a disposable `timescale/timescaledb:2.30.1-pg18` container of its own (on an ephemeral loopback port, removed at the end), so Docker must be running; to use an existing server instead, set `VISIONQUERY_TEST_DATABASE_URL` to a superuser's `postgresql://` URL.
Without either, those tests are skipped with the reason (and fail in CI, which runs them against a Postgres service through that variable).

**Migrations** (ADR 0011) are numbered SQL files in `packages/memory/src/visionquery/memory/migrations/`, shipped inside the `visionquery-memory` wheel.
A schema change is a new file numbered one after the last (`NNNN_lowercase_words.sql`); an applied file is never edited, renamed, renumbered or removed.
`visionquery-migrate` applies each pending file in its own transaction under an advisory lock and records its SHA-256 in `schema_migrations`.
It exits 0 when the schema is up to date, 1 when a migration failed (it is rolled back; the ones before it stay) or the database could not be reached, 2 when `VISIONQUERY_DATABASE__URL` is missing or invalid, and 3 when it refused because the files and the database's record disagree, naming each file at fault, or because the database is not a VisionQuery memory.
It writes only to an empty database (extensions allowed) or to a memory, one whose `public.schema_migrations` has the record's columns and at least one row; any other table, view, sequence, schema besides `public`, type (a relation's row type and array types do not count) or function that no extension owns, or a `schema_migrations` of another shape, makes it refuse and write nothing.
Every run takes back every privilege on `schema_migrations`, table-level or column-level, but `SELECT` from every role other than its owner (and from PUBLIC), `CASCADE`-ing to what those roles passed on, so only the migrating role can change the record.
The seal cannot reach a superuser, a member of the owning role, or a member of `pg_write_all_data` or `pg_maintain`; they can still write the record.
It also protects only a record the migrating role owns, so the Deployment's setup (#74) must make that role its owner (`ALTER TABLE public.schema_migrations OWNER TO ...`) before relying on it.
`visionquery-migrate status` changes nothing: it logs each applied migration (with `applied_at` and `sha256`), each pending one and a `status: N applied, M pending` line, in one read-only transaction and without waiting for a run in progress, and exits 0, or 3 when a run would refuse (1 and 2 as above).
The role it connects as must be able to create the `timescaledb` and `vector` extensions.

Connecting gives up after the URL's `connect_timeout`, else libpq's `PGCONNECT_TIMEOUT` (empty is unset, `0` waits forever as in libpq, and anything but a whole number is a settings error, exit 2, naming it), else `VISIONQUERY_CONNECT_TIMEOUT` (10 s).
The api's health check is the exception: it gives up after 2 s whatever the URL's `connect_timeout` or `PGCONNECT_TIMEOUT` say (a smaller value is kept; `0` and anything larger are not), and bounds its query with `statement_timeout`, which it appends to the URL's `options`, else to `PGOPTIONS` (whose value it carries over, since an `options` keyword replaces the variable), unless they already set `statement_timeout` themselves; migrations keep the URL's values.
Validating `VISIONQUERY_DATABASE__URL` (`visionquery.memory.settings`) is best effort: it turns the values libpq would quote back into exit 2 before anything connects, and accepts libpq's multi-host forms (`h1,h2`, `h1:5432,h2:5433`, `?port=,5433`) and IPv6 zones, but not a non-ASCII host name (write its `xn--` form).
The guarantee that no libpq text reaches a log line is the connection layer's (`visionquery.memory.connection`), which names a failure only by a fixed category, and the runtime keeps the `psycopg` logger at WARNING or above at any level, because psycopg logs failed attempts at DEBUG with libpq's text.

## Architecture

A closed, single-Owner system (ADR 0001) that records every camera, remembers every person it sees as Tracks with embeddings, links Tracks across cameras into Visits, and answers Questions about the past with Answers whose every statement cites Evidence.

```
cameras ──> MediaMTX ──┬──> recorder ──> Footage segments (disk) + segment index
                       └──> perception workers ──> Triton ──> Tracks, Keyframes, embeddings
                                                                │
                              linker (confident Links) <────────┤  memory (Postgres +
                              Following (on demand)    <────────┤  TimescaleDB + pgvector)
                                                                │
                     tools (typed operations returning Evidence)
                       ├──> api (FastAPI) ──> web (React)
                       └──> reasoner (pluggable LLM) ──> Answers
```

Python packages in one `uv` workspace under `packages/`, with `web/` beside them.
Each is a src-layout distribution named `visionquery-<package>` whose module is `visionquery.<package>` (for example `packages/memory/src/visionquery/memory/`), so no module collides with a PyPI package such as `tools` or `api`.
`visionquery` is a namespace package shared by all of them: no package may add `visionquery/__init__.py`.
A package's tests live in `packages/<package>/tests/`; tests about the workspace as a whole live in `tests/`.

| Package | Owns |
|---|---|
| `memory` | Domain model and the only code that touches Postgres, including the embedding search module (ADR 0006). |
| `tools` | The typed operations shared by the API and the Reasoner (ADR 0009). |
| `perception` | Perception Workers: NVDEC decode, batched Triton calls via CUDA shared memory, per-camera tracking, Keyframes and embeddings (ADR 0007). |
| `linker` | Continuous confident linking and query-time Following (ADR 0004). |
| `recorder` | Copy-only Footage recording in epoch-aligned segments with an NTP-anchored clock. |
| `reasoner` | Pluggable LLM interface (local by default) and the Answer builder. |
| `api` | FastAPI over `tools`. |
| `runtime` | What every process starts with: env settings, JSON logs scrubbed of credentials, and a Prometheus `/metrics` endpoint (ADR 0012). |

Every process starts with `visionquery.runtime.start("<service>", <Settings subclass>)` (ADR 0012): it reads the settings from `VISIONQUERY_*` environment variables (a missing or invalid one, or a metrics address it cannot listen on, is logged by name and the process exits with status 2), sends every log record to stdout as one JSON line with `service` and `level`, scrubbed of URL credentials, and serves Prometheus metrics on `/metrics`; it returns a `Process` holding the settings and the metrics port, with a `close()`.
A service adds its own settings as fields of a `Settings` subclass (`footage_dir: Path` is read from `VISIONQUERY_FOOTAGE_DIR`), declares credentials as `SecretStr`, and declares metrics with `prometheus_client` where they are measured.
A nested model is set with a double underscore (`VISIONQUERY_DATABASE__URL`), a list or dict as JSON in one variable (`VISIONQUERY_PORTS='[8554, 8555]'`), and a settings field takes no alias.
In `compose.yaml` every VisionQuery service merges `<<: *runtime-environment` into its `environment`, which sets the log level and `VISIONQUERY_METRICS_HOST=0.0.0.0` so Prometheus can scrape it (`tests/test_compose.py` checks it).
Each service has its own env file, never one shared by all, so a service sees only the variables it declares.
Secrets are declared as `SecretStr`.
Camera credentials live in the camera registry (`visionquery.memory.cameras`), never in the environment.
A pulled Camera's URL is secret in its user, password, path and query alike (ADR 0014): its `CameraUrl` shows, logs and serialises only the redacted form (`rtsps://***@host:7441/***`, with `***@` only when there is userinfo and `/***` only when there is a path or query) and gives the full URL only through `get_secret_value()`; the registry stores the path and query apart from the public part, clears them with the credentials when a Camera is removed, never logs a URL, and cannot pickle a `CameraUrl`.
pydantic cannot read a `CameraUrl` from input, because its validation errors quote the input: an api reads a camera URL as a `SecretStr` and then makes the `CameraUrl`.
Log through `logging.getLogger(__name__)` with structured fields in `extra=`; never print.
A service that embeds uvicorn must run it with `log_config=None`, because uvicorn otherwise installs its own non-JSON handlers and its access lines bypass the runtime's formatter.

| Variable | Default | Meaning |
|---|---|---|
| `VISIONQUERY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `VISIONQUERY_METRICS_HOST` | `127.0.0.1` | Interface `/metrics` listens on; `compose.yaml` sets `0.0.0.0`. |
| `VISIONQUERY_METRICS_PORT` | `9100` | Port `/metrics` listens on; `0` picks a free one. |

A process that uses the memory also declares `database: DatabaseSettings` (from `visionquery.memory.settings`), read from `VISIONQUERY_DATABASE__URL`, a `postgresql://` URL held as a `SecretStr`.
`visionquery-migrate` reads it, and takes a free metrics port by default because a run lasts seconds.

Import contracts (enforced with import-linter from `.importlinter`, each proven by `tests/test_import_contracts.py`):

- The packages are layered `api | reasoner > tools > linker > memory > runtime`: a package may import any layer below its own and never one above it, and `api` and `reasoner` never import each other.
  Every chain counts, not only a direct import, so `tools -> api`, `linker -> tools` or `memory -> linker` through any number of modules is a violation.
  The layers are exhaustive: a new package is placed in a layer or beside them.
- `perception` and `recorder` sit beside the layers: they import only `memory` and `runtime`, never each other, and nothing imports them.
- Only `memory` imports the database driver.
- `reasoner` reaches the memory only through `tools`.
- `api` reaches the memory and the `linker` only through `tools` (ADR 0009), except for the health probe and settings it names (`ignore_imports` in `.importlinter`).
- `runtime` imports none of the other packages (every package may import it), and no database driver.

## Rules that are easy to break by accident

- Visual Data (frames, Footage, crops, embeddings) never leaves the Deployment, whatever the Reasoner mode (ADR 0002).
  A remote Reasoner gets text and structured facts only.
- Tracks are never revised.
  Identity changes happen only through Links.
- Investigator Assertions (confirmed or rejected Links, Adjacencies, Cases) are never overridden by inference.
  They are undone only by a recorded Withdrawal, and Contradictions are surfaced, never auto-resolved.
- A statement in an Answer without Evidence is a bug.
  Gaps (camera offline, Footage expired, uncovered Area, person lost between cameras) must be stated, never silently treated as "nothing happened".
- Descriptions are a model's interpretation and must be presented as such.
- Alerts come from Watches evaluated against the memory, never from a separate rule pipeline over frames (ADR 0008).
- Timestamps come from the recorder's NTP-anchored timebase, not from when a frame happened to be processed.
- Schema changes are new numbered SQL migration files; never edit an applied one (ADR 0011).
- Pyright runs in strict mode.

## Prior art

Two sibling projects informed the design and are worth reading for patterns, not for copying wholesale:

- `../../Cognecto/vision-stack/`: MediaMTX path registration through its REST API (including a patched MediaMTX that tolerates undeclared audio), adapter-driven Triton models, the latest-frame scheduler, and Postgres job claiming with `FOR UPDATE SKIP LOCKED`.
  Its frame bus, software decode and unbatched inference are what ADR 0007 deliberately avoids.
- `../../Cognecto/phyOS/`: the Photon recorder (`packages/adapters/src/adapters/photon/`), the perception vs resolution split behind Tracks and Links, lineage and processing ledgers, the checksummed migration runner, and import-linter contracts.
  Its signing, hash chains, multi-tenancy and per-customer write lock are not wanted here.
