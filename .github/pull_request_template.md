<!-- One `Closes #<n>` line per issue this PR closes. -->
Closes #

## What changes for the product

<!-- In CONTEXT.md terms: what an Operator can now do or will see differently. -->

## How it was built test-first

<!-- The tests written before the code, and which acceptance criterion each covers. -->

## Design concerns and risks

<!-- Which apply (state not made here, secrets, privilege, the dependency's contract, partial failure, signals, concurrency and restarts), the tests that cover each, the repeat runs, and what the tester and the reviewer should look at hard. -->

## Checklist

- [ ] Tests for every acceptance criterion of the issue, written first
- [ ] `make check` and the stack tests the change touches pass locally
- [ ] Names follow `CONTEXT.md`; new domain terms added there
- [ ] No ADR contradicted, or a new ADR supersedes it
- [ ] README setup and `CLAUDE.md` Commands updated if setup, services, env vars or commands changed
- [ ] `tester` verdict "ready" on the final head, or carried over to it by a clean develop merge (not needed for a docs-only or config-only change)
- [ ] `pr-reviewer` verdict "ready to merge" on the final head, or carried over to it by a clean develop merge, with all blocking findings addressed
