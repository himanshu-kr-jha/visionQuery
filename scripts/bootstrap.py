"""Bootstrap a development environment from a fresh clone (`make bootstrap`).

1. Checks every prerequisite the README lists, and names each one that is missing or too old,
   with how to get it, before changing anything: Docker Engine with a reachable daemon, the
   Compose and buildx plugins, and Node.js at the major `web/.nvmrc` pins, with npm.
   uv is checked by the Makefile, which needs it to run this.
2. Writes `.env` from `.env.example`, with a generated password for every value the example
   leaves empty (those are the secrets), readable by its owner alone. An `.env` that exists is
   never changed: it is only checked to set every secret, and each one it leaves empty or lacks
   is named.
3. Installs the workspace and the web app from their lockfiles (`make sync`).

    uv run --no-project scripts/bootstrap.py [--root PATH]

Run without the project, so that nothing is synced, or the lockfile changed, before step 3;
it needs nothing beyond the standard library. Exits 0 when the environment is ready, and 1 when
a prerequisite is missing, `.env` lacks a secret, or `make sync` failed.
"""

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: `docker compose wait`, which `make up` uses.
MINIMUM_COMPOSE = (2, 30)
#: Its BuildKit fetches the Dockerfile frontend that `COPY --parents` needs.
MINIMUM_DOCKER = (25, 0)
#: Compose refuses to build with an older one.
MINIMUM_BUILDX = (0, 17)

DOCKER_INSTALL = "https://docs.docker.com/engine/install/"
COMPOSE_INSTALL = "https://docs.docker.com/compose/install/linux/"
BUILDX_INSTALL = "https://docs.docker.com/build/concepts/overview/#install-buildx"
NODE_INSTALL = "https://nodejs.org/en/download (or `nvm install` in web/)"

_VERSION = re.compile(r"(?<![\w.])v?(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, ...] | None:
    """The first version in `text` as numbers, as a tool prints it (`v2.30.0`, `25.0.3+dfsg1`)."""
    match = _VERSION.search(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def _shown(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version))


def _output(*command: str) -> str | None:
    """What the command printed, or None when it failed."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    except OSError, subprocess.TimeoutExpired:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _too_old(name: str, text: str | None, minimum: tuple[int, ...], how: str) -> str | None:
    """Why the version `text` does not do, or None when it does."""
    version = parse_version(text or "")
    if version is not None and version >= minimum:
        return None
    found = "its version could not be read" if version is None else f"this is {_shown(version)}"
    return f"{name} {_shown(minimum)} or newer is needed, and {found}: {how}"


def _docker_problems() -> list[str]:
    if shutil.which("docker") is None:
        return [
            f"Docker is not installed: install Docker Engine {_shown(MINIMUM_DOCKER)} or newer "
            f"with the Compose and buildx plugins ({DOCKER_INSTALL})"
        ]
    problems: list[str | None] = []
    server = _output("docker", "version", "--format", "{{.Server.Version}}")
    if server is None:
        problems.append(
            "the Docker daemon cannot be reached: start it, and let your user use it without "
            "sudo (the `docker` group); `docker version` says what is wrong"
        )
    else:
        problems.append(_too_old("Docker Engine", server, MINIMUM_DOCKER, DOCKER_INSTALL))
    compose = _output("docker", "compose", "version", "--short")
    if compose is None:
        problems.append(f"the Docker Compose plugin is not installed ({COMPOSE_INSTALL})")
    else:
        problems.append(_too_old("Docker Compose", compose, MINIMUM_COMPOSE, COMPOSE_INSTALL))
    buildx = _output("docker", "buildx", "version")
    if buildx is None:
        problems.append(f"the Docker buildx plugin is not installed ({BUILDX_INSTALL})")
    else:
        problems.append(_too_old("Docker buildx", buildx, MINIMUM_BUILDX, BUILDX_INSTALL))
    return [problem for problem in problems if problem is not None]


def _node_problems(root: Path) -> list[str]:
    major = int((root / "web" / ".nvmrc").read_text().strip())
    wanted = f"Node.js {major} (the major web/.nvmrc pins; npm refuses any other)"
    problems: list[str] = []
    node = _output("node", "--version") if shutil.which("node") else None
    version = parse_version(node or "")
    if node is None or version is None:
        problems.append(f"{wanted} is not installed: {NODE_INSTALL}")
    elif version[0] != major:
        problems.append(f"{wanted} is needed, and this is {_shown(version)}: {NODE_INSTALL}")
    if shutil.which("npm") is None:
        problems.append(f"npm is not installed: it comes with {wanted}")
    return problems


def prerequisite_problems(root: Path) -> list[str]:
    """Every prerequisite that is missing or too old, each with how to get it."""
    return _docker_problems() + _node_problems(root)


def _variables(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )


def _secret_names(example: str) -> list[str]:
    return [name for name, value in _variables(example).items() if value == ""]


def write_env(root: Path) -> tuple[list[str], list[str]]:
    """Create `.env` from `.env.example` unless it exists: the secrets generated, and the
    secrets an existing `.env` leaves empty or lacks."""
    example = (root / ".env.example").read_text()
    path = root / ".env"
    if path.exists():
        existing = _variables(path.read_text())
        return [], [name for name in _secret_names(example) if not existing.get(name)]
    generated: list[str] = []
    lines: list[str] = []
    for line in example.splitlines(keepends=True):
        name, separator, value = line.rstrip("\n").partition("=")
        if separator and not line.lstrip().startswith("#") and value == "":
            line = f"{name}={secrets.token_hex(24)}\n"
            generated.append(name)
        lines.append(line)
    # Created, never replaced, and private from the first byte.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.writelines(lines)
    return generated, []


def bootstrap(root: Path) -> int:
    if problems := prerequisite_problems(root):
        print("make bootstrap: missing prerequisites (see Setup in README.md):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Prerequisites: Docker, Compose, buildx, Node.js and npm are all there.")

    generated, unset = write_env(root)
    if unset:
        print(
            "make bootstrap: .env exists (and is kept as it is) but does not set "
            f"{', '.join(unset)}, which Compose needs: set each, for example to the output of "
            "`openssl rand -hex 24` (see .env.example)",
            file=sys.stderr,
        )
        return 1
    if generated:
        print(f".env: created from .env.example, with {', '.join(generated)} generated.")
    else:
        print(".env: exists, kept as it is.")

    sys.stdout.flush()  # Before make's own output, which goes straight to the terminal.
    if subprocess.run(["make", "sync"], cwd=root, check=False).returncode != 0:
        print("make bootstrap: `make sync` failed, as it says above.", file=sys.stderr)
        return 1
    print(
        "Ready: start the stack with `make up` (`make up COMPOSE_PROFILES=dev` for fake cameras)."
    )
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap a development environment.")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository (default: this)")
    return bootstrap(parser.parse_args(arguments).root)


if __name__ == "__main__":
    sys.exit(main())
