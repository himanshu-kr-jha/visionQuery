"""The recorder's Prometheus metrics (ADR 0012), served on `/metrics` by the runtime.

Labelled by camera id, never by name or path: an id is never reused, and a Deployment has tens
of cameras, not thousands.
"""

from prometheus_client import Counter, Gauge

__all__ = [
    "BYTES",
    "CAMERAS",
    "CLOCK_VERIFIED",
    "CONNECTIONS",
    "FRAGMENTS",
    "RECEIVING",
    "SEGMENTS",
    "WALL_OFFSET",
]

CONNECTIONS = Counter(
    "visionquery_recorder_connections",
    "Connections the recorder made to a camera's stream in MediaMTX, by camera id.",
    ["camera"],
)
FRAGMENTS = Counter(
    "visionquery_recorder_fragments", "Fragments of Footage written, by camera id.", ["camera"]
)
BYTES = Counter("visionquery_recorder_bytes", "Bytes of Footage written, by camera id.", ["camera"])
SEGMENTS = Counter(
    "visionquery_recorder_segments",
    "Segments finished, by camera id and why (rolled, stream_ended, stopped, recovered).",
    ["camera", "reason"],
)
RECEIVING = Gauge(
    "visionquery_recorder_receiving",
    "1 while a camera's video is arriving and being recorded, 0 while it is not, by camera id.",
    ["camera"],
)
CAMERAS = Gauge(
    "visionquery_recorder_cameras", "Cameras being recorded, as of the last read of the registry."
)
CLOCK_VERIFIED = Gauge(
    "visionquery_recorder_clock_verified",
    "1 when a camera's last Segment opened on a synchronised NTP reading, 0 on the wall clock.",
)
WALL_OFFSET = Gauge(
    "visionquery_recorder_clock_wall_offset_seconds",
    "The wall clock less the recorder's timebase, when a Segment last finished.",
)
