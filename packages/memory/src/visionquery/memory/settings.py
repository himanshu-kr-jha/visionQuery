"""The settings a process that uses the memory declares."""

import ipaddress
import re
from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, SecretStr, field_validator

__all__ = ["DatabaseSettings"]

_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
_PREFIXES = ("postgresql://", "postgres://")

#: Options with a fixed set of values, which libpq checks only when it connects and then
#: quotes the value back (libpq 17 and 18). `sslmode` is the one that is always checked.
_ENUMS = {
    "sslmode": _SSL_MODES,
    "gssencmode": ("disable", "prefer", "require"),
    "channel_binding": ("disable", "prefer", "require"),
    "sslnegotiation": ("postgres", "direct"),
    "sslcertmode": ("disable", "allow", "require"),
    "load_balance_hosts": ("disable", "random"),
    "target_session_attrs": (
        "any", "read-write", "read-only", "primary", "standby", "prefer-standby",
    ),
}  # fmt: skip
#: Options libpq reads as non-negative integers and quotes back when they are not.
_INTEGERS = (
    "connect_timeout",
    "keepalives",
    "keepalives_idle",
    "keepalives_interval",
    "keepalives_count",
    "tcp_user_timeout",
)
_DIGITS = re.compile(r"[0-9]{1,9}")
#: A host name, an IP address or the path of a socket directory: what a host can reasonably be.
_HOST = re.compile(r"[A-Za-z0-9._:/+~-]+")


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
        # libpq splits the authority at the first `@` and `urlsplit` at the last, so a second one
        # (an unescaped `@` in the user or the password) leaves part of the password as the host.
        authority = raw.partition("://")[2].partition("/")[0].partition("?")[0]
        if authority.count("@") > 1 or "#" in authority:
            raise ValueError("has an unescaped @ or # in its user or password; percent-encode it")
        # libpq checks these only when it connects, and then quotes the value back.
        problem = _invalid_option({k: str(v) for k, v in options.items() if v is not None})
        if problem:
            raise ValueError(f"has an invalid {problem}")
        return url


def _invalid_option(options: dict[str, str]) -> str | None:
    """The name of the first option of a parsed URL that libpq would quote back, if any."""
    for name, allowed in _ENUMS.items():
        if name in options and options[name] not in allowed:
            return name
    for name in _INTEGERS:
        if name in options and not _DIGITS.fullmatch(options[name]):
            return name
    ports = options.get("port", "")
    if ports and not all(_DIGITS.fullmatch(p) and 0 < int(p) < 65536 for p in ports.split(",")):
        return "port"
    hosts = options.get("host", "")
    if hosts and not all(_HOST.fullmatch(host) for host in hosts.split(",")):
        return "host"
    addresses = options.get("hostaddr", "")
    if addresses and not all(_is_ip(address) for address in addresses.split(",")):
        return "hostaddr"
    return None


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True
