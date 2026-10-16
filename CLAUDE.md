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
   The tech lead posts each builder hand-back as a comment on the PR if one exists, otherwise on the issue.
   The builder also updates the milestone's status marker in `docs/plan/v1.md` as part of the same PR: `[~]` in the first PR of a milestone, `[x]` in the PR that closes its last sub-issue.
3. **`tester`** verifies the branch.
   If not ready, the report goes to `developer`, then back to `tester`, until it is ready.
4. **Tech lead** opens the PR from the template, with `Closes #<n>` and a Conventional Commit title.
5. **`pr-reviewer`** reviews the PR.
   Blocking findings go to `developer`, then back through `tester` (if code changed) and `pr-reviewer`, until the verdict is "ready to merge".
6. **Tech lead** squash-merges (`gh pr merge --squash --delete-branch`) once CI is green and the verdict is "ready to merge", and confirms the issue closed.
   If GitHub did not auto-close the issue, close it with a comment linking the merged PR.
   The tech lead updates the GitHub wiki (a separate repo) directly when a milestone completes or setup changes.
   After the user approves a milestone's demo, the tech lead closes the milestone issue with a comment summarising what shipped.

Rules for the loop:

- After the same finding bounces between `developer` and `tester` or `pr-reviewer` three times, or after five round trips in total for one issue, the tech lead stops and brings it to the user.
- A "do not merge" verdict, or a finding that needs a design change, goes to the user.
- Escalate to the user, rather than deciding alone, anything that would change an ADR, the glossary's meaning of an existing term, or an issue's acceptance criteria.
- Post each builder hand-back, tester verdict and reviewer verdict as a comment on the PR if one exists, otherwise on the issue, so the history is visible on GitHub, without agent attribution.
- One issue at a time by default.
  Independent issues may run in parallel only with `isolation: "worktree"` for each builder.

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
