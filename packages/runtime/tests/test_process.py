"""`start` hands back the loaded settings and the running metrics server, in one call."""

import json
import socket
import urllib.request
from typing import Any

import pytest
from pydantic import Field
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


class _PortsSettings(Settings):
    ports: list[int] = Field(default_factory=list[int])


def _logged(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return [json.loads(line) for line in captured.out.splitlines()]


def test_a_list_setting_that_does_not_parse_stops_the_process_naming_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VISIONQUERY_PORTS", "notjson")
    with pytest.raises(SystemExit) as exited:
        start("probe", _PortsSettings)
    assert exited.value.code == 2
    (line,) = _logged(capsys)
    assert line["level"] == "CRITICAL"
    assert line["variables"] == ["VISIONQUERY_PORTS"]
    assert "VISIONQUERY_PORTS is invalid" in line["message"]
    assert "notjson" not in line["message"]
