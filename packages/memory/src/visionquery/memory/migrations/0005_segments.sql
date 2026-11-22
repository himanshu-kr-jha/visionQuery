-- The Footage segment index (visionquery.memory.footage, ADR 0018): every finished Segment of
-- every camera, by the span of time it covers on the recorder's NTP-anchored clock (ADR 0017),
-- so that any instant on any camera resolves to a file and an offset in it.
--
-- A Segment covers [time_from, time_to): its start and not its end, where the next one on the
-- same connection begins. A Segment is at most a day long, which bounds every lookup to the
-- partitions of two days.
--
-- path is the Segment's file relative to the Footage directory, never absolute, because each
-- process mounts that directory where it likes. The index keeps no Footage, only where it is.
-- Why a Segment ended (reason), whether its times stand on a synchronised NTP reading
-- (verified), and by how many seconds the clock stepped just before it began (stepped_by, NULL
-- when it did not) are kept, as the recorder wrote them in its `.closed` file. indexed_order is
-- the order Segments were indexed in, which for one camera is the order they were recorded in.
--
-- A camera's Segments are kept after it is removed, as its row is: its Footage outlives it.
-- Partitioned by day on time_from (ADR 0006), so that expiring Footage is dropping partitions.
CREATE TABLE segments (
    camera_id     integer          NOT NULL REFERENCES cameras (id),
    segment_id    uuid             NOT NULL,
    time_from     timestamptz      NOT NULL,
    time_to       timestamptz      NOT NULL,
    path          text             NOT NULL,
    reason        text             NOT NULL
                  CHECK (reason IN ('rolled', 'stream_ended', 'stopped', 'recovered')),
    fragments     integer          NOT NULL CHECK (fragments > 0),
    verified      boolean          NOT NULL,
    stepped_by    double precision
                  CHECK (stepped_by <> 0 AND abs(stepped_by) < 'Infinity'::double precision),
    indexed_order bigint           NOT NULL GENERATED ALWAYS AS IDENTITY,
    CONSTRAINT segments_span
        CHECK (time_to > time_from AND time_to - time_from <= interval '1 day'),
    CONSTRAINT segments_path_is_relative_mp4
        CHECK (path ~ '^[^/]+(/[^/]+)*\.mp4$' AND path !~ '(^|/)\.\.?(/|$)')
);

SELECT create_hypertable('segments', by_range('time_from', interval '1 day'));

-- A lookup is by camera and instant; indexing a Segment again looks it up by its id, whatever
-- day it is in.
CREATE INDEX segments_camera_time ON segments (camera_id, time_from);
CREATE INDEX segments_segment_id ON segments (segment_id);

-- Every stretch of a camera's time that two of its Segments both hold, because the clock
-- stepped back between them (within the recorder's run, when the newer has stepped_by, or
-- across a restart), recorded when the newer is indexed: [time_from, time_to) is where the two
-- meet. Answers state it as they state a Gap. older_id is the Segment indexed first.
CREATE TABLE segment_overlaps (
    camera_id integer     NOT NULL REFERENCES cameras (id),
    time_from timestamptz NOT NULL,
    time_to   timestamptz NOT NULL,
    older_id  uuid        NOT NULL,
    newer_id  uuid        NOT NULL,
    CONSTRAINT segment_overlaps_span
        CHECK (time_to > time_from AND time_to - time_from <= interval '1 day'),
    CONSTRAINT segment_overlaps_of_two_segments CHECK (older_id <> newer_id),
    PRIMARY KEY (older_id, newer_id)
);

CREATE INDEX segment_overlaps_camera_time ON segment_overlaps (camera_id, time_from);
