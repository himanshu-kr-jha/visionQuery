"""Removing the credentials a URL can carry from any text that might be logged.

Cameras are reached by URLs such as `rtsp://admin:secret@10.0.0.7/stream1`, and those
URLs turn up in ffmpeg's errors, in exception messages and in whatever a service chooses
to log. A camera password in a log line ends up wherever the logs do.

Scrubbing runs on every log record, including ones an attacker or a broken library makes
huge, so every step here is linear in the length of the text.

Known limits, accepted because this is defence in depth and not a guarantee (the camera
registry never logs a camera URL, ADR 0014):

- A plain-text password containing whitespace, or one of `,;&)]}` outside a URL, leaks after
  that character, because an unquoted value ends there.
- `Authorization` values are masked up to the first whitespace after the scheme, so Digest
  headers (`Digest username="a", response="b"`) and any value of several tokens leak after
  the first.
- `Cookie` and `Set-Cookie` headers are not recognised.
- Escaped-slash URLs (`rtsp:\\/\\/user:pw@host`, as in JSON) are not recognised, because only
  `scheme://` introduces userinfo.
- A scheme-less `user:pw@host` is not recognised, for the same reason.
- Percent-encoded text is not decoded before scrubbing.
- Full-width or other look-alike `@` characters are not recognised.
- URLs whose path or query holds an `@` lose their host in logs, because the userinfo is
  taken to end at the last `@` before whitespace, so that unencoded passwords holding `@`,
  `/` or `?` are never kept.
"""

import re

__all__ = ["REDACTED", "is_credential_key", "scrub"]

_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")
_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_WHITESPACE = re.compile(r"\s")

#: Query parameters some cameras take credentials in, such as `?user=admin&pwd=secret`.
_QUERY_CREDENTIAL = re.compile(
    r"([?&;](?:user|usr|username|login|password|passwd|pass|pwd|secret|token|access_token"
    r"|api_key|apikey|key|auth)=)[^&;#\s]*",
    re.IGNORECASE,
)

#: The words that make a name a credential, as the whole name or its end (`password`,
#: `camera_password`, `cameraPassword`, `accessToken`), but never inside it (`token_count`,
#: `tokenizer`, `secretary`). Text and structured fields share this one list.
_CREDENTIAL_WORDS = "password|passwd|pwd|secret|token|passphrase|private[_-]?key|api[_-]?key"

#: `keyword: value` and `keyword=value` in plain text or JSON-ish text, quotes kept.
#: A quoted value runs to its closing quote (backslash escapes included, but not past the end
#: of the line); an unquoted one stops at whitespace or one of `,;&)]}`, so a password
#: holding those leaks after them. Every part is a bounded class, so the pattern is linear.
#: A value already masked (by the query pattern above) is left alone.
_KEYWORD_CREDENTIAL = re.compile(
    rf"((?:{_CREDENTIAL_WORDS})[\"']?[ \t]*[:=][ \t]*)"
    r"(?!\*\*\*)(?:\"(?:[^\"\\\r\n]|\\.)*\"?|'(?:[^'\\\r\n]|\\.)*'?|[^\s,;&\"')\]}]+)",
    re.IGNORECASE,
)

#: A name that ends in a credential word, or is `authorization`.
_CREDENTIAL_KEY = re.compile(rf"(?:{_CREDENTIAL_WORDS}|authorization)$", re.IGNORECASE)

#: `Authorization: Basic <credentials>`, keeping the header and its scheme.
_AUTHORIZATION = re.compile(
    r"(?<![a-z0-9])(authorization[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"(?:(?:basic|bearer|digest|token)[ \t]+)?)[^\s,;\"']+",
    re.IGNORECASE,
)

REDACTED = "***"


def is_credential_key(key: str) -> bool:
    """Whether a value stored under `key` is a credential, whatever it looks like."""
    return _CREDENTIAL_KEY.search(key) is not None


def _mask_keyword_credential(found: re.Match[str]) -> str:
    key, value = found.group(1), found.group(0)[len(found.group(1)) :]
    quote = value[0] if value[0] in "\"'" else ""
    closing = quote if quote and len(value) > 1 and value[-1] == quote else ""
    return f"{key}{quote}{REDACTED}{closing}"


def _remove_userinfo(text: str) -> str:
    """Replace the userinfo of every `scheme://userinfo@host` with `***`.

    The userinfo runs to the last `@` before whitespace, because people type passwords
    holding `@`, `/` or `?` without encoding them, so a path that happens to hold an `@`
    is lost rather than a password ever being kept. Each whitespace-free run is examined
    once, so no input makes the work quadratic.
    """
    parts: list[str] = []
    copied = 0  # text before this index is already in `parts`
    search = 0
    run_end = -1  # end of the whitespace-free run holding `search`
    last_at = -1  # the last `@` of that run
    while (sep := text.find("://", search)) != -1:
        if sep >= run_end:
            whitespace = _WHITESPACE.search(text, sep)
            run_end = whitespace.start() if whitespace else len(text)
            last_at = text.rfind("@", sep, run_end)
        if last_at == -1:  # nothing to remove anywhere later in this run
            search = run_end
            continue
        start = sep
        while start > copied and text[start - 1] in _SCHEME_CHARS:
            start -= 1
        while start < sep and text[start] not in _LETTERS:
            start += 1
        if start == sep:  # no scheme in front of this `://`
            search = sep + 3
            continue
        parts.append(text[copied : sep + 3])
        parts.append(REDACTED)
        copied = last_at
        search = last_at + 1
        run_end = -1
    parts.append(text[copied:])
    return "".join(parts)


def scrub(text: str) -> str:
    """`text` with the credentials of every URL in it, and plain-text ones, replaced by `***`."""
    text = _remove_userinfo(text)
    text = _QUERY_CREDENTIAL.sub(rf"\1{REDACTED}", text)
    text = _AUTHORIZATION.sub(rf"\1{REDACTED}", text)
    return _KEYWORD_CREDENTIAL.sub(_mask_keyword_credential, text)
