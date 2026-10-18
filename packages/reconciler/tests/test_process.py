"""`visionquery-reconciler`: reconciling every cycle, for as long as it runs, whatever fails.

The loop runs in a thread against a real Postgres (packages/conftest.py) and a real MediaMTX
(conftest.py), with a short cycle, while the test changes the registry or restarts MediaMTX.
"""

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import psycopg
import pytest
from prometheus_client import REGISTRY
from pydantic import SecretStr
from real_mediamtx import API_USER, READER_USER, MediaMTX
from visionquery.memory.cameras import Camera, CameraRegistry, PublishCredentials
from visionquery.memory.connection import connect
from visionquery.memory.migrate import migrate
from visionquery.memory.settings import DatabaseSettings
from visionquery.reconciler.reconcile import Changes, Reconciler
from visionquery.reconciler.serve import (
    MediaMTXSettings,
    ReconcilerSettings,
    health,
    main,
    run,
)

#: Seconds between reconciles in these tests.
CYCLE = 0.5
#: What a cycle may take on a busy machine, beyond its interval: a reconcile is a handful of
#: requests and one query.
SLACK = 3.0
DOCK_PASSWORD = "dock-camera-pa55word"


@pytest.fixture
def migrated(database: str) -> str:
    migrate(database)
    return database


@pytest.fixture
def registry(migrated: str) -> Iterator[CameraRegistry]:
    with connect(migrated, 5) as connection:
        yield CameraRegistry(connection)


def _settings(database_url: str, mediamtx: MediaMTX) -> ReconcilerSettings:
    return ReconcilerSettings.model_construct(
        database=DatabaseSettings(url=SecretStr(database_url)),
        mediamtx=MediaMTXSettings(
            url=mediamtx.api_url,
            user=API_USER,
            password=SecretStr(mediamtx.api_password),
            reader_user=READER_USER,
            reader_password=SecretStr(mediamtx.reader_password),
        ),
        reconcile_interval=CYCLE,
    )


class Running:
    """The loop, running in a thread until the test ends."""

    def __init__(self, settings: ReconcilerSettings, heartbeat: Path) -> None:
        self.heartbeat = heartbeat
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=run, args=(settings,), kwargs={"stop": self._stop, "heartbeat": heartbeat}
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
        assert not self._thread.is_alive()


@pytest.fixture
def start(tmp_path: Path) -> Iterator[Callable[[ReconcilerSettings], Running]]:
    started: list[Running] = []

    def starting(settings: ReconcilerSettings) -> Running:
        running = Running(settings, tmp_path / "healthy")
        started.append(running)
        return running

    yield starting
    for running in started:
        running.stop()


def _within(seconds: float, condition: Callable[[], bool]) -> float:
    """How long `condition` took to hold; fails the test past `seconds`."""
    began = time.monotonic()
    while not condition():
        assert time.monotonic() - began < seconds, f"not within {seconds} s"
        time.sleep(0.05)
    return time.monotonic() - began


def _cycles(outcome: str) -> float:
    value = REGISTRY.get_sample_value("visionquery_reconciler_cycles_total", {"outcome": outcome})
    return value or 0.0


# --- every cycle ------------------------------------------------------------------------------


def test_adding_editing_and_removing_a_camera_reach_mediamtx_within_one_cycle(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    start: Callable[[ReconcilerSettings], Running],
) -> None:
    running = start(_settings(migrated, mediamtx))
    _within(CYCLE + SLACK, running.heartbeat.exists)

    dock = registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    gate = registry.add_pulled("Gate 2", "rtsp://10.0.0.7/s")
    _within(CYCLE + SLACK, lambda: {"dock", gate.path} <= set(mediamtx.path_configs()))
    assert mediamtx.publish("dock", "dock", DOCK_PASSWORD).returncode == 0

    changed = registry.change_pull_url(gate.id, "rtsp://10.0.0.8/main")
    assert changed.pull_url is not None
    url = changed.pull_url.get_secret_value()
    _within(CYCLE + SLACK, lambda: mediamtx.path_configs()[gate.path]["source"] == url)

    registry.remove(dock.id)
    _within(CYCLE + SLACK, lambda: "dock" not in mediamtx.path_configs())
    assert mediamtx.publish("dock", "dock", DOCK_PASSWORD).returncode != 0


def test_a_restarted_mediamtx_is_put_right_without_help(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    start: Callable[[ReconcilerSettings], Running],
) -> None:
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    running = start(_settings(migrated, mediamtx))
    _within(CYCLE + SLACK, lambda: "dock" in mediamtx.path_configs())

    mediamtx.restart()

    _within(CYCLE + SLACK, lambda: "dock" in mediamtx.path_configs())
    # The publisher is back too: MediaMTX forgot it with everything else its API was told.
    _within(CYCLE + SLACK, lambda: mediamtx.publish("dock", "dock", DOCK_PASSWORD).returncode == 0)
    assert running.heartbeat.exists()


def test_while_mediamtx_is_down_the_reconciler_says_so_once_and_is_unhealthy_until_it_is_back(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    start: Callable[[ReconcilerSettings], Running],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="visionquery.reconciler")
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    running = start(_settings(migrated, mediamtx))
    _within(CYCLE + SLACK, lambda: "dock" in mediamtx.path_configs())
    failed = _cycles("failed")

    mediamtx.stop()
    try:
        _within(10 * CYCLE + SLACK, lambda: _cycles("failed") >= failed + 4)
        # The heartbeat promises health for a few cycles only.
        _within(10 * CYCLE + SLACK, lambda: health(running.heartbeat) != 0)
    finally:
        mediamtx.start()

    _within(CYCLE + SLACK, lambda: "dock" in mediamtx.path_configs())
    _within(CYCLE + SLACK, lambda: health(running.heartbeat) == 0)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "MediaMTX could not be reached" in warnings[0].getMessage()
    assert any("reconciling again" in r.getMessage() for r in caplog.records)


def test_an_unreachable_database_is_named_by_its_category_and_retried(
    mediamtx: MediaMTX,
    start: Callable[[ReconcilerSettings], Running],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="visionquery.reconciler")
    secret = "db-pa55word-never-logged"
    failed = _cycles("failed")
    # Port 9 (discard) on loopback: nothing listens there.
    running = start(_settings(f"postgresql://vq:{secret}@127.0.0.1:9/vq", mediamtx))
    _within(10 * CYCLE + SLACK, lambda: _cycles("failed") >= failed + 3)
    assert not running.heartbeat.exists()
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "connection refused or host unreachable" in warning.getMessage()
    assert all(secret not in r.getMessage() for r in caplog.records)


def test_no_camera_credential_reaches_the_logs(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    start: Callable[[ReconcilerSettings], Running],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    pulled = registry.add_pulled("Gate 2", "rtsp://admin:pulled-pa55word@10.0.0.7/s")
    pushed = registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    assert pushed.publish_credentials is not None
    running = start(_settings(migrated, mediamtx))
    _within(CYCLE + SLACK, lambda: pulled.path in mediamtx.path_configs())
    running.stop()

    logged = "\n".join(
        f"{r.getMessage()} {vars(r)}" for r in caplog.records if r.name.startswith("visionquery")
    )
    assert "reconciled" in logged
    for secret in ("pulled-pa55word", DOCK_PASSWORD, pushed.publish_credentials.password_hash):
        assert secret not in logged
    assert mediamtx.api_password not in logged
    assert mediamtx.reader_password not in logged


def test_each_cycle_is_counted(
    mediamtx: MediaMTX,
    migrated: str,
    start: Callable[[ReconcilerSettings], Running],
) -> None:
    ok = _cycles("ok")
    start(_settings(migrated, mediamtx))
    _within(4 * CYCLE + SLACK, lambda: _cycles("ok") >= ok + 3)
    last = REGISTRY.get_sample_value("visionquery_reconciler_last_success_timestamp_seconds")
    assert last is not None
    assert time.time() - last < CYCLE + SLACK


# --- health -----------------------------------------------------------------------------------


def test_healthy_only_while_a_recent_cycle_succeeded(tmp_path: Path) -> None:
    heartbeat = tmp_path / "healthy"
    assert health(heartbeat) == 1
    heartbeat.write_text(str(time.time() + 10))
    assert health(heartbeat) == 0
    heartbeat.write_text(str(time.time() - 1))
    assert health(heartbeat) == 1
    heartbeat.write_text("not a time")
    assert health(heartbeat) == 1


# --- settings ---------------------------------------------------------------------------------


def _environment(monkeypatch: pytest.MonkeyPatch, **variables: str) -> None:
    for name in list(os.environ):
        if name.startswith("VISIONQUERY_"):
            monkeypatch.delenv(name)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)


def test_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(
        monkeypatch,
        VISIONQUERY_DATABASE__URL="postgresql://vq@postgres:5432/vq",
        VISIONQUERY_MEDIAMTX__URL="http://mediamtx:9997",
        VISIONQUERY_MEDIAMTX__PASSWORD="api-password",
        VISIONQUERY_MEDIAMTX__READER_PASSWORD="reader-password",
        VISIONQUERY_RECONCILE_INTERVAL="2.5",
    )
    settings = ReconcilerSettings.load()
    assert settings.mediamtx.url == "http://mediamtx:9997"
    assert settings.mediamtx.user == "visionquery-api"
    assert settings.mediamtx.password.get_secret_value() == "api-password"
    assert settings.mediamtx.reader_user == "visionquery-reader"
    assert settings.mediamtx.reader_password.get_secret_value() == "reader-password"
    assert settings.reconcile_interval == 2.5


def test_the_cycle_defaults_to_five_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(
        monkeypatch,
        VISIONQUERY_DATABASE__URL="postgresql://vq@postgres:5432/vq",
        VISIONQUERY_MEDIAMTX__PASSWORD="api-password",
        VISIONQUERY_MEDIAMTX__READER_PASSWORD="reader-password",
    )
    settings = ReconcilerSettings.load()
    assert settings.reconcile_interval == 5
    assert settings.mediamtx.url == "http://127.0.0.1:9997"


@pytest.mark.parametrize(
    ("variables", "named"),
    [
        ({}, "VISIONQUERY_MEDIAMTX__PASSWORD"),
        ({"VISIONQUERY_MEDIAMTX__PASSWORD": "p"}, "VISIONQUERY_MEDIAMTX__READER_PASSWORD"),
        (
            {
                "VISIONQUERY_MEDIAMTX__PASSWORD": "p",
                "VISIONQUERY_MEDIAMTX__READER_PASSWORD": "r",
                "VISIONQUERY_RECONCILE_INTERVAL": "0",
            },
            None,
        ),
        (
            {
                "VISIONQUERY_MEDIAMTX__PASSWORD": "p",
                "VISIONQUERY_MEDIAMTX__READER_PASSWORD": "r",
                "VISIONQUERY_MEDIAMTX__URL": "http://visionquery:p@mediamtx:9997",
            },
            "VISIONQUERY_MEDIAMTX__URL",
        ),
        (
            {
                "VISIONQUERY_MEDIAMTX__PASSWORD": "p",
                "VISIONQUERY_MEDIAMTX__READER_PASSWORD": "r",
                "VISIONQUERY_MEDIAMTX__URL": "rtsp://m:8554",
            },
            "VISIONQUERY_MEDIAMTX__URL",
        ),
    ],
)
def test_bad_settings_stop_the_process_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variables: dict[str, str],
    named: str | None,
) -> None:
    _environment(
        monkeypatch,
        VISIONQUERY_DATABASE__URL="postgresql://vq@postgres:5432/vq",
        VISIONQUERY_METRICS_PORT="0",
        **variables,
    )
    with pytest.raises(SystemExit) as exited:
        main([])
    assert exited.value.code == 2
    output = capsys.readouterr().out
    assert (named or "VISIONQUERY_RECONCILE_INTERVAL") in output


def test_the_health_command_reads_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "healthy"
    monkeypatch.setattr("visionquery.reconciler.serve.HEARTBEAT", heartbeat)
    with pytest.raises(SystemExit) as exited:
        main(["health"])
    assert exited.value.code == 1
    heartbeat.write_text(str(time.time() + 10))
    with pytest.raises(SystemExit) as exited:
        main(["health"])
    assert exited.value.code == 0


# --- what goes wrong in a cycle ---------------------------------------------------------------


def _store_a_url_mediamtx_refuses(database: str, name: str) -> int:
    with psycopg.connect(database, autocommit=True) as connection:
        row = connection.execute(
            "INSERT INTO cameras (name, pull_url, pull_credentials, pull_target) "
            "VALUES (%s, 'rtsp://cam.invalid', ':refused-pa55word', '/s') RETURNING id",
            (name,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_a_camera_mediamtx_refuses_is_named_once_and_costs_no_other_camera_anything(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    start: Callable[[ReconcilerSettings], Running],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="visionquery.reconciler")
    bad = _store_a_url_mediamtx_refuses(migrated, "Bad")
    registry.add_pushed("Dock", "dock", PublishCredentials.of("dock", DOCK_PASSWORD))
    degraded = _cycles("degraded")

    running = start(_settings(migrated, mediamtx))

    _within(CYCLE + SLACK, lambda: "dock" in mediamtx.path_configs())
    _within(CYCLE + SLACK, lambda: mediamtx.publish("dock", "dock", DOCK_PASSWORD).returncode == 0)
    _within(10 * CYCLE + SLACK, lambda: _cycles("degraded") >= degraded + 3)
    # The gauge says how many paths MediaMTX refuses, and the reconciler is still healthy: what
    # is wrong is that camera's URL, which a restart would not change.
    assert REGISTRY.get_sample_value("visionquery_reconciler_failed_paths") == 1
    assert health(running.heartbeat) == 0
    refused = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(refused) == 1, [r.getMessage() for r in refused]
    assert f"camera {bad} (pulled/{bad})" in refused[0].getMessage()
    logged = "\n".join(f"{r.getMessage()} {vars(r)}" for r in caplog.records)
    assert "refused-pa55word" not in logged
    assert "cam.invalid" not in logged

    # Once the camera is removed, nothing is refused any more.
    registry.remove(bad)
    _within(
        CYCLE + SLACK, lambda: REGISTRY.get_sample_value("visionquery_reconciler_failed_paths") == 0
    )


def test_an_unexpected_failure_is_logged_once_by_its_class_and_the_loop_carries_on(
    mediamtx: MediaMTX,
    migrated: str,
    registry: CameraRegistry,
    start: Callable[[ReconcilerSettings], Running],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO, logger="visionquery.reconciler")
    registry.add_pushed("Dock", "dock")
    reconcile = Reconciler.reconcile
    calls = 0

    def unreliable(self: Reconciler, cameras: Sequence[Camera]) -> Changes:
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise KeyError("postgresql://vq:db-pa55word@db/vq")
        return reconcile(self, cameras)

    monkeypatch.setattr(Reconciler, "reconcile", unreliable)
    failed = _cycles("failed")

    running = start(_settings(migrated, mediamtx))

    _within(10 * CYCLE + SLACK, lambda: "dock" in mediamtx.path_configs())
    assert _cycles("failed") >= failed + 3
    _within(CYCLE + SLACK, lambda: health(running.heartbeat) == 0)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "KeyError" in warnings[0].getMessage()
    assert warnings[0].exc_info is None
    logged = "\n".join(f"{r.getMessage()} {vars(r)}" for r in caplog.records)
    assert "db-pa55word" not in logged
    assert any("reconciling again" in r.getMessage() for r in caplog.records)
