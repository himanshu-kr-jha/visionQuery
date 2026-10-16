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
        "postgresql://u:p@[::1]:5432/d",
        "postgresql://u:p@h/d?port=5432&connect_timeout=0",
        "postgresql://u:p@h/d?connect_timeout=10",
        "postgresql://u:p@h/d?hostaddr=127.0.0.1&sslmode=verify-full&gssencmode=disable",
        "postgresql://u:p@h/d?target_session_attrs=read-write&load_balance_hosts=random",
        "postgresql://u:p@h/d?channel_binding=require&sslnegotiation=direct",
        "postgresql://u:p@h/d?host=h1,h2&port=1,2",
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
