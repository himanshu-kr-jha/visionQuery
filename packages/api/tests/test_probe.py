"""The probe the api checks the database through: cached for a second, loud once per change."""

import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial

import pytest
from fastapi.testclient import TestClient
from visionquery.api.app import create_app
from visionquery.api.probe import (
    CACHE_SECONDS,
    CHECKING,
    DEADLINE_MARGIN_SECONDS,
    DEADLINE_SECONDS,
    DatabaseProbe,
    DatabaseState,
)
from visionquery.memory.health import HEALTH_BUDGET_SECONDS, DatabaseHealth, check_database

UP = DatabaseHealth(None)
DOWN = DatabaseHealth("connection refused or host unreachable")
TIMED_OUT = DatabaseHealth("timed out")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Check:
    """A check whose answer a test sets, and which counts how often it was asked."""

    def __init__(self, answer: DatabaseHealth) -> None:
        self.answer = answer
        self.calls = 0

    def __call__(self) -> DatabaseHealth:
        self.calls += 1
        return self.answer


def _workers_finished() -> None:
    """Waits for every check thread to end, after it has logged what it found.

    A worker publishes its answer before it logs the change, so a caller that has the answer
    may read the log first.
    """
    for thread in threading.enumerate():
        if thread.name == "database-check":
            thread.join(timeout=10)
            assert not thread.is_alive(), "a database check did not end"


def _probe(check: Callable[[], DatabaseHealth], clock: _Clock) -> DatabaseProbe:
    return DatabaseProbe(check, clock=clock)


def test_a_flood_of_requests_checks_the_database_once_a_second() -> None:
    check, clock = _Check(UP), _Clock()
    app = create_app(database_health=_probe(check, clock))
    with TestClient(app) as client:
        assert [client.get("/health").status_code for _ in range(50)] == [200] * 50
        assert check.calls == 1
        clock.now += CACHE_SECONDS + 0.01
        client.get("/health")
    assert check.calls == 2


def test_a_change_is_seen_once_the_cached_answer_expires() -> None:
    check, clock = _Check(UP), _Clock()
    probe = _probe(check, clock)
    assert probe() == UP
    check.answer = DOWN
    assert probe() == UP
    clock.now += CACHE_SECONDS + 0.01
    assert probe() == DOWN


def test_turning_unreachable_logs_one_warning_with_the_category_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    check, clock = _Check(UP), _Clock()
    probe = _probe(check, clock)
    with caplog.at_level(logging.INFO, logger="visionquery.api"):
        probe()
        _workers_finished()
        assert caplog.records == []  # reachable from the start is not news
        check.answer = DOWN
        for _ in range(5):
            clock.now += CACHE_SECONDS + 0.01
            probe()
            _workers_finished()
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "the database is unreachable: connection refused or host unreachable"
    )


def test_recovering_logs_one_info(caplog: pytest.LogCaptureFixture) -> None:
    check, clock = _Check(DOWN), _Clock()
    probe = _probe(check, clock)
    with caplog.at_level(logging.INFO, logger="visionquery.api"):
        probe()  # unreachable from the start is a change from "unknown"
        _workers_finished()
        check.answer = UP
        for _ in range(5):
            clock.now += CACHE_SECONDS + 0.01
            probe()
            _workers_finished()
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "the database is unreachable: connection refused or host unreachable"),
        (logging.INFO, "the database is reachable again"),
    ]


_SLOW_CHECKS: list[_Slow] = []


class _Slow:
    """A check that holds until a test lets it finish, and counts how often it was asked."""

    def __init__(self, answer: DatabaseHealth) -> None:
        self.answer = answer
        self.calls = 0
        self.started = threading.Event()
        self.finish = threading.Event()
        _SLOW_CHECKS.append(self)

    def __call__(self) -> DatabaseHealth:
        self.calls += 1
        self.started.set()
        assert self.finish.wait(timeout=30)
        return self.answer


def _ask_in_threads(probe: DatabaseProbe, count: int) -> list[DatabaseState]:
    answers: list[DatabaseState] = []
    threads = [
        threading.Thread(target=lambda: answers.append(probe()), daemon=True) for _ in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    return answers


def test_waiters_do_not_block_on_a_slow_first_check_and_say_it_is_still_checking() -> None:
    slow, clock = _Slow(UP), _Clock()
    probe = _probe(slow, clock)
    first: list[DatabaseState] = []
    leader = threading.Thread(target=lambda: first.append(probe()), daemon=True)
    leader.start()
    assert slow.started.wait(timeout=5)

    started = time.monotonic()
    answers = _ask_in_threads(probe, 8)
    assert time.monotonic() - started < 1.5
    assert answers == [CHECKING] * 8  # every waiter returned, none was left blocked
    assert slow.calls == 1

    slow.finish.set()
    leader.join(timeout=5)
    assert first == [UP]
    assert probe() == UP
    assert slow.calls == 1


def test_waiters_get_the_last_answer_while_a_refresh_is_slow() -> None:
    check, clock = _Check(UP), _Clock()
    slow = _Slow(DOWN)
    probe = DatabaseProbe(lambda: check() if check.calls == 0 else slow(), clock=clock)
    assert probe() == UP
    clock.now += CACHE_SECONDS + 0.01
    leader = threading.Thread(target=probe, daemon=True)
    leader.start()
    assert slow.started.wait(timeout=5)
    assert _ask_in_threads(probe, 4) == [UP] * 4
    slow.finish.set()
    leader.join(timeout=5)
    assert slow.calls == 1


def test_a_check_that_never_returns_is_reported_as_timed_out_not_as_the_old_answer() -> None:
    slow, clock = _Slow(UP), _Clock()
    probe = _probe(slow, clock)
    leader = threading.Thread(target=probe, daemon=True)
    leader.start()
    assert slow.started.wait(timeout=5)
    clock.now += DEADLINE_SECONDS + 0.01
    assert probe() == TIMED_OUT
    slow.finish.set()
    leader.join(timeout=5)


def test_the_deadline_is_the_checks_budget_plus_a_margin() -> None:
    # The check bounds itself (HEALTH_BUDGET_SECONDS); the probe gives it a little longer
    # before it stops waiting, and every client of /health waits longer still.
    assert DEADLINE_SECONDS == HEALTH_BUDGET_SECONDS + DEADLINE_MARGIN_SECONDS
    assert DEADLINE_MARGIN_SECONDS > 0


_RELEASE_FOREVER = threading.Event()


@pytest.fixture(autouse=True)
def _release_stuck_checks() -> Iterator[None]:
    """Ends every check thread a test started before the next test begins.

    A worker that finishes late logs "reachable again" into whichever test is running then, so
    the checks are released and every `database-check` thread is joined, failing loudly if one
    does not end.
    """
    _RELEASE_FOREVER.clear()
    _SLOW_CHECKS.clear()
    yield
    _RELEASE_FOREVER.set()
    for slow in _SLOW_CHECKS:
        slow.finish.set()
    _SLOW_CHECKS.clear()
    stuck: list[str] = []
    for thread in threading.enumerate():
        if thread.name == "database-check":
            thread.join(timeout=10)
            if thread.is_alive():
                stuck.append(thread.name)
    assert not stuck, f"database-check threads outlived their test: {stuck}"


def _forever() -> DatabaseHealth:
    _RELEASE_FOREVER.wait()
    return UP


def test_a_failure_to_start_the_check_does_not_wedge_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = threading.Thread.start
    failures = [RuntimeError("can't start new thread")]

    def flaky_start(self: threading.Thread) -> None:
        if failures:
            raise failures.pop()
        start(self)

    check, clock = _Check(UP), _Clock()
    probe = _probe(check, clock)
    monkeypatch.setattr(threading.Thread, "start", flaky_start)
    with pytest.raises(RuntimeError):
        probe()
    assert probe() == UP  # the next request starts a fresh check
    assert check.calls == 1


def test_the_request_that_runs_a_check_gives_up_at_the_deadline() -> None:
    # A database that freezes mid-query must not hang the request that started the check.
    probe = DatabaseProbe(_forever, deadline_seconds=0.3)
    started = time.monotonic()
    assert probe() == TIMED_OUT
    assert 0.3 <= time.monotonic() - started < 1.5


def test_a_request_over_http_is_bounded_by_the_deadline_too() -> None:
    app = create_app(database_health=DatabaseProbe(_forever, deadline_seconds=0.3))
    with TestClient(app) as client:
        started = time.monotonic()
        response = client.get("/health")
    assert time.monotonic() - started < 1.5
    assert response.status_code == 503
    assert response.json()["database"] == {"status": "unreachable", "problem": "timed out"}


def _eventually(condition: Callable[[], bool], timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_a_check_that_never_returns_is_logged_once_when_its_deadline_passes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    slow = _Slow(UP)
    probe = DatabaseProbe(slow, deadline_seconds=0.2)
    with caplog.at_level(logging.INFO, logger="visionquery.api"):
        assert probe() == TIMED_OUT  # the request that started it, at the deadline
        assert [probe() for _ in range(5)] == [TIMED_OUT] * 5  # the rest, at once
        assert _ask_in_threads(probe, 4) == [TIMED_OUT] * 4
        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (logging.WARNING, "the database check has not returned within 0.2 s")
        ]
        assert slow.calls == 1  # one stuck check, not one per request

        slow.finish.set()  # it comes back at last, with the database reachable
        assert _eventually(lambda: probe() == UP)
        _workers_finished()
    assert [r.getMessage() for r in caplog.records][1:] == ["the database is reachable again"]


def test_connect_timeout_zero_cannot_wedge_the_probe(caplog: pytest.LogCaptureFixture) -> None:
    # libpq waits forever for a server that never answers when `connect_timeout` is 0; the
    # check keeps its own limit, so it returns within its budget and the deadline never passes.
    with socket.socket() as server, caplog.at_level(logging.INFO, logger="visionquery.api"):
        server.bind(("127.0.0.1", 0))
        server.listen(8)  # the kernel completes the handshake; nothing ever replies
        url = f"postgresql://u:s@127.0.0.1:{server.getsockname()[1]}/db?connect_timeout=0"
        probe = DatabaseProbe(partial(check_database, url))
        started = time.monotonic()
        assert probe() == TIMED_OUT
        elapsed = time.monotonic() - started
        _workers_finished()
    # The check answers by itself; had it wedged, the probe would have waited out its deadline
    # and logged that instead, so the log line below is what proves the check returned.
    assert elapsed < DEADLINE_SECONDS
    assert [r.getMessage() for r in caplog.records] == ["the database is unreachable: timed out"]


def test_a_check_that_raises_is_another_failure_and_does_not_wedge_the_probe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[int] = []

    def broken() -> DatabaseHealth:
        calls.append(1)
        raise RuntimeError("postgresql://u:hunter2@db/x")

    clock = _Clock()
    probe = _probe(broken, clock)
    with caplog.at_level(logging.INFO, logger="visionquery.api"):
        assert probe() == DatabaseHealth("other connection failure")
        _workers_finished()
        clock.now += CACHE_SECONDS + 0.01
        assert probe() == DatabaseHealth("other connection failure")
        _workers_finished()
    assert len(calls) == 2
    assert "hunter2" not in caplog.text
    assert "RuntimeError" in caplog.text
