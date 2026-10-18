"""MediaMTX's control API (v3), as much of it as the reconciler needs.

Only path configuration and the internal users are touched. A failure is a `MediaMTXError` whose
message names what was asked and why it failed, with every URL in it replaced by `***`:
MediaMTX's own error text may quote a path's source, which is a camera's URL, and a camera's
URL is secret in its path and query as well as its password (ADR 0014).
"""

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any, cast
from urllib.parse import quote

from pydantic import SecretStr
from visionquery.runtime import scrub

__all__ = ["REDACTED", "Json", "MediaMTXClient", "MediaMTXError"]

#: What MediaMTX shows instead of a user's password, which it never shows back.
REDACTED = "<redacted>"
#: Paths listed per page; MediaMTX's own default is 100.
_PAGE = 1000
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")

type Json = dict[str, Any]


class MediaMTXError(Exception):
    """MediaMTX could not be reached, or refused what it was asked."""

    def __init__(self, message: str, *, refused: bool = False) -> None:
        super().__init__(message)
        #: Whether MediaMTX answered and said no (as opposed to not answering): a refusal of
        #: one request says nothing of the next, an unreachable MediaMTX says it of all.
        self.refused = refused


class MediaMTXClient:
    """Asks MediaMTX's control API at `url` as `user`, who must have the `api` permission."""

    def __init__(self, url: str, user: str, password: SecretStr, *, timeout: float = 10) -> None:
        self._url = url.rstrip("/")
        self.user = user
        self.password = password
        self._timeout = timeout

    def path_configs(self) -> dict[str, Json]:
        """Every configured path, by name, with every setting MediaMTX holds for it."""
        configs: dict[str, Json] = {}
        page, pages = 0, 1
        while page < pages:
            listed = self._call("GET", f"/v3/config/paths/list?itemsPerPage={_PAGE}&page={page}")
            for item in cast(list[Json], listed["items"]):
                configs[item["name"]] = item
            pages, page = int(listed["pageCount"]), page + 1
        return configs

    def paths(self) -> dict[str, Json]:
        """Every path's state (whether it is ready, its tracks, the bytes it received), by name."""
        states: dict[str, Json] = {}
        page, pages = 0, 1
        while page < pages:
            listed = self._call("GET", f"/v3/paths/list?itemsPerPage={_PAGE}&page={page}")
            for item in cast(list[Json], listed["items"]):
                states[item["name"]] = item
            pages, page = int(listed["pageCount"]), page + 1
        return states

    def path_defaults(self) -> Json:
        """The settings a path has when its configuration does not say otherwise."""
        return self._call("GET", "/v3/config/pathdefaults/get")

    def add_path(self, name: str, config: Json) -> None:
        self._call("POST", f"/v3/config/paths/add/{_quoted(name)}", config)

    def replace_path(self, name: str, config: Json) -> None:
        """Set the path's configuration to `config`, every other setting to its default."""
        self._call("POST", f"/v3/config/paths/replace/{_quoted(name)}", config)

    def delete_path(self, name: str) -> None:
        self._call("DELETE", f"/v3/config/paths/delete/{_quoted(name)}")

    def users(self) -> list[Json]:
        """The internal users, each password other than an empty one shown as `REDACTED`."""
        return cast(list[Json], self._call("GET", "/v3/config/global/get")["authInternalUsers"])

    def set_users(self, users: list[Json]) -> None:
        """Replace the internal users; MediaMTX applies them without dropping a connection."""
        self._call("PATCH", "/v3/config/global/patch", {"authInternalUsers": users})

    def _call(self, method: str, endpoint: str, body: Json | None = None) -> Json:
        request = urllib.request.Request(
            self._url + endpoint,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
        )
        if body is not None:
            request.add_header("Content-Type", "application/json")
        token = f"{self.user}:{self.password.get_secret_value()}".encode()
        request.add_header("Authorization", f"Basic {base64.b64encode(token).decode()}")
        asked = f"{method} {endpoint.partition('?')[0]}"
        failed: str
        refused = False
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return cast(Json, json.loads(response.read() or b"{}"))
        except urllib.error.HTTPError as error:
            failed = f"MediaMTX refused {asked}: {error.code} {_said(error)}"
            refused = True
        except (urllib.error.URLError, OSError) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            failed = f"MediaMTX could not be reached for {asked}: {type(reason).__name__}"
        except ValueError:
            failed = f"MediaMTX answered {asked} with something that is not JSON"
        # Raised outside the `except`, so no traceback carries the request or its body.
        raise MediaMTXError(scrub(_URL.sub("***", failed)), refused=refused)


def _quoted(name: str) -> str:
    return quote(name, safe="/")


def _said(error: urllib.error.HTTPError) -> str:
    """The error MediaMTX gave, from its JSON answer, or the HTTP reason."""
    try:
        said = json.loads(error.read() or b"{}").get("error")
    except ValueError, OSError, AttributeError:
        said = None
    return str(said) if said else str(error.reason)
