"""What `visionquery-api` is configured with."""

import pytest
from visionquery.api.serve import ApiSettings


def test_it_listens_on_this_machine_only_unless_told_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VISIONQUERY_HTTP_HOST", raising=False)
    monkeypatch.setenv("VISIONQUERY_DATABASE__URL", "postgresql://u@127.0.0.1:5432/db")
    assert ApiSettings.load().http_host == "127.0.0.1"


def test_the_host_can_be_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIONQUERY_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("VISIONQUERY_DATABASE__URL", "postgresql://u@127.0.0.1:5432/db")
    assert ApiSettings.load().http_host == "0.0.0.0"
