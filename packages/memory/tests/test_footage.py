"""The Footage segment index: every finished Segment, by Camera and the span of time it covers.

Any instant on any Camera resolves to the Segment that holds it and how far into it the instant
is, or to nothing when no Segment does. Every test runs against a real Postgres with the shipped
migrations applied (see packages/conftest.py), in a database of its own.
"""

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
from visionquery.memory.cameras import CameraRegistry
from visionquery.memory.connection import connect
from visionquery.memory.footage import (
    MAX_SPAN,
    IndexedSegment,
    Instant,
    InvalidSegmentError,
    SegmentConflictError,
    SegmentIndex,
    SegmentIndexError,
    SegmentOverlapError,
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


def rows(connection: psycopg.Connection) -> int:
    found = connection.execute("SELECT count(*) FROM segments").fetchone()
    assert found is not None
    return found[0]


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

    assert index.latest(dock) == made
    found = index.find(dock, AT + timedelta(minutes=2))
    assert found is not None
    assert found.segment == made
    assert found.segment.path == made.path


def test_every_reason_a_segment_ends_is_kept(index: SegmentIndex, cameras: tuple[int, int]) -> None:
    dock, _ = cameras
    for n, reason in enumerate(("rolled", "stream_ended", "stopped", "recovered")):
        made = segment(dock, AT + n * FIVE_MINUTES, reason=reason)
        index.index(made)
        assert index.latest(dock) == made


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


def test_a_camera_with_no_segment_has_no_latest(
    index: SegmentIndex, cameras: tuple[int, int]
) -> None:
    dock, _ = cameras
    assert index.latest(dock) is None


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
    ):
        with pytest.raises(SegmentConflictError, match=str(made.segment_id)):
            index.index(changed)

    assert rows(connection) == 1
    assert index.latest(dock) == made


# --- what is refused --------------------------------------------------------------------------


def test_a_segment_overlapping_another_of_its_camera_is_refused(
    index: SegmentIndex, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    dock, gate = cameras
    first = segment(dock, AT)
    index.index(first)

    for overlapping in (
        segment(dock, AT),  # the same start
        segment(dock, AT + timedelta(minutes=4)),  # starting inside it
        segment(dock, AT - timedelta(minutes=4)),  # ending inside it
        segment(dock, AT + timedelta(minutes=1), timedelta(minutes=1)),  # inside it
        segment(dock, AT - timedelta(minutes=1), timedelta(minutes=10)),  # around it
    ):
        with pytest.raises(SegmentOverlapError) as refused:
            index.index(overlapping)
        assert str(first.segment_id) in str(refused.value)
        assert str(overlapping.segment_id) in str(refused.value)

    assert rows(connection) == 1
    # Touching is not overlapping, and another camera's Footage is its own.
    assert index.index(segment(dock, AT + FIVE_MINUTES)) is True
    assert index.index(segment(dock, AT - FIVE_MINUTES)) is True
    assert index.index(segment(gate, AT)) is True


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
        pytest.param({"reason": "exploded"}, "reason", id="unknown-reason"),
        pytest.param({"path": PurePosixPath("/srv/footage/1/a.mp4")}, "path", id="absolute-path"),
        pytest.param({"path": PurePosixPath("1/../../etc/a.mp4")}, "path", id="path-climbs-out"),
        pytest.param({"path": PurePosixPath("1/2026-10-06/a.frags")}, "path", id="not-mp4"),
        pytest.param({"path": PurePosixPath(".")}, "path", id="empty-path"),
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
    connection.execute(insert, good)


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
            SegmentIndex(connection).latest(1)
    assert not isinstance(SegmentIndexError("x"), SegmentRefusedError)


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


def test_two_indexers_at_once_cannot_both_add_overlapping_segments(
    migrated: str, connection: psycopg.Connection, cameras: tuple[int, int]
) -> None:
    """The second waits for the first to commit, then sees its Segment and is refused."""
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

    assert len(outcome) == 1
    assert isinstance(outcome[0], SegmentOverlapError), outcome
    assert rows(connection) == 1


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
        assert index.latest(dock) == made
        held = runtime.execute(
            "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',"
            "'REFERENCES','TRIGGER']) AS p"
            " WHERE has_table_privilege(current_user, 'segments', p) ORDER BY p"
        ).fetchall()
        assert held == [("DELETE",), ("INSERT",), ("SELECT",), ("UPDATE",)]
        for statement in (
            b"TRUNCATE segments",
            b"DROP TABLE segments",
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
