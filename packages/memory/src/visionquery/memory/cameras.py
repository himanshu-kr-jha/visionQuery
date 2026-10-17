"""The camera registry: every camera of the Deployment, by a name unique within it.

A camera is added in one of two ways. A **pulled** camera is reached by its `rtsp://` or
`rtsps://` URL, which MediaMTX pulls into the path `pulled/<id>`; a **pushed** camera publishes
to a MediaMTX path the Operator chose. Either way `Camera.path` is the MediaMTX path its video
is on, unique among the cameras not removed.

**A camera URL's user, password, path and query are all secret** (ADR 0014): cameras carry
their credentials in any of them (a UniFi Protect token is the path, an XMEye login is in the
path, many cameras take `?token=`). They live only in the `CameraUrl`, whose `str`, `repr` and
pydantic serialisation are the redacted form, `rtsps://***@192.168.1.1:7441/***`: the `***@`
stands for the user and password, present only when the URL has them, and the `/***` for the
path and query, present only when the URL has any. Only `CameraUrl.get_secret_value()` gives
the full URL, for whatever has to connect. The registry never logs a URL; a camera is
identified in logs by its id, name and path. Errors never quote a URL, not even one that was
refused, and no error of the database driver, which may repeat the row it refused, is passed on.
Any URL of the right form is accepted, so none is refused for what its path or query holds.

**A removed camera keeps its row** (and its id, which is never reused), because its Footage
and Tracks outlive it; its name and push path are free again, and its secrets (credentials,
path and query) are forgotten.

The registry works on the connection it is given. Outside a transaction each operation commits
on its own; inside one the caller opened (`with connection.transaction():`) each runs in a
savepoint and is kept or rolled back with the caller's transaction.
"""

import builtins
import ipaddress
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, LiteralString

import psycopg
from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = [
    "PULLED_PATHS",
    "Camera",
    "CameraError",
    "CameraNameTakenError",
    "CameraNotFoundError",
    "CameraPathTakenError",
    "CameraRegistry",
    "CameraRegistryError",
    "CameraUrl",
    "InvalidCameraError",
]

_log = logging.getLogger(__name__)

#: The MediaMTX paths of pulled cameras begin with this, followed by the camera's id; no push
#: path may.
PULLED_PATHS = "pulled/"

#: The longest name and push path; long enough for any real one, short enough to show.
NAME_LIMIT = 100
PATH_LIMIT = 200
#: The longest URL accepted, as written.
URL_LIMIT = 2048


class CameraError(Exception):
    """Something the camera registry refused or could not do."""


class InvalidCameraError(CameraError, ValueError):
    """A name, URL or path the registry does not accept; the message never quotes it."""


class CameraNameTakenError(CameraError):
    """Another camera not removed already has this name, in any case."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"another camera is already named {name!r}")


class CameraPathTakenError(CameraError):
    """Another camera not removed already publishes to this path."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"another camera already publishes to the path {path!r}")


class CameraNotFoundError(CameraError, LookupError):
    """No camera has this id, or none that is not removed, for an operation that needs one."""

    def __init__(self, camera_id: int) -> None:
        self.camera_id = camera_id
        super().__init__(f"there is no camera {camera_id}")


class CameraRegistryError(CameraError):
    """The database failed; named by the class of the driver's error, never by its text."""


# --- pull URLs --------------------------------------------------------------------------------

#: RFC 3986's characters, less `@` outside the user and password: a URL's credentials run to
#: its last `@`, so one in the path would make the host ambiguous.
_UNRESERVED = r"A-Za-z0-9\-._~"
_SUB_DELIMS = r"!$&'()*+,;="
_ESCAPE = r"%[0-9A-Fa-f]{2}"
_USER = rf"(?:[{_UNRESERVED}{_SUB_DELIMS}]|{_ESCAPE})+"
_PASSWORD = rf"(?:[{_UNRESERVED}{_SUB_DELIMS}:]|{_ESCAPE})*"
_PASSWORD_REQUIRED = rf"(?:[{_UNRESERVED}{_SUB_DELIMS}:]|{_ESCAPE})+"
_SEGMENT = rf"(?:[{_UNRESERVED}{_SUB_DELIMS}:]|{_ESCAPE})*"
_QUERY = rf"(?:[{_UNRESERVED}{_SUB_DELIMS}:/?]|{_ESCAPE})*"
_URL = re.compile(
    rf"(?P<scheme>(?i:rtsps?))://"
    rf"(?:(?P<credentials>{_USER}(?::{_PASSWORD})?|:{_PASSWORD_REQUIRED})@)?"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9_.\-]+)"
    r"(?::(?P<port>[0-9]{1,5}))?"
    rf"(?P<rest>(?:/{_SEGMENT})*(?:\?{_QUERY})?)"
)
_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
_URL_FORM = (
    "must be rtsp:// or rtsps:// followed by [user[:password]@]host[:port][/path][?query], "
    "with any space, @, /, ?, # or % in the user or password percent-encoded"
)


class CameraUrl:
    """A pulled camera's URL, which shows only its redacted form.

    `str()`, `repr()` and pydantic's serialisation (what the api sends) give `redacted`, the URL
    with its user and password replaced by `***@` and its path and query by `/***` (each only
    when there is one); `get_secret_value()` gives the full URL. Pickling and copying it raise,
    because they would carry the full URL; pass `get_secret_value()` explicitly.
    pydantic cannot read one from input, because its validation errors quote the input: a URL
    from a request is read as a secret (`SecretStr`) and then given to `CameraUrl`.
    """

    __slots__ = ("_bare", "_credentials", "_redacted", "_target", "_url")

    def __init__(self, url: str) -> None:
        parts = _parse(url)
        if parts is None:
            raise InvalidCameraError(f"the camera URL {_URL_FORM}")
        self._set(*parts)

    @classmethod
    def _stored(cls, bare: str, credentials: str | None, target: str | None) -> CameraUrl:
        """A URL the registry stored, as its public part and its secrets (None once forgotten)."""
        url = cls.__new__(cls)
        scheme, _, authority = bare.partition("://")
        url._set(scheme, credentials, authority, target or "")
        return url

    def _set(self, scheme: str, credentials: str | None, authority: str, target: str) -> None:
        self._bare = f"{scheme}://{authority}"
        self._credentials = credentials
        self._target = target
        userinfo = "" if credentials is None else f"{credentials}@"
        self._url = f"{scheme}://{userinfo}{authority}{target}"
        self._redacted = (
            f"{scheme}://{'' if credentials is None else '***@'}{authority}"
            f"{'/***' if target else ''}"
        )

    @property
    def redacted(self) -> str:
        """The URL with its user and password, if it has them, replaced by `***@`, and its path
        and query, if it has any, by `/***`."""
        return self._redacted

    @property
    def has_credentials(self) -> bool:
        return self._credentials is not None

    def get_secret_value(self) -> str:
        """The full URL, secrets included, for whatever has to connect to the camera."""
        return self._url

    def __str__(self) -> str:
        return self._redacted

    def __repr__(self) -> str:
        return f"CameraUrl({self._redacted!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CameraUrl) and other._url == self._url

    def __hash__(self) -> int:
        return hash(self._url)

    def __reduce__(self) -> tuple[Any, ...]:
        raise TypeError(
            "a CameraUrl is not pickled or copied, because that would carry the full URL; "
            "pass get_secret_value() explicitly"
        )

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.is_instance_schema(
            cls,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _redacted, return_schema=core_schema.str_schema()
            ),
        )


def _redacted(url: CameraUrl) -> str:
    return url.redacted


def _parse(url: object) -> tuple[str, str | None, str, str] | None:
    """A URL's scheme, credentials, authority and path with query, normalised, or None when it
    is not valid."""
    if not isinstance(url, str) or len(url) > URL_LIMIT or not url.isascii():
        return None
    found = _URL.fullmatch(url)
    if found is None:
        return None
    host = _host(found["host"])
    port = found["port"]
    if host is None or (port is not None and not 0 < int(port) < 65536):
        return None
    authority = host if port is None else f"{host}:{int(port)}"
    return found["scheme"].lower(), found["credentials"], authority, found["rest"]


def _host(host: str) -> str | None:
    """The host in lower case, or None when it is not an IP address or a DNS name."""
    if host.startswith("["):
        return host.lower() if _is_ip(host[1:-1], ipaddress.IPv6Address) else None
    host = host.lower()
    if all(c.isdigit() or c == "." for c in host):
        return host if _is_ip(host, ipaddress.IPv4Address) else None
    labels = host.removesuffix(".").split(".")
    if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    return host


def _is_ip(text: str, kind: type[ipaddress.IPv4Address | ipaddress.IPv6Address]) -> bool:
    try:
        kind(text)
    except ValueError:
        return False
    return True


# --- names and push paths ---------------------------------------------------------------------

#: What MediaMTX accepts in a path name (`IsValidPathName` in MediaMTX 1.21).
_PATH = re.compile(r"[0-9A-Za-z_./-]+")
#: Path names MediaMTX itself gives a meaning: each matches every path.
_MEDIAMTX_PATHS = frozenset({"all", "all_others"})


def _name(name: str) -> str:
    """The name in its one kept form: NFC, with runs of white space made one space."""
    kept = " ".join(unicodedata.normalize("NFC", name).split())
    if not kept:
        raise InvalidCameraError("a camera's name must not be empty")
    if len(kept) > NAME_LIMIT:
        raise InvalidCameraError(f"a camera's name must be at most {NAME_LIMIT} characters")
    # Control and format characters (escape codes, zero-width spaces) make two names look alike.
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Co", "Cn"} for c in kept):
        raise InvalidCameraError("a camera's name must not hold control or invisible characters")
    return kept


def _push_path(path: str) -> str:
    """The path, when MediaMTX accepts it as a path name and it is not one set aside."""
    if not path or len(path) > PATH_LIMIT:
        raise InvalidCameraError(f"a push path must be 1 to {PATH_LIMIT} characters")
    if not _PATH.fullmatch(path):
        raise InvalidCameraError(
            "a push path may hold only letters, digits, and _ . - and / between parts"
        )
    if any(segment in {"", ".", ".."} for segment in path.split("/")):
        raise InvalidCameraError(
            "a push path must not begin or end with /, hold //, or have a part that is . or .."
        )
    if path in _MEDIAMTX_PATHS:
        raise InvalidCameraError(f"{path!r} has a meaning of its own to MediaMTX")
    if path.startswith(PULLED_PATHS):
        raise InvalidCameraError(f"push paths beginning with {PULLED_PATHS!r} are the registry's")
    return path


# --- the registry -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Camera:
    """One camera of the Deployment, as the registry knows it."""

    id: int
    #: Unique, whatever its case, among the cameras not removed.
    name: str
    #: The MediaMTX path the camera's video is on.
    path: str
    #: Where MediaMTX pulls the camera from, or None when the camera publishes to `path`.
    pull_url: CameraUrl | None
    added_at: datetime
    removed_at: datetime | None

    @property
    def pushed(self) -> bool:
        """Whether the camera publishes to its path, rather than being pulled."""
        return self.pull_url is None


_COLUMNS: LiteralString = (
    "id, name, pull_url, pull_credentials, pull_target, push_path, added_at, removed_at"
)


def _camera(row: tuple[Any, ...]) -> Camera:
    camera_id, name, pull_url, credentials, target, push_path, added_at, removed_at = row
    # The registry and `CameraUrl` are one module's, and only they see a URL's parts.
    stored = CameraUrl._stored  # pyright: ignore[reportPrivateUsage]
    url = None if pull_url is None else stored(pull_url, credentials, target)
    path = push_path if push_path is not None else f"{PULLED_PATHS}{camera_id}"
    return Camera(camera_id, name, path, url, added_at, removed_at)


class CameraRegistry:
    """Adds, finds, renames and removes the Deployment's cameras."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def add_pulled(self, name: str, url: CameraUrl | str) -> Camera:
        """Add a camera MediaMTX will pull from `url`."""
        name = _name(name)
        if not isinstance(url, CameraUrl):
            url = CameraUrl(url)
        camera = self._write(
            "INSERT INTO cameras (name, pull_url, pull_credentials, pull_target) "
            "VALUES (%s, %s, %s, %s)",
            (name, url._bare, url._credentials, url._target),  # pyright: ignore[reportPrivateUsage]
            name=name,
        )
        self._report("camera added", camera)
        return camera

    def add_pushed(self, name: str, path: str) -> Camera:
        """Add a camera that will publish to the MediaMTX path `path`."""
        name, path = _name(name), _push_path(path)
        camera = self._write(
            "INSERT INTO cameras (name, push_path) VALUES (%s, %s)",
            (name, path),
            name=name,
            path=path,
        )
        self._report("camera added", camera)
        return camera

    def get(self, camera_id: int) -> Camera:
        """The camera with this id, removed or not."""
        rows = self._run(f"SELECT {_COLUMNS} FROM cameras WHERE id = %s", (camera_id,))
        if not rows:
            raise CameraNotFoundError(camera_id)
        return _camera(rows[0])

    def list(self) -> builtins.list[Camera]:
        """Every camera not removed, in the order they were added."""
        rows = self._run(f"SELECT {_COLUMNS} FROM cameras WHERE removed_at IS NULL ORDER BY id")
        return [_camera(row) for row in rows]

    def rename(self, camera_id: int, name: str) -> Camera:
        """Give a camera not removed a new name."""
        name = _name(name)
        camera = self._write(
            "UPDATE cameras SET name = %s WHERE id = %s AND removed_at IS NULL",
            (name, camera_id),
            name=name,
            camera_id=camera_id,
        )
        self._report("camera renamed", camera)
        return camera

    def remove(self, camera_id: int) -> None:
        """Remove a camera: it leaves the list, frees its name and path, and forgets its
        credentials, path and query, but stays known by its id."""
        camera = self._write(
            "UPDATE cameras SET removed_at = now(), pull_credentials = NULL, pull_target = NULL "
            "WHERE id = %s AND removed_at IS NULL",
            (camera_id,),
            camera_id=camera_id,
        )
        self._report("camera removed", camera)

    def _write(
        self,
        statement: LiteralString,
        parameters: tuple[object, ...],
        *,
        name: str | None = None,
        path: str | None = None,
        camera_id: int | None = None,
    ) -> Camera:
        """Run one INSERT or UPDATE of one camera and return it as it now is.

        A unique index's refusal becomes the error naming what is taken, and an UPDATE that
        matched nothing becomes `CameraNotFoundError`.
        """
        taken: CameraError | None = None
        rows: builtins.list[tuple[Any, ...]] = []
        try:
            rows = self._run(f"{statement} RETURNING {_COLUMNS}", parameters, unique=True)
        except psycopg.errors.UniqueViolation as error:
            constraint = error.diag.constraint_name
            if constraint == "cameras_name_unique" and name is not None:
                taken = CameraNameTakenError(name)
            elif constraint == "cameras_push_path_unique" and path is not None:
                taken = CameraPathTakenError(path)
            else:
                taken = CameraRegistryError("the database refused the camera: UniqueViolation")
        # Raised here, outside the `except`, so the driver's error is not even its context.
        if taken is not None:
            raise taken
        if not rows:
            assert camera_id is not None
            raise CameraNotFoundError(camera_id)
        return _camera(rows[0])

    def _run(
        self, query: LiteralString, parameters: tuple[object, ...] = (), *, unique: bool = False
    ) -> builtins.list[tuple[Any, ...]]:
        """The rows of one statement, run in its own transaction or savepoint.

        Any error of the driver becomes `CameraRegistryError` naming only its class, because its
        text may repeat the row, credentials and all; with `unique`, a unique violation is let
        through for the caller to name what is taken.
        """
        failed: str | None = None
        try:
            with self._connection.transaction():
                return self._connection.execute(query, parameters).fetchall()
        except psycopg.errors.UniqueViolation:
            if unique:
                raise
            failed = "UniqueViolation"
        except psycopg.Error as error:
            failed = type(error).__name__
        raise CameraRegistryError(f"the camera registry's database failed: {failed}")

    @staticmethod
    def _report(event: str, camera: Camera) -> None:
        # Never the URL, whose path and query are secret: id, name and path identify a camera.
        _log.info(
            "%s: %s",
            event,
            camera.name,
            extra={
                "camera_id": camera.id,
                "camera_name": camera.name,
                "camera_path": camera.path,
            },
        )
