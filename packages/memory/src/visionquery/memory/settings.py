"""The settings a process that uses the memory declares."""

from urllib.parse import urlsplit

from pydantic import BaseModel, SecretStr, field_validator

__all__ = ["DatabaseSettings"]

_SCHEMES = ("postgresql", "postgres")


class DatabaseSettings(BaseModel):
    """Where the memory is, declared as a `database` field of a service's settings.

    Read from `VISIONQUERY_DATABASE__URL`, a `postgresql://` URL whose password is never
    shown in a `repr` or a log line. Anything else is rejected, because only a URL can be
    scrubbed from a log line (ADR 0012): a keyword string or a scheme-less `user:password`
    would carry its password where nothing recognises it.
    """

    url: SecretStr

    @field_validator("url")
    @classmethod
    def _is_a_postgres_url(cls, url: SecretStr) -> SecretStr:
        # Messages here never include the value: it may hold the password.
        try:
            parts = urlsplit(url.get_secret_value())
            parts.port  # noqa: B018 - raises for a port that is not a number in range
        except ValueError:
            raise ValueError("must be a postgresql:// URL, and this one cannot be parsed") from None
        if parts.scheme not in _SCHEMES or (not parts.netloc and not parts.path.strip("/")):
            raise ValueError("must be a postgresql:// or postgres:// URL")
        return url
