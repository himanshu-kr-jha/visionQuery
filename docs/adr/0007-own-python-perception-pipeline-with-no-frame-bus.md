# Our own Python perception pipeline, with no frame bus

Perception Workers lease a set of cameras, decode with NVDEC at a fixed sampled rate (default 8 fps), pass frames to Triton through CUDA shared memory batched across cameras, track per camera, embed only Keyframes, and write Tracks straight to Postgres.
Footage is recorded separately by a copy-only recorder in epoch-aligned segments with an NTP-anchored clock.

## Considered Options

DeepStream and Savant offer more raw throughput but make tracking, Keyframe selection and timestamps hard to customise and debug.
The predecessor's design (OpenCV software decode, JPEG frames over ZeroMQ PUB/SUB to every shard, unbatched per-frame Triton calls) wasted most of its bandwidth and GPU and ran at about 1 fps, too low to track people.
If profiling shows Python is the bottleneck, decode and preprocessing may move to native code inside the same worker contract.
