"""The Footage segment index: every finished Segment, by Camera and the span of time it covers.

Any instant on any Camera resolves to the Segment that holds it and how far into it the instant
is, or to nothing when no Segment does. Every test runs against a real Postgres with the shipped
migrations applied (see packages/conftest.py), in a database of its own.
"""

import math
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit
from uuid import uuid4

import psycopg
import pytest
from visionquery.memory import footage
from visionquery.memory.cameras import CameraRegistry
from visionquery.memory.connection import connect
from visionquery.memory.footage import (
    MAX_SPAN,
    IndexedSegment,
    Instant,
    InvalidSegmentError,
    Overlap,
    SegmentConflictError,
    SegmentIndex,
    SegmentIndexError,
    SegmentRefusedError,
    UnknownCameraError,
)
from visionquery.memory.migrate import migrate
from visionquery.memory.roles import RUNTIME, enable_logins

AT = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
FIVE_MINUTES = timedelta(minutes=5)


@pytest.fixture
def migrated(database: str) -> str:
    migrate(database)
    return database


@pytest.fixture
def connection(migrated: str) -> Iterator[psycopg.Connection]:
    with connect(migrated, 5) as connection:
        yield connection


@pytest.fixture
def index(connection: psycopg.Connection) -> SegmentIndex:
    return SegmentIndex(connection)


@pytest.fixture
def cameras(connection: psycopg.Connection) -> tuple[int, int]:
    registry = CameraRegistry(connection)
    return registry.add_pushed("Dock", "dock").id, registry.add_pushed("Gate 2", "gate").id


def segment(
    camera_id: int,
    time_from: datetime = AT,
    length: timedelta = FIVE_MINUTES,
    **changes: object,
) -> IndexedSegment:
    """A Segment of `camera_id` from `time_from`, filed as the recorder files it."""
    segment_id = uuid4()
    day = time_from.strftime("%Y-%m-%d")
    made = IndexedSegment(
        camera_id=camera_id,
        segment_id=segment_id,
        time_from=time_from,
        time_to=time_from + length,
        path=PurePosixPath(f"{camera_id}/{day}/{time_from:%H%M%S}-{segment_id.hex}.mp4"),
        reason="rolled",
        fragments=150,
        verified=True,
    )
    return replace(made, **changes)  # type: ignore[arg-type]


def rows(connection: psycopg.Connection, table: str = "segments") -> int:
    found = connection.execute(f"SELECT count(*) FROM {table}").fetchone()  # type: ignore[arg-type]
    assert found is not None
    return found[0]


def at(index: SegmentIndex, camera_id: int, instant: datetime) -> IndexedSegment | None:
    """The Segment an instant of camera `camera_id` resolves to, if any."""
    found = index.find(camera_id, instant)
    return None if found is None else found.segment


# --- the table --------------------------------------------------------------------------------


def test_segments_are_kept_in_a_table_partitioned_by_day_on_when_they_begin(
    connection: psycopg.Connection,
) -> None:
    dimensions = connection.execute(
        "SELECT column_name, time_interval FROM timescaledb_information.dimensions"
        " WHERE hypertable_name = 'segments'"
    ).fetchall()
    assert dimensions == [("time_from", timedelta(days=1))]


def test_segments_of_different_days_land_in_different_partitions(
    index: SegmentIndex, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    index.index(segment(dock, AT))
    index.index(segment(dock, AT + timedelta(days=1)))
    chunks = connection.execute(
        "SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'segments'"
    ).fetchone()
    assert chunks == (2,)


def test_a_segment_is_indexed_with_its_camera_span_and_path(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    made = segment(dock, reason="stream_ended", fragments=7, verified=False)

    assert index.index(made) is True

    assert index.segment_ids(dock) == {made.segment_id}
    found = index.find(dock, AT + timedelta(minutes=2))
    assert found is not None
    assert found.segment == made
    assert found.segment.path == made.path


def test_every_reason_a_segment_ends_is_kept(index: SegmentIndex, cameras: tuple[int, int]) -> None:
    dock, _ = cameras
    for n, reason in enumerate(("rolled", "stream_ended", "stopped", "recovered")):
        made = segment(dock, AT + n * FIVE_MINUTES, reason=reason)
        index.index(made)
        assert at(index, dock, made.time_from) == made


def test_by_how_much_the_clock_stepped_before_a_segment_is_kept(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    stepped = segment(dock, AT, stepped_by=-42.125)
    plain = segment(dock, AT + FIVE_MINUTES)
    index.index(stepped)
    index.index(plain)

    assert at(index, dock, AT) == stepped
    assert at(index, dock, plain.time_from) == plain
    assert plain.stepped_by is None


# --- finding an instant -----------------------------------------------------------------------


def test_an_instant_resolves_to_the_segment_holding_it_and_how_far_into_it(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    first = segment(dock, AT)
    second = segment(dock, AT + FIVE_MINUTES)
    index.index(first)
    index.index(second)

    assert index.find(dock, AT) == Instant(first, timedelta(0))
    assert index.find(dock, AT + timedelta(seconds=90.25)) == Instant(
        first, timedelta(seconds=90.25)
    )
    # A Segment covers its start and not its end, where the next one begins.
    assert index.find(dock, AT + FIVE_MINUTES - timedelta(microseconds=1)) == Instant(
        first, FIVE_MINUTES - timedelta(microseconds=1)
    )
    assert index.find(dock, AT + FIVE_MINUTES) == Instant(second, timedelta(0))


def test_an_instant_no_segment_covers_resolves_to_nothing(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, gate = cameras
    index.index(segment(dock, AT))
    index.index(segment(dock, AT + timedelta(minutes=10)))

    # Before the first, between two (the camera was away), after the last, on another camera.
    assert index.find(dock, AT - timedelta(microseconds=1)) is None
    assert index.find(dock, AT + timedelta(minutes=7)) is None
    assert index.find(dock, AT + timedelta(minutes=15)) is None
    assert index.find(gate, AT + timedelta(minutes=1)) is None
    assert index.find(9999, AT + timedelta(minutes=1)) is None


def test_an_instant_finds_a_segment_that_began_on_the_day_before(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    # A rolled Segment begins where the one before it ended, which may be before midnight,
    # so it is kept in the day before's partition while it holds instants of the next day.
    dock, _ = cameras
    midnight = datetime(2026, 10, 7, tzinfo=UTC)
    made = segment(dock, midnight - timedelta(milliseconds=400))
    index.index(made)

    found = index.find(dock, midnight + timedelta(minutes=1))
    assert found == Instant(made, timedelta(minutes=1, milliseconds=400))


def test_an_instant_finds_the_longest_segment_the_index_accepts(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    made = segment(dock, AT, MAX_SPAN)
    index.index(made)

    end = AT + MAX_SPAN - timedelta(microseconds=1)
    assert index.find(dock, end) == Instant(made, MAX_SPAN - timedelta(microseconds=1))


def test_an_instant_is_found_whatever_zone_it_is_told_in(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    made = segment(dock, AT)
    index.index(made)

    found = index.find(dock, (AT + timedelta(minutes=1)).astimezone())
    assert found == Instant(made, timedelta(minutes=1))
    assert found is not None
    assert found.segment.time_from.tzinfo is not None


def test_an_instant_without_a_zone_is_refused(index: SegmentIndex) -> None:
    with pytest.raises(ValueError, match="time zone"):
        index.find(1, datetime(2026, 10, 6, 10, 0))


def test_a_removed_cameras_footage_is_still_found(
    index: SegmentIndex, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    made = segment(dock)
    index.index(made)
    CameraRegistry(connection).remove(dock)

    assert index.find(dock, AT) == Instant(made, timedelta(0))
    # A Segment the recorder finished as the camera was being removed is indexed all the same.
    later = segment(dock, AT + FIVE_MINUTES, reason="stopped")
    assert index.index(later) is True


def test_the_ids_of_a_cameras_segments_are_every_one_indexed_whenever_it_begins(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    """What a catch-up compares the Segments on disk with, whatever their times say."""
    dock, gate = cameras
    assert index.segment_ids(dock) == frozenset()
    made = [segment(dock, AT + timedelta(days=days)) for days in (-30, 0, 2)]
    for each in made:
        index.index(each)
    index.index(segment(gate, AT))

    assert index.segment_ids(dock) == {each.segment_id for each in made}


# --- indexing again ---------------------------------------------------------------------------


def test_indexing_the_same_segment_again_changes_nothing(
    index: SegmentIndex, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    made = segment(dock)

    assert index.index(made) is True
    assert index.index(made) is False
    assert index.index(made) is False

    assert rows(connection) == 1
    assert index.find(dock, AT) == Instant(made, timedelta(0))


def test_a_segment_indexed_again_with_other_facts_is_refused_and_the_first_kept(
    index: SegmentIndex, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    dock, gate = cameras
    made = segment(dock)
    index.index(made)

    for changed in (
        replace(made, time_to=made.time_to + timedelta(seconds=1)),
        replace(made, time_from=made.time_from + timedelta(seconds=1)),
        replace(made, reason="recovered"),
        replace(made, fragments=3),
        replace(made, verified=False),
        replace(made, path=PurePosixPath(f"{dock}/elsewhere.mp4")),
        replace(made, camera_id=gate),
        replace(made, stepped_by=-1.5),
    ):
        with pytest.raises(SegmentConflictError, match=str(made.segment_id)):
            index.index(changed)

    assert rows(connection) == 1
    assert at(index, dock, AT) == made


# --- overlaps ---------------------------------------------------------------------------------


def test_segments_overlapping_another_of_their_camera_are_indexed_and_each_overlap_recorded(
    index: SegmentIndex, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    """A clock that stepped back, within a recorder's run or across a restart, gives a camera
    two Segments claiming the same instants: both are Footage, so both are kept."""
    dock, _ = cameras
    first = segment(dock, AT)
    index.index(first)

    for overlapping, (begins, ends) in (
        # the same start; starting inside it; ending inside it; inside it; around it
        (segment(dock, AT), (AT, AT + FIVE_MINUTES)),
        (segment(dock, AT + timedelta(minutes=4)), (AT + timedelta(minutes=4), first.time_to)),
        (segment(dock, AT - timedelta(minutes=4)), (AT, AT + timedelta(minutes=1))),
        (
            segment(dock, AT + timedelta(minutes=1), timedelta(minutes=1)),
            (AT + timedelta(minutes=1), AT + timedelta(minutes=2)),
        ),
        (segment(dock, AT - timedelta(minutes=1), timedelta(minutes=10)), (AT, first.time_to)),
    ):
        assert index.index(overlapping) is True
        with_first = [o for o in index.overlaps(dock, AT, first.time_to) if o.older == first]
        assert Overlap(dock, begins, ends, older=first, newer=overlapping) in with_first

    assert rows(connection) == 6
    # Each pair of the six that shares an instant is one Overlap: all fifteen but the three
    # that do not (starting inside it against ending inside it, and each of those against the
    # one inside it).
    assert rows(connection, "segment_overlaps") == 12


def test_touching_is_not_overlapping_and_another_cameras_footage_is_its_own(
    index: SegmentIndex, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    dock, gate = cameras
    index.index(segment(dock, AT))

    index.index(segment(dock, AT + FIVE_MINUTES))
    index.index(segment(dock, AT - FIVE_MINUTES))
    index.index(segment(gate, AT))

    assert rows(connection, "segment_overlaps") == 0
    assert index.overlaps(dock, AT - FIVE_MINUTES, AT + 2 * FIVE_MINUTES) == []


@pytest.mark.parametrize(
    ("older_verified", "newer_verified", "resolves_to"),
    [
        pytest.param(True, False, "older", id="only-the-older-verified"),
        pytest.param(False, True, "newer", id="only-the-newer-verified"),
        pytest.param(True, True, "newer", id="both-verified"),
        pytest.param(False, False, "newer", id="neither-verified"),
    ],
)
def test_an_instant_in_an_overlap_resolves_to_the_only_verified_segment_else_the_newer(
    index: SegmentIndex,
    cameras: tuple[int, int],
    older_verified: bool,
    newer_verified: bool,
    resolves_to: str,
) -> None:
    dock, _ = cameras
    older = segment(dock, AT, verified=older_verified)
    newer = segment(dock, AT + timedelta(minutes=3), verified=newer_verified, stepped_by=-120.0)
    index.index(older)
    index.index(newer)
    inside = AT + timedelta(minutes=4)
    chosen = {"older": older, "newer": newer}[resolves_to]

    found = index.find(dock, inside)

    overlap = Overlap(dock, newer.time_from, older.time_to, older=older, newer=newer)
    assert found == Instant(chosen, inside - chosen.time_from, overlaps=(overlap,))
    # Outside the overlap each Segment holds its instants alone, and says so.
    assert index.find(dock, AT + timedelta(minutes=1)) == Instant(older, timedelta(minutes=1))
    assert index.find(dock, older.time_to) == Instant(newer, timedelta(minutes=2))


def test_the_newer_of_two_overlapping_segments_is_the_one_indexed_later_wherever_it_begins(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    """After a step back further than a Segment, the Segment recorded later begins earlier on
    the clock; it is still the newer, because the recorder indexes in the order it records."""
    dock, _ = cameras
    older = segment(dock, AT, timedelta(minutes=5))
    newer = segment(dock, AT - timedelta(minutes=8), timedelta(minutes=10), stepped_by=-480.0)
    index.index(older)
    index.index(newer)

    found = index.find(dock, AT + timedelta(minutes=1))

    assert found is not None
    assert found.segment == newer
    assert found.overlaps == (
        Overlap(dock, AT, AT + timedelta(minutes=2), older=older, newer=newer),
    )


def test_an_overlap_with_no_step_recorded_before_it_is_recorded_alike(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    """A clock corrected while the recorder was down leaves no `stepped_by`: the Segment after
    the restart overlaps the one before it all the same."""
    dock, _ = cameras
    before = segment(dock, AT, reason="stopped")
    after = segment(dock, AT + timedelta(minutes=4), reason="rolled")
    index.index(before)
    index.index(after)

    (overlap,) = index.overlaps(dock, AT, AT + timedelta(hours=1))

    assert overlap == Overlap(dock, after.time_from, before.time_to, older=before, newer=after)
    assert (overlap.older.stepped_by, overlap.newer.stepped_by) == (None, None)


def test_an_instant_held_by_three_segments_resolves_to_the_newest_verified_one(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    first = segment(dock, AT, verified=True)
    second = segment(dock, AT + timedelta(minutes=1), verified=True)
    third = segment(dock, AT + timedelta(minutes=2), verified=False)
    for each in (first, second, third):
        index.index(each)
    inside = AT + timedelta(minutes=3)

    found = index.find(dock, inside)

    assert found is not None
    assert found.segment == second
    assert {(o.older, o.newer) for o in found.overlaps} == {
        (first, second),
        (first, third),
        (second, third),
    }


def test_the_overlaps_of_a_window_are_every_one_meeting_it_and_only_those(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    """What an Answer reads, like a Gap, for the camera and the stretch of time it is about."""
    dock, gate = cameras
    a = segment(dock, AT)
    b = segment(dock, AT + timedelta(minutes=4))  # overlaps a by a minute
    c = segment(dock, AT + timedelta(days=1))
    d = segment(dock, AT + timedelta(days=1, minutes=2))  # overlaps c by three minutes
    for each in (a, b, c, d, segment(gate, AT), segment(gate, AT + timedelta(minutes=1))):
        index.index(each)
    first = Overlap(dock, b.time_from, a.time_to, older=a, newer=b)
    second = Overlap(dock, d.time_from, c.time_to, older=c, newer=d)

    assert index.overlaps(dock, AT, AT + timedelta(days=2)) == [first, second]
    assert index.overlaps(dock, a.time_to - timedelta(microseconds=1), a.time_to) == [first]
    # It holds its start and not its end, as a Segment does.
    assert index.overlaps(dock, a.time_to, c.time_from) == []
    assert index.overlaps(dock, AT, b.time_from) == []
    assert index.overlaps(dock, d.time_from, d.time_from + timedelta(seconds=1)) == [second]
    assert len(index.overlaps(gate, AT, AT + timedelta(hours=1))) == 1
    assert index.overlaps(9999, AT, AT + timedelta(days=2)) == []


def test_indexing_an_overlapping_segment_again_records_its_overlap_once(
    index: SegmentIndex, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    older, newer = segment(dock, AT), segment(dock, AT + timedelta(minutes=2))
    index.index(older)
    assert index.index(newer) is True

    assert index.index(newer) is False
    assert index.index(older) is False

    assert rows(connection, "segment_overlaps") == 1
    (overlap,) = index.overlaps(dock, AT, AT + FIVE_MINUTES)
    assert (overlap.older, overlap.newer) == (older, newer)


def test_an_overlap_with_a_segment_that_began_the_day_before_is_found(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    midnight = datetime(2026, 10, 7, tzinfo=UTC)
    long_ago = segment(dock, midnight - MAX_SPAN + timedelta(minutes=1), MAX_SPAN)
    after_midnight = segment(dock, midnight - timedelta(minutes=5), FIVE_MINUTES * 2)
    index.index(long_ago)
    index.index(after_midnight)

    found = index.find(dock, midnight)

    assert found is not None
    assert found.segment == after_midnight
    assert [(o.older, o.newer) for o in found.overlaps] == [(long_ago, after_midnight)]


# --- what is refused --------------------------------------------------------------------------


def test_a_segment_of_a_camera_the_registry_never_had_is_refused(
    index: SegmentIndex, connection: psycopg.Connection
) -> None:
    with pytest.raises(UnknownCameraError, match="camera 4242"):
        index.index(segment(4242))
    assert rows(connection) == 0


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        pytest.param({"time_to": AT}, "ends", id="ends-where-it-begins"),
        pytest.param({"time_to": AT - timedelta(seconds=1)}, "ends", id="ends-before-it-begins"),
        pytest.param(
            {"time_to": AT + MAX_SPAN + timedelta(microseconds=1)}, "longer", id="too-long"
        ),
        pytest.param(
            {"time_from": datetime(2026, 10, 6, 10, 0)},
            "time zone",
            id="begins-without-a-zone",
        ),
        pytest.param(
            {"time_to": datetime(2026, 10, 6, 10, 5)},
            "time zone",
            id="ends-without-a-zone",
        ),
        pytest.param({"fragments": 0}, "Fragment", id="no-fragment"),
        pytest.param({"fragments": 2**31}, "Fragment", id="more-fragments-than-a-column-holds"),
        pytest.param({"reason": "exploded"}, "reason", id="unknown-reason"),
        pytest.param({"stepped_by": math.nan}, "stepped", id="stepped-by-nan"),
        pytest.param({"stepped_by": math.inf}, "stepped", id="stepped-by-infinity"),
        pytest.param({"stepped_by": 0.0}, "stepped", id="stepped-by-nothing"),
        pytest.param({"stepped_by": True}, "stepped", id="stepped-by-a-bool"),
        pytest.param({"path": PurePosixPath("/srv/footage/1/a.mp4")}, "path", id="absolute-path"),
        pytest.param({"path": PurePosixPath("1/../../etc/a.mp4")}, "path", id="path-climbs-out"),
        pytest.param({"path": PurePosixPath("1/2026-10-06/a.frags")}, "path", id="not-mp4"),
        pytest.param({"path": PurePosixPath(".")}, "path", id="empty-path"),
        pytest.param({"path": PurePosixPath("1/a\x00.mp4")}, "path", id="nul-in-path"),
        pytest.param(
            {"path": PurePosixPath("1/a\udcff.mp4")}, "path", id="path-not-utf-8"
        ),  # as Python names a file whose name is not UTF-8
    ],
)
def test_a_segment_that_cannot_be_footage_is_refused_before_the_database_sees_it(
    index: SegmentIndex,
    connection: psycopg.Connection,
    cameras: tuple[int, int],
    changes: dict[str, object],
    says: str,
) -> None:
    dock, _ = cameras
    with pytest.raises(InvalidSegmentError, match=says) as refused:
        index.index(replace(segment(dock), **changes))
    assert isinstance(refused.value, SegmentRefusedError)
    assert isinstance(refused.value, ValueError)
    assert rows(connection) == 0


def test_the_database_refuses_what_the_index_would_refuse(
    connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    """The table holds the same rules, for whatever writes to it without the index."""
    dock, _ = cameras
    insert = (
        "INSERT INTO segments (camera_id, segment_id, time_from, time_to, path, reason,"
        " fragments, verified) VALUES (%s, %s, %s, %s, %s, %s, %s, true)"
    )
    good = (dock, uuid4(), AT, AT + FIVE_MINUTES, f"{dock}/a.mp4", "rolled", 1)
    for bad in (
        (*good[:3], AT, *good[4:]),
        (*good[:3], AT + MAX_SPAN + timedelta(microseconds=1), *good[4:]),
        (*good[:4], "/abs/a.mp4", *good[5:]),
        (*good[:4], f"{dock}/../a.mp4", *good[5:]),
        (*good[:4], f"{dock}/a.frags", *good[5:]),
        (*good[:5], "exploded", *good[6:]),
        (*good[:6], 0),
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(insert, bad)
    for stepped_by in ("NaN", "Infinity", "-Infinity", "0"):
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO segments (camera_id, segment_id, time_from, time_to, path, reason,"
                " fragments, verified, stepped_by)"
                " VALUES (%s, %s, %s, %s, %s, 'rolled', 1, true, %s::float8)",
                (dock, uuid4(), AT, AT + FIVE_MINUTES, f"{dock}/a.mp4", stepped_by),
            )
    connection.execute(insert, good)
    overlap = (
        "INSERT INTO segment_overlaps (camera_id, time_from, time_to, older_id, newer_id)"
        " VALUES (%s, %s, %s, %s, %s)"
    )
    one, other = uuid4(), uuid4()
    for bad in (
        (dock, AT, AT, one, other),
        (dock, AT, AT + MAX_SPAN + timedelta(microseconds=1), one, other),
        (dock, AT, AT + FIVE_MINUTES, one, one),
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(overlap, bad)
    connection.execute(overlap, (dock, AT, AT + FIVE_MINUTES, one, other))
    with pytest.raises(psycopg.errors.UniqueViolation):
        connection.execute(overlap, (dock, AT, AT + FIVE_MINUTES, one, other))


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"reason": "exploded"}, id="check-violation"),
        pytest.param({"fragments": 2**31}, id="out-of-range"),
    ],
)
def test_a_segment_the_database_refuses_is_refused_for_good_not_a_failure(
    index: SegmentIndex,
    connection: psycopg.Connection,
    cameras: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
) -> None:
    """Whatever the index's own checks miss, the table refuses; that is a refusal, which no
    retry changes, never a database failure an indexer would retry forever."""
    dock, _ = cameras

    def check_nothing(segment: IndexedSegment) -> None:
        pass

    monkeypatch.setattr(footage, "_check", check_nothing)
    with pytest.raises(InvalidSegmentError, match="the database refused") as refused:
        index.index(replace(segment(dock), **changes))
    assert "exploded" not in str(refused.value)
    assert refused.value.__context__ is None
    assert rows(connection) == 0


def test_a_database_that_fails_is_named_by_the_class_of_its_error_only(
    database: str, server_roles: None
) -> None:
    # Not migrated: the table is missing.
    with connect(database, 5) as connection:
        with pytest.raises(SegmentIndexError, match="UndefinedTable") as failed:
            SegmentIndex(connection).find(1, AT)
        assert "segments" not in str(failed.value)
        with pytest.raises(SegmentIndexError, match="UndefinedTable"):
            SegmentIndex(connection).index(segment(1))
        with pytest.raises(SegmentIndexError, match="UndefinedTable"):
            SegmentIndex(connection).segment_ids(1)
        with pytest.raises(SegmentIndexError, match="UndefinedTable"):
            SegmentIndex(connection).overlaps(1, AT, AT + FIVE_MINUTES)
    assert not isinstance(SegmentIndexError("x"), SegmentRefusedError)


@pytest.mark.parametrize("camera_id", [0, -1, 2**31, -(2**31) - 1])
def test_a_camera_id_no_camera_can_have_is_refused_and_has_no_footage(
    index: SegmentIndex, connection: psycopg.Connection, camera_id: int
) -> None:
    """An id the registry could never give is refused for good, never a database failure that
    an indexer would retry forever."""
    with pytest.raises(UnknownCameraError, match=f"no camera {camera_id}"):
        index.index(segment(camera_id))
    assert index.find(camera_id, AT) is None
    assert index.segment_ids(camera_id) == frozenset()
    assert index.overlaps(camera_id, AT, AT + FIVE_MINUTES) == []
    assert rows(connection) == 0


# --- two at once ------------------------------------------------------------------------------


def _blocked_on_an_advisory_lock(admin: psycopg.Connection, within: float) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        waiting = admin.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        ).fetchone()
        if waiting is not None and waiting[0] > 0:
            return True
        time.sleep(0.02)
    return False


def test_two_indexers_at_once_adding_overlapping_segments_record_their_overlap_once(
    migrated: str, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    """The second waits for the first to commit, then sees its Segment and records the
    Overlap; neither can miss the other's."""
    dock, _ = cameras
    first, second = segment(dock, AT), segment(dock, AT + timedelta(minutes=1))
    outcome: list[BaseException | bool] = []

    def index_second(other: psycopg.Connection) -> None:
        try:
            outcome.append(SegmentIndex(other).index(second))
        except BaseException as error:
            outcome.append(error)

    with connect(migrated, 5) as one, connect(migrated, 5) as other:
        thread = threading.Thread(target=index_second, args=(other,))
        try:
            with one.transaction():
                assert SegmentIndex(one).index(first) is True
                thread.start()
                assert _blocked_on_an_advisory_lock(connection, within=10), (
                    f"the second indexer did not wait for the first: {outcome}"
                )
            # The first has committed (or, failing, rolled back); the second goes on.
        finally:
            if thread.is_alive() or thread.ident is not None:
                thread.join(timeout=30)
        assert not thread.is_alive()

    assert outcome == [True]
    assert rows(connection) == 2
    index = SegmentIndex(connection)
    assert index.overlaps(dock, AT, AT + FIVE_MINUTES) == [
        Overlap(dock, second.time_from, first.time_to, older=first, newer=second)
    ]


def test_two_indexers_indexing_one_segment_at_once_keep_one_row(
    migrated: str, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    made = segment(dock)
    outcome: list[BaseException | bool] = []
    start = threading.Barrier(4)

    def index_it() -> None:
        try:
            with connect(migrated, 5) as mine:
                start.wait(timeout=30)
                outcome.append(SegmentIndex(mine).index(made))
        except BaseException as error:
            outcome.append(error)

    threads = [threading.Thread(target=index_it) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    assert sorted(outcome, key=str) == [False, False, False, True], outcome
    assert rows(connection) == 1


# --- who may use it ---------------------------------------------------------------------------


def _as(database: str, role: str, password: str) -> str:
    parts = urlsplit(database)
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{parts.hostname}:{parts.port}"
    return parts._replace(netloc=netloc).geturl()


def test_the_runtime_role_indexes_and_finds_segments_and_can_do_nothing_more(
    migrated: str, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    enable_logins(migrated, {RUNTIME: "runtime-password"})
    made = segment(dock)
    with connect(_as(migrated, RUNTIME, "runtime-password"), 5) as runtime:
        index = SegmentIndex(runtime)
        assert index.index(made) is True
        assert index.index(made) is False
        assert index.find(dock, AT) == Instant(made, timedelta(0))
        assert index.segment_ids(dock) == {made.segment_id}
        newer = segment(dock, AT + timedelta(minutes=1))
        assert index.index(newer) is True
        assert len(index.overlaps(dock, AT, AT + FIVE_MINUTES)) == 1
        for table in ("segments", "segment_overlaps"):
            held = runtime.execute(
                "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',"
                "'REFERENCES','TRIGGER']) AS p"
                " WHERE has_table_privilege(current_user, %s, p) ORDER BY p",
                (table,),
            ).fetchall()
            assert held == [("DELETE",), ("INSERT",), ("SELECT",), ("UPDATE",)], table
        for statement in (
            b"TRUNCATE segments",
            b"TRUNCATE segment_overlaps",
            b"DROP TABLE segments",
            b"DROP TABLE segment_overlaps",
            b"ALTER TABLE segments ADD COLUMN x int",
            b"SELECT drop_chunks('segments', older_than => now())",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), runtime.transaction():
                runtime.execute(statement)


def test_migrating_a_memory_holding_segments_again_changes_nothing(
    migrated: str, index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    index.index(segment(dock, AT))
    index.index(segment(dock, AT + timedelta(days=3)))

    assert migrate(migrated) == ()
