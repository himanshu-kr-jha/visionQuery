"""scripts/register_fake_cameras.py: the dev profile's fake cameras, registered so they may publish.

Here rather than in tests/ because it needs what these tests have: a real Postgres
(packages/conftest.py) and a real MediaMTX (conftest.py), so that a registered fake camera is
shown publishing, through the reconciler, exactly as the fake cameras do.
"""

import logging
from collections.abc import Iterator

import pytest
from fake_cameras import Camera as FakeCamera
from fake_cameras import parse_cameras, publish_url
from pydantic import SecretStr
from real_mediamtx import API_USER, READER_USER, RTSP, MediaMTX
from register_fake_cameras import RegistrationError, main, register
from visionquery.memory.cameras import CameraRegistry, PublishCredentials
from visionquery.memory.connection import connect
from visionquery.memory.migrate import migrate
from visionquery.reconciler.mediamtx import MediaMTXClient
from visionquery.reconciler.reconcile import Reconciler

PASSWORD = "fake-camera-pa55word-0123456789"
CAMERAS = parse_cameras("gym-g299=meva-school-g299.avi,stairs=synthetic")


@pytest.fixture
def migrated(database: str) -> str:
    migrate(database)
    return database


@pytest.fixture
def registry(migrated: str) -> Iterator[CameraRegistry]:
    with connect(migrated, 5) as connection:
        yield CameraRegistry(connection)


def test_each_fake_camera_is_a_pushed_camera_publishing_as_itself(
    registry: CameraRegistry,
) -> None:
    assert register(registry, CAMERAS, PASSWORD) == ["gym-g299", "stairs"]
    cameras = registry.list()
    assert [(c.name, c.path, c.pushed) for c in cameras] == [
        ("gym-g299", "gym-g299", True),
        ("stairs", "stairs", True),
    ]
    for camera in cameras:
        assert camera.publish_credentials is not None
        assert camera.publish_credentials.user == camera.name
        assert camera.publish_credentials.verify(PASSWORD)


def test_registering_again_changes_nothing(registry: CameraRegistry) -> None:
    register(registry, CAMERAS, PASSWORD)
    before = registry.list()
    assert register(registry, CAMERAS, PASSWORD) == []
    assert registry.list() == before


def test_a_new_password_or_lost_credentials_are_put_right(registry: CameraRegistry) -> None:
    register(registry, CAMERAS, PASSWORD)
    gym, stairs = registry.list()
    registry.change_publish_credentials(stairs.id, None)
    assert register(registry, CAMERAS, PASSWORD) == ["stairs"]
    assert register(registry, CAMERAS, "another-pa55word-0123") == ["gym-g299", "stairs"]
    for camera in registry.list():
        assert camera.publish_credentials is not None
        assert camera.publish_credentials.verify("another-pa55word-0123")
    assert [c.id for c in registry.list()] == [gym.id, stairs.id]


def test_a_name_or_path_another_camera_holds_is_refused_naming_it(
    registry: CameraRegistry,
) -> None:
    registry.add_pushed("gym-g299", "elsewhere")
    with pytest.raises(RegistrationError, match="gym-g299"):
        register(registry, CAMERAS, PASSWORD)
    other = registry.add_pushed("The stairs", "stairs")
    with pytest.raises(RegistrationError, match="stairs"):
        register(registry, [FakeCamera("stairs", "synthetic")], PASSWORD)
    assert registry.get(other.id) == other


def test_a_registered_fake_camera_publishes_once_reconciled(
    mediamtx: MediaMTX, registry: CameraRegistry
) -> None:
    register(registry, CAMERAS, PASSWORD)
    client = MediaMTXClient(mediamtx.api_url, API_USER, SecretStr(mediamtx.api_password))
    Reconciler(client, {READER_USER: SecretStr(mediamtx.reader_password)}).reconcile(
        registry.list()
    )
    url = publish_url(f"rtsp://{RTSP}", CAMERAS[0], PASSWORD)
    assert url.startswith("rtsp://gym-g299:")
    user, password = url.removeprefix("rtsp://").split("@")[0].split(":")
    assert mediamtx.publish("gym-g299", user, password).returncode == 0
    assert mediamtx.publish("gym-g299", "stairs", PASSWORD).returncode != 0


def _environment(monkeypatch: pytest.MonkeyPatch, database: str, **variables: str) -> None:
    monkeypatch.setenv("VISIONQUERY_DATABASE__URL", database)
    monkeypatch.setenv("VISIONQUERY_FAKE_CAMERAS", "gym-g299=meva-school-g299.avi,stairs=synthetic")
    monkeypatch.setenv("VISIONQUERY_FAKE_CAMERA_PASSWORD", PASSWORD)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)


def test_the_command_registers_the_cameras_the_environment_lists(
    monkeypatch: pytest.MonkeyPatch,
    migrated: str,
    registry: CameraRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _environment(monkeypatch, migrated)
    caplog.set_level(logging.INFO)
    assert main() == 0
    assert [c.name for c in registry.list()] == ["gym-g299", "stairs"]
    assert PASSWORD not in caplog.text
    assert main() == 0


def test_the_command_fails_when_a_camera_cannot_be_registered(
    monkeypatch: pytest.MonkeyPatch, migrated: str, registry: CameraRegistry
) -> None:
    _environment(monkeypatch, migrated)
    registry.add_pulled("gym-g299", "rtsp://10.0.0.7/s")
    assert main() == 1


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("VISIONQUERY_FAKE_CAMERA_PASSWORD", "too-short"),
        # What Compose gives it when .env sets none (the dev profile alone needs one).
        ("VISIONQUERY_FAKE_CAMERA_PASSWORD", ""),
        ("VISIONQUERY_FAKE_CAMERAS", "bad name=synthetic"),
    ],
)
def test_bad_settings_stop_it_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated: str,
    variable: str,
    value: str,
) -> None:
    _environment(monkeypatch, migrated, **{variable: value})
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2
    output = capsys.readouterr().out
    assert variable in output
    assert "too-short" not in output


def test_publish_credentials_made_for_a_fake_camera_are_a_cameras_own() -> None:
    # The user is the camera's name, which MediaMTX and a URL both take as it is.
    for camera in parse_cameras("a=synthetic,gym_g-299=synthetic"):
        assert PublishCredentials.of(camera.name, PASSWORD).user == camera.name
