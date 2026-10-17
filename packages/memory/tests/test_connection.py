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


def _shown(connection: psycopg.Connection, setting: str) -> object:
    row = connection.execute("SELECT current_setting(%s)", [setting]).fetchone()
    return row[0] if row else None


def test_the_connection_bounds_a_statement(postgres: str) -> None:
    with connect(postgres, 3, statement_timeout_ms=1234) as connection:
        assert _shown(connection, "statement_timeout") == "1234ms"


@pytest.mark.parametrize(
    "options",
    [
        "-c work_mem=7MB",
        "-cwork_mem=7MB",
        "--work-mem=7MB",
        # An escaped space is part of a value: this sets application_name, not the bound.
        r"-c work_mem=7MB -c application_name=a\ -c\ statement_timeout=777",
        # Only ASCII whitespace separates words, as in the server: these are one word.
        "-c work_mem=7MB -c application_name=a -c statement_timeout=777",
        "-c work_mem=7MB -c application_name=a\x1c-c\x1fstatement_timeout=777",
        # A dangling backslash is dropped by the server, so it must not escape our separator.
        "-c work_mem=7MB\\",
        "-c work_mem=7MB \\",
        "-c work_mem=7MB -c application_name=a\\\\\\",
    ],
)
def test_a_url_with_its_own_options_keeps_them_and_gains_the_statement_bound(
    postgres: str, options: str
) -> None:
    with connect(_with_options(postgres, options), 3, statement_timeout_ms=1234) as connection:
        assert _shown(connection, "statement_timeout") == "1234ms"
        assert _shown(connection, "work_mem") == "7MB"


@pytest.mark.parametrize(
    "options",
    [
        "-c statement_timeout=777",
        "-cstatement_timeout=777",
        "-c work_mem=7MB -c statement_timeout=777",
        "--statement-timeout=777",
        "--statement_timeout=777",
        "-c Statement_Timeout=777",
        "  -c   statement_timeout=777  ",
    ],
)
def test_options_that_set_their_own_statement_bound_keep_it(postgres: str, options: str) -> None:
    with connect(_with_options(postgres, options), 3, statement_timeout_ms=1234) as connection:
        assert _shown(connection, "statement_timeout") == "777ms"


def test_pgoptions_is_kept_and_gains_the_statement_bound_when_the_url_has_no_options(
    postgres: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bound is passed as `options`, which takes the place of PGOPTIONS in libpq, so the
    # variable's value is carried over rather than dropped.
    monkeypatch.setenv("PGOPTIONS", "-c work_mem=7MB")
    with connect(postgres, 3, statement_timeout_ms=1234) as connection:
        assert _shown(connection, "statement_timeout") == "1234ms"
        assert _shown(connection, "work_mem") == "7MB"
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=777")
    with connect(postgres, 3, statement_timeout_ms=1234) as connection:
        assert _shown(connection, "statement_timeout") == "777ms"


def test_without_a_statement_bound_the_url_and_pgoptions_are_left_alone(
    postgres: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGOPTIONS", "-c work_mem=7MB")
    with connect(postgres, 3) as connection:
        assert _shown(connection, "work_mem") == "7MB"
        assert _shown(connection, "statement_timeout") == "0"
