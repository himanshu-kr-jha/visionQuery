"""A process serves its Prometheus metrics on `/metrics`, scraped here over real HTTP."""

import socket
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest
from prometheus_client import REGISTRY, Counter
from prometheus_client.parser import text_string_to_metric_families
from visionquery.runtime import MetricsServer, serve_metrics


@pytest.fixture
def server() -> Iterator[MetricsServer]:
    with serve_metrics("127.0.0.1", 0) as server:
        yield server


@pytest.fixture
def segments() -> Iterator[Counter]:
    counter = Counter("test_segments_sealed", "Segments sealed.", ["camera"])
    yield counter
    REGISTRY.unregister(counter)


def _get(port: int, path: str) -> tuple[int, str, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, response.headers["Content-Type"], response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], error.read().decode()


def _samples(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


def test_metrics_are_served_in_prometheus_text_format(
    server: MetricsServer, segments: Counter
) -> None:
    segments.labels(camera="gate-2").inc(3)
    status, content_type, body = _get(server.port, "/metrics")
    assert status == 200
    assert content_type.startswith("text/plain; version=0.0.4")
    samples = _samples(body)
    assert samples[("test_segments_sealed_total", (("camera", "gate-2"),))] == 3


def test_each_scrape_sees_the_current_values(server: MetricsServer, segments: Counter) -> None:
    segments.labels(camera="gate-2").inc()
    _get(server.port, "/metrics")
    segments.labels(camera="gate-2").inc()
    _, _, body = _get(server.port, "/metrics")
    assert _samples(body)[("test_segments_sealed_total", (("camera", "gate-2"),))] == 2


def test_the_process_itself_is_described(server: MetricsServer) -> None:
    names = {name for name, _ in _samples(_get(server.port, "/metrics")[2])}
    assert "process_resident_memory_bytes" in names
    assert "python_info" in names


def test_only_the_metrics_path_is_served(server: MetricsServer) -> None:
    assert _get(server.port, "/")[0] == 404
    assert _get(server.port, "/metrics/extra")[0] == 404


def test_scrapes_are_not_logged_to_stderr(
    server: MetricsServer, capfd: pytest.CaptureFixture[str]
) -> None:
    _get(server.port, "/metrics")
    _get(server.port, "/nowhere")
    assert capfd.readouterr().err == ""


def test_closing_frees_the_port() -> None:
    server = serve_metrics("127.0.0.1", 0)
    port = server.port
    server.close()
    with pytest.raises(ConnectionRefusedError), socket.create_connection(("127.0.0.1", port), 1):
        pass
