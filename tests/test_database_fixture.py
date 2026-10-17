"""The test database fixture never waits on Docker for ever.

A Docker daemon that stops answering must fail the session, not hang it, so every command the
fixture in `packages/conftest.py` runs has a timeout.
"""

import ast
from pathlib import Path

CONFTEST = Path(__file__).parent.parent / "packages" / "conftest.py"


def test_every_command_the_fixture_runs_has_a_timeout() -> None:
    tree = ast.parse(CONFTEST.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert calls, "the fixture runs no command: is this test still looking at the right file?"
    untimed = [call.lineno for call in calls if "timeout" not in {k.arg for k in call.keywords}]
    assert untimed == [], f"subprocess calls without a timeout at lines {untimed}"
