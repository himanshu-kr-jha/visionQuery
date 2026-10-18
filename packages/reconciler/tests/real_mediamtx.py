"""A real MediaMTX, configured as the Deployment configures it, and ffmpeg to publish and read.

The reconciler's tests import it by name (pytest's `pythonpath` holds this directory), and
conftest.py makes the fixtures. `running_mediamtx()` runs one container of the MediaMTX release
`docker/mediamtx/Dockerfile` builds on, with the Deployment's own `docker/mediamtx/mediamtx.yml`
and a generated password for each of its two users (`visionquery-api`, which drives the control
API, and `visionquery-reader`, which reads), on a Docker network of its own (as
`mediamtx`). Its API is published on an ephemeral loopback port, so the tests reach it as the
reconciler does: with credentials, and not from MediaMTX's own loopback. Cameras are played by
ffmpeg containers (the image the fake cameras use) on the same network. The container and the
network are removed afterwards; every container carries the label `packages/conftest.py` sweeps,
should a session be killed.
"""

import base64
import json
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

ROOT = Path(__file__).resolve().parents[3]
MEDIAMTX_CONFIG = ROOT / "docker" / "mediamtx"
#: The user the reconciler drives the control API as (permission `api` only).
API_USER = "visionquery-api"
#: The user every reader (the recorder, the Perception Workers, the live view) reads as
#: (permission `read` only).
READER_USER = "visionquery-reader"
LABEL = "visionquery-test=1"
STARTED_LABEL = "visionquery-test-started"
#: An RTSP URL's camera-facing side, as the publishers and readers on the network see it.
RTSP = "mediamtx:8554"


def _base_image(dockerfile: Path, stage: str) -> str:
    """The pinned image a Dockerfile's `FROM ... AS stage` names, so the tests run what ships."""
    found = re.search(rf"^FROM (\S+) AS {stage}$", dockerfile.read_text(), re.MULTILINE)
    assert found, f"{dockerfile} has no stage {stage}"
    return found[1]


MEDIAMTX_IMAGE = _base_image(MEDIAMTX_CONFIG / "Dockerfile", "mediamtx")
FFMPEG_IMAGE = _base_image(ROOT / "docker" / "fake-cameras" / "Dockerfile", "ffmpeg")


def _password_variable(name: str) -> str:
    """The variable MediaMTX reads user `name`'s password from as Compose sets it:
    `MTX_AUTHINTERNALUSERS_<index>_PASS`, the index being the user's place in the file."""
    users = yaml.safe_load((MEDIAMTX_CONFIG / "mediamtx.yml").read_text())["authInternalUsers"]
    (index,) = [i for i, user in enumerate(users) if user["user"] == name]
    return f"MTX_AUTHINTERNALUSERS_{index}_PASS"


def _docker(*arguments: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=False, timeout=timeout
    )


def docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info", timeout=30).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _credentials(user: str | None, password: str | None) -> str:
    if user is None:
        return ""
    return f"{quote(user, safe='')}:{quote(password or '', safe='')}@"


@dataclass(frozen=True)
class MediaMTX:
    """The running container, and what the tests do to it."""

    container: str
    network: str
    #: The control API, as the reconciler is given it.
    api_url: str
    #: The `visionquery-api` user's password.
    api_password: str
    #: The `visionquery-reader` user's password.
    reader_password: str

    def restart(self) -> None:
        """Restart MediaMTX, which forgets everything set through its API."""
        assert _docker("restart", "--time", "0", self.container).returncode == 0
        self.wait_until_ready()

    def stop(self) -> None:
        assert _docker("stop", "--time", "0", self.container).returncode == 0

    def start(self) -> None:
        assert _docker("start", self.container).returncode == 0
        self.wait_until_ready()

    def wait_until_ready(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.api("GET", "/v3/config/global/get")
                return
            except urllib.error.URLError, ConnectionError, OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def api(self, method: str, path: str, body: object = None) -> Any:
        """Ask the control API as the `visionquery-api` user; the JSON it answers."""
        request = urllib.request.Request(
            self.api_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        token = f"{API_USER}:{self.api_password}".encode()
        request.add_header("Authorization", "Basic " + base64.b64encode(token).decode())
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read() or b"null")

    def api_status(
        self,
        user: str | None,
        password: str | None,
        method: str = "GET",
        path: str = "/v3/config/global/get",
    ) -> int:
        """The HTTP status the control API gives `user` for a request (401 for bad credentials,
        403 for a user without the `api` permission); 0 if the connection was refused."""
        request = urllib.request.Request(self.api_url + path, method=method)
        if user is not None:
            token = f"{user}:{password}".encode()
            request.add_header("Authorization", "Basic " + base64.b64encode(token).decode())
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return error.code

    def with_known_passwords(self, users: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """`users` (as `users()` shows them) with the two VisionQuery users' passwords put back,
        which is what writing the list through the API needs."""
        known = {API_USER: self.api_password, READER_USER: self.reader_password}
        return [{**user, "pass": known.get(user["user"], user["pass"])} for user in users]

    def path_configs(self) -> dict[str, dict[str, Any]]:
        """Every configured path, by name, with all its settings."""
        listed = self.api("GET", "/v3/config/paths/list?itemsPerPage=1000")
        return {item["name"]: item for item in listed["items"]}

    def users(self) -> list[dict[str, Any]]:
        """The internal users, as MediaMTX shows them (passwords redacted)."""
        return self.api("GET", "/v3/config/global/get")["authInternalUsers"]

    def publish(
        self, path: str, user: str | None = None, password: str | None = None, seconds: int = 2
    ) -> subprocess.CompletedProcess[str]:
        """Publish a test pattern for `seconds` to `path`, as a camera would; ffmpeg exits 0
        only when MediaMTX accepted it."""
        return _docker(*self._publisher(path, user, password, seconds), timeout=60)

    @contextmanager
    def publishing(self, path: str, user: str, password: str) -> Generator[None]:
        """A camera publishing to `path` until the block ends; it fails the test if refused."""
        name = f"vq-test-publisher-{uuid.uuid4().hex[:12]}"
        command = self._publisher(path, user, password, 120, name=name, detach=True)
        assert _docker(*command).returncode == 0
        try:
            self.wait_until_published(path)
            yield
        finally:
            _docker("rm", "--force", name)

    def wait_until_published(self, path: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.api("GET", f"/v3/paths/get/{path}").get("ready"):
                    return
            except urllib.error.HTTPError:
                pass
            assert time.monotonic() < deadline, f"nothing was published to {path}"
            time.sleep(0.2)

    def read(
        self, path: str, user: str | None = None, password: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Read one frame of `path`, as the recorder would; ffmpeg exits 0 only when it did."""
        url = f"rtsp://{_credentials(user, password)}{RTSP}/{path}"
        return _docker(
            "run", "--rm", "--network", self.network, "--label", LABEL, FFMPEG_IMAGE,
            "-v", "error", "-rtsp_transport", "tcp", "-timeout", "10000000",
            "-i", url, "-frames:v", "1", "-f", "null", "-",
        )  # fmt: skip

    def _publisher(
        self,
        path: str,
        user: str | None,
        password: str | None,
        seconds: int,
        *,
        name: str | None = None,
        detach: bool = False,
    ) -> list[str]:
        url = f"rtsp://{_credentials(user, password)}{RTSP}/{path}"
        return [
            "run", "--rm", "--network", self.network, "--label", LABEL,
            *(["--name", name] if name else []), *(["--detach"] if detach else []),
            FFMPEG_IMAGE, "-v", "error", "-re",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=10:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-bf", "0", "-g", "10",
            "-f", "rtsp", "-rtsp_transport", "tcp", url,
        ]  # fmt: skip


@contextmanager
def running_mediamtx() -> Generator[MediaMTX]:
    """A MediaMTX container on a network of its own, removed with the network afterwards."""
    suffix = uuid.uuid4().hex[:12]
    network, container = f"vq-test-net-{suffix}", f"vq-test-mediamtx-{suffix}"
    api_password, reader_password = secrets.token_hex(24), secrets.token_hex(24)
    port = _free_port()
    assert _docker("network", "create", "--label", LABEL, network).returncode == 0
    try:
        started = _docker(
            "run", "--detach", "--name", container, "--network", network,
            "--network-alias", "mediamtx",
            "--label", LABEL, "--label", f"{STARTED_LABEL}={int(time.time())}",
            "--env", f"{_password_variable(API_USER)}={api_password}",
            "--env", f"{_password_variable(READER_USER)}={reader_password}",
            "--volume", f"{MEDIAMTX_CONFIG}:/etc/mediamtx:ro",
            # A port of its own, not an ephemeral one, which a restart would change.
            "--publish", f"127.0.0.1:{port}:9997",
            MEDIAMTX_IMAGE, "/etc/mediamtx/mediamtx.yml",
            timeout=600,
        )  # fmt: skip
        assert started.returncode == 0, started.stderr
        try:
            server = MediaMTX(
                container, network, f"http://127.0.0.1:{port}", api_password, reader_password
            )
            server.wait_until_ready()
            yield server
        finally:
            _docker("rm", "--force", "--volumes", container)
    finally:
        _docker("network", "rm", network)
