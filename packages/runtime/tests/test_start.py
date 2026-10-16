"""A real process started through the runtime: its settings, its log lines and its /metrics.

Each test runs a small service in a child Python process, the way a container would run
one, and watches only what whoever runs the container could see: its stdout, its stderr, its
exit code and its metrics endpoint over HTTP.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

import pytest
from prometheus_client.parser import text_string_to_metric_families

URL = "rtsp://admin:hunter2@10.0.0.7:554/stream1"
SCRUBBED = "rtsp://***@10.0.0.7:554/stream1"

SERVICE = """
import logging
import sys

from prometheus_client import Counter
from visionquery.runtime import Settings, start


class ProbeSettings(Settings):
    camera_url: str


settings = start("probe", ProbeSettings)
Counter("probe_frames", "Frames read.").inc(5)
logging.getLogger("probe").info("watching %s", settings.camera_url)
sys.stdin.read()  # until the test lets go
raise RuntimeError(f"stream died: {settings.camera_url}")
"""


@pytest.fixture
def service(tmp_path: Path) -> Path:
    path = tmp_path / "probe.py"
    path.write_text(SERVICE)
    return path


def _environment(**variables: str) -> dict[str, str]:
    inherited = {k: v for k, v in os.environ.items() if not k.upper().startswith("VISIONQUERY_")}
    return inherited | {f"VISIONQUERY_{name.upper()}": value for name, value in variables.items()}


def _run(service: Path, environment: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(service)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _lines_until(process: subprocess.Popen[str], message: str) -> Iterator[dict[str, Any]]:
    """Each JSON line the process writes, up to and including the one with `message`.

    A thread reads the pipe into a queue, so a line already buffered by the reader is never
    mistaken for "nothing yet" (as `select` on a buffered pipe would). The thread stops reading
    at `message`, so what the process writes afterwards is left for `communicate`.
    """
    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()

    def read(stream: TextIO) -> None:
        for raw in iter(stream.readline, ""):
            lines.put(raw)
            if json.loads(raw)["message"] == message:
                return
        lines.put("")  # the end of the output

    threading.Thread(target=read, args=(process.stdout,), daemon=True).start()
    deadline = time.monotonic() + 15
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            raw = lines.get(timeout=remaining)
        except queue.Empty:
            break
        assert raw, f"the process exited before logging {message!r}"
        line = json.loads(raw)
        yield line
        if line["message"] == message:
            return
    pytest.fail(f"no {message!r} within 15 s")


def test_lines_are_seen_even_when_the_reader_is_late() -> None:
    # The child flushes two lines and waits; the reader looks only half a second later.
    child = (
        "import sys, time\n"
        "for m in ('first', 'second'):\n"
        '    print(\'{"message": "%s"}\' % m, flush=True)\n'
        "time.sleep(30)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
    try:
        time.sleep(0.5)
        assert [line["message"] for line in _lines_until(process, "second")] == ["first", "second"]
    finally:
        process.kill()
        process.wait()


def _scrape(port: int) -> dict[str, float]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
        body = response.read().decode()
    return {
        sample.name: sample.value
        for family in text_string_to_metric_families(body)
        for sample in family.samples
    }


def test_a_started_process_logs_json_and_serves_metrics(service: Path) -> None:
    environment = _environment(camera_url=URL, metrics_host="127.0.0.1", metrics_port="0")
    process = _run(service, environment)
    try:
        lines = list(_lines_until(process, f"watching {SCRUBBED}"))
        (started,) = [line for line in lines if line["message"] == "started"]
        assert all(line["service"] == "probe" for line in lines)
        assert _scrape(started["metrics_port"])["probe_frames_total"] == 5
    finally:
        stdout, stderr = process.communicate(input="", timeout=15)
    assert process.returncode == 1
    (crash,) = [json.loads(line) for line in stdout.splitlines()]
    assert crash["level"] == "CRITICAL"
    assert f"RuntimeError: stream died: {SCRUBBED}" in crash["exception"]
    assert stderr == ""
    assert "hunter2" not in json.dumps(lines) + stdout


def test_the_log_level_comes_from_the_environment(service: Path) -> None:
    environment = _environment(
        camera_url=URL, metrics_host="127.0.0.1", metrics_port="0", log_level="warning"
    )
    stdout, stderr = _run(service, environment).communicate(input="", timeout=15)
    # Neither "started" nor "watching" is a warning; only the crash is left.
    assert [json.loads(line)["level"] for line in stdout.splitlines()] == ["CRITICAL"]
    assert stderr == ""


def test_a_missing_variable_stops_the_process_naming_it(service: Path) -> None:
    environment = _environment(metrics_host="127.0.0.1", metrics_port="0")
    process = _run(service, environment)
    stdout, stderr = process.communicate(input="", timeout=15)
    assert process.returncode == 2
    (line,) = [json.loads(raw) for raw in stdout.splitlines()]
    assert line["level"] == "CRITICAL"
    assert line["service"] == "probe"
    assert "VISIONQUERY_CAMERA_URL is missing" in line["message"]
    assert line["variables"] == ["VISIONQUERY_CAMERA_URL"]
    assert stderr == ""


def test_an_invalid_variable_is_reported_without_its_value(service: Path) -> None:
    environment = _environment(camera_url=URL, metrics_port=URL)
    process = _run(service, environment)
    stdout, stderr = process.communicate(input="", timeout=15)
    assert process.returncode == 2
    (line,) = [json.loads(raw) for raw in stdout.splitlines()]
    assert line["variables"] == ["VISIONQUERY_METRICS_PORT"]
    assert "hunter2" not in stdout + stderr
