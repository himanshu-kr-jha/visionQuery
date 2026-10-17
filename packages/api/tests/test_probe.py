"""The probe the api checks the database through: cached for a second, loud once per change."""

import logging
import threading
import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from visionquery.api.app import create_app
from visionquery.api.probe import CACHE_SECONDS, CHECKING, STUCK_SECONDS, DatabaseProbe
from visionquery.memory.health import DatabaseHealth

UP = DatabaseHealth(None)
DOWN = DatabaseHealth("connection refused or host unreachable")


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
        assert caplog.records == []  # reachable from the start is not news
        check.answer = DOWN
        for _ in range(5):
            clock.now += CACHE_SECONDS + 0.01
            probe()
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
        check.answer = UP
        for _ in range(5):
            clock.now += CACHE_SECONDS + 0.01
            probe()
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "the database is unreachable: connection refused or host unreachable"),
        (logging.INFO, "the database is reachable again"),
    ]


class _Slow:
    """A check that holds until a test lets it finish, and counts how often it was asked."""

    def __init__(self, answer: DatabaseHealth) -> None:
        self.answer = answer
        self.calls = 0
        self.started = threading.Event()
        self.finish = threading.Event()

    def __call__(self) -> DatabaseHealth:
        self.calls += 1
        self.started.set()
        assert self.finish.wait(timeout=30)
        return self.answer


def _ask_in_threads(probe: DatabaseProbe, count: int) -> list[DatabaseHealth]:
    answers: list[DatabaseHealth] = []
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
    first: list[DatabaseHealth] = []
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
    clock.now += STUCK_SECONDS + 0.01
    assert probe() == DatabaseHealth("timed out")
    slow.finish.set()
    leader.join(timeout=5)
