"""What every test session in the workspace shares.

A test that needs something a machine may lack (Docker, Postgres, the web app's dependencies)
is skipped without it, with a reason that starts with "needs ". In CI (`CI` set to anything but
empty, `0` or `false`, as GitHub Actions sets it) everything is provided, so such a skip means
a check silently did not run: it is reported as a failure instead. That covers a skip while a
module is collected too: `pytest.skip(..., allow_module_level=True)` with such a reason, and
`pytest.importorskip` (whose default reason is "could not import ..."). Skips for any other
reason stay skips. tests/test_prerequisites.py proves both.
"""

import os
from collections.abc import Generator

import pytest

pytest_plugins = ("pytester",)

PREREQUISITE = "needs "
#: What `pytest.importorskip` says when it is given no reason of its own.
MISSING_MODULE = "could not import "


def _in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in {"", "0", "false"}


def _is_prerequisite(reason: str) -> bool:
    return reason.startswith((PREREQUISITE, MISSING_MODULE))


def _skip_reason(report: pytest.TestReport | pytest.CollectReport) -> str:
    longrepr = report.longrepr
    reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
    return reason.removeprefix("Skipped: ")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport() -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.skipped and not hasattr(report, "wasxfail") and _in_ci():
        reason = _skip_reason(report)
        if _is_prerequisite(reason):
            report.outcome = "failed"
            report.longrepr = (
                f"{reason}\nCI provides every prerequisite, so this test may not be skipped there."
            )
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    report = yield
    if report.skipped and _in_ci():
        reason = _skip_reason(report)
        if _is_prerequisite(reason):
            report.outcome = "failed"
            report.longrepr = (
                f"{reason}\nCI provides every prerequisite, so {collector.nodeid} may not be "
                "skipped there."
            )
    return report
