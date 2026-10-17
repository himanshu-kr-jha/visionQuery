"""Whether the memory's database can be reached, said without a word of the URL.

Every test runs against a real Postgres (see packages/conftest.py) or a socket that stands in
for an unreachable one.
"""

import socket
import time
from urllib.parse import quote, urlsplit

from visionquery.memory.health import DatabaseHealth, check_database


def test_a_reachable_database_is_connected(postgres: str) -> None:
    health = check_database(postgres)
    assert health == DatabaseHealth(problem=None)
    assert health.connected


def test_a_wrong_password_is_an_authentication_failure_and_nothing_else(postgres: str) -> None:
    parts = urlsplit(postgres)
    password = "not-the-password-@:/#"
    netloc = f"{parts.username}:{quote(password, safe='')}@{parts.hostname}:{parts.port}"
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
