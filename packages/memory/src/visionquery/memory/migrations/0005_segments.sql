-- The Footage segment index (visionquery.memory.footage): every finished Segment of every
-- camera, by the span of time it covers on the recorder's NTP-anchored clock (ADR 0017), so
-- that any instant on any camera resolves to a file and an offset in it.
--
-- A Segment covers [time_from, time_to): its start and not its end, where the next one on the
-- same connection begins. One camera's Segments never overlap, so an instant is held by at most
-- one; TimescaleDB holds no exclusion constraint, so the index checks it under a per-camera
-- advisory lock, and a camera's starts are unique besides. A Segment is at most a day long,
-- which bounds every lookup to the partitions of two days.
--
-- path is the Segment's file relative to the Footage directory, never absolute, because each
-- process mounts that directory where it likes. The index keeps no Footage, only where it is.
-- Why a Segment ended (reason) and whether its times stand on a synchronised NTP reading
-- (verified) are kept, as the recorder wrote them in its `.closed` file.
--
-- A camera's Segments are kept after it is removed, as its row is: its Footage outlives it.
-- Partitioned by day on time_from (ADR 0006), so that expiring Footage is dropping partitions.
CREATE TABLE segments (
    camera_id  integer     NOT NULL REFERENCES cameras (id),
    segment_id uuid        NOT NULL,
    time_from  timestamptz NOT NULL,
    time_to    timestamptz NOT NULL,
    path       text        NOT NULL,
    reason     text        NOT NULL
                           CHECK (reason IN ('rolled', 'stream_ended', 'stopped', 'recovered')),
    fragments  integer     NOT NULL CHECK (fragments > 0),
    verified   boolean     NOT NULL,
    CONSTRAINT segments_span
        CHECK (time_to > time_from AND time_to - time_from <= interval '1 day'),
    CONSTRAINT segments_path_is_relative_mp4
        CHECK (path ~ '^[^/]+(/[^/]+)*\.mp4$' AND path !~ '(^|/)\.\.?(/|$)'),
    CONSTRAINT segments_one_start_per_camera UNIQUE (camera_id, time_from)
);

SELECT create_hypertable('segments', by_range('time_from', interval '1 day'));

-- Indexing a Segment again looks it up by its id, whatever day it is in.
CREATE INDEX segments_segment_id ON segments (segment_id);
