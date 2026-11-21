"""Giving a child process a URL that carries a credential, without the credential in its argv.

A process's command line is readable by every user of the host (`/proc/<pid>/cmdline`, or
`ps`), and a container's processes are host processes, so a password in argv is a password
published to everyone with a shell on the machine (ADR 0019). A file only its owner can read
is not.

`private_ffmpeg_input` writes the URL, and the input options that go with it, to such a file
and hands back the arguments that make ffmpeg read it: an ffconcat list holding the one input.
ffmpeg opens the URL itself from that file, so its argv names the file and never the URL. The
file lives in a directory of its own (mode 0700) at mode 0600, created exclusively, and both
are removed when the block ends. In a container that is the `/tmp` tmpfs, which holds nothing
once the container stops; a process killed before it could clean up leaves a file only its own
user can read until then.

ffmpeg still repeats the URL in its own error messages: whatever logs them must scrub them
(`visionquery.runtime.scrub`).
"""

import os
import re
import shutil
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

__all__ = ["FFMPEG_PROTOCOLS", "private_ffmpeg_input"]

#: The protocols ffmpeg may use for the listed input: the list itself is a file, and the URL
#: is RTSP, over TCP, with TLS for `rtsps`. Nothing else, so a list can never lead elsewhere.
FFMPEG_PROTOCOLS = "file,rtsp,rtsps,tcp,tls"


#: What an `option` line's name and value may hold. ffmpeg reads them unquoted (a quoted one is
#: taken literally and the option silently not applied), so anything that would need quoting
#: is refused rather than guessed at.
_OPTION_TOKEN = re.compile(r"[A-Za-z0-9_.:+/,-]+")


def _option(name: str, value: str) -> str:
    if not (_OPTION_TOKEN.fullmatch(name) and _OPTION_TOKEN.fullmatch(value)):
        raise ValueError(f"ffmpeg input option {name!r} has a name or value it cannot be given")
    return f"option {name} {value}"


def _quoted(text: str) -> str:
    """`text` as one ffconcat token: in single quotes, with each quote written `'\\''`."""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        # Never says the text: it may be the URL.
        raise ValueError("an ffmpeg input and its options may hold no control characters")
    return "'" + text.replace("'", "'\\''") + "'"


@contextmanager
def private_ffmpeg_input(
    url: str, options: Mapping[str, str] | None = None, *, directory: Path | None = None
) -> Generator[list[str]]:
    """The ffmpeg arguments that read `url` with `options`, without either in argv.

    `options` are the input's own options (`{"rtsp_transport": "tcp", "timeout": "10000000"}`),
    which ffmpeg would otherwise take as `-rtsp_transport tcp` before `-i`. `directory` is
    where the private directory goes: the system's temporary directory unless given.
    """
    lines = ["ffconcat version 1.0", f"file {_quoted(url)}"]
    lines += [_option(name, value) for name, value in (options or {}).items()]
    private = Path(tempfile.mkdtemp(prefix="vq-input-", dir=directory))
    try:
        path = private / "input.ffconcat"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
        yield [
            "-f", "concat", "-safe", "0", "-protocol_whitelist", FFMPEG_PROTOCOLS, "-i", str(path),
        ]  # fmt: skip
    finally:
        shutil.rmtree(private, ignore_errors=True)
