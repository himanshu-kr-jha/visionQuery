"""Download the sample videos the fake cameras loop (`make fetch-samples`).

The clips are listed in `samples/manifest.toml`, each with its URL, SHA-256, size, license
and attribution. Each is downloaded into `samples/` (which git ignores, except the manifest)
through a `.part` file that is verified before it takes the clip's name, so a clip on disk is
always the one the manifest describes. A clip already present and verified is skipped; one
that does not verify is downloaded again. Uses the standard library only.

    uv run scripts/fetch_samples.py [--manifest PATH] [--directory PATH]

Exits 0 when every clip is present, 1 when one could not be fetched (the others still are),
and 2 when the manifest is malformed. The clips are downloaded in parallel.
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

#: Clips downloaded at once.
PARALLEL = 4
_CHUNK = 1 << 20
_FILE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Clip:
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
    #: Where the clip is described, for a person checking its license.
    source: str


class Outcome(enum.Enum):
    PRESENT = "already present"
    FETCHED = "fetched"


class FetchError(Exception):
    """A clip could not be downloaded, or what was downloaded is not the clip."""


def load_manifest(path: Path) -> list[Clip]:
    """The clips the manifest lists; ValueError, naming the field, when one is malformed."""
    names = [f.name for f in fields(Clip)]
    entries: object = tomllib.loads(path.read_text()).get("clip", [])
    if not isinstance(entries, list):
        raise ValueError("clip must be an array of tables ([[clip]])")
    clips: list[Clip] = []
    for index, entry in enumerate(cast(list[object], entries), start=1):
        if not isinstance(entry, dict) or set(cast(dict[str, object], entry)) != set(names):
            raise ValueError(f"clip {index} must have exactly the fields {names}")
        values = cast(dict[str, object], entry)
        text = {n: v for n in names if n != "size" and isinstance(v := values[n], str) and v}
        if missing := [n for n in names if n != "size" and n not in text]:
            raise ValueError(f"clip {index}: {', '.join(missing)} must be text")
        size = values["size"]
        if not isinstance(size, int) or size <= 0:
            raise ValueError(f"clip {index}: size must be a positive number of bytes")
        clip = Clip(**text, size=size)
        if not _FILE.fullmatch(clip.file):
            raise ValueError(f"clip {index}: file must be a plain lower-case file name")
        if not _SHA256.fullmatch(clip.sha256):
            raise ValueError(f"{clip.file}: sha256 must be 64 lower-case hex digits")
        clips.append(clip)
    return clips


def _digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


class _CancelledError(Exception):
    pass


def _copy(response: BinaryIO, into: BinaryIO, cancelled: threading.Event) -> str:
    """Copy the response, returning the SHA-256 of what was copied."""
    digest = hashlib.sha256()
    while not cancelled.is_set() and (chunk := response.read(_CHUNK)):
        digest.update(chunk)
        into.write(chunk)
    if cancelled.is_set():
        raise _CancelledError
    return digest.hexdigest()


def fetch(clip: Clip, directory: Path, cancelled: threading.Event | None = None) -> Outcome:
    """Make `directory/clip.file` the clip the manifest describes, downloading it if needed.
    Setting `cancelled` stops the download, which then fails."""
    target = directory / clip.file
    if target.is_file() and _digest(target) == clip.sha256:
        return Outcome.PRESENT
    part = target.with_name(f"{target.name}.part")
    try:
        with urllib.request.urlopen(clip.url, timeout=60) as response, part.open("wb") as file:
            digest = _copy(response, file, cancelled or threading.Event())
        if digest != clip.sha256:
            raise FetchError(
                f"{clip.file}: the download's SHA-256 is {digest}, the manifest says "
                f"{clip.sha256} ({part.stat().st_size} of {clip.size} bytes arrived)"
            )
    except _CancelledError:
        part.unlink(missing_ok=True)
        raise FetchError(f"{clip.file}: cancelled") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        part.unlink(missing_ok=True)
        raise FetchError(f"{clip.file}: could not download {clip.url}: {error}") from error
    except BaseException:
        # A mismatch, or an interrupted run: never leave a partial file behind.
        part.unlink(missing_ok=True)
        raise
    # Readable by the fake cameras, which run as a user of their own.
    part.chmod(0o644)
    part.replace(target)
    return Outcome.FETCHED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--directory", type=Path, default=SAMPLES)
    arguments = parser.parse_args(argv)
    try:
        clips = load_manifest(arguments.manifest)
    except (ValueError, TypeError, tomllib.TOMLDecodeError) as error:
        print(f"fetch-samples: {arguments.manifest} is malformed: {error}", file=sys.stderr)
        return 2
    total = sum(clip.size for clip in clips)
    print(
        f"Fetching {len(clips)} clips ({total / 1e6:.0f} MB) into {arguments.directory}", flush=True
    )
    failed = 0
    # In parallel: one download from a dataset's bucket can be slow, several are faster.
    cancelled = threading.Event()
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        downloads = {
            pool.submit(fetch, clip, arguments.directory, cancelled): clip for clip in clips
        }
        try:
            for download in as_completed(downloads):
                clip = downloads[download]
                try:
                    outcome = download.result()
                except FetchError as error:
                    print(f"fetch-samples: {error}", file=sys.stderr, flush=True)
                    failed += 1
                    continue
                print(
                    f"{clip.file} ({clip.size / 1e6:.0f} MB, {clip.license}): {outcome.value}",
                    flush=True,
                )
        except KeyboardInterrupt:
            # Every download stops and removes its partial file before the pool is left.
            cancelled.set()
            raise
    if failed:
        print(f"fetch-samples: {failed} of {len(clips)} clips failed", file=sys.stderr)
        return 1
    print(f"Every clip is in {arguments.directory.resolve()}; the manifest credits each.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("fetch-samples: interrupted; no partial file is left", file=sys.stderr)
        sys.exit(130)
