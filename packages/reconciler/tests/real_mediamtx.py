"""A real MediaMTX, configured as the Deployment configures it, and ffmpeg to publish and read.

The reconciler's and the recorder's tests import it by name (pytest's `pythonpath` holds this
directory), and each one's conftest.py makes the fixtures. `running_mediamtx()` runs one
container of the MediaMTX release `docker/mediamtx/Dockerfile` builds on, with the Deployment's
own `docker/mediamtx/mediamtx.yml` and a generated password for the `visionquery` user, on a
Docker network of its own (as `mediamtx`). Its API and its RTSP port are published on loopback
ports of their own, so the tests reach them as the reconciler and the recorder do: with
credentials, and not from MediaMTX's own loopback. Cameras are played by ffmpeg containers (the
image the fake cameras use) on the same network. The container and the network are removed
afterwards; every container carries the label `packages/conftest.py` sweeps, should a session be
killed.
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
#: The user VisionQuery's own processes reach MediaMTX as.
SERVICE_USER = "visionquery"
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


def _service_user_index() -> int:
    """Where the `visionquery` user is in the configuration file, whose password MediaMTX
    reads from `MTX_AUTHINTERNALUSERS_<index>_PASS` as Compose sets it."""
    users = yaml.safe_load((MEDIAMTX_CONFIG / "mediamtx.yml").read_text())["authInternalUsers"]
    (index,) = [i for i, user in enumerate(users) if user["user"] == SERVICE_USER]
    return index


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
    #: The `visionquery` user's password.
    password: str
    #: The loopback port MediaMTX's RTSP is published on, for a reader on the host.
    rtsp_port: int

    def rtsp_url(self, path: str, user: str | None = None, password: str | None = None) -> str:
        """`path`'s RTSP URL as a reader on the host reaches it."""
        return f"rtsp://{_credentials(user, password)}127.0.0.1:{self.rtsp_port}/{path}"

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
        """Ask the control API as the `visionquery` user; the JSON it answers."""
        request = urllib.request.Request(
            self.api_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        token = f"{SERVICE_USER}:{self.password}".encode()
        request.add_header("Authorization", "Basic " + base64.b64encode(token).decode())
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read() or b"null")

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

    def publish_file(
        self, path: str, user: str, password: str, clip: Path
    ) -> subprocess.CompletedProcess[str]:
        """Publish `clip` once to `path` in real time, copied, as a camera would send it; ffmpeg
        exits 0 only when MediaMTX accepted all of it."""
        url = f"rtsp://{_credentials(user, password)}{RTSP}/{path}"
        return _docker(
            "run", "--rm", "--network", self.network, "--label", LABEL,
            "--volume", f"{clip.resolve()}:/clip{clip.suffix}:ro",
            FFMPEG_IMAGE, "-v", "error", "-re", "-i", f"/clip{clip.suffix}",
            "-c", "copy", "-f", "rtsp", "-rtsp_transport", "tcp", url,
            timeout=600,
        )  # fmt: skip

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
    password = secrets.token_hex(24)
    port, rtsp_port = _free_port(), _free_port()
    assert _docker("network", "create", "--label", LABEL, network).returncode == 0
    try:
        started = _docker(
            "run", "--detach", "--name", container, "--network", network,
            "--network-alias", "mediamtx",
            "--label", LABEL, "--label", f"{STARTED_LABEL}={int(time.time())}",
            "--env", f"MTX_AUTHINTERNALUSERS_{_service_user_index()}_PASS={password}",
            "--volume", f"{MEDIAMTX_CONFIG}:/etc/mediamtx:ro",
            # A port of its own, not an ephemeral one, which a restart would change.
            "--publish", f"127.0.0.1:{port}:9997",
            "--publish", f"127.0.0.1:{rtsp_port}:8554",
            MEDIAMTX_IMAGE, "/etc/mediamtx/mediamtx.yml",
            timeout=600,
        )  # fmt: skip
        assert started.returncode == 0, started.stderr
        try:
            server = MediaMTX(container, network, f"http://127.0.0.1:{port}", password, rtsp_port)
            server.wait_until_ready()
            yield server
        finally:
            _docker("rm", "--force", "--volumes", container)
    finally:
        _docker("network", "rm", network)
