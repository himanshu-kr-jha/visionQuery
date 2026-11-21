"""Settings come from `VISIONQUERY_` environment variables, and a bad one is named."""

import os
from pathlib import Path
from typing import Any, Self

import pytest
from pydantic import AliasPath, BaseModel, Field, SecretStr, create_model, model_validator
from pydantic_settings import BaseSettings, EnvSettingsSource
from pydantic_settings import SettingsError as SourceError
from visionquery.runtime import Settings, SettingsError, VariableError
from visionquery.runtime import settings as settings_module
from visionquery.runtime.settings import SOURCE_ERROR_FIELD


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


def test_every_process_has_a_log_level_and_a_metrics_port_on_loopback_by_default() -> None:
    settings = Settings.load()
    assert settings.log_level == "INFO"
    assert settings.metrics_host == "127.0.0.1"
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


class Credentials(BaseModel):
    user: str
    password: SecretStr


class Upstream(BaseModel):
    credentials: Credentials
    host: str
    port: int = 5432


class ProxySettings(Settings):
    upstream: Upstream


def test_an_unset_nested_group_names_each_variable_it_needs() -> None:
    # Nothing under VISIONQUERY_DATABASE__ is set, so pydantic reports `database` itself as
    # missing; there is no VISIONQUERY_DATABASE to set, so the error names what is.
    with pytest.raises(SettingsError) as raised:
        ApiSettings.load()
    assert raised.value.variables == ("VISIONQUERY_DATABASE__URL",)
    assert "VISIONQUERY_DATABASE__URL is missing" in str(raised.value)
    assert "VISIONQUERY_DATABASE is" not in str(raised.value)


def test_an_unset_group_of_groups_names_every_required_variable_in_it() -> None:
    with pytest.raises(SettingsError) as raised:
        ProxySettings.load()
    assert raised.value.variables == (
        "VISIONQUERY_UPSTREAM__CREDENTIALS__USER",
        "VISIONQUERY_UPSTREAM__CREDENTIALS__PASSWORD",
        "VISIONQUERY_UPSTREAM__HOST",
    )


def test_a_field_with_an_alias_is_rejected_when_the_settings_are_declared() -> None:
    # An alias is read from a variable without the prefix, which would make the error and
    # the documentation name a variable that is not the one read.
    def declare() -> type[Settings]:
        class Aliased(Settings):
            camera_url: str = Field(validation_alias="CAMERA_URL")

        return Aliased

    with pytest.raises(TypeError, match=r"camera_url.*alias"):
        declare()


class Pool(BaseModel):
    hosts: list[str] = []
    zones: list[str] = []
    size: int = 5
    maybe: list[str] | None = Field(default_factory=lambda: ["a"])
    loose: list[str] | str = "db"


class Outer(BaseModel):
    a: Pool = Pool()
    b: Pool = Pool()


class FleetSettings(Settings):
    ports: list[int] = Field(default_factory=list[int])
    limits: dict[str, int] = Field(default_factory=dict[str, int])
    pool: Pool = Pool()
    fleet: Outer = Outer()


def _bad_variables(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> SettingsError:
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError) as raised:
        FleetSettings.load()
    return raised.value


@pytest.mark.parametrize(
    "name",
    ["VISIONQUERY_PORTS", "VISIONQUERY_LIMITS", "VISIONQUERY_POOL", "VISIONQUERY_POOL__HOSTS"],
)
def test_a_list_or_dict_that_is_not_json_is_named_without_its_value(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    error = _bad_variables(monkeypatch, name, "hunter2")
    assert error.variables == (name,)
    assert f"{name} is invalid: " in str(error)
    assert "JSON" in str(error)
    assert "hunter2" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VISIONQUERY_PORTS", '[8554, "hunter2"]'),
        ("VISIONQUERY_LIMITS", '{"gate": "hunter2"}'),
        ("VISIONQUERY_LIMITS__GATE", "hunter2"),
        ("VISIONQUERY_POOL", '{"size": "hunter2"}'),
        ("VISIONQUERY_POOL__HOSTS", '["db", 3]'),
    ],
)
def test_an_invalid_item_names_the_variable_that_holds_it(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    # pydantic places the error at the item (`ports.1`), but the variable to fix is the one
    # the item was read from, never an invented VISIONQUERY_PORTS__1.
    error = _bad_variables(monkeypatch, name, value)
    assert error.variables == (name,)
    assert f"{name} is invalid: " in str(error)
    assert "hunter2" not in str(error)


def test_every_list_or_dict_that_is_not_json_is_named_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pydantic-settings stops at the first value it cannot decode; the Operator should not
    # have to fix them one restart at a time.
    monkeypatch.setenv("VISIONQUERY_PORTS", "hunter2")
    monkeypatch.setenv("VISIONQUERY_LIMITS", "hunter3")
    monkeypatch.setenv("VISIONQUERY_POOL__HOSTS", "hunter4")
    monkeypatch.setenv("VISIONQUERY_POOL__ZONES", "hunter5")
    with pytest.raises(SettingsError) as raised:
        FleetSettings.load()
    error = raised.value
    assert error.variables == (
        "VISIONQUERY_PORTS",
        "VISIONQUERY_LIMITS",
        "VISIONQUERY_POOL__HOSTS",
        "VISIONQUERY_POOL__ZONES",
    )
    assert str(error).count("is invalid: not valid JSON") == 4
    assert "hunter" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__


def test_two_groups_of_one_shape_holding_the_same_bad_value_are_both_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISIONQUERY_FLEET__A__HOSTS", "hunter2")
    monkeypatch.setenv("VISIONQUERY_FLEET__B__HOSTS", "hunter2")
    with pytest.raises(SettingsError) as raised:
        FleetSettings.load()
    assert set(raised.value.variables) == {
        "VISIONQUERY_FLEET__A__HOSTS",
        "VISIONQUERY_FLEET__B__HOSTS",
    }
    assert "hunter2" not in str(raised.value)


@pytest.mark.parametrize("name", ["VISIONQUERY_PORTS", "VISIONQUERY_LIMITS", "VISIONQUERY_POOL"])
def test_a_list_dict_or_group_set_to_null_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # pydantic-settings drops a top-level null and falls back to the default, so a variable
    # that is set would silently do nothing.
    error = _bad_variables(monkeypatch, name, "null")
    assert error.variables == (name,)
    assert f"{name} is invalid: null is not accepted" in str(error)


class OptionalPortsSettings(Settings):
    ports: list[int] | None = Field(default_factory=lambda: [8554])


def test_a_nested_optional_set_to_null_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inside a group null is kept, as it is inside JSON; only a top-level one is dropped.
    monkeypatch.setenv("VISIONQUERY_POOL__MAYBE", "null")
    assert FleetSettings.load().pool.maybe is None


def test_a_group_set_to_null_is_refused_even_when_its_variables_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISIONQUERY_POOL", "null")
    monkeypatch.setenv("VISIONQUERY_POOL__HOSTS", '["a"]')
    with pytest.raises(SettingsError) as raised:
        FleetSettings.load()
    assert raised.value.variables == ("VISIONQUERY_POOL",)
    assert "VISIONQUERY_POOL is invalid: null is not accepted" in str(raised.value)


def test_null_is_refused_even_where_the_field_could_be_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # It would not mean None: pydantic-settings would use the default, [8554].
    monkeypatch.setenv("VISIONQUERY_PORTS", "null")
    with pytest.raises(SettingsError) as raised:
        OptionalPortsSettings.load()
    assert raised.value.variables == ("VISIONQUERY_PORTS",)
    assert "VISIONQUERY_PORTS is invalid: null is not accepted" in str(raised.value)


class LooseSettings(Settings):
    hosts: list[str] | str = "db"


def test_a_field_that_also_takes_a_string_still_takes_one_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISIONQUERY_HOSTS", "db-1")
    assert LooseSettings.load().hosts == "db-1"
    monkeypatch.setenv("VISIONQUERY_HOSTS", '["db-1", "db-2"]')
    assert LooseSettings.load().hosts == ["db-1", "db-2"]


def test_a_nested_field_that_also_takes_a_string_still_takes_one_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISIONQUERY_POOL__LOOSE", "db-1")
    assert FleetSettings.load().pool.loose == "db-1"


class Shapes(BaseModel):
    names: list[str] = []
    tags: dict[str, int] = {}
    inner: Pool = Pool()
    maybe_names: list[str] | None = None
    either: list[str] | str = ""
    plain: str = ""
    count: int = 0


@pytest.mark.parametrize(
    ("field", "is_complex", "allows_text"),
    [
        ("names", True, False),
        ("tags", True, False),
        ("inner", True, False),
        ("maybe_names", True, True),
        ("either", True, True),
        ("plain", False, False),
        ("count", False, False),
    ],
)
def test_the_protected_field_rule_still_classifies_the_shapes_the_runtime_relies_on(
    field: str, is_complex: bool, allows_text: bool
) -> None:
    # `_Environment` calls pydantic-settings' protected `_field_is_complex`; when an upgrade
    # changes it, this fails naming the method, rather than a dozen settings tests.
    source = settings_module._Environment(RecorderSettings)  # pyright: ignore[reportPrivateUsage]
    result = source._field_is_complex(Shapes.model_fields[field])  # pyright: ignore[reportPrivateUsage]
    assert result == (is_complex, allows_text), (
        f"pydantic-settings' _field_is_complex changed for {field!r}: {result}"
    )


class AliasedPool(BaseModel):
    name: str = Field("x", validation_alias=AliasPath("name", 0))


@pytest.mark.parametrize(
    "annotation",
    [AliasedPool, list[AliasedPool], AliasedPool | None, dict[str, AliasedPool]],
)
def test_an_alias_on_a_nested_model_is_refused_like_one_on_the_settings(
    annotation: Any,
) -> None:
    with pytest.raises(TypeError, match="declares an alias"):
        create_model("Bad", __base__=Settings, pool=(annotation, None))


class PlainSettings(BaseSettings):
    footage_dir: str = "/srv/footage"


@pytest.mark.parametrize("hook", ["_get_resolved_field_value", "prepare_field_value"])
def test_the_source_error_pattern_matches_what_pydantic_settings_raises(
    monkeypatch: pytest.MonkeyPatch, hook: str
) -> None:
    # `Settings.load` finds the field of a source error in pydantic-settings' message; if a
    # new release rewords it, this fails rather than the error silently naming no variable.
    def refuse(*args: object) -> object:
        raise ValueError("cannot be read")

    monkeypatch.setattr(EnvSettingsSource, hook, refuse)
    with pytest.raises(SourceError) as raised:
        PlainSettings()
    named = SOURCE_ERROR_FIELD.search(str(raised.value))
    assert named is not None, f"pydantic-settings now says: {raised.value}"
    assert named.group(1) == "footage_dir"


def test_a_source_error_while_parsing_is_named_as_a_variable_without_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object) -> object:
        raise ValueError("hunter2 cannot be parsed")

    monkeypatch.setattr(EnvSettingsSource, "prepare_field_value", refuse)
    with pytest.raises(SettingsError) as raised:
        RecorderSettings.load()
    error = raised.value
    assert error.variables == ("VISIONQUERY_LOG_LEVEL",)
    assert "VISIONQUERY_LOG_LEVEL could not be read" in str(error)
    assert "hunter2" not in str(error)


def test_a_source_error_that_is_not_json_is_named_as_a_variable_without_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(self: object, field: object, field_name: str) -> object:
        raise RuntimeError("hunter2 cannot be read")

    monkeypatch.setattr(
        "visionquery.runtime.settings._Environment._get_resolved_field_value", refuse
    )
    with pytest.raises(SettingsError) as raised:
        RecorderSettings.load()
    error = raised.value
    assert error.variables == ("VISIONQUERY_LOG_LEVEL",)
    assert "VISIONQUERY_LOG_LEVEL" in str(error)
    assert "hunter2" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__


def test_a_validator_can_name_a_variable_other_than_its_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Checked(Settings):
        footage_dir: Path

        @model_validator(mode="after")
        def _other_variable(self) -> Self:
            raise VariableError("PGCONNECT_TIMEOUT", "must be a whole number of seconds")

    monkeypatch.setenv("VISIONQUERY_FOOTAGE_DIR", "/srv/footage")
    with pytest.raises(SettingsError) as raised:
        Checked.load()
    assert raised.value.variables == ("PGCONNECT_TIMEOUT",)
    assert "PGCONNECT_TIMEOUT is invalid: must be a whole number of seconds" in str(raised.value)
    assert "VISIONQUERY_" not in str(raised.value)
