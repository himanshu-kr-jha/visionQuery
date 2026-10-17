"""Fragmented MP4 built by hand, box by box, for the recorder's tests.

The tests import it by name (pytest's `pythonpath` holds this directory). Each helper writes
the smallest box the reader accepts, so a test says exactly what a stream holds.
"""

import struct

from visionquery.recorder.mp4 import Fragment, Init, Stream

__all__ = [
    "VIDEO",
    "box",
    "fragment",
    "fragments",
    "full",
    "init",
    "moof",
    "parsed_init",
    "traf",
]


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full(kind: bytes, payload: bytes, *, version: int = 0, flags: int = 0) -> bytes:
    return box(kind, struct.pack(">I", (version << 24) | flags) + payload)


def init(*tracks: tuple[int, int, bytes], trex: dict[int, int] | None = None) -> bytes:
    """`ftyp` and a `moov` naming each track's id, timescale and handler."""
    traks = b""
    for track_id, timescale, handler in tracks:
        tkhd = full(b"tkhd", struct.pack(">III", 0, 0, track_id) + b"\0" * 68)
        mdhd = full(b"mdhd", struct.pack(">IIII", 0, 0, timescale, 0) + b"\0" * 4)
        hdlr = full(b"hdlr", struct.pack(">I4s", 0, handler) + b"\0" * 13)
        traks += box(b"trak", tkhd + box(b"mdia", mdhd + hdlr))
    mvex = b""
    if trex:
        mvex = box(
            b"mvex",
            b"".join(
                full(b"trex", struct.pack(">IIIII", track_id, 1, duration, 0, 0))
                for track_id, duration in trex.items()
            ),
        )
    return box(b"ftyp", b"isom\0\0\x02\0isomiso6") + box(b"moov", traks + mvex)


def traf(
    track_id: int,
    decode_time: int,
    durations: list[int] | None = None,
    *,
    default_duration: int | None = None,
    samples: int = 0,
) -> bytes:
    """One track's run inside a `moof`, with durations per sample or from a default."""
    tfhd_flags = 0x020000  # default-base-is-moof, as ffmpeg writes it
    tfhd = struct.pack(">I", track_id)
    if default_duration is not None:
        tfhd_flags |= 0x08
        tfhd += struct.pack(">I", default_duration)
    trun_flags = 0x01  # data offset present
    if durations is not None:
        trun_flags |= 0x100
        count = len(durations)
        per_sample = b"".join(struct.pack(">I", d) for d in durations)
    else:
        count = samples
        per_sample = b""
    return box(
        b"traf",
        full(b"tfhd", tfhd, flags=tfhd_flags)
        + full(b"tfdt", struct.pack(">Q", decode_time), version=1)
        + full(b"trun", struct.pack(">Ii", count, 0) + per_sample, flags=trun_flags),
    )


def moof(*trafs: bytes, payload: bytes = b"\x01" * 32) -> bytes:
    """A `moof` over the given runs, and its `mdat`."""
    header = full(b"mfhd", struct.pack(">I", 1))
    return box(b"moof", header + b"".join(trafs)) + box(b"mdat", payload)


def fragment(
    track_id: int,
    decode_time: int,
    durations: list[int] | None = None,
    *,
    payload: bytes = b"\x01" * 32,
    default_duration: int | None = None,
    samples: int = 0,
) -> bytes:
    run = traf(track_id, decode_time, durations, default_duration=default_duration, samples=samples)
    return moof(run, payload=payload)


#: A video track: id 1, a timescale of 12800, as ffmpeg writes H.264 at 25 fps.
VIDEO = (1, 12800, b"vide")
HEAD = init(VIDEO)


def parsed_init() -> Init:
    (item,) = Stream().feed(HEAD)
    assert isinstance(item, Init)
    return item


def fragments(count: int, *, first: int = 0) -> list[Fragment]:
    """`count` two-second Fragments, the first starting `first` Fragments into the stream."""
    stream = Stream()
    stream.feed(HEAD)
    out: list[Fragment] = []
    for n in range(first, first + count):
        (item,) = stream.feed(fragment(1, n * 25600, [512] * 50, payload=bytes([n % 256]) * 64))
        assert isinstance(item, Fragment)
        out.append(item)
    return out
