"""Just enough SQL lexing to find statements that control the transaction a file runs in."""

import itertools
import re

__all__ = ["transaction_control"]

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_ESCAPING = {"COMMIT", "END", "ABORT", "BEGIN", "START", "ROLLBACK", "PREPARE"}


def transaction_control(sql: str) -> str | None:
    """The first top-level statement of `sql` that begins, commits or rolls back a transaction.

    Strings, quoted identifiers, comments and dollar-quoted bodies are skipped, so a function
    that contains `BEGIN ... END;` or a string that says `COMMIT;` is not one. `ROLLBACK TO
    SAVEPOINT` stays inside the transaction and is allowed, and so is the `END;` that closes the
    `BEGIN ATOMIC` body of a `CREATE [OR REPLACE] FUNCTION|PROCEDURE`.
    Returns the offending statement's keyword, upper-cased, else None.
    """
    words: list[str] = []  # the words of the statement being read
    atomic = 0  # open `BEGIN ATOMIC` bodies, whose inner statements are ordinary ones
    i, n = 0, len(sql)

    def finish() -> str | None:
        nonlocal atomic
        found = _verdict(words, atomic)
        if words[:1] == ["END"] and atomic:
            atomic -= 1
        elif _creates_routine(words) and any(
            a == "BEGIN" and b == "ATOMIC" for a, b in itertools.pairwise(words)
        ):
            atomic += 1
        words.clear()
        return found

    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            i = sql.find("\n", i)
            i = n if i < 0 else i
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif c == "'":
            backslashes = i > 0 and sql[i - 1] in "Ee" and not _in_word(sql, i - 1)
            i = _skip_quoted(sql, i, "'", backslashes)
            words.append("'")
        elif c == '"':
            i = _skip_quoted(sql, i, '"', False)
            words.append('"')
        elif c == "$" and (tag := _TAG.match(sql, i)):
            end = sql.find(tag[0], tag.end())
            i = n if end < 0 else end + len(tag[0])
            words.append("$")
        elif c == ";":
            if found := finish():
                return found
            i += 1
        elif word := _WORD.match(sql, i):
            words.append(word[0].upper())
            i = word.end()
        else:
            i += 1
    return finish()


def _creates_routine(words: list[str]) -> bool:
    """Whether the statement is `CREATE [OR REPLACE] FUNCTION|PROCEDURE`, the only home of
    a `BEGIN ATOMIC` body."""
    if words[:1] != ["CREATE"]:
        return False
    rest = words[3:] if words[1:3] == ["OR", "REPLACE"] else words[1:]
    return rest[:1] in (["FUNCTION"], ["PROCEDURE"])


def _in_word(sql: str, i: int) -> bool:
    """Whether the character at `i` continues an identifier rather than being an `E` prefix."""
    return i > 0 and (sql[i - 1].isalnum() or sql[i - 1] in "_$")


def _skip_quoted(sql: str, start: int, quote: str, backslashes: bool) -> int:
    i, n = start + 1, len(sql)
    while i < n:
        if backslashes and sql[i] == "\\":
            i += 2
        elif sql[i] == quote:
            if sql[i + 1 : i + 2] == quote:
                i += 2
            else:
                return i + 1
        else:
            i += 1
    return n


def _verdict(words: list[str], atomic: int) -> str | None:
    if not words or words[0] not in _ESCAPING:
        return None
    first, second = words[0], words[1] if len(words) > 1 else ""
    if first == "END" and atomic:
        return None
    if first == "ROLLBACK" and second in {"TO", "SAVEPOINT"}:
        return None
    if first == "PREPARE":
        return "PREPARE TRANSACTION" if second == "TRANSACTION" else None
    if first == "START":
        return "START TRANSACTION" if second == "TRANSACTION" else None
    return first
