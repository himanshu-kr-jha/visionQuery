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
  Agents work in worktrees, so the main checkout may legitimately be on a feature branch; never switch it while an agent uses it.
- `gh pr list --state open` with each PR's checks (`gh pr checks <n>`) and latest review comments.
  Only PRs whose head branch matches `^(feature|fix|chore|docs)/\d+-` are picked up; release and hotfix PRs are left for the user.
- Remote branches without a PR, matching `^(feature|fix|chore|docs)/\d+-`.
  Merges are squash merges (`gh pr merge --squash --delete-branch`), so `git branch -r --no-merged` is unreliable: ignore any branch whose issue is closed or which has a merged PR (`gh pr list --state merged --head <branch>`).
- The earliest open milestone issue and its open sub-issues.

Give the user a short status: what is merged, what is in flight and at which step, and what comes next.

## 2. Resume before starting anything new

Several issues may be in flight at once.
Go through every in-flight item, open PRs oldest first and then pushed issue branches without a PR, and apply the first matching rule to each:

1. **An open PR:** read the comments on both the PR and its issue, count its fix rounds (the `developer` hand-backs answering `tester` or `pr-reviewer` findings; build, rebase and CI-fix hand-backs do not count), and decide by the latest activity on the PR head, first match wins:
   - the latest commit has a review verdict "do not merge", or a finding that needs a design change, and no newer hand-back: stop and report to the user, in any round;
   - a real CI run failed on the PR head and no `developer` hand-back is newer than the failure: send the failing run to `developer`; if two CI-fix hand-backs already answered that same failing check, stop and report to the user instead;
   - the latest commit has a review verdict that is "ready to merge" or, once two fix rounds are done, that has no security or correctness finding, and the head has no `tester` verdict "not ready" (once two rounds are done, a "not ready" with no security or correctness finding does not block): if CI is still running, wait for it (`gh pr checks <n> --watch`); if the head has no CI run and, after two rounds, the tester's full local suite did not pass, stop and report to the user; once every CI check on the head is green (or, for a head with no CI run, the stand-in in section 3, item 5, is met: after two rounds it is "the tester's full local suite passed", not the tester's overall verdict), then, if fewer than two fix rounds are done and the verdict lists non-blocking findings within the issue's scope, send them to `developer`; otherwise file every open finding (blocking or not, from `pr-reviewer` and `tester`), if any, as the PR's one follow-up issue (step 3), then merge;
   - the latest review verdict has blocking findings and no newer hand-back: if fewer than two fix rounds have happened, send them to `developer`; otherwise stop and report to the user;
   - the latest commit has a `tester` verdict "not ready" and no newer hand-back: if fewer than two fix rounds have happened, send its findings to `developer`; otherwise stop and report to the user if any is a security or correctness defect, and, if none is and the head has no review verdict yet, run `pr-reviewer`;
   - a builder or `developer` hand-back newer than the last verdict: run `tester` if code changed, then `pr-reviewer` only after `tester` says "ready for PR"; for a docs-only or config-only change run `pr-reviewer` alone;
   - a docs-only or config-only PR with no review verdict on its head: run `pr-reviewer`;
   - a `tester` verdict "ready for PR" for the latest commit but no review verdict: run `pr-reviewer`;
   - none of the above, with CI still running on the head: wait for it (`gh pr checks <n> --watch`).
2. **A pushed issue branch whose hand-back names an unmerged parent branch, while it still carries the parent's commits:** first `git fetch origin pull/<parent PR>/head` so the parent's head exists even when its branch is pruned.
   It carries them while the parent PR's final head (`<parent head>`, from `gh pr view <parent> --json headRefOid`) is an ancestor of the branch, or, for a branch cut before the parent's last commits, while `git merge-base origin/<branch> <parent head>` is not an ancestor of `origin/develop`.
   While the parent's PR is open, leave it (the tester diffs it against the parent branch if it is tested early); once the parent's PR is merged, send it to `developer` to run `git rebase --onto origin/develop <parent head>` and push.
   If its builder is still running, wait for the hand-back.
   A branch that no longer carries the parent's commits has been moved already and falls to the rule below.
   A branch whose parent itself depends on unmerged work breaks the one-level limit: stop and report to the user.

3. **A pushed issue branch without a PR:** if a builder hand-back, or a `developer` rebase hand-back, for its latest commit exists as a comment on the issue, continue from the tester stage (or open the PR straight away for a docs-only or config-only change); otherwise send it back to its builder to finish, chosen by the issue's label (`senior-developer` for `task` and `spike`, `developer` for `bug`).

Then, if no `senior-developer` is building, start the next issue (or `$ARGUMENTS`): the lowest-numbered open sub-issue of the earliest open milestone, preferring one that does not depend on unmerged work.
Without `$ARGUMENTS`, if the earliest open milestone issue has no open sub-issues, there is no next issue: stop for its demo (step 4) instead, and start nothing from a later milestone until it is closed.

## 3. Drive issues through the team, pipelined

Follow the lifecycle and loop rules in `CLAUDE.md` exactly.
Keep the pipeline full: as soon as `senior-developer` hands an issue back, start it on the next issue in a new worktree, then send the handed-back branch on to `tester`.
Never run more than three agents at once; light docs or CI work (a docs-only or config-only review, a CI-fix on workflow files) may run beside them.

1. `senior-developer` for `task` and `spike` issues, `developer` for `bug` issues: build on the branch, test-first, with `isolation: "worktree"`.
2. `tester` verifies the branch; `developer` fixes and `tester` re-verifies.
   The tester runs the full stack tests once per PR, on the head it passes as ready for PR; a verification that finds the branch not ready, and each intermediate fix round, gets `make check` plus only the stack tests the change touches.
   Once CI runs on the PR, its `stack` job is the stack-test run, and a local stack run is optional: tell the tester so.
   Docs-only and config-only changes skip this step.
3. Open the PR from the template with `Closes #<n>`, without agent attribution.
4. `pr-reviewer` reviews; blocking findings go to `developer`, then back through `tester` if code changed, then `pr-reviewer`.
   At most two fix rounds; after that only security or correctness defects block, and every other finding becomes a follow-up issue.
   Non-blocking findings in a "ready to merge" verdict go to `developer` as a fix round while fewer than two rounds are done and the fix stays within the issue's scope; any still unfixed at the merge go into the follow-up issue.
5. When every CI check on the head is green (or the stand-in below applies), squash-merge with `gh pr merge --squash --delete-branch` and confirm the issue closed.
   If GitHub did not auto-close it, close it with a comment linking the merged PR.
   A PR whose head has a CI run merges only when every CI check on that head is green; the rulesets make GitHub enforce it.
   The tester's full local suite on the final commit stands in only for a head with no CI run; for a docs-only or config-only change, the `pr-reviewer` verdict on the final commit does.
   Merge when the verdict is "ready to merge", or when two fix rounds are done and no security or correctness finding remains; never over a "do not merge" verdict, a design-change finding, or a `tester` "not ready" on the head (after two rounds, only one with a security or correctness finding blocks).
   The PR title is a Conventional Commit `type(scope): subject`; the squash body is a short summary plus `Closes #<n>`, without the template checklist.
6. The `docs/plan/v1.md` status marker is part of the issue's own PR: the builder marks the milestone `[~]` in its first PR and `[x]` in the PR that closes its last sub-issue.
   The tech lead updates the GitHub wiki (a separate repo) directly when a milestone completes or setup changes.

Post each builder hand-back, tester verdict and reviewer verdict as a comment on the PR if one exists, otherwise on the issue.
After posting a builder hand-back, the tech lead (you) unlocks and removes the builder's worktree (`git worktree unlock <path>; git worktree remove --force <path>`; agent worktrees are locked), after confirming the worktree has no uncommitted changes and `git rev-parse <branch>` equals `origin/<branch>`, so the branch is free for `developer`; do the same for stale builder worktrees found when resuming.
Tell `tester` and `pr-reviewer` which fix round they are judging.
A PR gets at most one follow-up issue: before filing it, look for it with `gh issue list --state all --search '"Follow-up to #<pr>" in:body'` and file none if it exists; a new one goes under the same milestone and its body starts with `Follow-up to #<pr>`.
Give each agent everything it needs in its prompt: the issue number, the branch, and the full report it must act on.

## 4. Continue or stop

After a merge, return to step 2 for whatever is in flight and keep the pipeline full.

Stop and report to the user when any of these happens:

- the current milestone's last sub-issue is merged, or the earliest open milestone issue has no open sub-issues: show how to run the milestone's demo and wait for the user's go-ahead before the next milestone; once the user approves the demo, close the milestone issue with a comment summarising what shipped;
- a security or correctness defect remains after two fix rounds;
- the reviewer's verdict is "do not merge", or a finding needs a design change;
- something would change an ADR, the meaning of a glossary term, or an issue's acceptance criteria;
- a decision needs information only the user has (hardware, footage, credentials, a product choice);
- anything needs a human action outside the repository.

When you stop, say exactly where things stand and what `/work` will do next time.
