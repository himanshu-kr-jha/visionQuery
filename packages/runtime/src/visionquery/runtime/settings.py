"""Settings read from `VISIONQUERY_` environment variables, the only way a process is configured.

A service declares what it needs by extending `Settings` with its own fields:

    class RecorderSettings(Settings):
        footage_dir: Path

    settings = RecorderSettings.load()  # reads VISIONQUERY_FOOTAGE_DIR

A nested model is set with a double underscore (`VISIONQUERY_DATABASE__URL`). A field takes
no alias, so each is read from exactly the variable named after it.

Credentials are declared as `SecretStr`, so neither `repr` nor a log line shows them.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import BeforeValidator, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["LogLevel", "Settings", "SettingsError"]

PREFIX = "VISIONQUERY_"

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


class Settings(BaseSettings):
    """What every process is configured with; a service subclasses it to add its own fields."""

    model_config = SettingsConfigDict(
        env_prefix=PREFIX,
        # An empty variable is a missing one, not an empty path or name.
        env_ignore_empty=True,
        # A nested model is set as VISIONQUERY_DATABASE__URL.
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
    )

    log_level: Annotated[LogLevel, BeforeValidator(_upper)] = "INFO"
    #: Where `/metrics` listens; every interface by default, so Prometheus can reach it
    #: from another container.
    metrics_host: str = "0.0.0.0"
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
    def load(cls) -> Self:
        """Read the settings from the environment, or say which variables are wrong."""
        try:
            return cls()
        except ValidationError as error:
            # `from None`: pydantic's own error repeats the rejected value, which may be a secret.
            raise SettingsError(cls.__name__, error) from None


class SettingsError(Exception):
    """Environment variables a process needs are missing or invalid."""

    def __init__(self, settings: str, error: ValidationError) -> None:
        lines: list[str] = []
        variables: list[str] = []
        for detail in error.errors(include_input=False, include_url=False):
            if not detail["loc"]:
                # A rule across several variables belongs to none of them.
                lines.append(f"  {detail['msg']}")
                continue
            name = "__".join(str(part) for part in detail["loc"])
            variable = f"{PREFIX}{name}".upper()
            problem = "missing" if detail["type"] == "missing" else f"invalid: {detail['msg']}"
            lines.append(f"  {variable} is {problem}")
            variables.append(variable)
        #: The variables to fix, each named once, in the order the fields are declared.
        self.variables = tuple(dict.fromkeys(variables))
        joined = "\n".join(lines)
        super().__init__(f"{settings} could not be read from the environment:\n{joined}")
