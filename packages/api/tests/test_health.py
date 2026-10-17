"""`GET /health` says whether the Deployment's database can be reached, and never how to reach it.

Every test asks the api over HTTP (FastAPI's test client) while it checks a real Postgres (see
packages/conftest.py), or an address where none answers.
"""

import socket
from collections.abc import Callable
from functools import partial
from typing import Any
from urllib.parse import SplitResult, quote, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from visionquery.api.app import create_app, current_database_health, operator_router
from visionquery.memory.health import DatabaseHealth, check_database


def _host(parts: SplitResult) -> str:
    """The URL's host as a netloc needs it: an IPv6 address keeps its brackets."""
    host = parts.hostname or ""
    return f"[{host}]" if ":" in host else host


def _app(check: Callable[[], DatabaseHealth]) -> FastAPI:
    return create_app(database_health=check)


def _health(url: str) -> tuple[int, dict[str, Any]]:
    with TestClient(_app(partial(check_database, url))) as client:
        response = client.get("/health")
    return response.status_code, response.json()


def test_a_reachable_database_is_healthy(postgres: str) -> None:
    status, body = _health(postgres)
    assert status == 200
    assert body == {"status": "healthy", "database": {"status": "connected", "problem": None}}


def test_a_wrong_password_is_unhealthy_and_the_body_holds_nothing_of_the_url(
    postgres: str,
) -> None:
    parts = urlsplit(postgres)
    password = "not-the-password-hunter2"
    netloc = f"{parts.username}:{quote(password)}@{_host(parts)}:{parts.port}"
    status, body = _health(parts._replace(netloc=netloc).geturl())
    assert status == 503
    assert body == {
        "status": "unhealthy",
        "database": {"status": "unreachable", "problem": "authentication failed"},
    }


@pytest.fixture
def nobody_listening() -> str:
    """A URL whose port nothing listens on, with a password in it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"postgresql://visionquery:hunter2@127.0.0.1:{port}/visionquery"


def test_an_unreachable_database_is_unhealthy_with_the_reason(nobody_listening: str) -> None:
    status, body = _health(nobody_listening)
    assert status == 503
    assert body == {
        "status": "unhealthy",
        "database": {"status": "unreachable", "problem": "connection refused or host unreachable"},
    }


def test_no_part_of_the_url_is_ever_in_the_answer(nobody_listening: str) -> None:
    parts = urlsplit(nobody_listening)
    with TestClient(_app(partial(check_database, nobody_listening))) as client:
        text = client.get("/health").text
    for secret in ("hunter2", "visionquery", str(parts.port), "127.0.0.1", "postgresql"):
        assert secret not in text


def test_each_request_asks_the_provided_check() -> None:
    # The api itself does not cache: the provided check decides (see test_probe.py).
    calls: list[int] = []

    def check() -> DatabaseHealth:
        calls.append(1)
        return check_database("postgresql://u@127.0.0.1:1/db")

    with TestClient(_app(check)) as client:
        client.get("/health")
        client.get("/health")
    assert len(calls) == 2


def test_serves_no_page_that_loads_scripts_from_the_internet() -> None:
    # FastAPI's interactive docs load Swagger UI and ReDoc from a CDN; a Deployment is closed
    # (ADR 0001), so they are off. The schema itself is still served.
    with TestClient(_app(lambda: DatabaseHealth(None))) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 200


def test_health_is_at_the_root_and_operator_operations_will_be_under_api() -> None:
    # /health is for infrastructure (the Compose healthcheck); Operator operations get /api.
    assert operator_router.prefix == "/api"
    with TestClient(_app(lambda: DatabaseHealth(None))) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/health").status_code == 404
        assert "/health" in client.get("/openapi.json").json()["paths"]


def test_an_app_cannot_be_made_without_its_database_check() -> None:
    # A forgotten dependency is an error where the app is made, not a 500 on one route later.
    with pytest.raises(TypeError, match="database_health"):
        create_app()  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]


def test_tests_may_still_override_the_dependency_on_a_made_app() -> None:
    app = _app(lambda: DatabaseHealth(None))
    app.dependency_overrides[current_database_health] = lambda: DatabaseHealth("timed out")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 503
