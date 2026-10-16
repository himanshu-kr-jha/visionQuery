"""The settings a process that uses the memory declares."""

from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, SecretStr, field_validator

__all__ = ["DatabaseSettings"]

_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
_PREFIXES = ("postgresql://", "postgres://")


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
        raw = url.get_secret_value()
        try:
            parts = urlsplit(raw)
            parts.port  # noqa: B018 - raises for a port that is not a number in range
        except ValueError:
            raise ValueError("must be a postgresql:// URL, and this one cannot be parsed") from None
        # libpq reads the scheme case-sensitively, and `urlsplit` lowercases it.
        if not raw.startswith(_PREFIXES) or (not parts.netloc and not parts.path.strip("/")):
            raise ValueError("must be a postgresql:// or postgres:// URL")
        # What libpq itself will refuse (a bad sslmode, an unknown option, a bad escape) is
        # a settings error too, found before any process connects. libpq's own message quotes
        # the text it could not parse, which may hold the password, so it is dropped.
        try:
            options = conninfo_to_dict(raw)
        except psycopg.Error:
            raise ValueError(
                "must be a connection URL libpq can read, and this one is not"
            ) from None
        # Parsing does not check this one's value; libpq only does when it connects.
        if options.get("sslmode", "prefer") not in _SSL_MODES:
            raise ValueError("has an sslmode libpq does not know")
        return url
