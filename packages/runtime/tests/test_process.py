"""`start` hands back the loaded settings and the running metrics server, in one call."""

import socket
import urllib.request

import pytest
from visionquery.runtime import Settings, start

pytestmark = pytest.mark.usefixtures("restore_logging")


def test_start_returns_the_settings_and_the_metrics_port_it_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISIONQUERY_METRICS_HOST", "127.0.0.1")
    monkeypatch.setenv("VISIONQUERY_METRICS_PORT", "0")
    process = start("probe", Settings)
    try:
        assert process.settings.metrics_host == "127.0.0.1"
        assert process.metrics_port > 0
        url = f"http://127.0.0.1:{process.metrics_port}/metrics"
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 200
    finally:
        process.close()
    with (
        pytest.raises(ConnectionRefusedError),
        socket.create_connection(("127.0.0.1", process.metrics_port), 1),
    ):
        pass
