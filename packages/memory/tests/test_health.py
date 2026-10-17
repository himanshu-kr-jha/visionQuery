"""Whether the memory's database can be reached, said without a word of the URL.

Every test runs against a real Postgres (see packages/conftest.py) or a socket that stands in
for an unreachable one.
"""

import socket
import time
from typing import Any
from urllib.parse import SplitResult, quote, urlsplit

import psycopg
import pytest
from visionquery.memory.connection import connect
from visionquery.memory.health import DatabaseHealth, check_database


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
        health = check_database(
            f"postgresql://u:secret@127.0.0.1:{server.getsockname()[1]}/db", timeout=1
        )
    assert health.problem == "timed out"
    assert time.monotonic() - started < 8


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
    health = check_database(postgres, timeout=1)
    assert health.problem == "timed out"
    assert time.monotonic() - started < 8


def test_the_connection_bounds_a_statement_unless_the_url_sets_its_own_options(
    postgres: str,
) -> None:
    with connect(postgres, 3, statement_timeout_ms=1234) as connection:
        row = connection.execute("SHOW statement_timeout").fetchone()
        assert row == ("1234ms",)
    own = postgres + ("&" if "?" in postgres else "?") + "options=-c%20statement_timeout%3D777"
    with connect(own, 3, statement_timeout_ms=1234) as connection:
        assert connection.execute("SHOW statement_timeout").fetchone() == ("777ms",)
