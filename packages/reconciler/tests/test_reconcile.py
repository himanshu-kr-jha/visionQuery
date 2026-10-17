"""One reconcile makes a real MediaMTX match the camera registry, and changes nothing after.

Each test has a migrated database of its own (packages/conftest.py) and a freshly restarted
MediaMTX running the Deployment's configuration (conftest.py), which cameras publish to and are
read from by real ffmpeg containers.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr
from real_mediamtx import SERVICE_USER, MediaMTX
from visionquery.memory.cameras import CameraRegistry, PublishCredentials
from visionquery.memory.connection import connect
from visionquery.memory.migrate import migrate
from visionquery.reconciler.mediamtx import MediaMTXClient
from visionquery.reconciler.reconcile import Changes, ReconcileError, Reconciler

DOCK_PASSWORD = "dock-camera-pa55word"
GATE_PASSWORD = "gate-camera-pa55word"


@pytest.fixture
def registry(database: str) -> Iterator[CameraRegistry]:
    migrate(database)
    with connect(database, 5) as connection:
        yield CameraRegistry(connection)


@pytest.fixture
def reconciler(mediamtx: MediaMTX) -> Reconciler:
    return Reconciler(MediaMTXClient(mediamtx.api_url, SERVICE_USER, SecretStr(mediamtx.password)))


def _publishers(mediamtx: MediaMTX) -> dict[str, list[dict[str, Any]]]:
    """The users MediaMTX lets publish, by user, with what each may publish to."""
    users = mediamtx.users()
    return {
        user["user"]: user["permissions"]
        for user in users
        if any(permission["action"] == "publish" for permission in user["permissions"])
    }


def _snapshot(mediamtx: MediaMTX) -> tuple[object, object]:
    return mediamtx.path_configs(), mediamtx.users()


# --- adding -----------------------------------------------------------------------------------


def test_a_pushed_camera_can_publish_with_its_credentials_after_one_reconcile(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    camera = registry.add_pushed(
        "Dock", "site-a/dock", PublishCredentials.of("dock", DOCK_PASSWORD)
    )
    # Before the reconcile MediaMTX knows nothing of it.
    assert mediamtx.publish("site-a/dock", "dock", DOCK_PASSWORD).returncode != 0

    changes = reconciler.reconcile(registry.list())

    assert changes.added == ("site-a/dock",)
    assert changes.publishers
    assert mediamtx.path_configs()["site-a/dock"]["source"] == "publisher"
    assert _publishers(mediamtx) == {"dock": [{"action": "publish", "path": camera.path}]}
    published = mediamtx.publish("site-a/dock", "dock", DOCK_PASSWORD)
    assert published.returncode == 0, published.stderr
    # And VisionQuery's own services read it.
    with mediamtx.publishing("site-a/dock", "dock", DOCK_PASSWORD):
        read = mediamtx.read("site-a/dock", SERVICE_USER, mediamtx.password)
        assert read.returncode == 0, read.stderr


def test_a_pulled_camera_is_pulled_from_its_url_after_one_reconcile(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    # The camera MediaMTX pulls from is MediaMTX itself, reading a pushed camera's path as
    # VisionQuery's services do: so the pull goes over RTSP, with credentials in the URL.
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    pulled = registry.add_pulled(
        "Dock again", f"rtsp://{SERVICE_USER}:{mediamtx.password}@127.0.0.1:8554/dock"
    )

    changes = reconciler.reconcile(registry.list())

    assert set(changes.added) == {"dock", pulled.path}
    conf = mediamtx.path_configs()[pulled.path]
    assert pulled.pull_url is not None
    assert conf["source"] == pulled.pull_url.get_secret_value()
    # RTP over TCP, as everything else in the Deployment.
    assert conf["rtspTransport"] == "tcp"
    with mediamtx.publishing("dock", "dock", DOCK_PASSWORD):
        mediamtx.wait_until_published(pulled.path)
        read = mediamtx.read(pulled.path, SERVICE_USER, mediamtx.password)
        assert read.returncode == 0, read.stderr


# --- editing ----------------------------------------------------------------------------------


def test_a_new_url_is_pulled_from_after_one_reconcile(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    camera = registry.add_pulled("Gate 2", "rtsp://admin:old-pw@10.0.0.7/s")
    reconciler.reconcile(registry.list())
    changed = registry.change_pull_url(camera.id, "rtsp://admin:new-pw@10.0.0.8:8554/main")

    changes = reconciler.reconcile(registry.list())

    assert changes == Changes(updated=(camera.path,))
    assert changed.pull_url is not None
    assert mediamtx.path_configs()[camera.path]["source"] == changed.pull_url.get_secret_value()


def test_new_publish_credentials_take_over_after_one_reconcile(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    camera = registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    reconciler.reconcile(registry.list())
    registry.change_publish_credentials(camera.id, PublishCredentials.of("dock-2", GATE_PASSWORD))

    changes = reconciler.reconcile(registry.list())

    assert changes == Changes(publishers=True)
    assert _publishers(mediamtx) == {"dock-2": [{"action": "publish", "path": "dock"}]}
    assert mediamtx.publish("dock", "dock", DOCK_PASSWORD).returncode != 0
    assert mediamtx.publish("dock", "dock-2", GATE_PASSWORD).returncode == 0


def test_a_new_password_for_the_same_user_takes_over_after_one_reconcile(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    # MediaMTX never shows a password back, so the reconciler must notice this one itself.
    camera = registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    reconciler.reconcile(registry.list())
    registry.change_publish_credentials(camera.id, PublishCredentials.of("dock", GATE_PASSWORD))

    assert reconciler.reconcile(registry.list()) == Changes(publishers=True)
    assert mediamtx.publish("dock", "dock", DOCK_PASSWORD).returncode != 0
    assert mediamtx.publish("dock", "dock", GATE_PASSWORD).returncode == 0


def test_renaming_a_camera_changes_nothing_in_mediamtx(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    camera = registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    reconciler.reconcile(registry.list())
    before = _snapshot(mediamtx)
    registry.rename(camera.id, "Loading dock")
    assert reconciler.reconcile(registry.list()) == Changes()
    assert _snapshot(mediamtx) == before


# --- removing ---------------------------------------------------------------------------------


def test_a_removed_camera_is_gone_from_mediamtx_after_one_reconcile(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    dock = registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    gate = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    kept = registry.add_pushed("Stairs", "stairs", PublishCredentials.of("stairs", GATE_PASSWORD))
    reconciler.reconcile(registry.list())
    registry.remove(dock.id)
    registry.remove(gate.id)

    changes = reconciler.reconcile(registry.list())

    assert set(changes.removed) == {"dock", gate.path}
    assert changes.publishers
    assert set(mediamtx.path_configs()) == {kept.path}
    assert set(_publishers(mediamtx)) == {"stairs"}
    assert mediamtx.publish("dock", "dock", DOCK_PASSWORD).returncode != 0


def test_a_pushed_camera_without_credentials_has_a_path_nobody_may_publish_to(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    registry.add_pushed("Dock", "dock")
    reconciler.reconcile(registry.list())
    assert "dock" in mediamtx.path_configs()
    assert _publishers(mediamtx) == {}
    assert mediamtx.publish("dock").returncode != 0
    assert mediamtx.publish("dock", SERVICE_USER, mediamtx.password).returncode != 0


# --- twice, and drift -------------------------------------------------------------------------


def test_reconciling_twice_in_a_row_changes_nothing_the_second_time(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    registry.add_pushed("Stairs", "site-a/stairs")
    registry.add_pulled("Gate 2", "rtsp://admin:pw@10.0.0.7/s")
    assert reconciler.reconcile(registry.list())
    after_first = _snapshot(mediamtx)

    second = reconciler.reconcile(registry.list())

    assert second == Changes()
    assert not second
    assert _snapshot(mediamtx) == after_first


def test_an_empty_registry_changes_nothing_in_a_fresh_mediamtx(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    before = _snapshot(mediamtx)
    reconciler.reconcile(registry.list())
    assert reconciler.reconcile(registry.list()) == Changes()
    assert _snapshot(mediamtx) == before


def test_paths_changed_behind_the_reconcilers_back_are_put_right(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    gate = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    registry.add_pushed("Stairs", "stairs")
    reconciler.reconcile(registry.list())
    expected = _snapshot(mediamtx)
    mediamtx.api("PATCH", f"/v3/config/paths/patch/{gate.path}", {"record": True})
    mediamtx.api("DELETE", "/v3/config/paths/delete/stairs")
    mediamtx.api("POST", "/v3/config/paths/add/intruder", {"source": "publisher"})

    changes = reconciler.reconcile(registry.list())

    assert changes == Changes(added=("stairs",), updated=(gate.path,), removed=("intruder",))
    assert _snapshot(mediamtx) == expected


def test_publishers_changed_behind_the_reconcilers_back_are_put_right(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    reconciler.reconcile(registry.list())
    expected = _snapshot(mediamtx)
    users = mediamtx.users()
    for user in users:
        if user["user"] == SERVICE_USER:
            user["pass"] = mediamtx.password
    stranger: dict[str, Any] = {
        "user": "stranger",
        "pass": "",
        "ips": [],
        "permissions": [{"action": "publish", "path": "dock"}],
    }
    kept = [u for u in users if u["user"] != "dock"]
    mediamtx.api("PATCH", "/v3/config/global/patch", {"authInternalUsers": [*kept, stranger]})
    assert mediamtx.publish("dock", "stranger", "anything").returncode == 0

    assert reconciler.reconcile(registry.list()) == Changes(publishers=True)
    assert _snapshot(mediamtx) == expected
    assert mediamtx.publish("dock", "stranger", "anything").returncode != 0
    assert mediamtx.publish("dock", "dock", DOCK_PASSWORD).returncode == 0


def test_a_user_whose_password_cannot_be_kept_is_refused_and_nothing_is_written(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    # MediaMTX never shows a password back, so the reconciler, which rewrites the list of
    # users, can keep only those without one, and its own.
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    users = mediamtx.users()
    for user in users:
        if user["user"] == SERVICE_USER:
            user["pass"] = mediamtx.password
    other: dict[str, Any] = {
        "user": "viewer",
        "pass": "a-viewers-password",
        "ips": [],
        "permissions": [{"action": "read", "path": ""}],
    }
    mediamtx.api("PATCH", "/v3/config/global/patch", {"authInternalUsers": [*users, other]})
    before = mediamtx.users()

    with pytest.raises(ReconcileError, match="viewer"):
        reconciler.reconcile(registry.list())
    assert mediamtx.users() == before


# --- a restart --------------------------------------------------------------------------------


def test_a_restarted_mediamtx_is_put_right_by_one_reconcile(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    gate = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    reconciler.reconcile(registry.list())
    expected = _snapshot(mediamtx)

    # MediaMTX keeps what its API was told only until it restarts.
    mediamtx.restart()
    assert mediamtx.path_configs() == {}

    changes = reconciler.reconcile(registry.list())

    assert set(changes.added) == {gate.path, "dock"}
    assert changes.publishers
    assert _snapshot(mediamtx) == expected
    assert mediamtx.publish("dock", "dock", DOCK_PASSWORD).returncode == 0
    assert reconciler.reconcile(registry.list()) == Changes()


# --- who may publish and read (ADR 0002) ------------------------------------------------------


def test_only_a_cameras_own_credentials_publish_to_it_and_only_visionquery_reads(
    mediamtx: MediaMTX, registry: CameraRegistry, reconciler: Reconciler
) -> None:
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    registry.add_pushed("Gate", "gate", PublishCredentials.of("gate", GATE_PASSWORD))
    reconciler.reconcile(registry.list())

    # Publishing: never without credentials, never with another camera's, never to a path the
    # registry does not have, and never as VisionQuery's own services.
    assert mediamtx.publish("dock").returncode != 0
    assert mediamtx.publish("dock", "dock", "not-the-password").returncode != 0
    assert mediamtx.publish("dock", "gate", GATE_PASSWORD).returncode != 0
    assert mediamtx.publish("unregistered").returncode != 0
    assert mediamtx.publish("unregistered", "dock", DOCK_PASSWORD).returncode != 0
    assert mediamtx.publish("dock", SERVICE_USER, mediamtx.password).returncode != 0

    with mediamtx.publishing("dock", "dock", DOCK_PASSWORD):
        # Reading: only VisionQuery's own services, not anyone, nor the camera itself.
        refused = mediamtx.read("dock")
        assert refused.returncode != 0
        assert "401" in refused.stderr
        assert mediamtx.read("dock", "dock", DOCK_PASSWORD).returncode != 0
        assert mediamtx.read("dock", SERVICE_USER, "not-the-password").returncode != 0
        assert mediamtx.read("dock", SERVICE_USER, mediamtx.password).returncode == 0
