"""The fixtures of the reconciler's tests: a real MediaMTX (real_mediamtx.py), fresh for each test.

Without Docker every test that needs MediaMTX is skipped, saying so.
"""

from collections.abc import Iterator

import pytest
from real_mediamtx import MediaMTX, docker_works, running_mediamtx


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
