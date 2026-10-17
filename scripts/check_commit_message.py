"""Fail when a commit message, or a PR title, is not a Conventional Commit as CLAUDE.md asks.

    check_commit_message.py COMMIT_MSG_FILE      as a commit-msg hook (`#` lines are ignored)
    check_commit_message.py --env PR_TITLE       a PR title, which becomes the squash subject

The subject is `type(scope): subject` or `type: subject`, with `!` before the colon for a
breaking change. The scope is a package under packages/, `web`, or one of REPO_SCOPES. Subjects
git writes itself (merges, reverts, fixup! and squash! commits) pass, and a body must be
separated from the subject by a blank line. Exits 0 when the message conforms, 1 otherwise.
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPES = ("feat", "fix", "test", "refactor", "docs", "chore", "ci", "build", "perf")
REPO_SCOPES = ("agents", "ci", "repo", "docs")
SCISSORS = "# ------------------------ >8 ------------------------"
WRITTEN_BY_GIT = re.compile(r"^(Merge |Revert \"|fixup! |squash! |amend! )")


def _scopes() -> list[str]:
    packages = sorted(p.parent.name for p in (ROOT / "packages").glob("*/pyproject.toml"))
    return [*packages, "web", *REPO_SCOPES]


def _subject_pattern(scopes: list[str]) -> re.Pattern[str]:
    return re.compile(rf"^({'|'.join(TYPES)})(\(({'|'.join(scopes)})\))?!?: \S")


def _lines(text: str) -> list[str]:
    """The message git will record: no `#` comment lines, nothing below the scissors."""
    kept: list[str] = []
    for line in text.splitlines():
        if line == SCISSORS:
            break
        if not line.startswith("#"):
            kept.append(line)
    while kept and not kept[0].strip():
        kept.pop(0)
    return kept


def problems(text: str) -> list[str]:
    lines = _lines(text)
    subject = lines[0] if lines else ""
    if WRITTEN_BY_GIT.match(subject):
        return []
    scopes = _scopes()
    found: list[str] = []
    if not _subject_pattern(scopes).match(subject):
        found.append(
            f"{subject!r} is not `type(scope): subject` (or `type: subject`), with type one of "
            f"{', '.join(TYPES)} and scope one of {', '.join(scopes)}"
        )
    if len(lines) > 1 and lines[1].strip():
        found.append("the subject must be followed by a blank line before the body")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a commit message or PR title is not a Conventional Commit."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("file", nargs="?", help="a commit message file")
    source.add_argument("--env", metavar="NAME", help="an environment variable holding a title")
    arguments = parser.parse_args()
    if arguments.env:
        where, text = arguments.env, os.environ.get(arguments.env, "")
    else:
        where, text = arguments.file, Path(arguments.file).read_text(encoding="utf-8")
    found = problems(text)
    for problem in found:
        print(f"{where}: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
