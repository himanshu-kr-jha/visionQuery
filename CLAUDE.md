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
Required CI checks must pass before merging; nobody pushes directly to `main` or `develop`.
Until CI exists (issue #8), "CI is green" is replaced by "the tester ran the full local suite and it passed".
When no checks exist for the change, the tester's verdict on the final commit stands in for CI.
For a docs-only or config-only change, which has no tester run, the `pr-reviewer` verdict on the final commit stands in for CI.

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
3. **`tester`** verifies the branch, and is the one agent that runs the full stack tests (`make test-stack`) on each PR head.
   If not ready, the report goes to `developer`, then back to `tester`.
   Docs-only and config-only changes skip the tester: `pr-reviewer` alone checks them.
4. **Tech lead** opens the PR from the template, with `Closes #<n>` and a Conventional Commit title.
5. **`pr-reviewer`** reviews the PR, running `make check` and reading the change; it runs stack tests only when it suspects a defect there.
   Blocking findings go to `developer`, then back through `tester` (if code changed) and `pr-reviewer`.
6. **Tech lead** squash-merges (`gh pr merge --squash --delete-branch`) once CI is green and either the verdict is "ready to merge" or, after two fix rounds, no security or correctness finding remains; it confirms the issue closed.
   If GitHub did not auto-close the issue, close it with a comment linking the merged PR.
   The tech lead updates the GitHub wiki (a separate repo) directly when a milestone completes or setup changes.
   After the user approves a milestone's demo, the tech lead closes the milestone issue with a comment summarising what shipped.

Rules for the loop:

- At most two fix rounds per issue.
  A round is one `developer` hand-back answering findings from `tester` or `pr-reviewer`, counted from the hand-back comments on the PR and its issue; a `developer` hand-back for a build (a bug issue), a rebase or a CI failure is not a round.
  After the second round only security or correctness defects block; before merging, the tech lead files every other open finding, blocking or not, as one follow-up issue under the same milestone (once), and the PR merges.
  The tech lead tells `tester` and `pr-reviewer` which round they are judging.
  If a security or correctness defect remains after two rounds, the tech lead stops and brings it to the user.
- A "do not merge" verdict, or a finding that needs a design change, goes to the user.
- Non-blocking findings in a "ready to merge" verdict are fixed in the same PR, as a fix round, while fewer than two rounds are done and the fix stays within the issue's scope.
  Any finding still unfixed at the merge is filed before it as one follow-up issue.
- A follow-up issue filed before a merge goes under the same milestone, and its body starts with `Follow-up to #<pr>`.
  Before filing one, the tech lead looks for it with `gh issue list --state all --search "Follow-up to #<pr>"` and files none if it exists, so a PR gets at most one.
- Escalate to the user, rather than deciding alone, anything that would change an ADR, the glossary's meaning of an existing term, or an issue's acceptance criteria.
- Post each builder hand-back, tester verdict and reviewer verdict as a comment on the PR if one exists, otherwise on the issue, so the history is visible on GitHub, without agent attribution.
- Work is pipelined: once `senior-developer` hands back an issue it never returns to it, and starts the next issue straight away while `tester`, `developer` and `pr-reviewer` finish the previous one.
  Every builder runs with `isolation: "worktree"`, and so does `developer` whenever another agent is using the main checkout, so no two agents share a working tree.
  The next issue is the lowest-numbered open sub-issue of the earliest open milestone, preferring one that does not depend on unmerged work.
  If every candidate depends on unmerged work, the builder cuts its branch from the unmerged branch; once that merges, `developer` moves it onto develop with `git rebase --onto origin/develop <parent head>`, where `<parent head>` is the parent PR's final head (`gh pr view <parent> --json headRefOid`), and its PR is opened only after that.
  The move happens only while the branch still carries the parent's commits: while `<parent head>` is an ancestor of the branch, or, for a branch cut before the parent's last commits, while `git merge-base origin/<branch> <parent head>` is not an ancestor of `origin/develop`.
  Once moved, the branch is treated like any other.
- Stack tests are isolated per Compose project, so agents may run them at the same time.

## Project state

VisionQuery is being scaffolded (milestone M0): the uv workspace and its seven package skeletons exist, with no behaviour in them yet.
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
| Install everything from the lockfile | `uv sync --locked` (`make sync`) |
| Lint | `uv run ruff check` |
| Format check | `uv run ruff format --check` |
| Format and autofix | `make format` |
| Type check (pyright strict) | `uv run pyright` (`make typecheck`) |
| Import contracts | `uv run lint-imports` (`make contracts`) |
| All tests | `uv run pytest` (`make test`) |
| One package's tests | `uv run pytest packages/memory` |
| A single test | `uv run pytest tests/test_import_contracts.py::test_the_workspace_keeps_every_contract` |
| Every check CI runs | `make check` |
| Apply the database migrations | `VISIONQUERY_DATABASE__URL=postgresql://... uv run visionquery-migrate` |
| Start the stack (Postgres, migrations, MediaMTX); rebuilds what changed first, and fails if the migrations fail | `make up` |
| ...with Prometheus and Grafana | `make up COMPOSE_PROFILES=observability` |
| ...with Triton on the GPUs | `make up COMPOSE_PROFILES=gpu` |
| ...with Triton on a machine without a GPU | `make up COMPOSE_FILE=compose.yaml:compose.no-gpu.yaml COMPOSE_PROFILES=gpu` |
| Stop the stack, every profile included; keeps the data (`make down VOLUMES=1` deletes it) | `make down` |
| Rebuild after changing code: `make up` does it; a bare `docker compose up` builds only an absent image | `make up` |
| Stack tests (bring the stack up for real) | `make test-stack` (`uv run pytest -m stack`) |

`make lint` runs both ruff commands.
After changing a dependency, run `uv lock` and commit `uv.lock` with the change.

**The stack** is `compose.yaml` (Compose project `visionquery`), configured only through `.env`: copy `.env.example`, which documents every variable, and set the three passwords it leaves empty, or Compose refuses to start.
Every published port listens on `VISIONQUERY_BIND_ADDRESS`, or MediaMTX's camera-facing ones (RTSP, WebRTC) on `VISIONQUERY_MEDIAMTX_BIND_ADDRESS` (both loopback by default) and has a variable of its own; Postgres is on host port 15432.
Every VisionQuery process runs from the one image the root `Dockerfile` builds (`<project>-app:dev`: image tags are global to the Docker daemon, so every built image is named for its Compose project, or two projects would fight over one tag), choosing its process by its Compose `command`; `migrate` is a one-shot service that runs `visionquery-migrate` once Postgres is healthy, with the password in `PGPASSWORD` rather than in the URL.
Every future long-running service built from that image must declare `depends_on: migrate: condition: service_completed_successfully` (`tests/test_compose.py` enforces it), and the stack is started with `make up` because `up --wait` returns while `migrate` still runs (it needs Docker Engine 25+, Compose 2.30+ for `compose wait`, and the Dockerfile frontend pinned by digest in the Dockerfile's first line for `COPY --parents`; images are pulled by nobody, `pull_policy: never`).
Profiles: `gpu` adds Triton with the model repository in `docker/triton/models/`, `observability` adds Prometheus (`docker/prometheus/prometheus.yml`, which must scrape every long-running service) and Grafana, and `dev` is reserved for the fake camera publisher.
Every image is pinned to an exact tag and its digest; MediaMTX's configuration is `docker/mediamtx/mediamtx.yml`.
`tests/test_compose.py` checks all of this without starting anything (it needs the `docker` CLI, not the daemon) and runs with `make check`.
The stack tests in `tests/test_stack.py` bring each profile up under a throwaway project with ephemeral ports and take it down with its volumes; they are marked `stack`, left out of `make check`, and include Triton only with `VISIONQUERY_TEST_TRITON=gpu` (or `=cpu` without a GPU), because its image is about 9 GB to download and 25 GB on disk.

The memory's tests run against a real Postgres with TimescaleDB and pgvector.
By default the test session starts a disposable `timescale/timescaledb:2.30.1-pg18` container of its own (on an ephemeral loopback port, removed at the end), so Docker must be running; to use an existing server instead, set `VISIONQUERY_TEST_DATABASE_URL` to a superuser's `postgresql://` URL.
Without either, those tests are skipped with the reason.

**Migrations** (ADR 0011) are numbered SQL files in `packages/memory/src/visionquery/memory/migrations/`, shipped inside the `visionquery-memory` wheel.
A schema change is a new file numbered one after the last (`NNNN_lowercase_words.sql`); an applied file is never edited, renamed, renumbered or removed.
`visionquery-migrate` applies each pending file in its own transaction under an advisory lock and records its SHA-256 in `schema_migrations`.
It exits 0 when the schema is up to date, 1 when a migration failed (it is rolled back; the ones before it stay) or the database could not be reached, 2 when `VISIONQUERY_DATABASE__URL` is missing or invalid, and 3 when it refused because the files and the database's record disagree, naming each file at fault.
The role it connects as must be able to create the `timescaledb` and `vector` extensions.

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

Every process starts with `visionquery.runtime.start("<service>", <Settings subclass>)` (ADR 0012): it reads the settings from `VISIONQUERY_*` environment variables (a missing or invalid one is logged by name and the process exits with status 2), sends every log record to stdout as one JSON line with `service` and `level`, scrubbed of URL credentials, and serves Prometheus metrics on `/metrics`; it returns a `Process` holding the settings and the metrics port, with a `close()`.
A service adds its own settings as fields of a `Settings` subclass (`footage_dir: Path` is read from `VISIONQUERY_FOOTAGE_DIR`), declares credentials as `SecretStr`, and declares metrics with `prometheus_client` where they are measured.
A nested model is set with a double underscore (`VISIONQUERY_DATABASE__URL`), and a settings field takes no alias.
Each service has its own env file, never one shared by all, so a service sees only the variables it declares.
Secrets are declared as `SecretStr`.
Camera credentials live in the camera registry, never in the environment.
Log through `logging.getLogger(__name__)` with structured fields in `extra=`; never print.
A service that embeds uvicorn must run it with `log_config=None`, because uvicorn otherwise installs its own non-JSON handlers and its access lines bypass the runtime's formatter.

| Variable | Default | Meaning |
|---|---|---|
| `VISIONQUERY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `VISIONQUERY_METRICS_HOST` | `0.0.0.0` | Interface `/metrics` listens on. |
| `VISIONQUERY_METRICS_PORT` | `9100` | Port `/metrics` listens on; `0` picks a free one. |

A process that uses the memory also declares `database: DatabaseSettings` (from `visionquery.memory.settings`), read from `VISIONQUERY_DATABASE__URL`, a `postgresql://` URL held as a `SecretStr`.
`visionquery-migrate` reads it, and takes a free metrics port by default because a run lasts seconds.

Import contracts (enforced with import-linter from `.importlinter`, each proven by `tests/test_import_contracts.py`):

- `perception` and `recorder` never import `reasoner` or `api`.
- Only `memory` imports the database driver.
- `reasoner` reaches the memory only through `tools`.
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
