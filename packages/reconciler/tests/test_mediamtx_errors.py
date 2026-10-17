"""MediaMTX's error text may quote a path's source, a camera's URL; it never reaches a message."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import SecretStr
from visionquery.reconciler.mediamtx import MediaMTXClient, MediaMTXError

URL = "rtsps://admin:pa55word@192.168.1.1:7441/tok3n-in-the-path?token=q3ry"


class _Refuses(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.dumps({"error": f"invalid source: {URL}"}).encode()
        self.send_response(400)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def api_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Refuses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_a_refusal_quoting_a_cameras_url_shows_no_part_of_it(api_url: str) -> None:
    client = MediaMTXClient(api_url, "visionquery", SecretStr("api-password"))
    with pytest.raises(MediaMTXError) as refused:
        client.add_path("pulled/1", {"source": URL})
    message = str(refused.value)
    assert "400" in message
    for secret in ("pa55word", "admin", "tok3n-in-the-path", "q3ry", "192.168.1.1"):
        assert secret not in message
