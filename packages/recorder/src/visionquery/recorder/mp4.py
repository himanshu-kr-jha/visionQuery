"""Fragmented MP4, read as it is written.

The recorder decides where Segments begin and end, so it has to know where every Fragment ends
while the stream is still arriving. A Fragment is one `moof` and the `mdat` after it, one
keyframe interval long, and once written it is never rewritten. That is what lets a Segment be
cut at any Fragment boundary, rolled at one, or truncated to one after a crash, and still be a
file a player opens.

This module reads boxes and nothing else: it never decodes a frame (the recorder is copy-only).

Two things are refused rather than guessed. A box sized "to the end of the file" has no end in a
live stream, so its length would be invented. A Fragment that states no duration for its
samples, anywhere, has no length, and a Segment's span is read from the media.

Adapted from phyOS's Photon recorder (`adapters.photon.mp4`).
"""

import struct
from dataclasses import dataclass, field

__all__ = ["Fragment", "Init", "MalformedError", "Stream", "Track", "rebase"]


class MalformedError(ValueError):
    """The bytes are not a fragmented MP4 the recorder can cut safely."""


@dataclass(frozen=True)
class Track:
    track_id: int
    timescale: int
    handler: bytes
    #: `trex` default sample duration, the last place a duration can come from.
    default_duration: int | None = None

    @property
    def is_video(self) -> bool:
        return self.handler == b"vide"


@dataclass(frozen=True)
class Init:
    """`ftyp` and `moov`: what every Segment file starts with."""

    data: bytes
    tracks: dict[int, Track] = field(compare=False)


@dataclass(frozen=True)
class Fragment:
    """One `moof` and its `mdat`, dated by its video track where it has one."""

    data: bytes
    track_id: int
    timescale: int
    decode_time: int
    duration: int
    #: Every track's own decode time, by track id, which is what `rebase` subtracts from.
    starts: dict[int, int] = field(default_factory=dict[int, int], compare=False)

    @property
    def start_seconds(self) -> float:
        return self.decode_time / self.timescale

    @property
    def seconds(self) -> float:
        return self.duration / self.timescale


_HEADER = 8


def _box_at(buf: bytes | bytearray, at: int) -> tuple[bytes, int, int] | None:
    """`(type, header length, total length)` of the box at `at`, or None if incomplete."""
    if len(buf) - at < _HEADER:
        return None
    size, kind = struct.unpack_from(">I4s", buf, at)
    header = _HEADER
    if size == 1:
        if len(buf) - at < 16:
            return None
        (size,) = struct.unpack_from(">Q", buf, at + 8)
        header = 16
    elif size == 0:
        raise MalformedError(
            f"a {kind.decode(errors='replace')} box runs to the end of the stream, "
            "which a live stream does not have"
        )
    if size < header:
        name = kind.decode(errors="replace")
        raise MalformedError(f"a {name} box is shorter than its own header")
    return kind, header, size


def _children(data: bytes) -> list[tuple[bytes, bytes]]:
    """The boxes inside a container's payload, as `(type, payload)`."""
    out: list[tuple[bytes, bytes]] = []
    at = 0
    while at < len(data):
        found = _box_at(data, at)
        if found is None or at + found[2] > len(data):
            raise MalformedError("a box overruns the box that contains it")
        kind, header, size = found
        out.append((kind, data[at + header : at + size]))
        at += size
    return out


def _first(children: list[tuple[bytes, bytes]], kind: bytes) -> bytes | None:
    return next((payload for k, payload in children if k == kind), None)


def _full(payload: bytes) -> tuple[int, int, bytes]:
    """`(version, flags, rest)` of a full box."""
    (word,) = struct.unpack_from(">I", payload)
    return word >> 24, word & 0xFFFFFF, payload[4:]


def _tracks(moov: bytes) -> dict[int, Track]:
    children = _children(moov)
    defaults: dict[int, int] = {}
    mvex = _first(children, b"mvex")
    if mvex is not None:
        for kind, payload in _children(mvex):
            if kind == b"trex":
                _, _, rest = _full(payload)
                track_id, _, duration = struct.unpack_from(">III", rest)
                defaults[track_id] = duration

    tracks: dict[int, Track] = {}
    for kind, trak in children:
        if kind != b"trak":
            continue
        parts = _children(trak)
        tkhd, mdia = _first(parts, b"tkhd"), _first(parts, b"mdia")
        if tkhd is None or mdia is None:
            raise MalformedError("a track names no header or no media")
        version, _, rest = _full(tkhd)
        (track_id,) = struct.unpack_from(">I", rest, 16 if version == 1 else 8)
        media = _children(mdia)
        mdhd, hdlr = _first(media, b"mdhd"), _first(media, b"hdlr")
        if mdhd is None or hdlr is None:
            raise MalformedError(f"track {track_id} names no timescale or no handler")
        version, _, rest = _full(mdhd)
        (timescale,) = struct.unpack_from(">I", rest, 16 if version == 1 else 8)
        _, _, rest = _full(hdlr)
        tracks[track_id] = Track(track_id, timescale, rest[4:8], defaults.get(track_id))
    if not tracks:
        raise MalformedError("the init names no track")
    return tracks


def _run(traf: bytes, tracks: dict[int, Track]) -> tuple[Track, int, int]:
    """`(track, decode time, duration)` of one track's run inside a `moof`."""
    parts = _children(traf)
    tfhd, tfdt = _first(parts, b"tfhd"), _first(parts, b"tfdt")
    if tfhd is None or tfdt is None:
        raise MalformedError("a track fragment names no header or no decode time")

    _, flags, rest = _full(tfhd)
    (track_id,) = struct.unpack_from(">I", rest)
    track = tracks.get(track_id)
    if track is None:
        raise MalformedError(f"a fragment carries track {track_id}, which the init never named")
    at = 4
    at += 8 if flags & 0x01 else 0  # base data offset
    at += 4 if flags & 0x02 else 0  # sample description index
    default = track.default_duration
    if flags & 0x08:
        (default,) = struct.unpack_from(">I", rest, at)

    version, _, rest = _full(tfdt)
    (decode_time,) = struct.unpack_from(">Q" if version == 1 else ">I", rest)

    duration = 0
    for kind, payload in parts:
        if kind != b"trun":
            continue
        _, flags, rest = _full(payload)
        (count,) = struct.unpack_from(">I", rest)
        at = 4
        at += 4 if flags & 0x01 else 0  # data offset
        at += 4 if flags & 0x04 else 0  # first sample flags
        per_sample = 4 * sum(bool(flags & bit) for bit in (0x100, 0x200, 0x400, 0x800))
        if flags & 0x100:
            for n in range(count):
                (sample,) = struct.unpack_from(">I", rest, at + n * per_sample)
                duration += sample
        elif count:
            if default is None:
                raise MalformedError(f"track {track_id} states no duration for its samples")
            duration += count * default
    return track, decode_time, duration


class Stream:
    """Bytes in, an `Init` and then `Fragment`s out, each only once it is complete."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._init: Init | None = None
        self._ftyp: bytes | None = None
        self._moof: bytes | None = None

    @property
    def buffered(self) -> int:
        """Bytes held that do not yet make a whole item."""
        return len(self._buf) + len(self._moof or b"") + len(self._ftyp or b"")

    def feed(self, chunk: bytes) -> list[Init | Fragment]:
        self._buf += chunk
        out: list[Init | Fragment] = []
        while (found := _box_at(self._buf, 0)) is not None:
            kind, header, size = found
            if len(self._buf) < size:
                break
            whole = bytes(self._buf[:size])
            del self._buf[:size]
            item = self._take(kind, whole, whole[header:])
            if item is not None:
                out.append(item)
        return out

    def _take(self, kind: bytes, whole: bytes, payload: bytes) -> Init | Fragment | None:
        if self._moof is not None and kind != b"mdat":
            raise MalformedError("a moof was not followed by its mdat")
        if kind == b"ftyp":
            self._ftyp = whole
            return None
        if kind == b"moov":
            self._init = Init(data=(self._ftyp or b"") + whole, tracks=_tracks(payload))
            self._ftyp = None
            return self._init
        if kind == b"moof":
            if self._init is None:
                raise MalformedError("a fragment arrived before the init that says how to read it")
            self._moof = whole
            return None
        if kind == b"mdat" and self._moof is not None:
            moof, self._moof = self._moof, None
            return self._fragment(moof, whole)
        # `free`, `sidx`, `styp`, `mfra` and anything else between Fragments: not part of any
        # Fragment, and not needed to play one.
        return None

    def _fragment(self, moof: bytes, mdat: bytes) -> Fragment:
        assert self._init is not None
        (_, contents), *_ = _children(moof)
        runs = [
            _run(payload, self._init.tracks)
            for kind, payload in _children(contents)
            if kind == b"traf"
        ]
        if not runs:
            raise MalformedError("a moof carries no track fragment")
        track, decode_time, duration = next((r for r in runs if r[0].is_video), runs[0])
        return Fragment(
            data=moof + mdat,
            track_id=track.track_id,
            timescale=track.timescale,
            decode_time=decode_time,
            duration=duration,
            starts={t.track_id: start for t, start, _ in runs},
        )


def _whole(data: bytes, at: int) -> tuple[bytes, int, int]:
    """The box at `at` in bytes already known to hold it whole."""
    found = _box_at(data, at)
    if found is None:
        raise MalformedError("a box is cut short inside a Fragment")
    return found


def _tfdt_offsets(data: bytes) -> list[tuple[int, int, int]]:
    """`(track id, offset of the decode time, its width)` for every run in a Fragment."""
    out: list[tuple[int, int, int]] = []
    _, header, size = _whole(data, 0)
    at = header
    while at < size:
        kind, child_header, child_size = _whole(data, at)
        if kind == b"traf":
            track_id: int | None = None
            tfdt: tuple[int, int] | None = None
            inner = at + child_header
            while inner < at + child_size:
                k, h, n = _whole(data, inner)
                if k == b"tfhd":
                    (track_id,) = struct.unpack_from(">I", data, inner + h + 4)
                elif k == b"tfdt":
                    version = data[inner + h]
                    tfdt = (inner + h + 4, 8 if version == 1 else 4)
                inner += n
            if track_id is not None and tfdt is not None:
                out.append((track_id, *tfdt))
        at += child_size
    return out


def rebase(fragment: Fragment, base: dict[int, int]) -> Fragment:
    """The same Fragment with each track's decode time counted from `base[track]`.

    The stream's media clock runs on from whenever ffmpeg started, so the third Segment of a
    connection would otherwise begin minutes into its own timeline and play late. Only the
    `tfdt` field changes, in place and at the same width: no sample, size or offset moves, so
    this is repackaging and never a transcode.
    """
    data = bytearray(fragment.data)
    for track_id, offset, width in _tfdt_offsets(fragment.data):
        start = fragment.starts.get(track_id, 0)
        rebased = start - base.get(track_id, 0)
        if rebased < 0:
            raise MalformedError(f"track {track_id} would start before the Segment it belongs to")
        struct.pack_into(">Q" if width == 8 else ">I", data, offset, rebased)
    starts = {track: start - base.get(track, 0) for track, start in fragment.starts.items()}
    return Fragment(
        data=bytes(data),
        track_id=fragment.track_id,
        timescale=fragment.timescale,
        decode_time=starts.get(fragment.track_id, fragment.decode_time),
        duration=fragment.duration,
        starts=starts,
    )
