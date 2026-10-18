# Footage is indexed by camera and span, and a camera's Segments never overlap

Any instant on any camera must resolve to the Segment that holds it, for playback and for every piece of Evidence that is a span of Footage, and that index is a table every Deployment keeps (migration `0005_segments.sql`), so its shape outlives the code that writes it.

## Decision

The recorder indexes every Segment it finishes in the memory (`visionquery.memory.footage`), in a TimescaleDB hypertable partitioned by day on when the Segment begins (ADR 0006): its camera, its id, its span on the recorder's NTP-anchored clock (ADR 0017), its file relative to the Footage directory, why it ended, how many Fragments it holds, and whether NTP stood behind its times.

- **Keyed on the span, never the file name.** A Segment holds `[time_from, time_to)`: its start and not its end, where the next on the same connection begins.
  A lookup takes a camera and an instant and returns the Segment holding it and the offset, `instant - time_from`, or nothing.
- **A camera's Segments never overlap**, so an instant has at most one answer.
  TimescaleDB holds no exclusion constraint, so the index checks it under a per-camera advisory lock (two indexers at once cannot both add overlapping Segments), and a camera's starts are unique besides.
- **What does not fit is refused, never guessed at.** A Segment that overlaps an indexed one, that is already indexed with other facts, whose camera the registry never had (an id no camera can have included), or that cannot be Footage (a span that is empty, backwards or over a day; a path that is absolute, climbs out, holds a NUL or is not an `.mp4`; more Fragments than a column holds) is refused with an error naming why, before the database sees it, so it is never mistaken for the database failing and retried forever.
  Indexing the same Segment again with the same facts changes nothing.
  The recorder logs each refusal once as an ERROR, counts it (`visionquery_recorder_segments_refused`), and goes on with the rest; the Footage stays on disk.
- **A Segment is at most a day long**, so every lookup and overlap check reads two days' partitions, however much Footage there is.
- **The offset is on the Deployment's clock.** It equals the position in the file except where the stream stalled inside a Segment; playback maps it to the file's own timeline through the Segment's Fragment index (`.frags`), which stays on disk beside it.
- **The recorder never waits on the index.** Each camera queues what it finishes; one indexer, on a connection of its own as `visionquery_runtime` (ADR 0016), indexes the queue in order, retrying with backoff while the memory cannot be reached, and on start catches up from disk on everything from each camera's last indexed Segment on, recovered Segments included.
  Only a directory named as the recorder names a camera's (its id in ASCII digits, no leading zero) is read as that camera's Footage.

## Considered Options

Keying on the file name was rejected: names carry the window a Segment was filed under, not when it begins and ends, and a lookup is by instant.
Accepting overlapping Segments and answering with the latest one was rejected: two Segments claiming one instant on one camera means one of their clocks was wrong (a wall clock stepped across a restart, say), and choosing silently would let Evidence point at the wrong Footage.
Refusing makes the conflict visible instead; resolving it is an Operator's or a later tool's job.
Storing every Fragment in the database, to give the file offset directly, was rejected for now: it is a row per second per camera, and the `.frags` file already holds it beside the Footage it describes.
Absolute paths were rejected because each process mounts the Footage directory where it likes.

## Consequences

The time between two of a camera's Segments is time nothing was recorded, and why the one before it ended (`stream_ended`, `stopped`, `recovered`) says whether the camera or the recorder was away; Gaps (M1.5) are read from the index without changing it.
Expiring Footage is dropping partitions, together with its files.
A Segment refused for overlapping is on disk and not playable through the index until the conflict is settled.
