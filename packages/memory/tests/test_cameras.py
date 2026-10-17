"""The camera registry: cameras added by a pull URL or as a push path, with unique names.

Every test runs against a real Postgres with the shipped migrations applied (see
packages/conftest.py), in a database of its own.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql
from visionquery.memory.cameras import (
    PULLED_PATHS,
    Camera,
    CameraNameTakenError,
    CameraNotFoundError,
    CameraPathTakenError,
    CameraRegistry,
    CameraUrl,
    InvalidCameraError,
)
from visionquery.memory.connection import connect
from visionquery.memory.migrate import migrate


@pytest.fixture
def migrated(database: str) -> str:
    migrate(database)
    return database


@pytest.fixture
def registry(migrated: str) -> Iterator[CameraRegistry]:
    with connect(migrated, 5) as connection:
        yield CameraRegistry(connection)


@pytest.fixture(scope="module")
def shared(postgres: str) -> Iterator[CameraRegistry]:
    """One migrated database for the tests that only show what is refused, which write nothing."""
    name = f"test_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(postgres, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    url = postgres.rsplit("/", 1)[0] + f"/{name}"
    try:
        migrate(url)
        with connect(url, 5) as connection:
            yield CameraRegistry(connection)
    finally:
        with psycopg.connect(postgres, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


# --- adding -----------------------------------------------------------------------------------


def test_a_camera_is_added_by_its_pull_url(registry: CameraRegistry) -> None:
    before = datetime.now(UTC)
    camera = registry.add_pulled("Gate 2", "rtsp://admin:pw@10.0.0.7:554/stream1")
    assert camera.name == "Gate 2"
    assert camera.pull_url == CameraUrl("rtsp://admin:pw@10.0.0.7:554/stream1")
    assert not camera.pushed
    # MediaMTX pulls it into a path of the registry's choosing, which no push path can take.
    assert camera.path == f"{PULLED_PATHS}{camera.id}"
    assert before <= camera.added_at <= datetime.now(UTC)
    assert camera.removed_at is None
    assert registry.get(camera.id) == camera


def test_a_camera_is_added_as_a_push_path(registry: CameraRegistry) -> None:
    camera = registry.add_pushed("Loading dock", "site-a/loading-dock")
    assert camera.path == "site-a/loading-dock"
    assert camera.pull_url is None
    assert camera.pushed
    assert registry.get(camera.id) == camera


def test_a_pull_url_already_checked_is_taken_as_it_is(registry: CameraRegistry) -> None:
    url = CameraUrl("rtsp://10.0.0.7/s")
    assert registry.add_pulled("Gate 2", url).pull_url == url


@pytest.mark.parametrize(
    "url",
    [
        "10.0.0.7/stream1",
        "http://10.0.0.7/stream1",
        "rtsp://10.0.0.7:70000/s",
        "rtsp://admin:p@ss@10.0.0.7/s",
        "rtsp://10.0.0.7/s?password=x",
    ],
)
def test_an_invalid_pull_url_is_refused_and_nothing_is_added(
    shared: CameraRegistry, url: str
) -> None:
    with pytest.raises(InvalidCameraError):
        shared.add_pulled("Gate 2", url)
    assert shared.list() == []


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/gate-2",
        "gate-2/",
        "gate 2",
        "gate-2?x",
        "gate%202",
        "gate:2",
        "~^gate.*$",  # a MediaMTX regular-expression path
        "gateü",
        "a//b",
        "a/./b",
        "a/../b",
        "..",
        ".",
        "all",  # MediaMTX's own catch-all paths
        "all_others",
        f"{PULLED_PATHS}7",  # the registry's, for pulled cameras
        "x" * 201,
    ],
)
def test_an_invalid_push_path_is_refused_and_nothing_is_added(
    shared: CameraRegistry, path: str
) -> None:
    with pytest.raises(InvalidCameraError):
        shared.add_pushed("Gate 2", path)
    assert shared.list() == []


@pytest.mark.parametrize(
    "path", ["gate-2", "Gate_2", "site.a/gate-2", "a/b/c/d", "cam1.v2", "x" * 200, "pulled"]
)
def test_a_push_path_is_what_mediamtx_accepts(registry: CameraRegistry, path: str) -> None:
    assert registry.add_pushed("Gate 2", path).path == path


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("Gate 2", "Gate 2"),
        ("  Gate 2  ", "Gate 2"),
        ("Gate \t  2", "Gate 2"),
        ("Café door", "Café door"),  # one way of writing it, NFC
        ("गेट 2", "गेट 2"),
    ],
)
def test_a_name_is_kept_in_one_form(registry: CameraRegistry, given: str, kept: str) -> None:
    assert registry.add_pushed(given, "p").name == kept


@pytest.mark.parametrize("name", ["", "   ", "Gate\x002", "Gate\x1b2", "Gate​2", "x" * 101])
def test_an_invalid_name_is_refused(shared: CameraRegistry, name: str) -> None:
    with pytest.raises(InvalidCameraError):
        shared.add_pushed(name, "gate-2")
    assert shared.list() == []


# --- unique within the Deployment -------------------------------------------------------------


@pytest.mark.parametrize("again", ["Gate 2", "gate 2", "GATE  2", " Gate 2 "])
def test_two_cameras_cannot_share_a_name(registry: CameraRegistry, again: str) -> None:
    first = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    with pytest.raises(CameraNameTakenError) as raised:
        registry.add_pushed(again, "gate-2")
    assert raised.value.name == " ".join(again.split())
    with pytest.raises(CameraNameTakenError):
        registry.add_pulled(again, "rtsp://10.0.0.8/s")
    assert registry.list() == [first]


@pytest.mark.parametrize(
    ("first", "again"), [("Café door", "CAFÉ DOOR"), ("Γάτα", "ΓΆΤΑ"), ("Straße", "STRASSE")]
)
def test_names_are_unique_whatever_their_case_in_any_script_and_any_locale(
    postgres: str, first: str, again: str
) -> None:
    # The database's own locale must not decide it: in the C locale, lower() folds only ASCII.
    name = f"test_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(postgres, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0 LOCALE_PROVIDER libc LOCALE 'C'").format(
                sql.Identifier(name)
            )
        )
    url = postgres.rsplit("/", 1)[0] + f"/{name}"
    try:
        migrate(url)
        with connect(url, 5) as connection:
            registry = CameraRegistry(connection)
            registry.add_pushed(first, "a")
            with pytest.raises(CameraNameTakenError):
                registry.add_pushed(again, "b")
    finally:
        with psycopg.connect(postgres, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_two_cameras_cannot_share_a_push_path(registry: CameraRegistry) -> None:
    first = registry.add_pushed("Gate 2", "gate-2")
    with pytest.raises(CameraPathTakenError) as raised:
        registry.add_pushed("Gate 3", "gate-2")
    assert raised.value.path == "gate-2"
    assert registry.list() == [first]


def test_push_paths_differ_by_case(registry: CameraRegistry) -> None:
    # MediaMTX's paths are case-sensitive, so these are two paths.
    registry.add_pushed("Gate 2", "gate-2")
    assert registry.add_pushed("Gate 3", "Gate-2").path == "Gate-2"


def test_a_name_cannot_be_renamed_onto_another(registry: CameraRegistry) -> None:
    registry.add_pushed("Gate 2", "gate-2")
    other = registry.add_pushed("Gate 3", "gate-3")
    with pytest.raises(CameraNameTakenError):
        registry.rename(other.id, "gate 2")
    assert registry.get(other.id).name == "Gate 3"


def test_the_database_keeps_names_unique_whoever_writes(migrated: str) -> None:
    with psycopg.connect(migrated, autocommit=True) as connection:
        connection.execute("INSERT INTO cameras (name, push_path) VALUES ('Gate 2', 'a')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute("INSERT INTO cameras (name, push_path) VALUES ('GATE 2', 'b')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute("INSERT INTO cameras (name, push_path) VALUES ('Gate 3', 'a')")
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO cameras (name, push_path, pull_url) VALUES ('Gate 4', 'c', 'rtsp://x')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("INSERT INTO cameras (name) VALUES ('Gate 5')")
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO cameras (name, push_path) VALUES ('Gate 6', 'pulled/1')"
            )


# --- reading, renaming, removing --------------------------------------------------------------


def test_list_gives_every_current_camera_in_the_order_added(registry: CameraRegistry) -> None:
    a = registry.add_pushed("Stairs", "stairs")
    b = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    c = registry.add_pushed("Atrium", "atrium")
    assert registry.list() == [a, b, c]
    assert all(isinstance(camera, Camera) for camera in registry.list())


@pytest.mark.parametrize("unknown", [404, 0, -1, 2**31, 2**80])
def test_an_unknown_camera_is_not_found(registry: CameraRegistry, unknown: int) -> None:
    registry.add_pushed("Gate 2", "gate-2")
    for operation in (
        lambda: registry.get(unknown),
        lambda: registry.rename(unknown, "Gate 9"),
        lambda: registry.remove(unknown),
    ):
        with pytest.raises(CameraNotFoundError) as raised:
            operation()
        assert raised.value.camera_id == unknown
        assert isinstance(raised.value, LookupError)


def test_a_camera_is_renamed(registry: CameraRegistry) -> None:
    camera = registry.add_pulled("Gate 2", "rtsp://admin:pw@10.0.0.7/s")
    renamed = registry.rename(camera.id, "  North   gate ")
    assert renamed.name == "North gate"
    assert (renamed.id, renamed.path, renamed.pull_url) == (camera.id, camera.path, camera.pull_url)
    assert registry.get(camera.id) == renamed
    # Its old name is free again, and a camera may be renamed to another case of its own.
    registry.add_pushed("Gate 2", "gate-2")
    assert registry.rename(camera.id, "NORTH GATE").name == "NORTH GATE"


def test_renaming_to_an_invalid_name_is_refused(registry: CameraRegistry) -> None:
    camera = registry.add_pushed("Gate 2", "gate-2")
    with pytest.raises(InvalidCameraError):
        registry.rename(camera.id, " ")
    assert registry.get(camera.id) == camera


def test_a_removed_camera_leaves_the_list_and_frees_its_name_and_path(
    registry: CameraRegistry,
) -> None:
    pushed = registry.add_pushed("Gate 2", "gate-2")
    kept = registry.add_pushed("Gate 3", "gate-3")
    registry.remove(pushed.id)
    assert registry.list() == [kept]
    again = registry.add_pushed("Gate 2", "gate-2")
    assert again.id != pushed.id
    assert registry.list() == [kept, again]


def test_a_removed_camera_is_still_known_by_its_id(registry: CameraRegistry) -> None:
    # Footage and Tracks of a removed camera outlive it, and still say which camera saw them.
    camera = registry.add_pushed("Gate 2", "gate-2")
    registry.remove(camera.id)
    removed = registry.get(camera.id)
    assert removed.removed_at is not None
    assert (removed.name, removed.path) == ("Gate 2", "gate-2")
    with pytest.raises(CameraNotFoundError):
        registry.remove(camera.id)
    with pytest.raises(CameraNotFoundError):
        registry.rename(camera.id, "Gate 9")


def test_removing_a_pulled_camera_forgets_its_credentials(
    registry: CameraRegistry, migrated: str
) -> None:
    camera = registry.add_pulled("Gate 2", "rtsp://admin:pw@10.0.0.7/s")
    registry.remove(camera.id)
    removed = registry.get(camera.id)
    assert removed.pull_url == CameraUrl("rtsp://10.0.0.7/s")
    with psycopg.connect(migrated) as connection:
        row = connection.execute(
            "SELECT pull_credentials FROM cameras WHERE id = %s", (camera.id,)
        ).fetchone()
    assert row == (None,)


def test_ids_are_never_reused(registry: CameraRegistry) -> None:
    first = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    registry.remove(first.id)
    second = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    assert second.id > first.id
    assert second.path != first.path


def test_the_registry_works_inside_a_callers_transaction(registry: CameraRegistry) -> None:
    connection = registry._connection  # pyright: ignore[reportPrivateUsage]
    with connection.transaction(force_rollback=True):
        registry.add_pushed("Gate 2", "gate-2")
        assert [camera.name for camera in registry.list()] == ["Gate 2"]
    assert registry.list() == []
