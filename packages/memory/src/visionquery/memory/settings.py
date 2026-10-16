"""The settings a process that uses the memory declares."""

from pydantic import BaseModel, SecretStr

__all__ = ["DatabaseSettings"]


class DatabaseSettings(BaseModel):
    """Where the memory is, declared as a `database` field of a service's settings.

    Read from `VISIONQUERY_DATABASE__URL`, a `postgresql://` URL whose password is never
    shown in a `repr` or a log line.
    """

    url: SecretStr
