"""`visionquery-api`, run the way a Deployment runs it: a process configured by the environment.

Each test runs the installed command in a child process and looks only at what an Operator
could see: its HTTP answers, its exit status and its log lines.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

COMMAND = Path(sys.executable).parent / "visionquery-api"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _environment(**variables: str) -> dict[str, str]:
    inherited = {k: v for k, v in os.environ.items() if not k.upper().startswith("VISIONQUERY_")}
    return inherited | {f"VISIONQUERY_{k.upper()}": v for k, v in variables.items()}


def _get(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@contextmanager
def _running(database_url: str) -> Generator[tuple[subprocess.Popen[str], int]]:
    """The api serving on a free loopback port, stopped as a container is (SIGTERM)."""
    port = _free_port()
    environment = _environment(
        database__url=database_url,
        http_host="127.0.0.1",
        http_port=str(port),
        metrics_host="127.0.0.1",
        metrics_port="0",
    )
    process = subprocess.Popen(
        [str(COMMAND)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                assert process.poll() is None, process.communicate()
                assert time.monotonic() < deadline, "the api never listened"
                time.sleep(0.1)
        yield process, port
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)


def test_serves_health_on_its_port_and_logs_only_json_lines(postgres: str) -> None:
    with _running(postgres) as (process, port):
        status, body = _get(f"http://127.0.0.1:{port}/health")
    stdout, stderr = process.communicate()
    assert (status, body["status"]) == (200, "healthy")
    # uvicorn logs through the runtime (log_config=None), so its lines are JSON and scrubbed.
    lines = [json.loads(line) for line in stdout.splitlines()]
    assert {line["service"] for line in lines} == {"api"}
    assert any(line["logger"].startswith("uvicorn") for line in lines), lines
    assert stderr == ""
    # Stopped gracefully: uvicorn finishes, then lets SIGTERM end the process as it would have.
    assert "Finished server process" in lines[-1]["message"], lines
    assert process.returncode in {0, -signal.SIGTERM}


def test_never_logs_the_database_password() -> None:
    password = "hunter2-in-the-url"
    url = f"postgresql://visionquery:{password}@127.0.0.1:{_free_port()}/visionquery"
    with _running(url) as (process, port):
        status, _ = _get(f"http://127.0.0.1:{port}/health")
    stdout, stderr = process.communicate()
    assert status == 503
    assert password not in stdout + stderr


def test_without_a_database_url_it_exits_naming_the_variable() -> None:
    done = subprocess.run(
        [str(COMMAND)],
        env=_environment(metrics_host="127.0.0.1", metrics_port="0"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 2
    assert "VISIONQUERY_DATABASE__URL" in done.stdout
