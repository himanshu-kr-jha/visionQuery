"""Register the dev profile's fake cameras in the camera registry, so that they may publish.

MediaMTX lets a camera publish only to a path the registry has, with that camera's own
credentials (ADR 0002, ADR 0015). Each fake camera VISIONQUERY_FAKE_CAMERAS lists is therefore
made a pushed camera named for it, publishing to the path of its name as the user of its name,
with VISIONQUERY_FAKE_CAMERA_PASSWORD, which docker/fake-cameras/fake_cameras.py publishes with.
A camera already registered so is left alone, or given the password again when it changed; a
name or path another camera holds is refused, naming it. The reconciler then puts them in
MediaMTX within one cycle.

A development tool, never part of a Deployment: Compose's dev profile runs it once
(`register-fake-cameras`), in the VisionQuery image, before the fake cameras start.

    VISIONQUERY_DATABASE__URL=postgresql://... VISIONQUERY_FAKE_CAMERAS=gate=synthetic \\
    VISIONQUERY_FAKE_CAMERA_PASSWORD=... python scripts/register_fake_cameras.py

Exits 0 when every camera is registered, 1 when one could not be or the database could not be
reached, and 2 when a setting is missing or invalid.
"""

import logging
from collections.abc import Sequence
from typing import Annotated

from fake_cameras import Camera, parse_cameras
from pydantic import Field, SecretStr, field_validator
from visionquery.memory.cameras import (
    PASSWORD_MINIMUM,
    CameraError,
    CameraRegistry,
    PublishCredentials,
)
from visionquery.memory.connection import ConnectionFailedError, connect
from visionquery.memory.settings import DatabaseSettings
from visionquery.runtime import Settings, start

_log = logging.getLogger("register-fake-cameras")


class RegistrationError(Exception):
    """A fake camera could not be registered."""


class FakeCameraSettings(Settings):
    """What the registration reads from the environment."""

    database: DatabaseSettings
    #: As the fake cameras read it: `name=video`, comma-separated.
    fake_cameras: str
    fake_camera_password: Annotated[SecretStr, Field(min_length=PASSWORD_MINIMUM)]
    #: It runs for a second: any free port.
    metrics_port: int = 0

    @field_validator("fake_cameras")
    @classmethod
    def _lists_cameras(cls, spec: str) -> str:
        parse_cameras(spec)
        return spec


def register(registry: CameraRegistry, cameras: Sequence[Camera], password: str) -> list[str]:
    """Make each fake camera a pushed camera publishing as itself with `password`; the names of
    those added or changed."""
    current = {camera.path: camera for camera in registry.list()}
    changed: list[str] = []
    for fake in cameras:
        try:
            credentials = PublishCredentials.of(fake.name, password)
            found = current.get(fake.name)
            if found is None:
                registry.add_pushed(fake.name, fake.name, credentials)
            elif not found.pushed or found.name.casefold() != fake.name.casefold():
                raise RegistrationError(
                    f"the path {fake.name!r} is camera {found.id}'s, named {found.name!r}: "
                    "remove it, or rename the fake camera"
                )
            elif (has := found.publish_credentials) is None or not (
                has.user == fake.name and has.verify(password)
            ):
                registry.change_publish_credentials(found.id, credentials)
            else:
                continue
        except CameraError as error:
            raise RegistrationError(f"{fake.name}: {error}") from None
        changed.append(fake.name)
    return changed


def main() -> int:
    settings = start("register-fake-cameras", FakeCameraSettings).settings
    cameras = parse_cameras(settings.fake_cameras)
    try:
        with connect(settings.database.url.get_secret_value(), 10) as connection:
            changed = register(
                CameraRegistry(connection),
                cameras,
                settings.fake_camera_password.get_secret_value(),
            )
    except (ConnectionFailedError, RegistrationError) as error:
        _log.error("the fake cameras could not be registered: %s", error)
        return 1
    _log.info(
        "%d fake cameras registered, %d of them added or changed",
        len(cameras),
        len(changed),
        extra={"changed": changed},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
