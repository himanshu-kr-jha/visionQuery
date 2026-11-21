"""The fixtures of the recorder's tests: the ffmpeg the recorder ships with, and a real MediaMTX.

ffmpeg is the binary the VisionQuery image copies from its `ffmpeg` stage (the root
Dockerfile), taken out of that pinned image once per session, so the tests run the version
that ships rather than whatever the machine has. MediaMTX is the reconciler's real one
(real_mediamtx.py), configured as the Deployment configures it. Without Docker every test that
needs either is skipped, saying so.
"""

import logging
import re
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from real_mediamtx import LABEL, MediaMTX, docker_works, running_mediamtx

ROOT = Path(__file__).resolve().parents[3]


def ffmpeg_image() -> str:
    """The image the VisionQuery image takes ffmpeg from."""
    found = re.search(r"^FROM (\S+) AS ffmpeg$", (ROOT / "Dockerfile").read_text(), re.MULTILINE)
    assert found, "the Dockerfile has no ffmpeg stage"
    return found[1]


@pytest.fixture(scope="session")
def ffmpeg(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The ffmpeg binary the recorder runs in the Deployment."""
    if not docker_works():
        pytest.skip("needs Docker, to take ffmpeg from the image the recorder ships with")
    name = f"vq-test-ffmpeg-{uuid.uuid4().hex[:12]}"
    created = subprocess.run(
        ["docker", "create", "--name", name, "--label", LABEL, ffmpeg_image(), "/ffmpeg"],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert created.returncode == 0, created.stderr
    try:
        binary = tmp_path_factory.mktemp("ffmpeg") / "ffmpeg"
        copied = subprocess.run(
            ["docker", "cp", f"{name}:/ffmpeg", str(binary)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert copied.returncode == 0, copied.stderr
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)
    return binary


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
