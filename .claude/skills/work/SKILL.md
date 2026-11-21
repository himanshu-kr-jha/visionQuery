---
name: work
description: Start or resume the VisionQuery development workflow - orient, find work in flight or pick the next issue, and drive it through the agent team to a merged PR, continuing issue by issue to the end of the current milestone.
argument-hint: "[issue number]"
disable-model-invocation: true
---

You are the tech lead described in `CLAUDE.md` ("The team").
Drive the workflow below until a stop condition is reached.
If an issue number was given (`$ARGUMENTS`), work on that issue instead of choosing one.

## 1. Orient

Run the "Start of every session" routine in `CLAUDE.md`, then establish what is in flight:

- `git fetch --prune`, `git status` and `git worktree list`.
  If the main checkout is not clean and no agent of this session is working in it, stop and report to the user.
- `gh pr list --state open`, with each PR's checks (`gh pr checks <n>`) and its comments.
  Only PRs whose head branch matches `^(feature|fix|chore|docs)/\d+-` are picked up; release and hotfix PRs are left for the user.
- Remote issue branches (`^(feature|fix|chore|docs)/\d+-`) with no PR, ignoring any whose issue is closed or which has a merged PR (`gh pr list --state merged --head <branch>`).
- Builder worktrees left behind (`git worktree list`): look at each before removing it, and never lose unpushed commits.
- The earliest open milestone issue and its open sub-issues.

Give the user a short status: what is merged, what is in flight and at which step, and what comes next.

## 2. Resume before starting anything new

For each open PR, oldest first, read the PR's comments, count its fix rounds (`developer` hand-backs answering `tester` or `pr-reviewer` findings), and take the first matching step:

1. A "do not merge" verdict, or a finding that needs a design change, on the head: stop and report to the user.
2. A draft PR whose parent PR has merged: send it to `developer` to merge `origin/develop` in, then mark it ready (`gh pr ready <n>`).
   A draft whose parent is still open is left alone.
3. A head missing a verdict it needs (a `tester` verdict, unless the change is docs-only or config-only, and a `pr-reviewer` verdict): run the missing one(s), in parallel.
4. CI red on the head, or a verdict with blocking findings, and no newer `developer` hand-back: send everything open to `developer` as one list, within the round limits in `CLAUDE.md`; past them, stop and report to the user if a security or correctness defect remains, and otherwise go on as in item 6.
5. No blocking finding and CI still running: wait (`gh pr checks <n> --watch`).
6. No blocking finding and CI green: if fewer than two rounds are done and there are non-blocking findings within the issue's scope, send them to `developer`; otherwise file any open findings as the PR's one follow-up issue, then merge (step 3, item 5).

For an issue branch with no PR: if its builder is still running, wait; otherwise send it back to its builder (`senior-developer` for `task` and `spike`, `developer` for `bug`) to finish and open the PR.

Then, if no `senior-developer` is building, start the next issue (or `$ARGUMENTS`): the lowest-numbered open sub-issue of the earliest open milestone, preferring one that does not depend on unmerged work.
If the earliest open milestone has no open sub-issues, stop for its demo (step 4) and start nothing from a later milestone until it is closed.

## 3. Drive issues through the team, pipelined

Follow the lifecycle and loop rules in `CLAUDE.md`.
Never run more than three agents at once.

1. Start the builder with `isolation: "worktree"`, giving it the issue number and, if the issue depends on an open PR, that PR's branch to cut from.
2. When the builder hands back (its PR is open), remove its worktree, start it on the next issue, and start `tester` and `pr-reviewer` on the PR at the same time, telling each the round (0).
   Docs-only and config-only PRs get `pr-reviewer` alone.
3. When both verdicts are in, decide by step 2: blocking findings and any CI failure go to `developer` together, with the PR number, the branch and the full text of every finding.
   After its hand-back, start `tester` (if code changed) and `pr-reviewer` again in parallel, telling them the round and that they start from the fix diff.
4. Make sure every hand-back and verdict is on the PR as a comment (the agents post their own; post one yourself if it is missing), without agent attribution.
5. Before merging, file every finding still open, if any, as the PR's one follow-up issue (`CLAUDE.md` says how).
   Merge with `gh pr merge <n> --squash --delete-branch --body "<short summary>"$'\n\n'"Closes #<issue>"` once CI is green and no blocking finding remains, and confirm the issue closed.
6. When a milestone completes or setup changes, update the GitHub wiki (a separate repo).

## 4. Continue or stop

After a merge, return to step 2 and keep the pipeline full.

Stop and report to the user when any of these happens:

- the current milestone's last sub-issue is merged: show how to run the milestone's demo and wait for the user's go-ahead before the next milestone; once the user approves the demo, close the milestone issue with a comment summarising what shipped;
- a security or correctness defect remains after two fix rounds;
- the reviewer's verdict is "do not merge", or a finding needs a design change;
- something would change an ADR, the meaning of a glossary term, or an issue's acceptance criteria;
- a decision needs information only the user has (hardware, footage, credentials, a product choice);
- anything needs a human action outside the repository.

When you stop, say exactly where things stand and what `/work` will do next time.
