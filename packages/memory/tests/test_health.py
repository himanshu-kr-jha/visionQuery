"""Whether the memory's database can be reached, said without a word of the URL.

Every test runs against a real Postgres (see packages/conftest.py) or a socket that stands in
for an unreachable one.
"""

import socket
import threading
import time
from collections.abc import Callable
from typing import Any, get_args
from urllib.parse import SplitResult, quote, urlsplit

import psycopg
import pytest
from visionquery.memory.connection import ConnectionProblem, connect
from visionquery.memory.health import (
    CONNECT_SECONDS,
    HEALTH_BUDGET_SECONDS,
    STATEMENT_SECONDS,
    DatabaseHealth,
    check_database,
)


def _within[T](seconds: float, call: Callable[[], T]) -> T:
    """What `call` returns, or a failure once `seconds` pass: a wedged check fails, not hangs."""
    results: list[T] = []
    worker = threading.Thread(target=lambda: results.append(call()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert results, f"still running after {seconds} s"
    return results[0]


def _host(parts: SplitResult) -> str:
    """The URL's host as a netloc needs it: an IPv6 address keeps its brackets."""
    host = parts.hostname or ""
    return f"[{host}]" if ":" in host else host


def test_a_reachable_database_is_connected(postgres: str) -> None:
    health = check_database(postgres)
    assert health == DatabaseHealth(problem=None)
    assert health.connected


def test_a_wrong_password_is_an_authentication_failure_and_nothing_else(postgres: str) -> None:
    parts = urlsplit(postgres)
    password = "not-the-password-@:/#"
    netloc = f"{parts.username}:{quote(password, safe='')}@{_host(parts)}:{parts.port}"
    health = check_database(parts._replace(netloc=netloc).geturl())
    assert health == DatabaseHealth(problem="authentication failed")
    assert not health.connected


def test_a_missing_database_is_named_as_such(postgres: str) -> None:
    url = urlsplit(postgres)._replace(path="/no_such_database").geturl()
    assert check_database(url).problem == "database does not exist"


def test_a_port_nobody_listens_on_is_refused() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # The socket is closed, so nothing listens there now.
    health = check_database(f"postgresql://u:secret@127.0.0.1:{port}/db")
    assert health.problem == "connection refused or host unreachable"


def test_a_server_that_never_answers_times_out_within_the_timeout() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)  # the kernel completes the handshake; nothing ever replies
        started = time.monotonic()
        health = check_database(f"postgresql://u:secret@127.0.0.1:{server.getsockname()[1]}/db")
    assert health.problem == "timed out"
    assert time.monotonic() - started < CONNECT_SECONDS + 1.5


def test_a_query_that_never_finishes_is_cancelled_and_reported_as_timed_out(
    postgres: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `SELECT 1` has no client-side limit, so the server is asked to give up on a slow query.
    # A server that answers the handshake and then hangs is stood in for by a slow statement.
    real_connect = connect

    def slow(url: str, timeout: int, **kwargs: Any) -> psycopg.Connection:
        connection = real_connect(url, timeout, **kwargs)
        execute = connection.execute

        def sleeping(query: Any, *args: Any, **kw: Any) -> Any:
            return execute("SELECT pg_sleep(30)", *args, **kw)

        monkeypatch.setattr(connection, "execute", sleeping, raising=False)
        return connection

    monkeypatch.setattr("visionquery.memory.health.connect", slow)
    started = time.monotonic()
    health = check_database(postgres)
    assert health.problem == "timed out"
    assert time.monotonic() - started < HEALTH_BUDGET_SECONDS + 1.5


def test_the_check_has_one_budget_made_of_its_connect_and_statement_bounds() -> None:
    # The api's probe waits for this plus a margin, and every client of /health longer still.
    assert HEALTH_BUDGET_SECONDS == CONNECT_SECONDS + STATEMENT_SECONDS
    # libpq takes a `connect_timeout` under 2 s as 2 s, so a smaller one would not be honest.
    assert CONNECT_SECONDS >= 2


@pytest.mark.parametrize(
    ("query", "pgconnect_timeout"),
    [
        ("?connect_timeout=60", None),
        # 0 waits forever in libpq; the check must not.
        ("?connect_timeout=0", None),
        ("?connect_timeout=-1", None),
        ("", "60"),
        ("", "0"),
    ],
)
def test_the_check_keeps_its_own_connect_limit_whatever_the_url_or_environment_say(
    monkeypatch: pytest.MonkeyPatch, query: str, pgconnect_timeout: str | None
) -> None:
    if pgconnect_timeout is None:
        monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("PGCONNECT_TIMEOUT", pgconnect_timeout)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)  # the kernel completes the handshake; nothing ever replies
        url = f"postgresql://u:s@127.0.0.1:{server.getsockname()[1]}/db{query}"
        health = _within(CONNECT_SECONDS + 1.5, lambda: check_database(url))
    assert health.problem == "timed out"


def test_still_checking_is_not_a_reason_the_database_cannot_be_reached() -> None:
    # Whether a check has finished is the caller's business (the api's probe), not the memory's.
    assert "still checking" not in get_args(ConnectionProblem.__value__)
