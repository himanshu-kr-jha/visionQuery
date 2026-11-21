# Footage is timed by its arrival on the recorder's NTP-anchored clock, and kept as fragmented MP4 with its own index

Every statement about when something happened rests on Footage's timestamps, and Footage is the one thing a Deployment cannot regenerate, so how it is timed and how it is laid out on disk outlive any code that writes it.

## Decision

The recorder (`visionquery-recorder`) reads every Camera from MediaMTX over RTSP, as the `visionquery-reader` user (ADR 0015), through one ffmpeg per Camera that copies the video, never decoding it, into fragmented MP4 on a pipe.
The recorder itself cuts that stream into Segments, at Fragment (keyframe) boundaries: one Segment per epoch-aligned window of `VISIONQUERY_SEGMENT_SECONDS` (300 by default, and always a divisor of a day, so no Segment straddles midnight), rolling at the first Fragment that starts at or after its window's end.

- **Time.** A Segment's times are when its Fragments arrived at the recorder, on a timebase that reads the kernel's NTP-disciplined clock when the Segment opens and carries that reading forward with the monotonic clock, so a wall clock stepped under a running Segment moves nothing.
  A Segment begins at its first Fragment's arrival less that Fragment's own duration, read from the media (never before its connection opened or the Segment before it ended; a Segment rolled on the same connection begins exactly where the one before it ended), and ends at its last Fragment's arrival.
  Anchoring again can move the time: a slew (NTP's, at most 150 ms in five minutes) keeps Segments joined, but a larger shift is a step of the clock, and the old timeline's pins mean nothing on the new one.
  The Segment then begins where its own first Fragment puts it on the new timeline, is filed under that window, and the step is logged and counted, so the time between it and the Segment before is a gap or an overlap, never a span that claims time not recorded.
  A Segment whose span cannot be true (negative, or further outside its window than a Fragment and a stall) is refused when it is finished.
  Each Segment records whether a synchronised NTP reading stood behind every time it rests on (what it begins on and each Fragment's arrival; a reading carried forward counts for two hours); with none ever, it is still kept, timed by the wall clock, and says so.
- **Layout.** A Camera's Footage is `<footage>/<camera id>/<YYYY-MM-DD>/<HHMMSS>-<segment id>.mp4` (the UTC day and window it starts in), beside `.frags`, the Fragment index (one JSON line per Fragment: where it ends in the file, when it arrived, how long it lasts), and `.closed`, written once when the Segment is finished (why, its span, its Fragment count, whether NTP stood behind it).
  Each file's own timeline starts at zero: the only bytes changed from what the camera sent are each Fragment's decode time.
  Only video is kept.
- **Crashes.** A Fragment is fsynced before the index line that vouches for it, so a crash costs at most the Fragment being written; the next start, before recording, truncates every unfinished Segment to its last indexed Fragment and finishes it `recovered`.
  Unfinished Segments are found by a note kept for each Segment being written (`<camera>/.open/`), so a start costs what is unfinished and not every Segment ever written, and each is recovered by itself: one that cannot be (a damaged index, say) is logged as an ERROR naming it, counted (`visionquery_recorder_segments_quarantined`) and moved to `<footage>/.quarantine/`, so that it stops no Camera from recording and no later start meets it again.
  A Segment whose writing fails (a full disk) is finished `recovered` at once, from the Fragments it holds.
  One recorder holds a lock on the Footage directory for as long as it runs.

## Considered Options

The cameras' own timestamps (RTCP sender reports, ONVIF) were rejected: cameras' clocks are commonly wrong and drift apart, and Footage from different Cameras of one Deployment must agree to be followed across them.
Arrival on one disciplined clock costs the network's and the buffers' latency, well under a second on a camera network, which a later refinement can measure and subtract without changing anything stored.
MediaMTX's own recording, and ffmpeg's segment muxer, were rejected: neither tells the recorder each Fragment as it exists, so neither can date Segments by the recorder's clock or index them as they grow, and MediaMTX's names its files by its own clock.
Windows aligned to when recording began were rejected: every Camera's Segments would cut at different instants, and every restart would shift them.
Decoding, or demuxing in-process (PyAV), was rejected: copying costs no GPU and almost no CPU, and ffmpeg in a process of its own cannot take the recorder down with it.

## Consequences

Any instant on a Camera resolves to a Segment and, through its Fragment index, an offset in it, which is what the Footage segment index (M1.4) and playback build on.
Time between two Segments of a Camera is time nothing was recorded, which Gaps (M1.5) state; a stall inside one connection stays inside a Segment's span, which may then be longer than its media.
A stream that ends (a camera that stops publishing, or a restarted MediaMTX) may end inside its last frame, which is kept as it arrived: decoders conceal it.
The host must run an NTP client for Footage's times to be verified; the recorder exports whether they are (`visionquery_recorder_clock_verified`).
One process records every Camera, so its death stops all of them until Compose restarts it, when each recovers what it was writing.
