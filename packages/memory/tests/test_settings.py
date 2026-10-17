"""`DatabaseSettings` refuses a URL libpq would misread or would quote back in an error.

None of these tests needs a database. A rejected value must never appear in the error.
"""

import pytest
from pydantic import SecretStr, ValidationError
from visionquery.memory.settings import DatabaseSettings

SECRET = "SECRETPW"


def _rejects(url: str) -> None:
    with pytest.raises(ValidationError) as raised:
        DatabaseSettings(url=SecretStr(url))
    assert SECRET not in str(raised.value)
    assert "SEC" not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://u:{SECRET}@127.0.0.1:1/d",
        "postgresql://u:p%40ss@127.0.0.1:5432/d",
        "postgresql://u@/d?host=%2Fvar%2Frun%2Fpostgresql",
        "postgresql://visionquery@postgres:5432/visionquery",
        "postgresql://u:p@[::1]:5432/d",
        "postgresql://u:p@h/d?port=5432&connect_timeout=0",
        "postgresql://u:p@h/d?connect_timeout=10",
        "postgresql://u:p@h/d?hostaddr=127.0.0.1&sslmode=verify-full&gssencmode=disable",
        "postgresql://u:p@h/d?target_session_attrs=read-write&load_balance_hosts=random",
        "postgresql://u:p@h/d?channel_binding=require&sslnegotiation=direct",
        "postgresql://u:p@h/d?host=h1,h2&port=1,2",
        # libpq's multi-host forms: default ports, ports per host, and IPv6 hosts among them.
        "postgresql://u:p@h1,h2/d",
        "postgresql://u:p@h1,h2/d?port=,5433",
        "postgresql://u:p@h1:5432,h2:5433/d",
        "postgresql://u:p@h1:5432,h2/d",
        "postgresql://u:p@[::1]:5432,[::2]:5433/d",
        "postgresql://u:p@h1:5432,[::1]:5433/d",
        # An IPv6 host with its zone.
        "postgresql://u:p@[fe80::1%25eth0]:5432/d",
        "postgresql://u:p@/d?host=fe80::1%25eth0",
        # The authentication methods the client accepts, or refuses with `!`.
        "postgresql://u:p@h/d?require_auth=scram-sha-256",
        "postgresql://u:p@h/d?require_auth=password,md5,gss,sspi,none,oauth",
        "postgresql://u:p@h/d?require_auth=!password,!md5",
        "postgresql://u:p@h/d?ssl_min_protocol_version=TLSv1.2&ssl_max_protocol_version=TLSv1.3",
        "postgresql://u:p@h/d?ssl_min_protocol_version=tlsv1",
        "postgresql://u:p@h/d?min_protocol_version=3.0&max_protocol_version=latest",
        "postgresql://u:p@h/d?max_protocol_version=3.2",
    ],
)
def test_ordinary_urls_are_accepted(url: str) -> None:
    DatabaseSettings(url=SecretStr(url))


@pytest.mark.parametrize(
    "url",
    [
        # An unescaped `@` in the password or the user: libpq and urlsplit split differently.
        f"postgresql://postgres:SEC@{SECRET}@127.0.0.1:55432/d",
        f"postgresql://us@er:{SECRET}@127.0.0.1:55432/d",
        f"postgresql://u:a@b@c@{SECRET}@127.0.0.1/d",
        f"postgresql://u:SEC@{SECRET}@[::1]:5432/d",
        f"postgresql://u:SEC@{SECRET}#x@h/d",
        f"postgresql://u:{SECRET}@[::1@x]/d",
        f"postgresql://u:{SECRET}@/d?host={SECRET}@x",
        f"postgresql://u:{SECRET}@h/d?host=a%40b",
        # Options libpq quotes back when they are wrong.
        f"postgresql://u:{SECRET}@h/d?port={SECRET}",
        f"postgresql://u:{SECRET}@h:1/d?port={SECRET}",
        f"postgresql://u:{SECRET}@h/d?port=70000",
        f"postgresql://u:{SECRET}@h/d?port=0",
        f"postgresql://u:{SECRET}@h/d?port=1,{SECRET}",
        f"postgresql://u:{SECRET}@h/d?connect_timeout={SECRET}",
        f"postgresql://u:{SECRET}@h/d?connect_timeout=abc",
        f"postgresql://u:{SECRET}@h/d?connect_timeout=10s",
        f"postgresql://u:{SECRET}@h/d?connect_timeout=-1",
        f"postgresql://u:{SECRET}@h/d?connect_timeout=",
        f"postgresql://u:{SECRET}@h/d?keepalives_idle={SECRET}",
        f"postgresql://u:{SECRET}@h/d?host=bad host",
        f"postgresql://u:{SECRET}@h/d?host=a'b",
        f"postgresql://u:{SECRET}@h/d?hostaddr={SECRET}",
        f"postgresql://u:{SECRET}@h/d?hostaddr=999.1.1.1",
        f"postgresql://u:{SECRET}@h/d?sslmode={SECRET}",
        f"postgresql://u:{SECRET}@h/d?gssencmode={SECRET}",
        f"postgresql://u:{SECRET}@h/d?target_session_attrs={SECRET}",
        f"postgresql://u:{SECRET}@h/d?load_balance_hosts={SECRET}",
        f"postgresql://u:{SECRET}@h/d?channel_binding={SECRET}",
        f"postgresql://u:{SECRET}@h/d?sslnegotiation={SECRET}",
        f"postgresql://u:{SECRET}@h/d?sslcertmode={SECRET}",
    ],
)
def test_a_url_libpq_would_misread_or_quote_back_is_rejected(url: str) -> None:
    _rejects(url)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://u:{SECRET}@h1:5432,h2:{SECRET}/d",
        f"postgresql://u:{SECRET}@h1:5432,h2:70000/d",
        f"postgresql://u:{SECRET}@h1,h2/d?port=,{SECRET}",
        f"postgresql://u:{SECRET}@h/d?require_auth={SECRET}",
        f"postgresql://u:{SECRET}@h/d?require_auth=password,{SECRET}",
        f"postgresql://u:{SECRET}@h/d?require_auth=!{SECRET}",
        # libpq refuses a mix of accepted and refused methods, and a method named twice.
        f"postgresql://u:{SECRET}@h/d?require_auth=!password,md5",
        f"postgresql://u:{SECRET}@h/d?require_auth=md5,md5",
        f"postgresql://u:{SECRET}@h/d?require_auth=",
        f"postgresql://u:{SECRET}@h/d?ssl_min_protocol_version={SECRET}",
        f"postgresql://u:{SECRET}@h/d?ssl_max_protocol_version={SECRET}",
        f"postgresql://u:{SECRET}@h/d?min_protocol_version={SECRET}",
        f"postgresql://u:{SECRET}@h/d?max_protocol_version={SECRET}",
        f"postgresql://u:{SECRET}@h/d?max_protocol_version=3.1",
    ],
)
def test_other_options_libpq_would_quote_back_are_rejected(url: str) -> None:
    _rejects(url)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://u:{SECRET}@bücher.example/d",
        f"postgresql://u:{SECRET}@b%C3%BCcher.example/d",
        f"postgresql://u:{SECRET}@/d?host=bücher.example",
        f"postgresql://u:{SECRET}@h1,bücher.example/d",
    ],
)
def test_a_non_ascii_host_name_is_refused_with_how_to_write_it(url: str) -> None:
    with pytest.raises(ValidationError) as raised:
        DatabaseSettings(url=SecretStr(url))
    message = str(raised.value)
    assert "non-ASCII host name" in message
    assert "xn--" in message
    assert SECRET not in message
    assert "cher" not in message
