"""The HTTP api a Deployment's web app and Operators talk to."""

from collections.abc import Callable
from importlib.metadata import version
from typing import Literal

from fastapi import FastAPI, Response, status
from pydantic import BaseModel
from visionquery.memory.connection import ConnectionProblem
from visionquery.memory.health import DatabaseHealth

__all__ = ["DatabaseStatus", "Health", "create_app"]


class DatabaseStatus(BaseModel):
    """Whether the api can reach the Deployment's database, and if not, why."""

    status: Literal["connected", "unreachable"]
    #: Why it is unreachable, as a fixed category; never the URL or any part of it.
    problem: ConnectionProblem | None


class Health(BaseModel):
    """Whether the api can serve: healthy only when everything it depends on is reachable."""

    status: Literal["healthy", "unhealthy"]
    database: DatabaseStatus


def create_app(check_database: Callable[[], DatabaseHealth]) -> FastAPI:
    """The api, asking `check_database` for the database's state on every health request."""
    # No interactive docs: FastAPI's load Swagger UI and ReDoc from a CDN, and a Deployment is
    # closed (ADR 0001). The schema is still served at /openapi.json.
    app = FastAPI(
        title="VisionQuery", version=version("visionquery-api"), docs_url=None, redoc_url=None
    )

    @app.get(
        "/health",
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Health}},
        summary="Whether the Deployment's database can be reached",
    )
    def health(response: Response) -> Health:
        database = check_database()
        if not database.connected:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return Health(
                status="unhealthy",
                database=DatabaseStatus(status="unreachable", problem=database.problem),
            )
        return Health(status="healthy", database=DatabaseStatus(status="connected", problem=None))

    return app
