"""Serving the process's Prometheus metrics on `/metrics`, for Prometheus to pull.

Metrics are declared where they are measured, with `prometheus_client` and its default
registry (`Counter("segments_sealed", ...)`); this module only serves them.
"""

import socket
import threading
from collections.abc import Iterable
from types import TracebackType
from typing import Self, cast, override
from wsgiref.simple_server import WSGIRequestHandler, make_server
from wsgiref.types import StartResponse, WSGIApplication, WSGIEnvironment

import prometheus_client
from prometheus_client import REGISTRY
from prometheus_client.exposition import ThreadingWSGIServer

__all__ = ["MetricsServer", "serve_metrics"]

PATH = "/metrics"


def serve_metrics(host: str, port: int) -> MetricsServer:
    """Start serving `/metrics` on `host:port` (0 for any free port) in a background thread."""
    return MetricsServer(host, port)


class MetricsServer:
    """An HTTP server answering `/metrics` with every metric in the default registry."""

    def __init__(self, host: str, port: int) -> None:
        # prometheus_client declares the app as a bare `Callable`; it is a WSGI app.
        exposition = cast(WSGIApplication, prometheus_client.make_wsgi_app(REGISTRY))  # pyright: ignore[reportUnknownMemberType]

        def app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
            if environ.get("PATH_INFO") != PATH:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"Not Found: metrics are served on /metrics\n"]
            return exposition(environ, start_response)

        self._server = make_server(
            host, port, app, server_class=_server_class(host, port), handler_class=_QuietHandler
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="metrics", daemon=True
        )
        self._thread.start()

    @property
    def port(self) -> int:
        """The port actually listened on, which is the chosen one when 0 was asked for."""
        return self._server.server_port

    def close(self) -> None:
        """Stop serving and release the port."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _server_class(host: str, port: int) -> type[ThreadingWSGIServer]:
    """The server class listening on the address family `host` belongs to (IPv4 or IPv6)."""
    family = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)[0][0]

    class Server(ThreadingWSGIServer):
        address_family = family

    return Server


class _QuietHandler(WSGIRequestHandler):
    """Prometheus scrapes every few seconds; a line on stderr for each would drown the logs."""

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass
