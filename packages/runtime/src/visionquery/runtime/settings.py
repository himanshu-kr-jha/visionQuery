"""Settings read from `VISIONQUERY_` environment variables, the only way a process is configured.

A service declares what it needs by extending `Settings` with its own fields:

    class RecorderSettings(Settings):
        footage_dir: Path

    settings = RecorderSettings.load()  # reads VISIONQUERY_FOOTAGE_DIR

A nested model is set with a double underscore (`VISIONQUERY_DATABASE__URL`), and a list or
dict as JSON (`VISIONQUERY_PORTS='[8554, 8555]'`). A field takes no alias, so each is read
from exactly the variable named after it, and an error names the variable that was set.

Credentials are declared as `SecretStr`, so neither `repr` nor a log line shows them.
"""

import os
import re
from collections.abc import Iterable, Set
from typing import Annotated, Any, Literal, Self, override

from pydantic import BaseModel, BeforeValidator, Field, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings import SettingsError as SourceError

__all__ = ["LogLevel", "Settings", "SettingsError"]

PREFIX = "VISIONQUERY_"
#: Between a nested group and its field, as in VISIONQUERY_DATABASE__URL.
NESTED = "__"

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


class Settings(BaseSettings):
    """What every process is configured with; a service subclasses it to add its own fields."""

    model_config = SettingsConfigDict(
        env_prefix=PREFIX,
        # An empty variable is a missing one, not an empty path or name.
        env_ignore_empty=True,
        env_nested_delimiter=NESTED,
        extra="ignore",
        frozen=True,
    )

    log_level: Annotated[LogLevel, BeforeValidator(_upper)] = "INFO"
    #: Where `/metrics` listens; loopback by default, so a process run outside Compose exposes
    #: nothing. compose.yaml sets 0.0.0.0, so Prometheus can reach it from its own container.
    metrics_host: str = "127.0.0.1"
    #: 0 asks the operating system for a free port.
    metrics_port: Annotated[int, Field(ge=0, le=65535)] = 9100

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        for name, field in cls.model_fields.items():
            if field.alias is not None or field.validation_alias is not None:
                # An alias is read from a variable without the prefix, so errors and docs
                # would name a variable other than the one a field is documented under.
                raise TypeError(
                    f"{cls.__name__}.{name} declares an alias; settings fields are read from "
                    f"{PREFIX}{name.upper()} and take no alias"
                )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, _Environment(settings_cls), dotenv_settings, file_secret_settings)

    @classmethod
    def load(cls) -> Self:
        """Read the settings from the environment, or say which variables are wrong."""
        # `from None` throughout: pydantic's own errors repeat the rejected value, which may be
        # a secret typed into the wrong variable.
        try:
            return cls()
        except ValidationError as error:
            raise SettingsError(cls, _problems(cls, error)) from None
        except SourceError as error:
            # pydantic-settings decodes lists, dicts and groups before validation, so a value
            # that is not JSON fails here, outside the ValidationError.
            cause = error.__cause__
            if isinstance(cause, _NotJsonError):
                raise SettingsError(cls, [(cause.variable, "is invalid: not valid JSON")]) from None
            # Any other failure of the source names its field in quotes; its cause may hold
            # the value, so only the variable is kept.
            named = re.search(r'field "(\w+)"', str(error))
            variable = f"{PREFIX}{named.group(1)}".upper() if named else None
            raise SettingsError(cls, [(variable, "could not be read")]) from None


#: A variable at fault (None for a rule across several), and what is wrong with it.
type Problem = tuple[str | None, str]


class SettingsError(Exception):
    """Environment variables a process needs are missing or invalid."""

    def __init__(self, settings: type[BaseModel], problems: Iterable[Problem]) -> None:
        lines: list[str] = []
        variables: list[str] = []
        for variable, what in problems:
            if variable is None:
                lines.append(f"  {what}")
            else:
                lines.append(f"  {variable} {what}")
                variables.append(variable)
        #: The variables to fix, each named once, in the order the fields are declared.
        self.variables = tuple(dict.fromkeys(variables))
        joined = "\n".join(lines)
        super().__init__(f"{settings.__name__} could not be read from the environment:\n{joined}")


def _problems(settings: type[BaseModel], error: ValidationError) -> list[Problem]:
    present = {name.upper() for name, value in os.environ.items() if value}
    problems: list[Problem] = []
    for detail in error.errors(include_input=False, include_url=False):
        if not detail["loc"]:
            problems.append((None, detail["msg"]))
        elif detail["type"] == "missing":
            for loc in _required_leaves(settings, detail["loc"]):
                problems.append((_variable(loc), "is missing"))
        else:
            variable = _holding(detail["loc"], present)
            problems.append((variable, f"is invalid: {detail['msg']}"))
    return problems


def _variable(loc: tuple[int | str, ...]) -> str:
    return f"{PREFIX}{NESTED.join(str(part) for part in loc)}".upper()


def _holding(loc: tuple[int | str, ...], present: Set[str]) -> str:
    """The variable the invalid value at `loc` was read from.

    pydantic places an error at the item that is wrong (`ports.1`, `limits.gate`), but a list
    or dict, or a whole nested group, may be set as JSON in one variable (`VISIONQUERY_PORTS`).
    The variable is the longest start of `loc` that names one that is set.
    """
    for end in range(len(loc), 0, -1):
        if (variable := _variable(loc[:end])) in present:
            return variable
    return _variable(loc)


class _NotJsonError(ValueError):
    """The value of `variable`, a list, dict or nested group, is not valid JSON."""

    def __init__(self, variable: str) -> None:
        super().__init__(f"{variable} is not valid JSON")
        self.variable = variable


class _Environment(EnvSettingsSource):
    """The environment, naming the variable whose list, dict or group is not valid JSON.

    pydantic-settings decodes those values itself, before validation, and reports a failure
    for the field as a whole, without saying which variable held it.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._field_variable = ""

    @override
    def prepare_field_value(
        self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool
    ) -> Any:
        # No field takes an alias, so this is the variable it is read from.
        self._field_variable = f"{PREFIX}{field_name}".lower()
        return super().prepare_field_value(field_name, field, value, value_is_complex)

    @override
    def decode_complex_value(self, field_name: str, field: FieldInfo, value: Any) -> Any:
        try:
            return super().decode_complex_value(field_name, field, value)
        except ValueError as error:
            raise _NotJsonError(self._holding(field_name, value)) from error

    def _holding(self, name: str, value: object) -> str:
        """The variable of the current field, or under it, that ends in `name` and holds `value`."""
        field = self._field_variable.lower()
        for variable, held in self.env_vars.items():
            lowered = variable.lower()
            in_field = lowered == field or (
                lowered.startswith(f"{field}{NESTED}")
                and lowered.endswith(f"{NESTED}{name.lower()}")
            )
            if in_field and held == value:
                return variable.upper()
        return field.upper()


def _required_leaves(
    model: type[BaseModel], loc: tuple[int | str, ...]
) -> list[tuple[int | str, ...]]:
    """The variables to set for the missing field at `loc`.

    A nested group with none of its variables set is reported by pydantic as the group
    itself (`database`), but there is no `VISIONQUERY_DATABASE` to set: what is missing is
    each required variable inside it (`VISIONQUERY_DATABASE__URL`).
    """
    annotation: object = model
    for part in loc:
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            return [loc]
        field = annotation.model_fields.get(str(part))
        if field is None:
            return [loc]
        annotation = field.annotation
    if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
        return [loc]
    return [
        leaf
        for name, field in annotation.model_fields.items()
        if field.is_required()
        for leaf in _required_leaves(model, (*loc, name))
    ]
