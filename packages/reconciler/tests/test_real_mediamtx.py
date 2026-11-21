"""The readiness waits of `real_mediamtx.py`, which the reconciler's and the recorder's tests share.

A MediaMTX is ready for a test when its control API answers and its RTSP port accepts a
connection and speaks RTSP, and a wait for either says what it saw when its deadline passes.
Under load the API and the RTSP port do not come up at the same moment, and a connection
refused or reset in between is the container not being up yet, never the test's own failure.
The first tests stand a fake API and a fake RTSP port in for MediaMTX, so that each condition
can be held back on its own; the last ones use the real container.
"""

import json
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from real_mediamtx import MediaMTX


class _Api(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"authInternalUsers": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _fake_api() -> Generator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve_rtsp(port: int, stop: threading.Event) -> None:
    """Answer every connection's request as MediaMTX does, with an RTSP status line."""
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        listener.settimeout(0.05)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(1)
                try:
                    connection.recv(1024)
                    connection.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
                except OSError:
                    pass


def _mediamtx(api_url: str, rtsp_port: int) -> MediaMTX:
    return MediaMTX("none", "none", api_url, "api-password", "reader-password", rtsp_port)


def _after(delay: float, action: Callable[[], None]) -> threading.Thread:
    def run() -> None:
        time.sleep(delay)
        action()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_mediamtx_is_not_ready_while_only_its_api_answers() -> None:
    with _fake_api() as api_url:
        server = _mediamtx(api_url, _closed_port())

        with pytest.raises(AssertionError) as raised:
            server.wait_until_ready(timeout=1)

    message = str(raised.value)
    assert "RTSP" in message
    assert "API" in message


def test_mediamtx_is_ready_once_its_rtsp_port_serves_too() -> None:
    port, stop = _closed_port(), threading.Event()
    server_thread = threading.Thread(target=_serve_rtsp, args=(port, stop), daemon=True)
    with _fake_api() as api_url:
        late = _after(0.5, server_thread.start)
        started = time.monotonic()
        try:
            _mediamtx(api_url, port).wait_until_ready(timeout=10)
            waited = time.monotonic() - started
        finally:
            late.join()
            stop.set()
            server_thread.join()

    assert waited >= 0.5


def test_a_port_that_accepts_but_does_not_speak_rtsp_is_not_ready() -> None:
    """What a connection through a proxy that has no backend yet looks like: it is accepted and
    closed without a word."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def accept_and_close() -> None:
        listener.settimeout(0.05)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            connection.close()

    thread = threading.Thread(target=accept_and_close, daemon=True)
    thread.start()
    try:
        with _fake_api() as api_url:
            server = _mediamtx(api_url, int(listener.getsockname()[1]))
            with pytest.raises(AssertionError, match="RTSP"):
                server.wait_until_ready(timeout=1)
    finally:
        stop.set()
        thread.join()
        listener.close()


def test_waiting_for_a_publisher_while_mediamtx_is_down_fails_saying_what_was_seen(
    mediamtx: MediaMTX,
) -> None:
    """While MediaMTX is stopped its API refuses or resets every connection; the wait goes on
    until its deadline and then says so, instead of dying on the first refused connection."""
    mediamtx.stop()
    try:
        with pytest.raises(AssertionError, match="nothing was published to dock") as raised:
            mediamtx.wait_until_published("dock", timeout=2)
    finally:
        mediamtx.start()

    assert "API" in str(raised.value)


def test_waiting_for_a_publisher_survives_mediamtx_coming_back(mediamtx: MediaMTX) -> None:
    """A wait that starts while MediaMTX is down goes on through the refused connections, and
    reports the publisher's absence once MediaMTX answers."""
    mediamtx.stop()
    restart = _after(1.5, lambda: mediamtx.start())
    try:
        with pytest.raises(AssertionError, match="nothing was published to dock"):
            mediamtx.wait_until_published("dock", timeout=6)
    finally:
        restart.join()


def test_a_restarted_mediamtx_serves_rtsp_when_restart_returns(mediamtx: MediaMTX) -> None:
    for _ in range(5):
        mediamtx.restart()
        with socket.create_connection(("127.0.0.1", mediamtx.rtsp_port), timeout=2) as connection:
            connection.sendall(b"OPTIONS rtsp://127.0.0.1/ RTSP/1.0\r\nCSeq: 1\r\n\r\n")
            assert connection.recv(64).startswith(b"RTSP/1.0")
