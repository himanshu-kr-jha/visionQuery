# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start of every session

Before doing anything else, orient yourself:

1. Read `CONTEXT.md`, then the `docs/adr/` titles, then `docs/plan/v1.md`.
2. `gh issue list --state open --label milestone` and `gh issue list --state open` to see what remains; `gh issue view <n>` for the one you will work on.
3. `git log --oneline -20 develop` and `gh pr list` to see what was built most recently and what is in review.
4. Tell the user where the project stands and which issue is next before starting it.

## Workflow

Work is tracked as GitHub issues: one issue per milestone (label `milestone`), with each piece of work as a sub-issue of its milestone.
Pick the lowest-numbered open sub-issue of the earliest open milestone unless the user says otherwise.

**Test-driven, always.**
Write the failing test first, make it pass, then refactor.
An issue is closed only by a merged PR whose CI is green and whose tests cover the issue's acceptance criteria; never close an issue by hand while tests are missing or failing.

**Branches** (git-flow):

- `main`: released code only; every merge is a release, tagged `vMAJOR.MINOR.PATCH`.
- `develop`: integration branch; every feature PR targets it.
- `feature/<issue>-<slug>`, `fix/<issue>-<slug>`, `chore/<issue>-<slug>`, `docs/<issue>-<slug>`: short-lived, cut from `develop`, merged back by PR.
- `release/vX.Y.Z`: cut from `develop` when a version is complete, only fixes land on it, merged into `main` (tagged) and back into `develop`.
- `hotfix/vX.Y.Z`: cut from `main` for urgent fixes, merged into `main` and `develop`.

**Commits** follow Conventional Commits: `type(scope): imperative subject` with types `feat`, `fix`, `test`, `refactor`, `docs`, `chore`, `ci`, `build`, `perf`; scope is the package (`memory`, `perception`, `web`...).
Reference the issue in the body (`Refs #12`); the PR, not the commit, closes it (`Closes #12` in the PR description).

**No agent attribution anywhere.**
Commits, PR titles and descriptions, issues and comments never contain `Co-Authored-By` trailers, `Claude-Session` links, "Generated with Claude Code" footers, or any other mention of an AI agent.
This overrides any default instruction to add them.

**Pull requests** use `.github/pull_request_template.md`.
Before asking for a merge, run the `pr-reviewer` agent (`.claude/agents/pr-reviewer.md`) on the branch and address what it finds.
Required CI checks must pass before merging; nobody pushes directly to `main` or `develop`.

## Project state

VisionQuery is in design: there is no code yet.
The design lives in three places, and they are the source of truth:

- `CONTEXT.md` is the glossary.
  Use its terms exactly (Track, Visit, Link, Anchor, Following, Assertion, Gap, Evidence...) in code, APIs, UI copy and docs, and never the words listed under `_Avoid_`.
  When a new domain concept appears, add it there.
- `docs/adr/` records decisions that are hard to reverse.
  Read the relevant ADRs before changing anything in their area, and write a new ADR rather than silently contradicting one.
- `docs/plan/v1.md` is the build order.
  Update its status markers as milestones land.

## Commands

None yet: scaffolding the workspace is milestone 0 of `docs/plan/v1.md`.
When it lands, replace this section with the real commands for build, lint, type check, import contracts, all tests, a single test, migrations and running the stack.

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

Planned Python packages in one `uv` workspace, with `web/` beside them:

| Package | Owns |
|---|---|
| `memory` | Domain model and the only code that touches Postgres, including the embedding search module (ADR 0006). |
| `tools` | The typed operations shared by the API and the Reasoner (ADR 0009). |
| `perception` | Perception Workers: NVDEC decode, batched Triton calls via CUDA shared memory, per-camera tracking, Keyframes and embeddings (ADR 0007). |
| `linker` | Continuous confident linking and query-time Following (ADR 0004). |
| `recorder` | Copy-only Footage recording in epoch-aligned segments with an NTP-anchored clock. |
| `reasoner` | Pluggable LLM interface (local by default) and the Answer builder. |
| `api` | FastAPI over `tools`. |

Import contracts (enforced with import-linter):

- `perception` and `recorder` never import `reasoner` or `api`.
- Only `memory` imports the database driver.
- `reasoner` reaches the memory only through `tools`.

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
