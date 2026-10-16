"""Settings come from `VISIONQUERY_` environment variables, and a bad one is named."""

import os
from pathlib import Path
from typing import Self

import pytest
from pydantic import BaseModel, Field, SecretStr, model_validator
from visionquery.runtime import Settings, SettingsError


class RecorderSettings(Settings):
    footage_dir: Path
    segment_seconds: int = 60
    camera_password: SecretStr | None = None


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.upper().startswith("VISIONQUERY_"):
            monkeypatch.delenv(name)


def test_fields_are_read_from_prefixed_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_FOOTAGE_DIR", "/srv/footage")
    monkeypatch.setenv("VISIONQUERY_SEGMENT_SECONDS", "10")
    settings = RecorderSettings.load()
    assert settings.footage_dir == Path("/srv/footage")
    assert settings.segment_seconds == 10


def test_every_process_has_a_log_level_and_a_metrics_port_by_default() -> None:
    settings = Settings.load()
    assert settings.log_level == "INFO"
    assert settings.metrics_host == "0.0.0.0"
    assert settings.metrics_port == 9100


def test_common_fields_are_read_from_the_environment_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_LOG_LEVEL", "debug")
    monkeypatch.setenv("VISIONQUERY_METRICS_PORT", "9200")
    settings = Settings.load()
    assert settings.log_level == "DEBUG"
    assert settings.metrics_port == 9200


def test_a_missing_variable_is_named() -> None:
    with pytest.raises(SettingsError) as raised:
        RecorderSettings.load()
    assert raised.value.variables == ("VISIONQUERY_FOOTAGE_DIR",)
    assert "VISIONQUERY_FOOTAGE_DIR is missing" in str(raised.value)


def test_an_empty_variable_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_FOOTAGE_DIR", "")
    with pytest.raises(SettingsError, match="VISIONQUERY_FOOTAGE_DIR is missing"):
        RecorderSettings.load()


def test_an_invalid_variable_is_named_with_what_is_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_FOOTAGE_DIR", "/srv/footage")
    monkeypatch.setenv("VISIONQUERY_SEGMENT_SECONDS", "ten")
    with pytest.raises(SettingsError) as raised:
        RecorderSettings.load()
    assert raised.value.variables == ("VISIONQUERY_SEGMENT_SECONDS",)
    assert "VISIONQUERY_SEGMENT_SECONDS is invalid: " in str(raised.value)
    assert "integer" in str(raised.value)


def test_every_bad_variable_is_named_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_SEGMENT_SECONDS", "ten")
    monkeypatch.setenv("VISIONQUERY_METRICS_PORT", "70000")
    monkeypatch.setenv("VISIONQUERY_LOG_LEVEL", "LOUD")
    with pytest.raises(SettingsError) as raised:
        RecorderSettings.load()
    assert set(raised.value.variables) == {
        "VISIONQUERY_FOOTAGE_DIR",
        "VISIONQUERY_SEGMENT_SECONDS",
        "VISIONQUERY_METRICS_PORT",
        "VISIONQUERY_LOG_LEVEL",
    }
    assert "RecorderSettings" in str(raised.value)


def test_the_error_never_repeats_the_value_it_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # A rejected value may be a secret typed into the wrong variable.
    monkeypatch.setenv("VISIONQUERY_FOOTAGE_DIR", "/srv/footage")
    monkeypatch.setenv("VISIONQUERY_SEGMENT_SECONDS", "hunter2")
    with pytest.raises(SettingsError) as raised:
        RecorderSettings.load()
    assert "hunter2" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_a_secret_is_never_shown_by_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_FOOTAGE_DIR", "/srv/footage")
    monkeypatch.setenv("VISIONQUERY_CAMERA_PASSWORD", "hunter2")
    settings = RecorderSettings.load()
    assert settings.camera_password is not None
    assert settings.camera_password.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)


def test_unrelated_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_FOOTAGE_DIR", "/srv/footage")
    monkeypatch.setenv("VISIONQUERY_SOMETHING_ELSE", "x")
    monkeypatch.setenv("FOOTAGE_DIR", "/elsewhere")
    assert RecorderSettings.load().footage_dir == Path("/srv/footage")


def test_settings_cannot_be_changed_once_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_FOOTAGE_DIR", "/srv/footage")
    settings = RecorderSettings.load()
    with pytest.raises(ValueError, match="frozen"):
        settings.segment_seconds = 5


class WindowSettings(Settings):
    start_hour: int = 0
    end_hour: int = 24

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start_hour >= self.end_hour:
            raise ValueError("start_hour must come before end_hour")
        return self


def test_a_rule_across_variables_is_reported_without_naming_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISIONQUERY_START_HOUR", "20")
    monkeypatch.setenv("VISIONQUERY_END_HOUR", "6")
    with pytest.raises(SettingsError) as raised:
        WindowSettings.load()
    assert raised.value.variables == ()
    assert "start_hour must come before end_hour" in str(raised.value)


class Database(BaseModel):
    url: SecretStr
    pool_size: int = 5


class ApiSettings(Settings):
    database: Database


def test_nested_settings_are_read_with_a_double_underscore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISIONQUERY_DATABASE__URL", "postgresql://u:hunter2@db/vq")
    monkeypatch.setenv("VISIONQUERY_DATABASE__POOL_SIZE", "9")
    settings = ApiSettings.load()
    assert settings.database.url.get_secret_value() == "postgresql://u:hunter2@db/vq"
    assert settings.database.pool_size == 9
    assert "hunter2" not in repr(settings)


def test_a_bad_nested_variable_is_named_as_it_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_DATABASE__URL", "postgresql://db/vq")
    monkeypatch.setenv("VISIONQUERY_DATABASE__POOL_SIZE", "many")
    with pytest.raises(SettingsError) as raised:
        ApiSettings.load()
    assert raised.value.variables == ("VISIONQUERY_DATABASE__POOL_SIZE",)
    assert "VISIONQUERY_DATABASE__POOL_SIZE is invalid: " in str(raised.value)


def test_a_missing_nested_variable_is_named_as_it_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_DATABASE__POOL_SIZE", "9")
    with pytest.raises(SettingsError) as raised:
        ApiSettings.load()
    assert raised.value.variables == ("VISIONQUERY_DATABASE__URL",)
    assert "VISIONQUERY_DATABASE__URL is missing" in str(raised.value)


def test_a_field_with_an_alias_is_rejected_when_the_settings_are_declared() -> None:
    # An alias is read from a variable without the prefix, which would make the error and
    # the documentation name a variable that is not the one read.
    with pytest.raises(TypeError, match=r"camera_url.*alias"):

        class Aliased(Settings):
            camera_url: str = Field(validation_alias="CAMERA_URL")

        _ = Aliased
