"""Fail when a commit message or a PR's text carries agent attribution, which CLAUDE.md forbids.

    check_attribution.py COMMIT_MSG_FILE...      as a commit-msg hook (git's comments and
                                                 the diff below the scissors line are ignored)
    check_attribution.py --commits BASE..HEAD    every commit in a git range
    check_attribution.py --env PR_TITLE --env PR_BODY
                                                 text in environment variables (unset or empty
                                                 is nothing to check)

The text is normalised before matching (HTML entities and percent-encoding undone, tags
removed, Markdown backslash escapes undone, emphasis marks between letters dropped, NFKD with
invisible and combining characters dropped, every dash and hyphen look-alike mapped to a hyphen,
Cyrillic and Greek letter look-alikes mapped), so a marker written to look different from what a
reader sees is still found; in CI the body is also checked as GitHub renders it (`body_text`).
Letter look-alikes outside the table below (other scripts, the Latin letters of other blocks that
NFKD does not fold) are a known limit: the check finds the text as written, not every text that
could be made to look like it.
Matching is case-insensitive, and the footer is found across spaces, hyphens and line breaks.
This enforces the trailer forms CLAUDE.md names, not the whole "no agent attribution" rule,
which still binds whoever writes the text: the hyphenated trailer key is refused wherever it
stands, and the spaced spellings only as git and GitHub read a trailer (at the start of a line,
with its colon), so a sentence that uses their words passes.
Exits 0 when nothing matched, 1 naming each match, and 2 on a usage error or a range git cannot
list.
"""

import argparse
import html
import os
import re
import subprocess
import sys
import unicodedata
import urllib.parse
from collections.abc import Iterable, Iterator
from functools import partial
from pathlib import Path

# The markers: the hyphenated co-author and session keys anywhere (a trailer's key is never
# prose, and a PR body is rendered, so a tag, link, table or entity around it still shows it);
# the spaced spellings of both keys only as the first word of a line and followed by its colon,
# whatever indent, quote, list or emphasis mark comes before it; a session URL, the agent's
# no-reply address, the footer's link, and the generated-by footer, matched across whitespace,
# hyphens, line breaks and markdown emphasis or link brackets (`Generated with
# [**Claude Code**](...)`). The trailers' words in a sentence ("co-authored by two people",
# "the Claude session"), the words "Claude Code" on their own, and role names are ordinary prose
# and pass. Everything here is written for the normalised text (lower case, ASCII look-alikes).
_LINE_START = r"^[^a-z\n]*"
_KEY_END = r"[ \t*_`]*:"
FORBIDDEN = re.compile(
    r"co[\s-]*authored-[\s-]*by"
    r"|claude[ \t]*-[ \t-]*session"
    rf"|{_LINE_START}co[ \t-]*authored[ \t-]*by{_KEY_END}"
    rf"|{_LINE_START}claude[ \t-]*session{_KEY_END}"
    r"|claude[\s-]*\.ai/code/session"
    r"|claude\.com/claude-code"
    r"|noreply@anthropic\.com"
    r"|generated[\s*_`-]+with[\s*_`\[-]+claude[\s*_`-]*code",
    re.MULTILINE,
)
_TAG = re.compile(r"<[^>]*>")
#: Every dash punctuation character (Unicode category Pd) is read as a hyphen, and so are these,
#: which the category does not hold: the minus sign, the modifier minus, the hyphen bullet, the
#: katakana prolonged sound mark.
_EXTRA_DASHES = frozenset("\N{MINUS SIGN}\N{MODIFIER LETTER MINUS SIGN}\N{HYPHEN BULLET}\u30fc")
#: Characters that show as nothing but are letters, not format characters: the Hangul fillers.
_BLANKS = frozenset("\u115f\u1160\u3164\uffa0")
#: A Markdown backslash escape (`\-`, `\.`, `\@`) shows as its character.
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
#: Emphasis and code marks between two letters (`Auth*ored*`, `Auth_ored_`).
_INSIDE_WORD = re.compile(r"(?<=[^\W\d_])[*_`~]+(?=[^\W\d_])")
_MARKUP = re.compile(r"[*_`~]+")
#: Cyrillic and Greek letters, by code point, that look like Latin ones (the text is lower case
#: by then).
_LOOK_ALIKES = {
    **dict(
        zip(
            (
                0x430,
                0x435,
                0x43E,
                0x440,
                0x441,
                0x445,
                0x443,
                0x456,
                0x458,
                0x455,
                0x501,
                0x51B,
                0x51D,
                0x4BB,
            ),
            "aeopcxyijsdqwh",
            strict=True,
        )
    ),
    **dict(zip((0x3BF, 0x3B1, 0x3BD, 0x3C1, 0x3B9, 0x3BA, 0x3B5), "oavpike", strict=True)),
    0x131: "i",
}
SCISSORS = "# ------------------------ >8 ------------------------"


def _undone(text: str) -> str:
    """The text with entities and percent-encoding undone, even when written twice over."""
    for _ in range(3):
        again = urllib.parse.unquote(html.unescape(text))
        if again == text:
            break
        text = again
    return text


def _folded(text: str) -> str:
    """The text as one reads it: decomposed, invisible and combining marks gone, dashes unified."""
    kept: list[str] = []
    for char in unicodedata.normalize("NFKD", text):
        category = unicodedata.category(char)
        if category in ("Cf", "Mn") or char in _BLANKS:
            continue
        kept.append("-" if category == "Pd" or char in _EXTRA_DASHES else char)
    return unicodedata.normalize("NFKC", "".join(kept))


def _tag_as(replacement: str, tag: re.Match[str]) -> str:
    return replacement + "\n" * tag[0].count("\n")


def _views(text: str) -> Iterator[str]:
    """What a reader or a trailer parser may see: tags kept, removed, or read as spaces."""
    text = _ESCAPE.sub(r"\1", _undone(text))
    for tag_as in (None, "", " "):
        view = text
        if tag_as is not None:
            view = _TAG.sub(partial(_tag_as, tag_as), text)
        view = _folded(view).lower().translate(_LOOK_ALIKES)
        view = _INSIDE_WORD.sub("", view)
        # Once with every emphasis and code mark, which `Co-**Authored**-By` puts next to a
        # hyphen and not between letters, left out as well.
        yield view
        yield _MARKUP.sub("", view)


def _matches(where: str, text: str) -> Iterator[str]:
    # Searched as a whole, so that a marker split across a line break is found; it is reported
    # at the line it starts on, as the text was written.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    numbers: set[int] = set()
    for view in _views(text):
        for match in FORBIDDEN.finditer(view):
            numbers.add(view.count("\n", 0, match.start()) + 1)
    for number in sorted(numbers):
        shown = lines[min(number, len(lines)) - 1]
        yield f"{where}:{number}: {shown.strip()}"


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
