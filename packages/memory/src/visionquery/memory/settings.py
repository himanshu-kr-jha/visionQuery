"""The settings a process that uses the memory declares.

Validating the URL here is best effort: it turns the mistakes libpq would quote back (and so
might print part of a password) into a settings error, exit status 2, before anything connects.
The guarantee that libpq's text never reaches a log line is the connection layer's
(`visionquery.memory.connection`), which reports any failure to connect by fixed category only.
"""

import ipaddress
import os
import re
from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, SecretStr, field_validator, model_validator

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
    # Empty means libpq's default.
    "min_protocol_version": ("", "3.0", "3.2", "latest"),
    "max_protocol_version": ("", "3.0", "3.2", "latest"),
}  # fmt: skip
#: Like `_ENUMS`, but libpq compares these without regard to case.
_CASELESS_ENUMS = {
    "ssl_min_protocol_version": ("", "tlsv1", "tlsv1.1", "tlsv1.2", "tlsv1.3"),
    "ssl_max_protocol_version": ("", "tlsv1", "tlsv1.1", "tlsv1.2", "tlsv1.3"),
}
#: What `require_auth` may list, each optionally refused with `!`.
_AUTH_METHODS = frozenset({"password", "md5", "gss", "sspi", "scram-sha-256", "oauth", "none"})
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
        scheme, separator, rest = raw.partition("://")
        authority = rest.partition("/")[0].partition("?")[0]
        userinfo, at, hosts = authority.rpartition("@")
        single = raw
        if "," in hosts:
            # libpq's multi-host form (`h1:5432,[::1]:5433`) is more than urlsplit reads, so it
            # sees one stand-in host; libpq parses the list, whose hosts and ports are checked
            # below.
            single = f"{scheme}{separator}{userinfo}{at}hosts{rest[len(authority) :]}"
        try:
            parts = urlsplit(single)
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
        if authority.count("@") > 1 or "#" in authority:
            raise ValueError("has an unescaped @ or # in its user or password; percent-encode it")
        # libpq checks these only when it connects, and then quotes the value back.
        problem = _invalid_option({k: str(v) for k, v in options.items() if v is not None})
        if problem:
            raise ValueError(problem)
        return url

    @model_validator(mode="after")
    def _pgconnect_timeout_is_an_integer(self) -> DatabaseSettings:
        # libpq's own variable, which connecting reads when the URL sets no timeout; an empty
        # one is unset and 0 waits forever, as in libpq. libpq would quote a bad one back.
        value = os.environ.get("PGCONNECT_TIMEOUT", "")
        if value and not _DIGITS.fullmatch(value):
            raise ValueError("PGCONNECT_TIMEOUT must be a non-negative whole number of seconds")
        return self


def _invalid_option(options: dict[str, str]) -> str | None:
    """What is wrong with the first option of a parsed URL that libpq would quote back, if any."""
    for name, allowed in _ENUMS.items():
        if name in options and options[name] not in allowed:
            return f"has an invalid {name}"
    for name, allowed in _CASELESS_ENUMS.items():
        if name in options and options[name].lower() not in allowed:
            return f"has an invalid {name}"
    if "require_auth" in options and not _valid_require_auth(options["require_auth"]):
        return "has an invalid require_auth"
    for name in _INTEGERS:
        if name in options and not _DIGITS.fullmatch(options[name]):
            return f"has an invalid {name}"
    ports = options.get("port", "")
    # An empty entry is the default port, as in `h1,h2` or `?port=,5433`.
    if ports and not all(p == "" or _is_port(p) for p in ports.split(",")):
        return "has an invalid port"
    hosts = options.get("host", "").split(",")
    if not all(host.isascii() for host in hosts):
        # libpq does not translate one, so it would not be found.
        return (
            "has a non-ASCII host name; write it in its ASCII form, the xn-- name that "
            "`python3 -c \"print('name'.encode('idna').decode())\"` prints for it"
        )
    if hosts != [""] and not all(_HOST.fullmatch(host) or _is_ip(host) for host in hosts):
        return "has an invalid host"
    addresses = options.get("hostaddr", "")
    if addresses and not all(_is_ip(address) for address in addresses.split(",")):
        return "has an invalid hostaddr"
    return None


def _valid_require_auth(value: str) -> bool:
    """Known methods, none named twice, and either all refused with `!` or none."""
    methods = value.split(",")
    names = [method.removeprefix("!") for method in methods]
    refused = {method.startswith("!") for method in methods}
    return len(refused) == 1 and len(set(names)) == len(names) and set(names) <= _AUTH_METHODS


def _is_port(text: str) -> bool:
    return bool(_DIGITS.fullmatch(text)) and 0 < int(text) < 65536


def _is_ip(text: str) -> bool:
    """An IPv4 or IPv6 address, an IPv6 one with its zone (`fe80::1%eth0`) included."""
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True
