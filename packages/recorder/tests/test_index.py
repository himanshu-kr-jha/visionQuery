"""The recorder indexes every finished Segment in the memory, and catches up on what it missed.

Segments are written to disk as the recorder writes them (`SegmentFile`, with Fragments built
by hand in boxes.py), and indexed in a real Postgres (packages/conftest.py), so that the
memory's own rules (its partitions, its refusals, its roles) are the ones met.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest
from boxes import fragments, parsed_init
from prometheus_client import REGISTRY
from psycopg import sql
from pydantic import SecretStr
from visionquery.memory.cameras import CameraRegistry
from visionquery.memory.connection import connect
from visionquery.memory.footage import IndexedSegment, SegmentIndex
from visionquery.memory.migrate import migrate
from visionquery.memory.settings import DatabaseSettings
from visionquery.recorder import index as index_module
from visionquery.recorder.index import Indexer
from visionquery.recorder.segment import Segment, SegmentFile, recover, segments, window

AT = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
#: Waits short enough for a test, as the recorder's are for a Deployment.
FIRST_WAIT, LAST_WAIT = 0.1, 0.5
#: What the indexer may take, beyond the wait it is asked for, on a busy machine.
SLACK = 15.0


@pytest.fixture
def migrated(database: str) -> str:
    migrate(database)
    return database


@pytest.fixture
def memory(migrated: str) -> Iterator[psycopg.Connection]:
    with connect(migrated, 5) as connection:
        yield connection


@pytest.fixture
def cameras(memory: psycopg.Connection) -> tuple[int, int]:
    registry = CameraRegistry(memory)
    return registry.add_pushed("Dock", "dock").id, registry.add_pushed("Gate 2", "gate").id


def write(footage: Path, camera_id: int, at: datetime, count: int = 3) -> SegmentFile:
    """A Segment of `count` two-second Fragments, the first beginning at `at`, left open."""
    start, _ = window(at, timedelta(minutes=5))
    segment = SegmentFile.open(
        footage / str(camera_id), parsed_init(), window_start=start, not_before=at, verified=True
    )
    for n, fragment in enumerate(fragments(count)):
        segment.append(fragment, arrived_at=at + timedelta(seconds=2 * (n + 1)))
    return segment


def finish(footage: Path, camera_id: int, at: datetime, count: int = 3) -> Segment:
    done = write(footage, camera_id, at, count).close("rolled")
    assert done is not None
    return done


def indexed(footage: Path, camera_id: int, segment: Segment) -> IndexedSegment:
    """How the index should hold `segment`, filed under `footage`."""
    return IndexedSegment(
        camera_id=camera_id,
        segment_id=segment.segment_id,
        time_from=segment.time_from,
        time_to=segment.time_to,
        path=PurePosixPath(segment.path.relative_to(footage).as_posix()),
        reason=segment.reason,
        fragments=segment.fragments,
        verified=segment.verified,
    )


def held(memory: psycopg.Connection) -> int:
    found = memory.execute("SELECT count(*) FROM segments").fetchone()
    assert found is not None
    return found[0]


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Running:
    """An `Indexer`, run in an event loop of its own on a thread until the test stops it."""

    def __init__(self, indexer: Indexer) -> None:
        self.indexer = indexer
        self.failure: BaseException | None = None
        self.returned_at: float | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._loop.run_until_complete(self.indexer.run())
        except BaseException as error:
            self.failure = error
        finally:
            self.returned_at = time.monotonic()

    def finished(self, camera_id: int, segment: Segment) -> None:
        """Tell the indexer a Segment was finished, as the recorder's cameras do, in its loop."""
        self._loop.call_soon_threadsafe(self.indexer.finished, camera_id, segment)

    def stop(self, timeout: float = 60) -> None:
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self.indexer.stop)
            self._thread.join(timeout=timeout)
        assert not self._thread.is_alive(), "the indexer did not return once stopped"
        self._loop.close()


@pytest.fixture
def start() -> Iterator[Callable[[Path, str], Running]]:
    started: list[Running] = []

    def starting(footage: Path, url: str) -> Running:
        indexer = Indexer(
            footage,
            DatabaseSettings(url=SecretStr(url)),
            first_wait=FIRST_WAIT,
            last_wait=LAST_WAIT,
        )
        running = Running(indexer)
        started.append(running)
        return running

    yield starting
    for running in started:
        running.stop()


def within(seconds: float, condition: Callable[[], bool], saying: Callable[[], object]) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"not within {seconds} s: {saying()}"
        time.sleep(0.05)


# --- as Segments finish -----------------------------------------------------------------------


def test_a_finished_segment_is_indexed_and_any_instant_it_holds_found(
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
) -> None:
    dock, _ = cameras
    running = start(tmp_path, migrated)
    segment = finish(tmp_path, dock, AT)
    indexed_before = sample("visionquery_recorder_segments_indexed_total", camera=str(dock))

    running.finished(dock, segment)

    index = SegmentIndex(memory)
    within(SLACK, lambda: index.latest(dock) is not None, lambda: held(memory))
    assert index.latest(dock) == indexed(tmp_path, dock, segment)
    middle = segment.time_from + (segment.time_to - segment.time_from) / 2
    found = index.find(dock, middle)
    assert found is not None
    assert tmp_path / found.segment.path == segment.path
    assert found.offset == middle - segment.time_from
    assert (
        sample("visionquery_recorder_segments_indexed_total", camera=str(dock))
        == indexed_before + 1
    )
    within(SLACK, lambda: sample("visionquery_recorder_index_pending") == 0, lambda: "pending")
    assert sample("visionquery_recorder_index_up") == 1
    assert running.failure is None


def test_segments_are_indexed_in_the_order_they_finish_across_cameras(
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
) -> None:
    dock, gate = cameras
    running = start(tmp_path, migrated)
    made = [
        (camera, finish(tmp_path, camera, AT + timedelta(minutes=5 * n)))
        for n in range(3)
        for camera in (dock, gate)
    ]
    for camera, segment in made:
        running.finished(camera, segment)

    within(SLACK, lambda: held(memory) == 6, lambda: held(memory))
    index = SegmentIndex(memory)
    for camera, segment in made:
        assert index.find(camera, segment.time_from) is not None


def test_one_refused_segment_does_not_stop_the_others(
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Segment of a camera the registry never had, and one overlapping a Segment already
    indexed, are each refused, logged once and counted; those after them are indexed."""
    caplog.set_level(logging.INFO, logger="visionquery.recorder")
    dock, gate = cameras
    first = finish(tmp_path, dock, AT)
    SegmentIndex(memory).index(indexed(tmp_path, dock, first))
    refused_before = sample("visionquery_recorder_segments_refused_total", camera=str(dock))
    running = start(tmp_path, migrated)
    # Caught up on what is on disk; what follows is finished while recording.
    within(SLACK, lambda: sample("visionquery_recorder_index_up") == 1, lambda: "never up")

    overlapping = finish(tmp_path, dock, AT + timedelta(seconds=1))
    stranger = finish(tmp_path, 4242, AT)
    good = finish(tmp_path, gate, AT)
    later = finish(tmp_path, dock, AT + timedelta(minutes=5))
    for camera, segment in ((dock, overlapping), (4242, stranger), (gate, good), (dock, later)):
        running.finished(camera, segment)

    index = SegmentIndex(memory)
    # In the order they finished, so the last one indexed means every one before it was seen.
    within(
        SLACK,
        lambda: index.latest(dock) == indexed(tmp_path, dock, later),
        lambda: index.latest(dock),
    )
    assert sample("visionquery_recorder_index_pending") == 0
    assert index.latest(gate) == indexed(tmp_path, gate, good)
    assert index.find(4242, AT + timedelta(seconds=1)) is None
    refusals = [r for r in caplog.records if r.levelno == logging.ERROR]
    named = [r.getMessage() for r in refusals]
    assert len([m for m in named if overlapping.path.name in m]) == 1, named
    assert len([m for m in named if stranger.path.name in m]) == 1, named
    assert all(r.exc_info is None for r in refusals), "a refusal is not a crash"
    assert (
        sample("visionquery_recorder_segments_refused_total", camera=str(dock))
        == refused_before + 1
    )
    assert running.failure is None


# --- catching up ------------------------------------------------------------------------------


def test_segments_on_disk_not_yet_indexed_are_indexed_when_it_starts(
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
) -> None:
    """A recorder that stopped before indexing what it finished, or crashed while writing a
    Segment it then recovered, has them indexed when its indexer starts."""
    dock, gate = cameras
    already = finish(tmp_path, dock, AT)
    SegmentIndex(memory).index(indexed(tmp_path, dock, already))
    missed = [
        (dock, finish(tmp_path, dock, AT + timedelta(minutes=5))),
        (dock, finish(tmp_path, dock, AT + timedelta(days=1))),
        (gate, finish(tmp_path, gate, AT - timedelta(days=2))),
    ]
    crashed = write(tmp_path, gate, AT + timedelta(minutes=5))
    (recovered,) = recover(tmp_path)
    assert recovered.segment_id == crashed.segment_id

    start(tmp_path, migrated)

    within(SLACK, lambda: held(memory) == 5, lambda: held(memory))
    index = SegmentIndex(memory)
    for camera, segment in [*missed, (gate, recovered)]:
        found = index.find(camera, segment.time_from)
        assert found is not None
        assert found.segment == indexed(tmp_path, camera, segment)


def test_starting_again_indexes_nothing_twice(
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
) -> None:
    dock, _ = cameras
    for n in range(3):
        finish(tmp_path, dock, AT + timedelta(minutes=5 * n))
    start(tmp_path, migrated).stop()
    assert held(memory) == 3

    start(tmp_path, migrated).stop()

    assert held(memory) == 3


def test_a_segment_on_disk_that_cannot_be_read_does_not_stop_the_catch_up(
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="visionquery.recorder")
    dock, gate = cameras
    broken = finish(tmp_path, dock, AT)
    broken.path.with_suffix(".closed").write_text("{")
    after = finish(tmp_path, dock, AT + timedelta(minutes=5))
    other = finish(tmp_path, gate, AT)

    running = start(tmp_path, migrated)

    within(SLACK, lambda: held(memory) == 2, lambda: held(memory))
    index = SegmentIndex(memory)
    assert index.latest(dock) == indexed(tmp_path, dock, after)
    assert index.latest(gate) == indexed(tmp_path, gate, other)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len([m for m in errors if broken.path.name in m]) == 1, errors
    running.stop()
    assert running.failure is None


def test_an_unforeseen_failure_is_said_once_and_indexing_goes_on(
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing foreseen raises while catching up; whatever does must not end indexing."""
    caplog.set_level(logging.INFO, logger="visionquery.recorder")
    dock, _ = cameras
    segment = finish(tmp_path, dock, AT)
    failures = 3
    real = segments

    def failing(directory: Path, **kwargs: Any) -> list[Segment]:
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("secret detail that must not be logged")
        return real(directory, **kwargs)

    monkeypatch.setattr(index_module, "segments", failing)

    running = start(tmp_path, migrated)

    within(SLACK, lambda: held(memory) == 1, lambda: (failures, held(memory)))
    assert SegmentIndex(memory).latest(dock) == indexed(tmp_path, dock, segment)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "RuntimeError" in warnings[0]
    assert all("secret detail" not in r.getMessage() for r in caplog.records)
    running.stop()
    assert running.failure is None


def test_what_is_not_a_cameras_footage_is_left_alone(
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
) -> None:
    dock, _ = cameras
    (tmp_path / ".lock").write_text("")
    (tmp_path / "lost+found").mkdir()
    (tmp_path / "lost+found" / "x.mp4").write_bytes(b"")
    segment = finish(tmp_path, dock, AT)

    running = start(tmp_path, migrated)

    within(SLACK, lambda: held(memory) == 1, lambda: held(memory))
    assert SegmentIndex(memory).latest(dock) == indexed(tmp_path, dock, segment)
    running.stop()
    assert running.failure is None


# --- when the memory cannot be reached --------------------------------------------------------


def _connections(postgres: str, database: str, *, allowed: bool) -> None:
    name = urlsplit(database).path.lstrip("/")
    with psycopg.connect(postgres, autocommit=True) as admin:
        admin.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS {}").format(
                sql.Identifier(name), sql.Literal(allowed)
            )
        )


def test_segments_finished_while_the_memory_is_unreachable_are_indexed_once_it_is_back(
    postgres: str,
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="visionquery.recorder")
    dock, gate = cameras
    _connections(postgres, migrated, allowed=False)
    try:
        running = start(tmp_path, migrated)
        first, second = finish(tmp_path, dock, AT), finish(tmp_path, gate, AT)
        running.finished(dock, first)
        running.finished(gate, second)
        within(
            SLACK,
            lambda: (
                sample("visionquery_recorder_index_up") == 0
                and sample("visionquery_recorder_index_pending") == 2
            ),
            lambda: (
                sample("visionquery_recorder_index_up"),
                sample("visionquery_recorder_index_pending"),
            ),
        )
        # Several attempts fail, and are said once.
        attempts = time.monotonic()
        within(SLACK, lambda: time.monotonic() - attempts > 4 * LAST_WAIT, lambda: "")
        assert held(memory) == 0
    finally:
        _connections(postgres, migrated, allowed=True)

    within(SLACK, lambda: held(memory) == 2, lambda: held(memory))
    within(SLACK, lambda: sample("visionquery_recorder_index_pending") == 0, lambda: "pending")
    assert sample("visionquery_recorder_index_up") == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "segment index" in warnings[0].getMessage()
    assert any("segment index" in r.getMessage() for r in caplog.records if r.levelno == 20)
    password = urlsplit(migrated).password
    assert password
    assert all(password not in r.getMessage() for r in caplog.records)


def test_a_connection_lost_mid_way_is_made_again(
    postgres: str,
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
) -> None:
    """The memory restarting under the recorder ends its connection; the next Segment is
    indexed on a new one."""
    dock, _ = cameras
    running = start(tmp_path, migrated)
    running.finished(dock, finish(tmp_path, dock, AT))
    within(SLACK, lambda: held(memory) == 1, lambda: held(memory))

    name = urlsplit(migrated).path.lstrip("/")
    with psycopg.connect(postgres, autocommit=True) as admin:
        ended = admin.execute(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
            " WHERE datname = %s AND backend_type = 'client backend'"
            " AND pid <> pg_backend_pid() AND pid <> %s",
            (name, memory.info.backend_pid),
        ).fetchone()
    assert ended == (1,), "the indexer held no connection of its own"

    running.finished(dock, finish(tmp_path, dock, AT + timedelta(minutes=5)))
    within(SLACK, lambda: held(memory) == 2, lambda: held(memory))
    assert running.failure is None


# --- stopping ---------------------------------------------------------------------------------


def test_segments_finished_as_the_recorder_stops_are_indexed_before_it_returns(
    migrated: str,
    memory: psycopg.Connection,
    cameras: tuple[int, int],
    tmp_path: Path,
    start: Callable[[Path, str], Running],
) -> None:
    dock, _ = cameras
    running = start(tmp_path, migrated)
    within(SLACK, lambda: sample("visionquery_recorder_index_up") == 1, lambda: "never up")
    for n in range(4):
        running.finished(dock, finish(tmp_path, dock, AT + timedelta(minutes=5 * n)))

    running.stop()

    assert held(memory) == 4


def test_stopping_while_the_memory_is_unreachable_returns_at_once(
    tmp_path: Path, start: Callable[[Path, str], Running]
) -> None:
    unreachable = "postgresql://visionquery:db-pa55word@127.0.0.1:9/visionquery?connect_timeout=1"
    running = start(tmp_path, unreachable)
    running.finished(1, finish(tmp_path, 1, AT))
    within(SLACK, lambda: sample("visionquery_recorder_index_up") == 0, lambda: "still up")

    asked = time.monotonic()
    running.stop(timeout=SLACK)

    assert running.returned_at is not None
    assert running.returned_at - asked < 5
    assert running.failure is None
