# One image per Deployment, run with Compose and `make up`

Every VisionQuery process (migrate, recorder, Perception Workers, linker, api, Reasoner) runs from one image per Deployment, built from the root `Dockerfile` and differing only in its command and environment.
Third-party services keep their own images (MediaMTX, for instance, has its own patched one).
The image is built once, by `migrate` alone, and is named for the Compose project (`${COMPOSE_PROJECT_NAME}-app:dev`), because image tags are global to the Docker daemon and two projects sharing a tag would keep recreating each other's containers.
Perception may get a CUDA build target of its own in milestone 2, in the same `Dockerfile`, when NVDEC and Triton's CUDA shared memory need a base the other processes do not.

A Deployment runs with Docker Compose on one box (ADR 0005), and is started with `make up`.
`make up` builds with BuildKit's attestations off, so an unchanged tree gives the same image ID and no running container is recreated; then runs `migrate` and waits for it, failing loudly when it did not exit 0; then starts the long-running services and waits for their healthchecks.
Every other service that runs the VisionQuery image declares `depends_on: { migrate: { condition: service_completed_successfully } }`, so no process ever runs against a schema older than its code (ADR 0011).

## Considered Options

An image per service was rejected: the processes share the workspace's packages and dependencies, so separate images would multiply build time and disk for no isolation the processes need, and would let their versions drift apart.
Plain `docker compose up` was rejected as the way to start a Deployment: it builds only an absent image, so changed code would not run, and `--wait` returns while `migrate` is still running, so a failed migration would go unnoticed.
Kubernetes and other orchestrators were rejected: a Deployment is one box (ADR 0005), and an orchestrator would add a control plane to operate without anything to schedule across.

## Consequences

Changing a dependency of one process rebuilds the image every process runs from.
`docker compose up` still works on a stack that is already built, but `make up` is the only supported way to start or update a Deployment.
