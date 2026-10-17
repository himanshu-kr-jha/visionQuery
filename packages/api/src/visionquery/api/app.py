"""The HTTP api a Deployment's web app and Operators talk to."""

from collections.abc import Callable
from importlib.metadata import version
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from pydantic import BaseModel
from visionquery.api.probe import ConnectionProblem, DatabaseHealth

__all__ = [
    "DatabaseStatus",
    "Health",
    "create_app",
    "current_database_health",
    "operator_router",
]


class DatabaseStatus(BaseModel):
    """Whether the api can reach the Deployment's database, and if not, why."""

    status: Literal["connected", "unreachable"]
    #: Why it is unreachable, as a fixed category; never the URL or any part of it.
    problem: ConnectionProblem | None


class Health(BaseModel):
    """Whether the api is ready to serve: ready only when everything it depends on is reachable."""

    status: Literal["healthy", "unhealthy"]
    database: DatabaseStatus


def current_database_health(request: Request) -> DatabaseHealth:
    """The dependency that looks at the database: the check `create_app` was given."""
    check: Callable[[], DatabaseHealth] = request.app.state.database_health
    return check()


#: Every Operator operation lives under `/api` (ADR 0009: they go through `tools`). `/health`
#: is the one route at the root: it is for infrastructure (the Compose healthcheck), not Operators.
operator_router = APIRouter(prefix="/api")


def create_app(*, database_health: Callable[[], DatabaseHealth]) -> FastAPI:
    """The api over the dependencies it is given, which it keeps on `app.state`.

    Every dependency is an argument, so one that is forgotten is an error here and now, not a
    500 on one route later. Routes read them through `Depends` on `request.app.state` (see
    `current_database_health`), so a test may still replace one with `app.dependency_overrides`.
    Later endpoints (the `tools` they call) are arguments the same way.
    """
    # No interactive docs: FastAPI's load Swagger UI and ReDoc from a CDN, and a Deployment is
    # closed (ADR 0001). The schema is still served at /openapi.json.
    app = FastAPI(
        title="VisionQuery", version=version("visionquery-api"), docs_url=None, redoc_url=None
    )
    app.state.database_health = database_health
    app.include_router(operator_router)

    @app.get(
        "/health",
        operation_id="health",
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Health}},
        summary="Whether the Deployment is ready to serve: its database can be reached",
    )
    def health(
        response: Response, database: Annotated[DatabaseHealth, Depends(current_database_health)]
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
