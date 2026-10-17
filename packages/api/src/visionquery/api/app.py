"""The HTTP api a Deployment's web app and Operators talk to."""

from importlib.metadata import version
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Response, status
from pydantic import BaseModel
from visionquery.memory.connection import ConnectionProblem
from visionquery.memory.health import DatabaseHealth

__all__ = ["DatabaseStatus", "Health", "create_app", "database_health", "operator_router"]


class DatabaseStatus(BaseModel):
    """Whether the api can reach the Deployment's database, and if not, why."""

    status: Literal["connected", "unreachable"]
    #: Why it is unreachable, as a fixed category; never the URL or any part of it.
    problem: ConnectionProblem | None


class Health(BaseModel):
    """Whether the api is ready to serve: ready only when everything it depends on is reachable."""

    status: Literal["healthy", "unhealthy"]
    database: DatabaseStatus


def database_health() -> DatabaseHealth:
    """The dependency that looks at the database; the process provides it.

    Provide it with `app.dependency_overrides[database_health] = ...`, as `visionquery-api` does
    and as tests do. Later endpoints receive what they need the same way, with `Depends`.
    """
    raise RuntimeError("no database check was provided: set app.dependency_overrides")


#: Every Operator operation lives under `/api` (ADR 0009: they go through `tools`). `/health`
#: is the one route at the root: it is for infrastructure (the Compose healthcheck), not Operators.
operator_router = APIRouter(prefix="/api")


def create_app() -> FastAPI:
    """The api, with no dependency provided yet; see `database_health`."""
    # No interactive docs: FastAPI's load Swagger UI and ReDoc from a CDN, and a Deployment is
    # closed (ADR 0001). The schema is still served at /openapi.json.
    app = FastAPI(
        title="VisionQuery", version=version("visionquery-api"), docs_url=None, redoc_url=None
    )
    app.include_router(operator_router)

    @app.get(
        "/health",
        operation_id="health",
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Health}},
        summary="Whether the Deployment is ready to serve: its database can be reached",
    )
    def health(
        response: Response, database: Annotated[DatabaseHealth, Depends(database_health)]
    ) -> Health:
        """Readiness, not liveness: 200 only while the api can do its work.

        A running api that cannot reach the database answers 503 here, so the Compose
        healthcheck (which asks this) marks it unhealthy rather than letting an api that
        can answer nothing look fine. The body names the problem by category only.
        """
        if not database.connected:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return Health(
                status="unhealthy",
                database=DatabaseStatus(status="unreachable", problem=database.problem),
            )
        return Health(status="healthy", database=DatabaseStatus(status="connected", problem=None))

    return app
