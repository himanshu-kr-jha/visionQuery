"""Connecting says why it failed in fixed words, and honours libpq's own timeout variable.

The tests run against a real Postgres (see packages/conftest.py), or a socket that stands in for
a server that misbehaves in one particular way.
"""

import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import quote

import psycopg
import pytest
from visionquery.memory.connection import ConnectionFailedError, categorize, connect

SECRET = "SECRETPW"


def _with_options(url: str, options: str) -> str:
    return url + ("&" if "?" in url else "?") + "options=" + quote(options, safe="")


def _failure(url: str) -> ConnectionFailedError:
    with pytest.raises(ConnectionFailedError) as failed:
        connect(url, 5)
    assert SECRET not in str(failed.value)
    return failed.value


@pytest.fixture
def server_without_tls() -> Iterator[int]:
    """A port whose server answers a TLS request with "no", as Postgres with `ssl = off` does."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def answer() -> None:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        with client:
            client.recv(8)  # the SSLRequest
            client.sendall(b"N")
            client.recv(1024)  # libpq gives up and closes

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()
        thread.join(timeout=5)


def test_a_server_that_will_not_do_the_required_tls_is_named_as_such(
    server_without_tls: int,
) -> None:
    url = f"postgresql://u:{SECRET}@127.0.0.1:{server_without_tls}/d?sslmode=require"
    assert _failure(url).category == "TLS required or failed"


@pytest.mark.parametrize("option", ["-c nosuchparam=1", "-c statement_timeout=abc"])
def test_a_startup_option_the_server_rejects_is_named_as_such(postgres: str, option: str) -> None:
    assert _failure(_with_options(postgres, option)).category == (
        "server rejected a startup option"
    )


def test_a_failure_to_connect_carries_no_sqlstate(postgres: str) -> None:
    # Why connection failures are told apart by their text: libpq gives them no SQLSTATE.
    with pytest.raises(psycopg.OperationalError) as failed:
        psycopg.connect(_with_options(postgres, "-c nosuchparam=1"))
    assert failed.value.sqlstate is None


def test_a_statement_cancelled_by_the_server_is_a_timeout(postgres: str) -> None:
    # The one error with a SQLSTATE that categorize reads: a connected session's cancelled
    # statement, which is how a health check that waited too long ends.
    with (
        connect(postgres, 5, statement_timeout_ms=100) as connection,
        pytest.raises(psycopg.errors.QueryCanceled) as cancelled,
    ):
        connection.execute("SELECT pg_sleep(5)")
    assert categorize(cancelled.value) == "timed out"


def test_pgconnect_timeout_wins_over_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "2")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)  # the kernel completes the handshake; nothing ever replies
        started = time.monotonic()
        with pytest.raises(ConnectionFailedError, match="timed out"):
            connect(f"postgresql://u:p@127.0.0.1:{server.getsockname()[1]}/d", 60)
    assert time.monotonic() - started < 10


def test_a_connect_timeout_in_the_url_wins_over_pgconnect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "60")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        started = time.monotonic()
        with pytest.raises(ConnectionFailedError, match="timed out"):
            connect(f"postgresql://u:p@127.0.0.1:{server.getsockname()[1]}/d?connect_timeout=2", 60)
    assert time.monotonic() - started < 10


def test_an_empty_pgconnect_timeout_is_unset_so_the_default_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # libpq ignores an empty variable, so leaving the keyword out would wait forever.
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        started = time.monotonic()
        with pytest.raises(ConnectionFailedError, match="timed out"):
            connect(f"postgresql://u:p@127.0.0.1:{server.getsockname()[1]}/d", 2)
    assert time.monotonic() - started < 10
