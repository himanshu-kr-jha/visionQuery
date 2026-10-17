"""`visionquery-reconciler status`: whether every camera's video is arriving in MediaMTX.

Against a real Postgres (packages/conftest.py) and a real MediaMTX (conftest.py), with a camera
publishing through ffmpeg as a real one would.
"""

import json
import logging
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from real_mediamtx import SERVICE_USER, MediaMTX
from visionquery.memory.cameras import CameraRegistry, PublishCredentials
from visionquery.memory.connection import connect
from visionquery.memory.migrate import migrate
from visionquery.reconciler.mediamtx import MediaMTXClient
from visionquery.reconciler.reconcile import Reconciler
from visionquery.reconciler.serve import main

DOCK_PASSWORD = "dock-camera-pa55word"


@pytest.fixture
def migrated(database: str) -> str:
    migrate(database)
    return database


@pytest.fixture
def registry(migrated: str) -> Iterator[CameraRegistry]:
    with connect(migrated, 5) as connection:
        yield CameraRegistry(connection)


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, migrated: str, mediamtx: MediaMTX) -> None:
    monkeypatch.setenv("VISIONQUERY_DATABASE__URL", migrated)
    monkeypatch.setenv("VISIONQUERY_MEDIAMTX__URL", mediamtx.api_url)
    monkeypatch.setenv("VISIONQUERY_MEDIAMTX__PASSWORD", mediamtx.password)
    # As in the container, where the running reconciler holds the default metrics port.
    monkeypatch.delenv("VISIONQUERY_METRICS_PORT", raising=False)


def _status(capsys: pytest.CaptureFixture[str]) -> tuple[int, list[dict[str, object]]]:
    with pytest.raises(SystemExit) as exited:
        main(["status"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    code = exited.value.code
    assert isinstance(code, int)
    return code, lines


def _reconcile(registry: CameraRegistry, mediamtx: MediaMTX) -> None:
    client = MediaMTXClient(mediamtx.api_url, SERVICE_USER, SecretStr(mediamtx.password))
    Reconciler(client).reconcile(registry.list())


@pytest.mark.usefixtures("environment")
def test_every_camera_receiving_video_is_ok(
    mediamtx: MediaMTX, registry: CameraRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    _reconcile(registry, mediamtx)
    with mediamtx.publishing("dock", "dock", DOCK_PASSWORD):
        code, lines = _status(capsys)
    assert code == 0, lines
    (camera,) = [line for line in lines if line.get("camera") == "Dock"]
    assert camera["path"] == "dock"
    assert camera["receiving"] is True
    assert camera["level"] == "INFO"
    assert lines[-1]["message"] == "status: 1 cameras, 1 receiving video"


@pytest.mark.usefixtures("environment")
def test_a_camera_receiving_nothing_is_named_and_fails_the_status(
    mediamtx: MediaMTX, registry: CameraRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    registry.add_pulled("Gate", "rtsp://admin:gate-pw@10.0.0.7/s")
    # Not reconciled: MediaMTX has no path for either yet.
    code, lines = _status(capsys)
    assert code == 1
    named = {line.get("camera"): line for line in lines if "camera" in line}
    assert set(named) == {"Dock", "Gate"}
    assert all(line["receiving"] is False and line["level"] == "WARNING" for line in named.values())
    assert lines[-1]["message"] == "status: 2 cameras, 0 receiving video"
    assert "gate-pw" not in json.dumps(lines)


@pytest.mark.usefixtures("environment")
def test_no_camera_is_ok(capsys: pytest.CaptureFixture[str]) -> None:
    code, lines = _status(capsys)
    assert code == 0
    assert lines[-1]["message"] == "status: 0 cameras, 0 receiving video"


@pytest.mark.usefixtures("environment")
def test_mediamtx_unreachable_fails_the_status_saying_so(
    mediamtx: MediaMTX,
    registry: CameraRegistry,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    registry.add_pushed("Dock", "dock")
    mediamtx.stop()
    try:
        code, lines = _status(capsys)
    finally:
        mediamtx.start()
    assert code == 1
    assert "MediaMTX could not be reached" in str(lines[-1]["message"])
