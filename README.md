# VisionQuery

VisionQuery watches every camera on a site and remembers every person it sees, so you can ask it about the past in plain words: "where did the man in the red jacket go after gate 2 yesterday afternoon?"
It finds the people who match, lets you pick the right one, follows them across cameras, and answers with clips and stills you can open and check, telling you plainly where it could not see.

It is a closed system: one installation per owner, running on the owner's own hardware, and no video, image or embedding ever leaves it.

## How it works

1. **Cameras** are added by URL (or publish into the system) through MediaMTX.
2. **Footage** from every camera is recorded continuously in fixed segments.
3. **Perception workers** run person detection, tracking and appearance embedding on the GPU through Triton, producing a **Track** for every person each camera sees, with a few **Keyframes** showing how they looked.
4. A **linker** connects Tracks across cameras into **Visits**, but only when it is confident, using appearance and how long it takes to walk between cameras.
5. When you ask about someone, you confirm the right person (an **Anchor**), and **Following** searches outward across cameras for the rest of their Visit.
   You confirm or reject each step, and the system never overrides what you decided.
6. A local language model, the **Reasoner**, turns your Question into searches through a fixed set of tools and writes an **Answer** in which every statement links to its **Evidence**, along with any **Gaps** (cameras offline, Footage expired, places no camera covers).

Everything lives in one PostgreSQL database (TimescaleDB for time partitioning, pgvector for appearance search) plus the Footage on disk.

## Where to read more

| Document | What it is |
|---|---|
| [`CONTEXT.md`](CONTEXT.md) | The glossary: the exact meaning of every term above. Read this first. |
| [`docs/adr/`](docs/adr/) | Why the system is built the way it is, one decision per file. |
| [`docs/plan/v1.md`](docs/plan/v1.md) | What v1 is, and the milestones to get there. |
| [GitHub issues](../../issues) | Progress: one issue per milestone, with its tasks as sub-issues. |

## Setup

The project is being scaffolded (milestone M0): the stack runs, with fake cameras for development, but nothing watches them yet.

You need Docker Engine 25 or newer with the Compose plugin v2.30 or newer (`docker compose wait`; the Dockerfile frontend that `COPY --parents` needs is pinned in the Dockerfile and fetched by BuildKit) and, to work on the code, [uv](https://docs.astral.sh/uv/) and Node.js 24 with npm (`web/.nvmrc`; `make sync` installs the Python packages and the web app's from their lockfiles).
A GPU is needed only for Triton (the `gpu` profile), and then with NVIDIA's container toolkit.

1. Configure the stack.
   Copy `.env.example` to `.env` (git ignores it) and set the three passwords it leaves empty, for example each to the output of `openssl rand -hex 24`.
   Every other variable has a working default, and `.env.example` explains each.
2. Start it: `make up`.
   It builds the images (rebuilding only what changed, so edited code is what runs; a bare `docker compose up` builds only an image that is absent and never rebuilds), then starts Postgres and runs the database migrations and waits for them to succeed (`MIGRATE_TIMEOUT`, 300 s), and only then starts the long-running services and waits for their healthchecks (`WAIT_TIMEOUT`, 300 s).
   `docker compose up --wait` alone would return while a migration is still running, so a failed or stuck migration stops `make up` with an error and the migration's log.
   It starts Postgres (TimescaleDB and pgvector), MediaMTX and the VisionQuery api, and returns once every service is healthy.
   Postgres listens on `127.0.0.1:15432` (user and database `visionquery`), MediaMTX on `127.0.0.1:8554` (RTSP) and `127.0.0.1:9997` (API, user `visionquery`), and the api on `127.0.0.1:8080`, where <http://127.0.0.1:8080/health> says whether it can reach the database.
3. Add what you need with profiles:
   - `make up COMPOSE_PROFILES=observability` (or `--profile observability` with `docker compose`): Prometheus on <http://127.0.0.1:9090> scraping every service, and Grafana on <http://127.0.0.1:3000> (user `admin`) reading it.
   - `COMPOSE_PROFILES=gpu` (or `--profile gpu`): Triton on `127.0.0.1:8000` (HTTP) and `8001` (gRPC), on every GPU.
     On a machine without one, run it on the CPU with `make up COMPOSE_FILE=compose.yaml:compose.no-gpu.yaml COMPOSE_PROFILES=gpu`.
   - `COMPOSE_PROFILES=dev` (or `--profile dev`): fake cameras, each looping a video into MediaMTX over RTSP as a real camera would publish it, so that everything can be developed without one.
     See "Fake cameras" below.
4. To see the web app, run `npm --prefix web run dev` and open the address it prints: a page showing the Deployment's health, read from the api.
5. Stop it with `make down`, which stops every service whatever its profile (Triton included) and keeps the data; `make down VOLUMES=1` deletes the database and metrics as well.

### Fake cameras

The `dev` profile's `fake-cameras` service publishes one MediaMTX path per camera that `VISIONQUERY_FAKE_CAMERAS` in `.env` lists, as `name=video` pairs.
By default these are three cameras of the [MEVA](https://mevadata.org/) dataset's school ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/legalcode)), recording the same five minutes: two views of a busy gym from opposite sides, and a stairwell by an entrance.

1. Download the videos once: `make fetch-samples` (about 400 MB, into `samples/`, which git ignores).
   Each is checked against the SHA-256 in [`samples/manifest.toml`](samples/manifest.toml), which also gives its license and attribution, and one already present is skipped.
2. Start the stack with the profile: `make up COMPOSE_PROFILES=dev`.
   Each video is first re-encoded once as a camera sends video (H.264 without B-frames, a keyframe a second), which takes about a minute per video and is kept in the `fake-cameras` volume; the camera shows a test pattern meanwhile.
   A camera whose video is not in `samples/` shows the test pattern instead, with a warning in `docker compose logs fake-cameras`, so the profile works before anything is downloaded; restart the service (`docker compose restart fake-cameras`) once the video is there.
3. Watch a camera: `ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/gym-g299` (or the same URL in VLC), or open <http://127.0.0.1:8889/gym-g299> in a browser for MediaMTX's WebRTC player.

All the cameras play their videos in step.
Each video is cut to the same five minutes (`VISIONQUERY_FAKE_CAMERA_DURATION`, in seconds; MEVA's recordings are 300.0 to 300.3 s long and would otherwise drift apart), and every camera shows the second of its video that the wall clock says, counted from the Unix epoch.
A camera that starts late, is still being prepared, or restarts rejoins that timeline at once, so the cameras that recorded the same minutes show the same people at the same moment, to within a fraction of a second, however long the stack has been up.
The five minutes then repeat for ever, beginning at every five minutes of the hour (UTC): the same people walk by again every five minutes, so expect the same Visits, Tracks and Links to recur when developing perception and the linker against these cameras.
(The test pattern, and a camera on it while its video is prepared, are not part of the timeline.)

Any number of cameras can be listed, several may loop the same video, `synthetic` is a test pattern that needs no video, and any video file in `VISIONQUERY_FAKE_CAMERA_SAMPLES` (`samples/` by default) can be used.
Each camera reconnects by itself whenever MediaMTX restarts.

The samples are changed from the originals: each is re-encoded for streaming (H.264 without B-frames, a keyframe a second, no audio), as CC BY 4.0 asks to be said.
A camera that starts on the test pattern switches to its prepared sample once the sample is ready, which disconnects a viewer once: reconnect it (play the URL again, or reload the page) to see the sample.
A video that exists but that the container cannot read (as after `umask 077`) is reported as such in `docker compose logs fake-cameras`; `make fetch-samples` makes the directory and the samples world-readable, or run `chmod -R a+rX samples`.

Every port listens on loopback only unless `VISIONQUERY_BIND_ADDRESS` (or, for the camera-facing ports of MediaMTX alone, `VISIONQUERY_MEDIAMTX_BIND_ADDRESS`) says otherwise, and each can be moved with its own variable when something on the machine already uses it.

Every VisionQuery process is configured only through environment variables prefixed `VISIONQUERY_`, logs JSON lines to stdout, and serves Prometheus metrics on `/metrics` (port `9100` unless `VISIONQUERY_METRICS_PORT` says otherwise).
The variables every process reads are listed in [`CLAUDE.md`](CLAUDE.md#architecture); a process that is missing one it needs exits with status 2 after logging which.

The database schema is applied with `visionquery-migrate`, which reads `VISIONQUERY_DATABASE__URL` (a `postgresql://` URL to a Postgres with TimescaleDB and pgvector) and is safe to run on every start: it applies only what is new, and refuses, naming the file, when the migrations and the database disagree.

The memory's and the api's tests run against a real Postgres: they need Docker (each session starts and removes its own container) or `VISIONQUERY_TEST_DATABASE_URL` pointing at a superuser's `postgresql://` URL on a server with TimescaleDB and pgvector.
Without either they are skipped, saying why (in CI, where `CI` is set, a test skipped for a missing prerequisite fails instead).

## Contributing

Work is test-driven and tracked in GitHub issues.
Branches follow git-flow (`main`, `develop`, `feature/<issue>-<slug>`, `release/vX.Y.Z`), commits follow Conventional Commits, and every PR into `develop` or `main` must pass CI and review.
The details are in [`CLAUDE.md`](CLAUDE.md), which serves as the working guide for both people and coding agents.

`make check` runs locally what CI runs on every PR, except the stack tests (`make test-stack`).
CI (`.github/workflows/ci.yml`) runs lint (ruff, ruff format, actionlint), the type check, the import contracts, the Python tests against Postgres, the web app's lint, type check and tests with its API types check, the Compose stack tests, and a check that no commit, PR title or PR body carries agent attribution and that the PR title is a Conventional Commit.
`develop` and `main` accept changes only through a PR on which every one of those checks passed.

To have git run the fast checks for you, opt in to the pre-commit hooks:

```sh
uv run pre-commit install
```

It installs a `pre-commit` hook (ruff and ruff format on the staged files) and a `commit-msg` hook (the commit message is a Conventional Commit and carries no agent attribution) into `.git/hooks`, taking the place of any hook of those names already there.
pre-commit keeps such a hook as `<name>.legacy` and still runs it before its own checks, so a stricter hook of your own (one that refuses the `ci`, `build` or `perf` types, say) keeps refusing; `uv run pre-commit install --overwrite` deletes it instead.
`uv run pre-commit uninstall` removes pre-commit's hooks and puts back any it set aside.
