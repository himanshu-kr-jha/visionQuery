"""The probe the api checks the database through: cached for a second, loud once per change."""

import logging
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from visionquery.api.app import create_app, database_health
from visionquery.api.probe import CACHE_SECONDS, DatabaseProbe
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
    app = create_app()
    app.dependency_overrides[database_health] = _probe(check, clock)
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
