"""`make fetch-samples`: the sample videos are downloaded and verified, never committed.

The script's own logic is checked against a local HTTP server and `file://` URLs, so nothing
here needs the internet. The committed manifest is checked for what every sample must carry.
"""

import hashlib
import http.server
import json
import re
import subprocess
import threading
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fetch_samples import (
    MANIFEST,
    SAMPLES,
    FetchError,
    Outcome,
    Sample,
    fetch,
    load_manifest,
    main,
)

ROOT = Path(__file__).resolve().parents[1]

#: SPDX identifiers of licenses that let anyone download and use a sample for development.
OPEN_LICENSES = {"CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-3.0", "CC-BY-SA-3.0"}
#: What a developer downloads once: a few minutes of a few cameras, not a dataset.
MAX_TOTAL_BYTES = 400_000_000


# --- The committed manifest -------------------------------------------------------------------


def test_the_manifest_and_the_samples_directory_are_where_the_script_looks() -> None:
    assert ROOT / "samples" == SAMPLES
    assert SAMPLES / "manifest.toml" == MANIFEST


def test_every_sample_names_its_checksum_size_license_and_attribution() -> None:
    samples = load_manifest(MANIFEST)
    assert samples
    for sample in samples:
        assert re.fullmatch(r"[0-9a-f]{64}", sample.sha256), sample.file
        assert sample.size > 0, sample.file
        assert sample.url.startswith("https://"), sample.file
        assert sample.license in OPEN_LICENSES, sample.file
        assert sample.license_url.startswith("https://"), sample.file
        assert sample.attribution.strip(), sample.file
        assert sample.source.startswith("https://"), sample.file


def test_samples_are_plain_file_names_each_used_once() -> None:
    files = [sample.file for sample in load_manifest(MANIFEST)]
    assert len(files) == len(set(files))
    for name in files:
        assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name), name
        assert name != MANIFEST.name


def test_the_download_stays_modest() -> None:
    assert sum(sample.size for sample in load_manifest(MANIFEST)) <= MAX_TOTAL_BYTES


def test_the_samples_are_git_ignored_and_the_manifest_is_not() -> None:
    def ignored(path: str) -> bool:
        check = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", path], cwd=ROOT, check=False
        )
        return check.returncode == 0

    assert not ignored("samples/manifest.toml")
    for sample in load_manifest(MANIFEST):
        assert ignored(f"samples/{sample.file}"), sample.file
        assert ignored(f"samples/{sample.file}.part"), sample.file
    # And any video a developer adds by hand.
    assert ignored("samples/my-own-camera.mkv")


def test_no_video_is_committed() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "samples"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert tracked == ["samples/manifest.toml"]


# --- The script, against a server on loopback -------------------------------------------------


@dataclass
class Server:
    """Serves `files` on loopback, and counts the requests it answered."""

    files: dict[str, bytes] = field(default_factory=dict[str, bytes])
    requests: list[str] = field(default_factory=list[str])
    #: Close the connection after this many bytes of the body, to imitate a dropped download.
    cut_after: int | None = None
    #: Answer only once this many requests are waiting, to tell parallel downloads apart.
    together: threading.Barrier | None = None
    port: int = 0

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"


@pytest.fixture
def server() -> Iterator[Server]:
    state = Server()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            name = self.path.lstrip("/")
            state.requests.append(name)
            if state.together is not None:
                try:
                    state.together.wait(timeout=5)
                except threading.BrokenBarrierError:
                    self.send_error(503, "the downloads did not run in parallel")
                    return
            body = state.files.get(name)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body if state.cut_after is None else body[: state.cut_after])

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()


VIDEO = b"\x00\x00\x00\x18ftypisom" + bytes(range(256)) * 400


def _sample(url: str, content: bytes = VIDEO, file: str = "walk.mp4") -> Sample:
    return Sample(
        file=file,
        url=url,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="Someone, for the tests",
        source="https://example.org/",
    )


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_a_sample_is_downloaded_and_verified(server: Server, tmp_path: Path) -> None:
    server.files["walk.mp4"] = VIDEO
    assert fetch(_sample(server.url("walk.mp4")), tmp_path) is Outcome.FETCHED
    assert (tmp_path / "walk.mp4").read_bytes() == VIDEO
    assert _leftovers(tmp_path) == ["walk.mp4"]


def test_a_downloaded_sample_is_readable_by_the_fake_cameras_user(
    server: Server, tmp_path: Path
) -> None:
    # The fake cameras run as a user of their own and read the directory through a bind mount.
    server.files["walk.mp4"] = VIDEO
    fetch(_sample(server.url("walk.mp4")), tmp_path)
    assert (tmp_path / "walk.mp4").stat().st_mode & 0o004


def test_a_directory_and_samples_made_private_are_opened_to_the_fake_cameras_user(
    server: Server, tmp_path: Path
) -> None:
    # As after `umask 077`: the sample is skipped, but would otherwise stay unreadable.
    samples = tmp_path / "samples"
    samples.mkdir(mode=0o700)
    samples.chmod(0o700)
    (samples / "walk.mp4").write_bytes(VIDEO)
    (samples / "walk.mp4").chmod(0o600)
    assert fetch(_sample(server.url("walk.mp4")), samples) is Outcome.PRESENT
    assert samples.stat().st_mode & 0o005 == 0o005
    assert (samples / "walk.mp4").stat().st_mode & 0o004


def test_a_sample_already_present_is_not_downloaded_again(server: Server, tmp_path: Path) -> None:
    (tmp_path / "walk.mp4").write_bytes(VIDEO)
    assert fetch(_sample(server.url("walk.mp4")), tmp_path) is Outcome.PRESENT
    assert server.requests == []


def test_a_present_sample_with_other_content_is_downloaded_again(
    server: Server, tmp_path: Path
) -> None:
    server.files["walk.mp4"] = VIDEO
    (tmp_path / "walk.mp4").write_bytes(b"a truncated copy")
    assert fetch(_sample(server.url("walk.mp4")), tmp_path) is Outcome.FETCHED
    assert (tmp_path / "walk.mp4").read_bytes() == VIDEO


def test_a_checksum_mismatch_fails_and_leaves_nothing(server: Server, tmp_path: Path) -> None:
    server.files["walk.mp4"] = b"not the sample the manifest describes"
    with pytest.raises(FetchError, match=r"walk\.mp4.*SHA-256"):
        fetch(_sample(server.url("walk.mp4")), tmp_path)
    assert _leftovers(tmp_path) == []


def test_a_mismatch_never_replaces_a_good_copy_with_a_bad_one(
    server: Server, tmp_path: Path
) -> None:
    # The present copy is wrong, and so is what the server sends: the download never takes its
    # place, and the next run tries again because the present copy still does not verify.
    (tmp_path / "walk.mp4").write_bytes(b"stale")
    server.files["walk.mp4"] = b"also wrong"
    with pytest.raises(FetchError):
        fetch(_sample(server.url("walk.mp4")), tmp_path)
    assert _leftovers(tmp_path) == ["walk.mp4"]
    assert (tmp_path / "walk.mp4").read_bytes() == b"stale"


def test_a_dropped_download_is_cleaned_up(server: Server, tmp_path: Path) -> None:
    server.files["walk.mp4"] = VIDEO
    server.cut_after = 1000
    with pytest.raises(FetchError, match=r"walk\.mp4"):
        fetch(_sample(server.url("walk.mp4")), tmp_path)
    assert _leftovers(tmp_path) == []


def test_a_partial_file_left_by_a_killed_run_is_replaced(server: Server, tmp_path: Path) -> None:
    (tmp_path / "walk.mp4.part").write_bytes(VIDEO[:100])
    server.files["walk.mp4"] = VIDEO
    assert fetch(_sample(server.url("walk.mp4")), tmp_path) is Outcome.FETCHED
    assert _leftovers(tmp_path) == ["walk.mp4"]


def test_a_cancelled_download_stops_and_leaves_nothing(server: Server, tmp_path: Path) -> None:
    # What Ctrl-C does to every download in progress.
    server.files["walk.mp4"] = VIDEO
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(FetchError, match=r"walk\.mp4.*cancelled"):
        fetch(_sample(server.url("walk.mp4")), tmp_path, cancelled)
    assert _leftovers(tmp_path) == []


def test_a_missing_url_fails_naming_the_sample(server: Server, tmp_path: Path) -> None:
    with pytest.raises(FetchError, match=r"walk\.mp4.*404"):
        fetch(_sample(server.url("walk.mp4")), tmp_path)
    assert _leftovers(tmp_path) == []


def test_file_urls_work(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "walk.mp4").write_bytes(VIDEO)
    samples = tmp_path / "samples"
    samples.mkdir()
    assert fetch(_sample((origin / "walk.mp4").as_uri()), samples) is Outcome.FETCHED
    assert (samples / "walk.mp4").read_bytes() == VIDEO


# --- The command ------------------------------------------------------------------------------


def _write_manifest(path: Path, samples: list[Sample], **changes: object) -> None:
    """Write the samples as a manifest, with `changes` made to every one of them."""
    lines = ["# For the tests.", ""]
    for sample in samples:
        lines += ["[[sample]]"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in (vars(sample) | changes).items()]
        lines += [""]
    path.write_text("\n".join(lines))


def test_the_test_manifest_writer_round_trips(tmp_path: Path) -> None:
    samples = [_sample("https://example.org/a.mp4")]
    _write_manifest(tmp_path / "manifest.toml", samples)
    assert load_manifest(tmp_path / "manifest.toml") == samples


def test_the_command_fetches_every_sample_and_then_skips_them(
    server: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = VIDEO[::-1]
    server.files |= {"a.mp4": VIDEO, "b.mp4": other}
    manifest = tmp_path / "manifest.toml"
    _write_manifest(
        manifest,
        [_sample(server.url("a.mp4"), file="a.mp4"), _sample(server.url("b.mp4"), other, "b.mp4")],
    )
    arguments = ["--manifest", str(manifest), "--directory", str(tmp_path)]
    assert main(arguments) == 0
    assert sorted(server.requests) == ["a.mp4", "b.mp4"]
    first = capsys.readouterr().out
    assert "a.mp4" in first
    assert "CC-BY-4.0" in first

    assert main(arguments) == 0
    assert sorted(server.requests) == ["a.mp4", "b.mp4"]
    assert "already present" in capsys.readouterr().out


def test_the_command_fetches_the_rest_and_fails_when_one_sample_fails(
    server: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server.files |= {"bad.mp4": b"wrong", "good.mp4": VIDEO}
    manifest = tmp_path / "manifest.toml"
    _write_manifest(
        manifest,
        [
            _sample(server.url("bad.mp4"), file="bad.mp4"),
            _sample(server.url("good.mp4"), file="good.mp4"),
        ],
    )
    assert main(["--manifest", str(manifest), "--directory", str(tmp_path)]) == 1
    assert (tmp_path / "good.mp4").read_bytes() == VIDEO
    assert not (tmp_path / "bad.mp4").exists()
    assert "bad.mp4" in capsys.readouterr().err


def test_a_malformed_manifest_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "manifest.toml"
    manifest.write_text('[[sample]]\nfile = "a.mp4"\n')
    assert main(["--manifest", str(manifest), "--directory", str(tmp_path)]) == 2
    assert "manifest" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("file", "../escape.mp4"),
        ("file", "nested/sample.mp4"),
        ("sha256", "abc"),
        ("size", 0),
        ("size", "big"),
        ("url", 7),
        ("url", ""),
    ],
)
def test_a_sample_that_could_write_outside_the_directory_or_never_verify_is_refused(
    tmp_path: Path, key: str, value: str | int
) -> None:
    manifest = tmp_path / "manifest.toml"
    _write_manifest(manifest, [_sample("https://example.org/a.mp4")], **{key: value})
    with pytest.raises(ValueError, match=key):
        load_manifest(manifest)


def test_the_real_manifest_parses_as_toml() -> None:
    assert tomllib.loads(MANIFEST.read_text())["sample"]


def test_the_samples_are_downloaded_in_parallel(server: Server, tmp_path: Path) -> None:
    # A single download from the dataset's bucket can be slow; several at once are faster.
    others = [VIDEO[::-1], VIDEO[1:]]
    server.files |= {"a.mp4": VIDEO, "b.mp4": others[0], "c.mp4": others[1]}
    server.together = threading.Barrier(3)
    manifest = tmp_path / "manifest.toml"
    _write_manifest(
        manifest,
        [
            _sample(server.url("a.mp4"), file="a.mp4"),
            _sample(server.url("b.mp4"), others[0], "b.mp4"),
            _sample(server.url("c.mp4"), others[1], "c.mp4"),
        ],
    )
    assert main(["--manifest", str(manifest), "--directory", str(tmp_path)]) == 0
