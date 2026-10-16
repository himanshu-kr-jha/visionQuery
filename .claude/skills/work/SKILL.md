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

- `git fetch --prune` and `git status`.
  If the working tree is not clean, stop and report to the user.
  Otherwise `git switch develop && git pull`.
- `gh pr list --state open` with each PR's checks (`gh pr checks <n>`) and latest review comments.
  Only PRs whose head branch matches `^(feature|fix|chore|docs)/\d+-` are picked up; release and hotfix PRs are left for the user.
- Remote branches without a PR, matching `^(feature|fix|chore|docs)/\d+-`.
  Merges are squash merges (`gh pr merge --squash --delete-branch`), so `git branch -r --no-merged` is unreliable: ignore any branch whose issue is closed or which has a merged PR (`gh pr list --state merged --head <branch>`).
- The earliest open milestone issue and its open sub-issues.

Give the user a short status: what is merged, what is in flight and at which step, and what comes next.

## 2. Resume before starting anything new

Take the first that applies:

1. **An open PR exists:** read the comments on both the PR and its issue, and decide by the latest activity on the PR head:
   - a builder or `developer` hand-back newer than the last verdict, with code changed: run `tester`, then `pr-reviewer`;
   - a `tester` verdict "ready for PR" for the latest commit but no review verdict: run `pr-reviewer`;
   - the latest review verdict has blocking findings and no newer hand-back: send them to `developer`;
   - the review verdict on the latest commit is "ready to merge" and CI is green (or the pre-CI rule below is met): merge.
2. **A pushed issue branch has no PR:** if a builder hand-back for its latest commit exists as a comment on the issue (or the PR), continue from the tester stage; otherwise send it back to its builder to finish, chosen by the issue's label (`senior-developer` for `task` and `spike`, `developer` for `bug`).
3. **Nothing in flight:** pick the lowest-numbered open sub-issue of the earliest open milestone (or `$ARGUMENTS`), and start it.

## 3. Drive one issue through the team

Follow the lifecycle in `CLAUDE.md` exactly:

1. `senior-developer` for `task` and `spike` issues, `developer` for `bug` issues: build on the branch, test-first.
2. `tester` verifies the branch; `developer` fixes and `tester` re-verifies until "ready for PR".
3. Open the PR from the template with `Closes #<n>`, without agent attribution.
4. `pr-reviewer` reviews; blocking findings go to `developer`, then back through `tester` if code changed, then `pr-reviewer`, until "ready to merge".
5. When CI is green, squash-merge with `gh pr merge --squash --delete-branch` and confirm the issue closed.
   If GitHub did not auto-close it, close it with a comment linking the merged PR.
   Until CI exists (issue #8), "CI is green" means the tester ran the full local suite and it passed.
   When no checks exist for the change, the tester's verdict on the final commit stands in for CI.
   The PR title is a Conventional Commit `type(scope): subject`; the squash body is a short summary plus `Closes #<n>`, without the template checklist.
6. The `docs/plan/v1.md` status marker is part of the issue's own PR: the builder marks the milestone `[~]` in its first PR and `[x]` in the PR that closes its last sub-issue.
   The tech lead updates the GitHub wiki (a separate repo) directly when a milestone completes or setup changes.

Post each builder hand-back, tester verdict and reviewer verdict as a comment on the PR if one exists, otherwise on the issue.
Give each agent everything it needs in its prompt: the issue number, the branch, and the full report it must act on.

## 4. Continue or stop

After a merge, return to step 2 for the next issue.

Stop and report to the user when any of these happens:

- the current milestone's last sub-issue is merged: show how to run the milestone's demo and wait for the user's go-ahead before the next milestone; once the user approves the demo, close the milestone issue with a comment summarising what shipped;
- a finding has bounced three times, or the issue has had five round trips in total;
- the reviewer's verdict is "do not merge", or a finding needs a design change;
- something would change an ADR, the meaning of a glossary term, or an issue's acceptance criteria;
- a decision needs information only the user has (hardware, footage, credentials, a product choice);
- anything needs a human action outside the repository.

When you stop, say exactly where things stand and what `/work` will do next time.
