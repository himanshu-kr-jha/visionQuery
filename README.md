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

Not available yet: the project is being scaffolded (milestone M0).
This section will hold the complete steps to go from a fresh clone to a running stack with sample cameras.

Every VisionQuery process is configured only through environment variables prefixed `VISIONQUERY_`, logs JSON lines to stdout, and serves Prometheus metrics on `/metrics` (port `9100` unless `VISIONQUERY_METRICS_PORT` says otherwise).
The variables every process reads are listed in [`CLAUDE.md`](CLAUDE.md#architecture); a process that is missing one it needs exits with status 2 after logging which.

The database schema is applied with `visionquery-migrate`, which reads `VISIONQUERY_DATABASE__URL` (a `postgresql://` URL to a Postgres with TimescaleDB and pgvector) and is safe to run on every start: it applies only what is new, and refuses, naming the file, when the migrations and the database disagree.

## Contributing

Work is test-driven and tracked in GitHub issues.
Branches follow git-flow (`main`, `develop`, `feature/<issue>-<slug>`, `release/vX.Y.Z`), commits follow Conventional Commits, and every PR into `develop` must pass CI and review.
The details are in [`CLAUDE.md`](CLAUDE.md), which serves as the working guide for both people and coding agents.
