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
- `gh pr list --state open`, with each PR's checks (`gh pr checks <n>`) and its comments (`gh api --paginate repos/{owner}/{repo}/issues/<n>/comments`).
  Only PRs whose head branch matches `^(feature|fix|chore|docs)/\d+-` are picked up; release and hotfix PRs are left for the user.
- Remote issue branches (`^(feature|fix|chore|docs)/\d+-`) with no PR, ignoring any whose issue is closed or which has a merged PR (`gh pr list --state merged --head <branch>`).
- Builder worktrees left behind (`git worktree list`): look at each before removing it, and never lose unpushed commits.
- Leftovers of a stopped agent, by the names the tech lead assigned (step 3, item 2): scratch paths `<scratch>/<role>-pr<n>-<short head>` (`git worktree list`; `git worktree remove --force <path>`, then `git worktree prune`) and Compose projects `vq-<role>-pr<n>` (`docker compose ls --all`; `docker compose -p <project> down -v`), only those whose PR has no running agent of this session.
  Never remove anything else by hand: `vqtest-*` projects and the containers labelled `visionquery-test=1` belong to the stack tests, whose own age-based sweeps (`tests/test_stack.py`, `packages/conftest.py`) remove them.
- The earliest open milestone issue and its open sub-issues.

Give the user a short status: what is merged, what is in flight and at which step, and what comes next.

## 2. Resume before starting anything new

For each open PR, oldest first, read the PR's comments, count its fix rounds (`developer` hand-backs answering `tester` or `pr-reviewer` findings), and take the one step whose condition holds.
The conditions are disjoint: items 3 to 10 concern only a PR that is not a draft and whose head carries no "do not merge" verdict or design-change finding, and each of them names every state it needs.
*Known red* and *a newer `developer` hand-back* are as `CLAUDE.md` lifecycle step 4 defines them.

1. The head carries a "do not merge" verdict, or a finding that needs a design change: stop and report to the user.
2. The PR is a draft (its parent is the `Depends on #<n>` line in its description): if the parent merged, send it to `developer` to merge `origin/develop` in and mark it ready (`gh pr ready <n>`); if the parent is still open, leave it and go to the next PR; if the parent was closed without merging, or is itself a draft, stop and report to the user.
3. `commits` is red and its cause is only the PR's title or body: fix the text and re-run that run yourself (`CLAUDE.md`, CI), then take the step for the PR's state again, which no longer has this red (the re-run is pending, so item 7 or 6 applies).
4. `commits` is red and its cause is a pushed commit's message: stop and report to the user, with the options of a force-push the user approves or a replacement PR with the same content and corrected messages (the old one closed).
5. Any other known red on the head, and no newer `developer` hand-back: send it to `developer` as a CI fix under the CI-fix limit in `CLAUDE.md`, never a round; judges still running are not stopped, and any blocking findings already posted stay open for the next head (`CLAUDE.md` lifecycle step 4).
6. No known red, and a verdict the head needs is missing (a `tester` verdict, unless the change is docs-only or config-only, and a `pr-reviewer` verdict, counting verdicts carried over a clean develop merge as `CLAUDE.md` says): run the missing one(s), in parallel, each from the diff since the last head that judge itself judged, or the whole diff if it has judged none, and list every blocking finding still open from a superseded head for each to confirm or close.
   A `tester` verdict on a change that touches Triton or the samples counts only if it lists those stack test runs; otherwise it is missing.
7. No known red, every verdict in, and CI still running on the head: wait (`gh pr checks <n> --watch`).
8. Every verdict in, CI finished with no known red, a blocking finding open, and fewer than two fix rounds done: send every open blocking finding to `developer` as one round.
9. Every verdict in, CI finished with no known red, a blocking finding open that is a security or correctness defect, and two fix rounds done: stop and report to the user.
10. Every verdict in, CI green, and no blocking finding left to send (none open, or only non-blocking ones after two rounds): if fewer than two rounds are done and there are non-blocking findings within the issue's scope, send them to `developer`; otherwise, if the PR is behind `develop`, send it to `developer` to merge `origin/develop` in (a clean merge keeps the verdicts and CI then decides); otherwise file any open findings as the PR's one follow-up issue, then merge (step 3, item 5).

For an issue branch with no PR: if its builder is still running, wait; if its builder stopped with a report, post it on the issue and bring it to the user; otherwise send it back to its builder (`senior-developer` for `task` and `spike`, `developer` for `bug`) to finish and open the PR.

Then, if no `senior-developer` is building, start the next issue (or `$ARGUMENTS`): the lowest-numbered open sub-issue of the earliest open milestone, preferring one that does not depend on unmerged work.
Without `$ARGUMENTS`, if the earliest open milestone has no open sub-issues, stop for its demo (step 4) and start nothing from a later milestone until it is closed.

## 3. Drive issues through the team, pipelined

Follow the lifecycle and loop rules in `CLAUDE.md`.
Never run more than three agents at once, `developer` included; work waiting for a slot starts in the order `CLAUDE.md` gives (fixes and verdicts for open PRs, oldest PR first, then the next build).
An agent holds its slot until its run ends: when one has reported but is still running, stop it with `TaskStop` (this is the only way the tech lead stops an agent), and count it until it has stopped.
Whoever stops an agent removes what it left behind, and only what carries that agent's assigned name (item 2 below): its scratch path (`git worktree remove --force <path>`, then `git worktree prune`) and its Compose project (found with `docker compose ls --all`, then `docker compose -p <project> down -v`).
Never take down a `vqtest-*` project or a labelled test container by hand.

1. Start the builder with `isolation: "worktree"`, giving it the issue number and, if the issue depends on an open PR, that PR's branch to cut from.
2. When the builder hands back (its PR is open), remove its worktree with the checks in `CLAUDE.md` (lifecycle step 2), start `tester` and `pr-reviewer` on the PR at the same time, telling each the round (0) and its names (item 2 below), unless it is a draft (a draft is judged only once step 2 marks it ready), and start the builder on the next issue when a slot is free (`CLAUDE.md` gives the order).
   Docs-only and config-only PRs get `pr-reviewer` alone.
   Every `tester` and `pr-reviewer` prompt carries the names the agent must use, chosen before it starts: its scratch path `<scratch>/<role>-pr<n>-<short head>` (`<scratch>` is the session's scratchpad directory) and, for `tester`, its end-to-end Compose project `vq-tester-pr<n>`.
   The agent uses exactly those, and they are what cleanup later removes.
3. When every verdict the head needs is in and CI has finished on that head, or sooner for a known red only (`CLAUDE.md` lifecycle step 4), decide by step 2: the item that holds sends to `developer`, with the PR number, the branch and the full text of every finding; start `developer` with `isolation: "worktree"` whenever another agent uses the main checkout.
   After its hand-back, start `tester` (if code changed) and `pr-reviewer` again in parallel, telling them the round, their names (item 2 below), the blocking findings still open from superseded heads (each confirms or closes them), and that each starts from the diff since the last head it judged itself, or reviews the whole diff if it has judged none.
4. Make sure every `developer` hand-back and every verdict is on the PR as a comment (the agents post their own; post one yourself if it is missing), without agent attribution; the builder's hand-back is the PR description.
5. Before merging, file every finding still open, if any, as the PR's one follow-up issue (`CLAUDE.md` says how).
   Merge with `gh pr merge <n> --squash --delete-branch --body "<short summary>"`, the body ending with one `Closes #<n>` line per issue the PR closes, once step 2 says to, and confirm each of those issues closed.
6. When a milestone completes or setup changes, update the GitHub wiki (a separate repo).

## 4. Continue or stop

After a merge, return to step 2 and keep the pipeline full.

Stop and report to the user when any of these happens:

- without `$ARGUMENTS`, the current milestone's last sub-issue is merged, or the earliest open milestone issue has no open sub-issues: show how to run the milestone's demo and wait for the user's go-ahead before the next milestone; once the user approves the demo, close the milestone issue with a comment summarising what shipped;
- a security or correctness defect remains after two fix rounds;
- the reviewer's verdict is "do not merge", or a finding needs a design change;
- something would change an ADR, the meaning of a glossary term, or an issue's acceptance criteria;
- a decision needs information only the user has (hardware, footage, credentials, a product choice);
- anything needs a human action outside the repository.

When you stop, say exactly where things stand and what `/work` will do next time.
