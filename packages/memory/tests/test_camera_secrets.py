"""A camera's credentials never reach a log line, a repr, an exception or what the api sends.

Every operation of the registry is run with a URL whose user and password are distinctive,
with every logger in the process recording at DEBUG, the database driver's included, and
everything that could be shown is searched for them. Runs against a real Postgres (see
packages/conftest.py).
"""

import logging
import traceback
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from pydantic import TypeAdapter
from visionquery.memory.cameras import (
    Camera,
    CameraError,
    CameraNameTakenError,
    CameraRegistry,
    CameraRegistryError,
)
from visionquery.memory.connection import connect
from visionquery.memory.migrate import migrate

USER = "operator-u53r"
PASSWORD = "s3cr3t-Pa55"
PATH_TOKEN = "Xk9aQ2bLm4ZtTq1R"
QUERY_TOKEN = "q-t0k3n-77"
URL = f"rtsp://{USER}:{PASSWORD}@10.0.0.7:554/{PATH_TOKEN}/stream1?sig={QUERY_TOKEN}"
REDACTED = "rtsp://***@10.0.0.7:554/***"


def _leaks(text: str) -> bool:
    return any(secret in text for secret in (USER, PASSWORD, PATH_TOKEN, QUERY_TOKEN, "stream1"))


@pytest.fixture
def migrated(database: str) -> str:
    migrate(database)
    return database


@pytest.fixture
def registry(migrated: str) -> Iterator[CameraRegistry]:
    with connect(migrated, 5) as connection:
        yield CameraRegistry(connection)


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Every record of every logger, at DEBUG; `caplog.records` is read in the test itself."""
    caplog.set_level(logging.DEBUG)
    for name in ("psycopg", "visionquery"):
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


def _everything_in(record: logging.LogRecord) -> str:
    standard = set(vars(logging.makeLogRecord({})))
    extras: dict[str, Any] = {k: v for k, v in vars(record).items() if k not in standard}
    exception = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
    return "\n".join([record.getMessage(), repr(record.args), repr(extras), exception])


def _shown(error: BaseException) -> str:
    """Everything a traceback of `error` would print, its causes and contexts included."""
    return "".join(traceback.format_exception(error)) + repr(error)


def test_no_operation_logs_the_credentials(
    registry: CameraRegistry, logs: pytest.LogCaptureFixture
) -> None:
    camera = registry.add_pulled("Gate 2", URL)
    registry.get(camera.id)
    registry.list()
    registry.rename(camera.id, "North gate")
    with pytest.raises(CameraNameTakenError):
        registry.add_pulled("north gate", URL)
    registry.remove(camera.id)

    logged = [_everything_in(record) for record in logs.records]
    assert any("Gate 2" in text for text in logged), "adding a camera is logged"
    assert not any("10.0.0.7" in text for text in logged), "the URL is not logged at all"
    assert not any(_leaks(text) for text in logged)


def test_a_camera_shows_no_credentials(registry: CameraRegistry) -> None:
    camera = registry.add_pulled("Gate 2", URL)
    assert camera.pull_url is not None
    assert camera.pull_url.get_secret_value() == URL
    for shown in (repr(camera), str(camera), f"{camera}", repr(registry.list())):
        assert not _leaks(shown)
        assert REDACTED in shown


def test_serialised_as_the_api_will_serialise_it_a_camera_has_no_credentials(
    registry: CameraRegistry,
) -> None:
    camera = registry.add_pulled("Gate 2", URL)
    adapter = TypeAdapter(Camera)
    dumped = adapter.dump_python(camera, mode="json")
    assert dumped["pull_url"] == REDACTED
    assert dumped["path"] == camera.path
    assert not _leaks(adapter.dump_json(camera).decode())
    assert not _leaks(TypeAdapter(list[Camera]).dump_json(registry.list()).decode())
    assert not _leaks(str(adapter.json_schema(mode="serialization")))


def test_a_refusal_of_a_camera_with_credentials_does_not_quote_them(
    registry: CameraRegistry,
) -> None:
    registry.add_pushed("Gate 2", "gate-2")
    with pytest.raises(CameraNameTakenError) as taken:
        registry.add_pulled("Gate 2", URL)
    assert not _leaks(_shown(taken.value))


def test_a_database_failure_while_adding_does_not_quote_the_credentials(
    registry: CameraRegistry, migrated: str
) -> None:
    # Postgres repeats the row it refused in its error ("Failing row contains ..."), so a
    # failure the registry did not foresee must not pass the driver's text on.
    with psycopg.connect(migrated, autocommit=True) as admin:
        admin.execute("ALTER TABLE cameras ADD CONSTRAINT refuse_all CHECK (false) NOT VALID")
    with pytest.raises(CameraRegistryError) as raised:
        registry.add_pulled("Gate 2", URL)
    error = raised.value
    assert isinstance(error, CameraError)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not _leaks(_shown(error))
    assert "CheckViolation" in str(error)


def test_a_lost_connection_while_adding_does_not_quote_the_credentials(migrated: str) -> None:
    connection = connect(migrated, 5)
    registry = CameraRegistry(connection)
    connection.close()
    with pytest.raises(CameraRegistryError) as raised:
        registry.add_pulled("Gate 2", URL)
    assert not _leaks(_shown(raised.value))


def test_removing_a_camera_leaves_neither_credentials_nor_path_nor_query(
    registry: CameraRegistry, migrated: str
) -> None:
    camera = registry.add_pulled("Gate 2", URL)
    registry.remove(camera.id)
    removed = registry.get(camera.id)
    assert removed.pull_url is not None
    assert removed.pull_url.get_secret_value() == "rtsp://10.0.0.7:554"
    with psycopg.connect(migrated) as connection:
        rows = connection.execute("SELECT to_jsonb(cameras) FROM cameras").fetchall()
    assert not _leaks(repr(rows))


def test_the_database_keeps_the_path_and_query_apart_from_the_public_part(
    registry: CameraRegistry, migrated: str
) -> None:
    camera = registry.add_pulled("Gate 2", URL)
    with psycopg.connect(migrated) as connection:
        row = connection.execute(
            "SELECT pull_url FROM cameras WHERE id = %s", (camera.id,)
        ).fetchone()
    assert row == ("rtsp://10.0.0.7:554",)
