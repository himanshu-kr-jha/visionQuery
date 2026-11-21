"""What `visionquery-recorder` reads from the environment, and the URL it reads a camera from."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from visionquery.memory.cameras import Camera
from visionquery.recorder.serve import MediaMTXReader, RecorderSettings, source_for
from visionquery.runtime import SettingsError

ADDED = datetime(2026, 10, 6, tzinfo=UTC)


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VISIONQUERY_DATABASE__URL", "postgresql://visionquery@postgres/visionquery")
    monkeypatch.setenv("VISIONQUERY_MEDIAMTX__PASSWORD", "read-pa55word")
    monkeypatch.setenv("VISIONQUERY_FOOTAGE_DIR", str(tmp_path))


@pytest.mark.usefixtures("environment")
def test_segments_are_five_minutes_unless_told_otherwise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = RecorderSettings.load()

    assert settings.segment_seconds == 300
    assert settings.footage_dir == tmp_path
    assert settings.mediamtx.rtsp_url == "rtsp://127.0.0.1:8554"
    assert settings.mediamtx.user == "visionquery-reader"

    monkeypatch.setenv("VISIONQUERY_SEGMENT_SECONDS", "60")
    assert RecorderSettings.load().segment_seconds == 60


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("seconds", ["0", "-5", "7", "7200", "five"])
def test_a_segment_length_must_divide_a_day_into_whole_segments_of_an_hour_at_most(
    monkeypatch: pytest.MonkeyPatch, seconds: str
) -> None:
    """So that no Segment straddles midnight, and a day of Footage is one directory."""
    monkeypatch.setenv("VISIONQUERY_SEGMENT_SECONDS", seconds)

    with pytest.raises(SettingsError) as raised:
        RecorderSettings.load()

    assert raised.value.variables == ("VISIONQUERY_SEGMENT_SECONDS",)


@pytest.mark.usefixtures("environment")
def test_the_footage_directory_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISIONQUERY_FOOTAGE_DIR")

    with pytest.raises(SettingsError) as raised:
        RecorderSettings.load()

    assert raised.value.variables == ("VISIONQUERY_FOOTAGE_DIR",)


@pytest.mark.parametrize(
    "url",
    [
        "http://mediamtx:8554",
        "rtsp://",
        "rtsp://visionquery-reader:secret@mediamtx:8554",
        "rtsp://mediamtx:8554/dock",
        "rtsp://mediamtx:port",
        "mediamtx:8554",
    ],
)
@pytest.mark.usefixtures("environment")
def test_mediamtx_is_an_rtsp_url_without_credentials_or_a_path(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("VISIONQUERY_MEDIAMTX__RTSP_URL", url)

    with pytest.raises(SettingsError, match="rtsp") as raised:
        RecorderSettings.load()

    assert raised.value.variables == ("VISIONQUERY_MEDIAMTX__RTSP_URL",)
    assert "secret" not in str(raised.value)


def test_a_camera_is_read_from_its_path_as_the_reader() -> None:
    reader = MediaMTXReader(rtsp_url="rtsp://mediamtx:8554", password=SecretStr("p@ss/w:rd#1?"))
    camera = Camera(12, "Gate 2", "pulled/12", None, ADDED, None)

    source = source_for(reader)(camera)

    assert (
        source.url.get_secret_value()
        == "rtsp://visionquery-reader:p%40ss%2Fw%3Ard%231%3F@mediamtx:8554/pulled/12"
    )
    assert "p@ss" not in repr(source)


def test_an_rtsps_mediamtx_is_read_over_tls() -> None:
    reader = MediaMTXReader(rtsp_url="rtsps://mediamtx:8322", password=SecretStr("pw"))
    camera = Camera(3, "Dock", "dock", None, ADDED, None)

    assert (
        source_for(reader)(camera).url.get_secret_value()
        == "rtsps://visionquery-reader:pw@mediamtx:8322/dock"
    )
