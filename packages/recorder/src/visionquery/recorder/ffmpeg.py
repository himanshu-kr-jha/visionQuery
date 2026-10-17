"""One ffmpeg per camera, copying its stream into fragmented MP4 on a pipe.

ffmpeg speaks RTSP and repackages the stream; the recorder reads its stdout, sees each Fragment
as it closes (`visionquery.recorder.mp4`), and decides where Segments begin and end. Neither
MediaMTX's own recorder nor ffmpeg's segment muxer can stand in for that, because only the
component writing a Segment knows each Fragment the moment it exists, and only it can date
Segments by the NTP-anchored timebase.

**It copies and never transcodes.** The stored bytes are what the camera sent, repackaged into
a container, so recording costs no GPU and no decoding, and Footage is what the camera saw.

**Video only.** Cameras commonly send G.711 audio, which MP4 cannot hold, and carrying it would
make every such camera fail to record at all.

**ffmpeg's stderr is piped, never inherited** (ADR 0012): each line is logged at DEBUG, and the
last few say why a recording ended, all scrubbed of credentials first, because ffmpeg repeats
the URL it was given in its errors. The URL, with VisionQuery's own MediaMTX password, is on
ffmpeg's command line, which only the recorder's own container can see.

Adapted from phyOS's Photon recorder (`adapters.photon.recorder`).
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import SecretStr
from visionquery.recorder.mp4 import Fragment, Init, Stream
from visionquery.runtime import scrub

__all__ = ["Recording", "Source"]

_log = logging.getLogger(__name__)

_FRAGMENTED = "+frag_keyframe+empty_moov+default_base_moof"
_CHUNK = 1 << 16
#: How many of ffmpeg's last lines say why a recording ended.
_LAST_WORDS = 20
#: How long ffmpeg has to finish its last Fragment once told to stop, before it is killed.
_STOP_WITHIN = 5.0


@dataclass(frozen=True)
class Source:
    """Where one camera's stream is read from, and how."""

    url: SecretStr
    #: Input options placed before `-i`, for a source that needs them.
    options: tuple[str, ...] = ()
    ffmpeg: str = "ffmpeg"
    #: Seconds of silence on an RTSP connection before ffmpeg gives up on it. A camera can stop
    #: sending without anything closing the connection, and without this the recorder would
    #: wait for ever: never finishing what it has, never reconnecting.
    stall_after: float = 10.0
    _password: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_password", urlsplit(self.url.get_secret_value()).password or "")

    @property
    def redacted(self) -> str:
        """The URL without its credentials: what the recorder may say about its source."""
        parts = urlsplit(self.url.get_secret_value())
        if parts.username is None and parts.password is None:
            return self.url.get_secret_value()
        host = parts.hostname or ""
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))

    def __repr__(self) -> str:
        return f"Source({self.redacted!r})"

    def scrubbed(self, text: str) -> str:
        """`text` without the source's password, wherever ffmpeg put it, nor any URL's."""
        if self._password:
            text = text.replace(self._password, "***")
        return scrub(text)

    def argv(self) -> list[str]:
        url = self.url.get_secret_value()
        transport: list[str] = []
        if url.startswith(("rtsp://", "rtsps://")):
            transport = [
                "-rtsp_transport", "tcp",
                "-timeout", str(round(self.stall_after * 1_000_000)),
            ]  # fmt: skip
        return [
            self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            *transport, *self.options, "-i", url,
            "-map", "0:v:0", "-c", "copy",
            "-f", "mp4", "-movflags", _FRAGMENTED, "-flush_packets", "1",
            "pipe:1",
        ]  # fmt: skip


class Recording:
    """One run of ffmpeg against one source, from start until the stream ends.

    `async with` starts it and stops it; iterating it yields the `Init` and then each `Fragment`
    as it closes. When the stream ends, `ended_because` says why, in ffmpeg's own words where it
    gave any.
    """

    def __init__(self, source: Source) -> None:
        self.source = source
        self.ended_because: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stderr: deque[str] = deque(maxlen=_LAST_WORDS)
        self._drain: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self._process = await asyncio.create_subprocess_exec(
            *self.source.argv(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._drain = asyncio.create_task(self._read_stderr())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Whoever iterated has left, perhaps on an error with ffmpeg's stdout unread.
        await self.stop(drain=True)

    async def _read_stderr(self) -> None:
        assert self._process is not None
        assert self._process.stderr is not None
        async for line in self._process.stderr:
            text = self.source.scrubbed(line.decode(errors="replace").strip())
            if text:
                self._stderr.append(text)
                _log.debug("ffmpeg: %s", text, extra={"source": self.source.redacted})

    async def __aiter__(self) -> AsyncIterator[Init | Fragment]:
        assert self._process is not None
        assert self._process.stdout is not None
        stream = Stream()
        while chunk := await self._process.stdout.read(_CHUNK):
            for item in stream.feed(chunk):
                yield item
        code = await self._process.wait()
        if self._drain is not None:
            await self._drain
        said = "; ".join(self._stderr)
        if code == 0 and not said:
            self.ended_because = "the source ended"
        else:
            self.ended_because = (
                f"ffmpeg exited {code}: {said}" if said else f"ffmpeg exited {code}"
            )

    async def stop(self, *, drain: bool = False) -> None:
        """End the recording: ffmpeg finishes its last Fragment and the iteration ends.

        `drain` reads and drops what ffmpeg still writes, for when nothing iterates any more:
        waiting on a process also waits until its stdout is seen to close, and a pipe nobody
        reads fills, blocks ffmpeg, and is never seen to close.
        """
        process = self._process
        if process is not None:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(_ended(process, drain), timeout=_STOP_WITHIN)
            except TimeoutError:
                process.kill()
                await _ended(process, drain)
        if self._drain is not None:
            self._drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain


async def _ended(process: asyncio.subprocess.Process, drain: bool) -> None:
    if drain and process.stdout is not None:
        while await process.stdout.read(_CHUNK):
            pass
    await process.wait()
