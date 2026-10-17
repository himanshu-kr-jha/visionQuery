"""`visionquery-api`: serve the api over HTTP, started the way every process starts (ADR 0012)."""

from functools import partial
from typing import Annotated

import uvicorn
from pydantic import Field
from visionquery.api.app import create_app
from visionquery.memory.health import check_database
from visionquery.memory.settings import DatabaseSettings
from visionquery.runtime import Settings, start

__all__ = ["ApiSettings", "main"]


class ApiSettings(Settings):
    """What `visionquery-api` reads from the environment."""

    database: DatabaseSettings
    #: Where the api listens; every interface by default, so it can be published from a container.
    http_host: str = "0.0.0.0"
    http_port: Annotated[int, Field(ge=0, le=65535)] = 8080


def main() -> None:
    """The `visionquery-api` command."""
    settings = start("api", ApiSettings).settings
    app = create_app(partial(check_database, settings.database.url.get_secret_value()))
    uvicorn.run(
        app,
        host=settings.http_host,
        port=settings.http_port,
        # uvicorn's loggers propagate to the runtime's handler, so they are JSON and scrubbed;
        # its own configuration would replace that handler with plain text on stderr.
        log_config=None,
        # A line per request would be mostly the healthcheck's, every few seconds.
        access_log=False,
        server_header=False,
    )
