"""Removing the credentials a URL can carry from any text that might be logged.

Cameras are reached by URLs such as `rtsp://admin:secret@10.0.0.7/stream1`, and those
URLs turn up in ffmpeg's errors, in exception messages and in whatever a service chooses
to log. A camera password in a log line ends up wherever the logs do.
"""

import re

__all__ = ["scrub"]

#: `scheme://` followed by everything up to the last `@` before whitespace: the userinfo.
#: Greedy on purpose: people type passwords holding `@`, `/` or `?` without encoding them,
#: so a path that happens to hold an `@` is lost rather than a password ever being kept.
_USERINFO = re.compile(r"([a-z][a-z0-9+.\-]*://)\S*@", re.IGNORECASE)

#: Query parameters some cameras take credentials in, such as `?user=admin&pwd=secret`.
_QUERY_CREDENTIAL = re.compile(
    r"([?&;](?:user|usr|username|login|password|passwd|pass|pwd|secret|token|access_token"
    r"|api_key|apikey|key|auth)=)[^&;#\s]*",
    re.IGNORECASE,
)

_REDACTED = "***"


def scrub(text: str) -> str:
    """`text` with the credentials of every URL in it replaced by `***`."""
    text = _USERINFO.sub(rf"\1{_REDACTED}@", text)
    return _QUERY_CREDENTIAL.sub(rf"\1{_REDACTED}", text)
