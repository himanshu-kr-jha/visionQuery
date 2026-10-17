# syntax=docker/dockerfile:1.20@sha256:26147acbda4f14c5add9946e2fd2ed543fc402884fd75146bd342a7f6271dc1d
# The image every VisionQuery process runs from.
#
# One image holds the whole workspace, installed from uv.lock without dev dependencies, and
# each Compose service chooses its process by its command (`visionquery-migrate` today; the
# recorder, linker, api and the rest as they arrive). One image means one build, one set of
# layers on disk and one version string for a Deployment, and the packages are small next to
# their dependencies, which they share. The exception will be perception: when it needs the
# CUDA and NVDEC libraries (M2) it gets a target of its own on a CUDA base, so that no other
# process carries gigabytes it never loads.
#
#   make up                          (or: docker compose build)
#
# The syntax line above pins the Dockerfile frontend: `COPY --parents` needs 1.20 or newer.

FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv

FROM debian:trixie-20261005-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS build
COPY --from=uv /uv /usr/local/bin/uv
# Python is uv's own build, as on a developer's machine (`python-preference = "only-managed"`),
# installed where the runtime stage copies it from; the environment is installed beside it.
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/visionquery \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1
WORKDIR /src

# Dependencies first, from the lockfile and the packages' metadata only, so that changing
# source code does not reinstall them.
COPY pyproject.toml uv.lock .python-version ./
COPY --parents packages/*/pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

# Then the workspace's own packages, installed as wheels (not editable), so the runtime stage
# needs nothing from /src.
COPY packages packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM debian:trixie-20261005-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS runtime
# ca-certificates for TLS (the remote Reasoner, ADR 0002, is reached over it) and tzdata for
# zone names; nothing else is added to a base that has no shell tools worth keeping.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 visionquery \
    && useradd --system --uid 10001 --gid visionquery --no-create-home \
        --home-dir /tmp --shell /usr/sbin/nologin visionquery
COPY --from=build /opt/python /opt/python
COPY --from=build /opt/visionquery /opt/visionquery
ENV PATH=/opt/visionquery/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER visionquery:visionquery
WORKDIR /
# Every process serves its metrics here (ADR 0012).
EXPOSE 9100
CMD ["python", "-c", "raise SystemExit('name a VisionQuery command, such as visionquery-migrate')"]
