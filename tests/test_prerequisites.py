"""A test skipped for a missing prerequisite is a failure in CI, where nothing may be missing.

A developer's machine may lack Docker or the web app's dependencies, so a test that needs them
says so and is skipped (its reason starts with "needs "). In CI (`CI` set, as GitHub Actions
sets it) the same skip would hide a check that never ran, so the root `conftest.py` reports
it as a failure. Skips for any other reason, such as a host without IPv6 or Triton being opt-in,
stay skips everywhere.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SKIPS = """
import pytest

@pytest.fixture
def docker():
    pytest.skip("needs a running Docker daemon")

def test_in_a_fixture(docker):
    pass

def test_in_the_test():
    pytest.skip("needs Postgres: set a URL or make Docker available")

@pytest.mark.skipif(True, reason="needs the web app's dependencies: run `make sync`")
def test_by_marker():
    pass

@pytest.mark.skipif(True, reason="this host has no IPv6 loopback")
def test_for_another_reason():
    pass

@pytest.mark.xfail(reason="needs nothing; expected to fail", strict=True)
def test_expected_to_fail():
    assert False
"""


@pytest.fixture
def suite(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeconftest((ROOT / "conftest.py").read_text())
    pytester.makepyfile(test_skips=SKIPS)
    return pytester


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_in_ci_a_missing_prerequisite_fails(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CI", value)
    result = suite.runpytest("-rA")
    # A skip at setup (by a fixture or a marker) becomes an error, one in the test a failure.
    result.assert_outcomes(failed=1, errors=2, skipped=1, xfailed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*needs a running Docker daemon*", "*CI*"])


@pytest.mark.parametrize("value", [None, "", "false", "0"])
def test_outside_ci_a_missing_prerequisite_is_skipped(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", value)
    suite.runpytest().assert_outcomes(skipped=4, xfailed=1)


def test_every_prerequisite_skip_in_the_workspace_says_what_it_needs() -> None:
    # The convention the conftest relies on: a skip for something missing starts with "needs ".
    # These are the ones that exist today; a new one must follow the convention.
    sources = {
        "packages/conftest.py": 'pytest.skip(f"needs Postgres',
        "tests/test_stack.py": 'pytest.skip("needs a running Docker daemon',
        "tests/test_compose.py": 'reason="needs the docker CLI',
        "tests/test_api_types.py": 'reason="needs the web app',
    }
    for path, skip in sources.items():
        assert skip in (ROOT / path).read_text(), path
