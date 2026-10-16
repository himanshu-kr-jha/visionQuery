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

- `git fetch --prune`, `git status`, and `git switch develop && git pull` if the working tree is clean.
- `gh pr list --state open` with each PR's checks (`gh pr checks <n>`) and latest review comments.
- Remote branches without a PR: `git branch -r --no-merged origin/develop`.
- The earliest open milestone issue and its open sub-issues.

Give the user a short status: what is merged, what is in flight and at which step, and what comes next.

## 2. Resume before starting anything new

Take the first that applies:

1. **An open PR exists:** continue its review loop (step 3, from the PR stage): failing CI or blocking reviewer findings go to `developer`; otherwise run `pr-reviewer` if there is no verdict on the latest commit; merge if CI is green and the verdict is "ready to merge".
2. **A pushed issue branch has no PR:** continue from the tester stage.
3. **Nothing in flight:** pick the lowest-numbered open sub-issue of the earliest open milestone (or `$ARGUMENTS`), and start it.

## 3. Drive one issue through the team

Follow the lifecycle in `CLAUDE.md` exactly:

1. `senior-developer` for `task` and `spike` issues, `developer` for `bug` issues: build on the branch, test-first.
2. `tester` verifies the branch; `developer` fixes and `tester` re-verifies until "ready for PR".
3. Open the PR from the template with `Closes #<n>`, without agent attribution.
4. `pr-reviewer` reviews; blocking findings go to `developer`, then back through `tester` if code changed, then `pr-reviewer`, until "ready to merge".
5. When CI is green, squash-merge, confirm the issue closed, and update `docs/plan/v1.md` status markers (and the wiki if a milestone or setup changed).

Post each tester and reviewer verdict as a PR comment (or an issue comment before the PR exists).
Give each agent everything it needs in its prompt: the issue number, the branch, and the full report it must act on.

## 4. Continue or stop

After a merge, return to step 2 for the next issue.

Stop and report to the user when any of these happens:

- the current milestone's last sub-issue is merged: show how to run the milestone's demo and wait for the user's go-ahead before the next milestone;
- a finding has bounced three times;
- something would change an ADR, the meaning of a glossary term, or an issue's acceptance criteria;
- a decision needs information only the user has (hardware, footage, credentials, a product choice);
- anything needs a human action outside the repository.

When you stop, say exactly where things stand and what `/work` will do next time.
