"""The fixtures of the reconciler's tests: a real MediaMTX (real_mediamtx.py), fresh for each test.

Without Docker every test that needs MediaMTX is skipped, saying so.
"""

import logging
from collections.abc import Iterator

import pytest
from real_mediamtx import MediaMTX, docker_works, running_mediamtx


@pytest.fixture(autouse=True)
def _logging_as_it_was() -> Iterator[None]:
    """Undo what a command's `start()` does to logging (its hooks, the root's handlers and
    level), which would otherwise leak into every test that runs after it in the session."""
    root = logging.getLogger()
    factory, make_record = logging.getLogRecordFactory(), logging.Logger.makeRecord
    handlers, level = root.handlers[:], root.level
    yield
    logging.setLogRecordFactory(factory)
    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture(scope="session")
def mediamtx_session() -> Iterator[MediaMTX]:
    if not docker_works():
        pytest.skip("needs Docker, to run MediaMTX and ffmpeg")
    with running_mediamtx() as server:
        yield server


@pytest.fixture
def mediamtx(mediamtx_session: MediaMTX) -> MediaMTX:
    """MediaMTX as its configuration file alone sets it up: no path, no camera's publisher."""
    mediamtx_session.restart()
    return mediamtx_session
