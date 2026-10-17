"""Download the sample videos the fake cameras loop (`make fetch-samples`).

The samples are listed in `samples/manifest.toml`, each with its URL, SHA-256, size, license
and attribution. Each is downloaded into `samples/` (which git ignores, except the manifest)
through a `.part` file that is verified before it takes the sample's name, so a sample on disk is
always the one the manifest describes. A sample already present and verified is skipped; one
that does not verify is downloaded again. Uses the standard library only.

    uv run scripts/fetch_samples.py [--manifest PATH] [--directory PATH]

Exits 0 when every sample is present, 1 when one could not be fetched (the others still are),
and 2 when the manifest is malformed. The samples are downloaded in parallel.
"""

import argparse
import enum
import hashlib
import http.client
import re
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields
from pathlib import Path
from typing import BinaryIO, cast

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
MANIFEST = SAMPLES / "manifest.toml"

#: Samples downloaded at once.
PARALLEL = 4
_CHUNK = 1 << 20
_FILE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Sample:
    """One sample video, as the manifest describes it."""

    #: The name it is saved under in the samples directory, and that cameras refer to it by.
    file: str
    url: str
    sha256: str
    #: In bytes.
    size: int
    #: An SPDX identifier.
    license: str
    license_url: str
    attribution: str
    #: Where the sample is described, for a person checking its license.
    source: str


class Outcome(enum.Enum):
    PRESENT = "already present"
    FETCHED = "fetched"


class FetchError(Exception):
    """A sample could not be downloaded, or what was downloaded is not the sample."""


def load_manifest(path: Path) -> list[Sample]:
    """The samples the manifest lists; ValueError, naming the field, when one is malformed."""
    names = [f.name for f in fields(Sample)]
    entries: object = tomllib.loads(path.read_text()).get("sample", [])
    if not isinstance(entries, list):
        raise ValueError("sample must be an array of tables ([[sample]])")
    samples: list[Sample] = []
    for index, entry in enumerate(cast(list[object], entries), start=1):
        if not isinstance(entry, dict) or set(cast(dict[str, object], entry)) != set(names):
            raise ValueError(f"sample {index} must have exactly the fields {names}")
        values = cast(dict[str, object], entry)
        text = {n: v for n in names if n != "size" and isinstance(v := values[n], str) and v}
        if missing := [n for n in names if n != "size" and n not in text]:
            raise ValueError(f"sample {index}: {', '.join(missing)} must be text")
        size = values["size"]
        if not isinstance(size, int) or size <= 0:
            raise ValueError(f"sample {index}: size must be a positive number of bytes")
        sample = Sample(**text, size=size)
        if not _FILE.fullmatch(sample.file):
            raise ValueError(f"sample {index}: file must be a plain lower-case file name")
        if not _SHA256.fullmatch(sample.sha256):
            raise ValueError(f"{sample.file}: sha256 must be 64 lower-case hex digits")
        samples.append(sample)
    return samples


def _digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


class _CancelledError(Exception):
    pass


class _TooLargeError(Exception):
    pass


def _copy(response: BinaryIO, into: BinaryIO, cancelled: threading.Event, limit: int) -> str:
    """Copy the response, returning the SHA-256 of what was copied; stop, rather than fill the
    disk, once more than `limit` bytes have arrived."""
    digest = hashlib.sha256()
    copied = 0
    while not cancelled.is_set() and (chunk := response.read(_CHUNK)):
        copied += len(chunk)
        if copied > limit:
            raise _TooLargeError
        digest.update(chunk)
        into.write(chunk)
    if cancelled.is_set():
        raise _CancelledError
    return digest.hexdigest()


def fetch(sample: Sample, directory: Path, cancelled: threading.Event | None = None) -> Outcome:
    """Make `directory/sample.file` the sample the manifest describes, downloading it if needed.
    Setting `cancelled` stops the download, which then fails."""
    target = directory / sample.file
    try:
        # The fake cameras run as a user of their own, and read the directory (and, below, each
        # sample) through a bind mount: a umask of 077 would otherwise lock them out.
        directory.chmod(directory.stat().st_mode | 0o005)
        if target.is_file() and _digest(target) == sample.sha256:
            target.chmod(target.stat().st_mode | 0o004)
            return Outcome.PRESENT
    except OSError as error:
        raise FetchError(
            f"{sample.file}: cannot use {directory} (is it yours? `chmod a+rX` it, or fix its "
            f"owner): {error}"
        ) from error
    part = target.with_name(f"{target.name}.part")
    try:
        with urllib.request.urlopen(sample.url, timeout=60) as response, part.open("wb") as file:
            digest = _copy(response, file, cancelled or threading.Event(), sample.size)
        if digest != sample.sha256:
            raise FetchError(
                f"{sample.file}: the download's SHA-256 is {digest}, the manifest says "
                f"{sample.sha256} ({part.stat().st_size} of {sample.size} bytes arrived)"
            )
    except _CancelledError:
        part.unlink(missing_ok=True)
        raise FetchError(f"{sample.file}: cancelled") from None
    except _TooLargeError:
        part.unlink(missing_ok=True)
        raise FetchError(
            f"{sample.file}: more than the {sample.size} bytes the manifest says arrived, so "
            "it is not the sample the manifest describes"
        ) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        part.unlink(missing_ok=True)
        raise FetchError(f"{sample.file}: could not download {sample.url}: {error}") from error
    except BaseException:
        # A mismatch, or an interrupted run: never leave a partial file behind.
        part.unlink(missing_ok=True)
        raise
    part.chmod(0o644)
    part.replace(target)
    return Outcome.FETCHED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--directory", type=Path, default=SAMPLES)
    arguments = parser.parse_args(argv)
    try:
        samples = load_manifest(arguments.manifest)
    except (ValueError, TypeError, tomllib.TOMLDecodeError) as error:
        print(f"fetch-samples: {arguments.manifest} is malformed: {error}", file=sys.stderr)
        return 2
    try:
        arguments.directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"fetch-samples: cannot create {arguments.directory}: {error}", file=sys.stderr)
        return 1
    total = sum(sample.size for sample in samples)
    print(
        f"Fetching {len(samples)} samples ({total / 1e6:.0f} MB) into {arguments.directory}",
        flush=True,
    )
    failed = 0
    # In parallel: one download from a dataset's bucket can be slow, several are faster.
    cancelled = threading.Event()
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        downloads = {
            pool.submit(fetch, sample, arguments.directory, cancelled): sample for sample in samples
        }
        try:
            for download in as_completed(downloads):
                sample = downloads[download]
                try:
                    outcome = download.result()
                except FetchError as error:
                    print(f"fetch-samples: {error}", file=sys.stderr, flush=True)
                    failed += 1
                    continue
                size, license_ = f"{sample.size / 1e6:.0f} MB", sample.license
                print(f"{sample.file} ({size}, {license_}): {outcome.value}", flush=True)
        except KeyboardInterrupt:
            # Every download stops and removes its partial file before the pool is left.
            cancelled.set()
            raise
    if failed:
        print(f"fetch-samples: {failed} of {len(samples)} samples failed", file=sys.stderr)
        return 1
    print(f"Every sample is in {arguments.directory.resolve()}; the manifest credits each.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("fetch-samples: interrupted; no partial file is left", file=sys.stderr)
        sys.exit(130)
