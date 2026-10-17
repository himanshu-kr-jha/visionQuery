"""What a camera publishes with, and the edits MediaMTX must follow: new publish credentials for a
pushed camera, a new URL for a pulled one.

A pushed camera publishes to its path with credentials of its own, which the registry keeps only
as an argon2id hash; a pulled camera can be given a new URL when the camera moves or its password
changes. Runs against a real Postgres with the shipped migrations applied (see
packages/conftest.py), in a database of its own.
"""

import traceback
from collections.abc import Iterator

import psycopg
import pytest
from pydantic import SecretStr, TypeAdapter
from visionquery.memory.cameras import (
    Camera,
    CameraNotFoundError,
    CameraRegistry,
    CameraUrl,
    InvalidCameraError,
    PublishCredentials,
)
from visionquery.memory.connection import connect
from visionquery.memory.migrate import migrate

PASSWORD = "dock-camera-pa55word"


@pytest.fixture
def migrated(database: str) -> str:
    migrate(database)
    return database


@pytest.fixture
def registry(migrated: str) -> Iterator[CameraRegistry]:
    with connect(migrated, 5) as connection:
        yield CameraRegistry(connection)


def _shown(error: BaseException) -> str:
    return "".join(traceback.format_exception(error)) + repr(error)


# --- publish credentials ----------------------------------------------------------------------


def test_publish_credentials_keep_only_a_hash_of_the_password() -> None:
    credentials = PublishCredentials.of("dock", PASSWORD)
    assert credentials.user == "dock"
    # What MediaMTX checks a publisher against (`argon2:` + this), salted: never the password.
    assert credentials.password_hash.startswith("$argon2id$")
    assert PASSWORD not in credentials.password_hash
    assert PublishCredentials.of("dock", PASSWORD).password_hash != credentials.password_hash
    assert credentials.verify(PASSWORD)
    assert not credentials.verify(PASSWORD + "x")
    assert not credentials.verify("")


def test_publish_credentials_accept_a_secret_and_show_neither_password_nor_hash() -> None:
    credentials = PublishCredentials.of("dock", SecretStr(PASSWORD))
    assert credentials.verify(PASSWORD)
    for shown in (repr(credentials), str(credentials)):
        assert "dock" in shown
        assert PASSWORD not in shown
        assert credentials.password_hash not in shown


@pytest.mark.parametrize(
    "user",
    [
        "",
        # MediaMTX lets the user `any` in with any password, or none.
        "any",
        "ANY",
        "Any",
        "dock camera",
        "dock:1",
        "dock@site",
        "site/dock",
        "dock%20",
        "döck",
        "d" * 65,
    ],
)
def test_a_publish_user_that_could_not_be_put_in_a_url_or_lets_anyone_in_is_refused(
    user: str,
) -> None:
    with pytest.raises(InvalidCameraError):
        PublishCredentials.of(user, PASSWORD)


@pytest.mark.parametrize("user", ["dock", "Dock-2", "site.a_dock-2", "d" * 64])
def test_a_publish_user_of_letters_digits_dots_dashes_and_underscores_is_accepted(
    user: str,
) -> None:
    assert PublishCredentials.of(user, PASSWORD).user == user


@pytest.mark.parametrize(
    "password",
    ["", "short", "fifteen-chars-x", "p" * 257, "tab\tinside-a-password", "nul\0inside-password"],
)
def test_a_weak_or_unprintable_publish_password_is_refused_without_being_quoted(
    password: str,
) -> None:
    with pytest.raises(InvalidCameraError) as raised:
        PublishCredentials.of("dock", password)
    if password:
        assert password not in _shown(raised.value)


@pytest.mark.parametrize("password", ["sixteen-chars-xx", "p" * 256, "with spaces and ünïcode ok"])
def test_a_long_enough_printable_publish_password_is_accepted(password: str) -> None:
    assert PublishCredentials.of("dock", password).verify(password)


def test_a_pushed_camera_is_added_with_its_publish_credentials(registry: CameraRegistry) -> None:
    camera = registry.add_pushed(
        "Loading dock", "loading-dock", PublishCredentials.of("dock", PASSWORD)
    )
    assert camera.publish_credentials is not None
    assert camera.publish_credentials.user == "dock"
    assert camera.publish_credentials.verify(PASSWORD)
    assert registry.get(camera.id) == camera
    assert registry.list() == [camera]


def test_a_pushed_camera_added_without_credentials_has_none(registry: CameraRegistry) -> None:
    # Nobody can publish to it until it is given some.
    camera = registry.add_pushed("Loading dock", "loading-dock")
    assert camera.publish_credentials is None
    pulled = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    assert pulled.publish_credentials is None


def test_a_pushed_cameras_publish_credentials_are_changed(registry: CameraRegistry) -> None:
    camera = registry.add_pushed(
        "Loading dock", "loading-dock", PublishCredentials.of("dock", PASSWORD)
    )
    changed = registry.change_publish_credentials(
        camera.id, PublishCredentials.of("dock-2", "another-pa55word-x")
    )
    assert changed.publish_credentials is not None
    assert changed.publish_credentials.user == "dock-2"
    assert changed.publish_credentials.verify("another-pa55word-x")
    assert not changed.publish_credentials.verify(PASSWORD)
    assert (changed.id, changed.name, changed.path) == (camera.id, camera.name, camera.path)
    assert registry.get(camera.id) == changed
    # And taken away, so that nothing can publish to it.
    assert registry.change_publish_credentials(camera.id, None).publish_credentials is None


def test_a_pulled_camera_has_no_publish_credentials_to_change(registry: CameraRegistry) -> None:
    camera = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    with pytest.raises(InvalidCameraError):
        registry.change_publish_credentials(camera.id, PublishCredentials.of("gate", PASSWORD))
    assert registry.get(camera.id) == camera


def test_removing_a_pushed_camera_forgets_its_publish_credentials(
    registry: CameraRegistry, migrated: str
) -> None:
    camera = registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", PASSWORD))
    registry.remove(camera.id)
    assert registry.get(camera.id).publish_credentials is None
    with psycopg.connect(migrated) as connection:
        row = connection.execute(
            "SELECT publish_user, publish_password_hash FROM cameras WHERE id = %s", (camera.id,)
        ).fetchone()
    assert row == (None, None)


@pytest.mark.parametrize(
    "statement",
    [
        # A pulled camera publishes nothing.
        "INSERT INTO cameras (name, pull_url, pull_target, publish_user, "
        "publish_password_hash) VALUES ('Gate 2', 'rtsp://10.0.0.7', '/s', 'gate', '$argon2id$x')",
        # A user without a password, or a password without a user.
        "INSERT INTO cameras (name, push_path, publish_user) VALUES ('Dock', 'dock', 'dock')",
        "INSERT INTO cameras (name, push_path, publish_password_hash) "
        "VALUES ('Dock', 'dock', '$argon2id$x')",
        # Only a hash is ever kept.
        "INSERT INTO cameras (name, push_path, publish_user, publish_password_hash) "
        "VALUES ('Dock', 'dock', 'dock', 'a-plain-password')",
        # A removed camera keeps no credentials.
        "INSERT INTO cameras (name, push_path, publish_user, publish_password_hash, removed_at) "
        "VALUES ('Dock', 'dock', 'dock', '$argon2id$x', now())",
    ],
)
def test_the_database_refuses_publish_credentials_that_make_no_sense(
    migrated: str, statement: str
) -> None:
    with (
        psycopg.connect(migrated, autocommit=True) as connection,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        connection.execute(statement.encode())


def test_a_camera_serialises_without_its_password_hash(registry: CameraRegistry) -> None:
    camera = registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", PASSWORD))
    assert camera.publish_credentials is not None
    dumped = TypeAdapter(Camera).dump_json(camera).decode()
    assert camera.publish_credentials.password_hash not in dumped
    assert '"user":"dock"' in dumped


# --- a new pull URL ---------------------------------------------------------------------------


def test_a_pulled_cameras_url_is_changed(registry: CameraRegistry, migrated: str) -> None:
    camera = registry.add_pulled("Gate 2", "rtsp://admin:old-pw@10.0.0.7/s")
    changed = registry.change_pull_url(camera.id, "rtsp://admin:new-pw@10.0.0.8:8554/main")
    assert changed.pull_url == CameraUrl("rtsp://admin:new-pw@10.0.0.8:8554/main")
    # The path MediaMTX pulls it into is its id's, whatever the URL.
    assert (changed.id, changed.name, changed.path) == (camera.id, camera.name, camera.path)
    assert registry.get(camera.id) == changed
    # And to one without credentials, which are then forgotten, with the path and query kept
    # apart from the public part.
    registry.change_pull_url(camera.id, CameraUrl("rtsp://10.0.0.9/s?token=t0k3n"))
    with psycopg.connect(migrated) as connection:
        row = connection.execute(
            "SELECT pull_url, pull_credentials, pull_target FROM cameras WHERE id = %s",
            (camera.id,),
        ).fetchone()
    assert row == ("rtsp://10.0.0.9", None, "/s?token=t0k3n")
    # A URL with no path or query is stored with an empty target, which the database requires.
    registry.change_pull_url(camera.id, CameraUrl("rtsp://10.0.0.9"))
    with psycopg.connect(migrated) as connection:
        row = connection.execute(
            "SELECT pull_url, pull_target FROM cameras WHERE id = %s", (camera.id,)
        ).fetchone()
    assert row == ("rtsp://10.0.0.9", "")


def test_an_invalid_new_url_is_refused_and_the_old_one_kept(registry: CameraRegistry) -> None:
    camera = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    with pytest.raises(InvalidCameraError):
        registry.change_pull_url(camera.id, "http://10.0.0.8/s")
    assert registry.get(camera.id) == camera


def test_a_pushed_camera_has_no_url_to_change(registry: CameraRegistry) -> None:
    camera = registry.add_pushed("Dock", "dock")
    with pytest.raises(InvalidCameraError):
        registry.change_pull_url(camera.id, "rtsp://10.0.0.8/s")
    assert registry.get(camera.id) == camera


@pytest.mark.parametrize("removed", [True, False])
def test_an_unknown_or_removed_camera_cannot_be_edited(
    registry: CameraRegistry, removed: bool
) -> None:
    pulled = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    pushed = registry.add_pushed("Dock", "dock")
    if removed:
        registry.remove(pulled.id)
        registry.remove(pushed.id)
    pulled_id, pushed_id = (pulled.id, pushed.id) if removed else (404, 405)
    with pytest.raises(CameraNotFoundError):
        registry.change_pull_url(pulled_id, "rtsp://10.0.0.8/s")
    with pytest.raises(CameraNotFoundError):
        registry.change_publish_credentials(pushed_id, PublishCredentials.of("dock", PASSWORD))
