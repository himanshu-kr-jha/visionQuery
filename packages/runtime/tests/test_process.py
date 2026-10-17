"""`start` hands back the loaded settings and the running metrics server, in one call."""

import errno
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


def test_a_metrics_port_already_in_use_stops_the_process_naming_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        monkeypatch.setenv("VISIONQUERY_METRICS_HOST", "127.0.0.1")
        monkeypatch.setenv("VISIONQUERY_METRICS_PORT", str(port))
        with pytest.raises(SystemExit) as exited:
            start("probe", Settings)
    assert exited.value.code == 2
    (line,) = _logged(capsys)
    assert line["level"] == "CRITICAL"
    assert line["variables"] == ["VISIONQUERY_METRICS_PORT"]
    assert f"127.0.0.1:{port}" in line["message"]
    assert "VISIONQUERY_METRICS_PORT" in line["message"]
    assert "exception" not in line


def test_a_metrics_host_that_is_not_this_machines_stops_the_process_naming_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 192.0.2.0/24 is reserved for documentation, so no interface of this machine has it.
    monkeypatch.setenv("VISIONQUERY_METRICS_HOST", "192.0.2.1")
    monkeypatch.setenv("VISIONQUERY_METRICS_PORT", "0")
    with pytest.raises(SystemExit) as exited:
        start("probe", Settings)
    assert exited.value.code == 2
    (line,) = _logged(capsys)
    assert line["variables"] == ["VISIONQUERY_METRICS_HOST"]
    assert "exception" not in line


def _failing_bind(error: OSError) -> Any:
    def serve_metrics(host: str, port: int) -> Any:
        raise error

    return serve_metrics


@pytest.mark.parametrize(
    ("error", "variable"),
    [
        (socket.gaierror(socket.EAI_NONAME, "Name or service not known"), "HOST"),
        (OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address"), "HOST"),
        (OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol"), "HOST"),
        (OSError(errno.EADDRINUSE, "Address already in use"), "PORT"),
        (OSError(errno.EACCES, "Permission denied"), "PORT"),
    ],
)
def test_a_bind_failure_names_the_variable_it_is_the_fault_of(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: OSError,
    variable: str,
) -> None:
    monkeypatch.setattr("visionquery.runtime.process.serve_metrics", _failing_bind(error))
    with pytest.raises(SystemExit) as exited:
        start("probe", Settings)
    assert exited.value.code == 2
    (line,) = _logged(capsys)
    assert line["variables"] == [f"VISIONQUERY_METRICS_{variable}"]


@pytest.mark.parametrize("code", [errno.EMFILE, errno.ENOMEM, errno.ENOBUFS])
def test_a_bind_failure_that_is_neither_the_host_nor_the_port_is_not_blamed_on_either(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    error = OSError(code, "resource trouble")
    monkeypatch.setattr("visionquery.runtime.process.serve_metrics", _failing_bind(error))
    with pytest.raises(OSError, match="resource trouble") as raised:
        start("probe", Settings)
    assert raised.value is error
