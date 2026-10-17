"""Fail when a commit message or a PR's text carries agent attribution, which CLAUDE.md forbids.

    check_attribution.py COMMIT_MSG_FILE...      as a commit-msg hook (git's comments and
                                                 the diff below the scissors line are ignored)
    check_attribution.py --commits BASE..HEAD    every commit in a git range
    check_attribution.py --env PR_TITLE --env PR_BODY
                                                 text in environment variables (unset or empty
                                                 is nothing to check)

Matching is case-insensitive, and the footer is found across spaces, hyphens and line breaks.
Exits 0 when nothing matched, 1 naming each match, and 2 on a usage error or a range git cannot
list.
"""

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

# The three markers. The footer is matched across whitespace, hyphens and line breaks, with an
# optional `[` for its markdown link (`Generated with [Claude Code](...)`). The words
# "Claude Code" on their own, and role names, are ordinary prose and pass.
FORBIDDEN = re.compile(
    r"co-authored-by|claude-session|generated[\s-]+with[\s-]+\[?claude[\s-]*code",
    re.IGNORECASE,
)
SCISSORS = "# ------------------------ >8 ------------------------"


def _matches(where: str, text: str) -> Iterator[str]:
    # Searched as a whole, so that a marker split across a line break is found; it is reported
    # at the line it starts on.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    for match in FORBIDDEN.finditer(text):
        number = text.count("\n", 0, match.start()) + 1
        yield f"{where}:{number}: {lines[number - 1].strip()}"


def _commit_message(text: str) -> str:
    """The message git will record: no `#` comment lines, nothing below the scissors."""
    kept: list[str] = []
    for line in text.splitlines():
        if line == SCISSORS:
            break
        kept.append("" if line.startswith("#") else line)
    return "\n".join(kept)


def _commits(revisions: str) -> Iterator[tuple[str, str]]:
    listed = subprocess.run(
        ["git", "log", "--format=%H%x00%s%x00%B%x00", revisions, "--"],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise ValueError(f"git log {revisions}: {listed.stderr.strip()}")
    fields = listed.stdout.split("\0")
    for start in range(0, len(fields) - 2, 3):
        commit, subject, body = fields[start : start + 3]
        yield f"commit {commit.strip()[:12]} ({subject})", body


def _findings(arguments: argparse.Namespace) -> Iterable[str]:
    for path in arguments.files:
        yield from _matches(path, _commit_message(Path(path).read_text(encoding="utf-8")))
    for revisions in arguments.commits:
        for where, body in _commits(revisions):
            yield from _matches(where, body)
    for name in arguments.env:
        yield from _matches(name, os.environ.get(name, ""))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail on agent attribution in commit messages or PR text."
    )
    parser.add_argument("files", nargs="*", help="commit message files")
    parser.add_argument("--commits", action="append", default=[], metavar="RANGE")
    parser.add_argument("--env", action="append", default=[], metavar="NAME")
    arguments = parser.parse_args()
    if not (arguments.files or arguments.commits or arguments.env):
        parser.error("nothing to check: give message files, --commits or --env")
    try:
        findings = list(_findings(arguments))
    except ValueError as error:
        print(f"check_attribution: {error}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(
            "Agent attribution is not allowed in commits, PR titles or PR bodies (CLAUDE.md): "
            "remove these lines.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
