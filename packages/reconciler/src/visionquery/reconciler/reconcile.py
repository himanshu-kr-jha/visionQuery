"""Making MediaMTX match the camera registry, once: one reconcile.

MediaMTX gets exactly one path per camera not removed, `Camera.path`: a pulled camera's pulls
its URL over RTSP on TCP, and a pushed camera's waits for a publisher. Every other path is
removed. Every setting of a path is compared, not only those the registry decides, so a path
changed behind the reconciler's back is put back as well.

Who may publish is the other half (ADR 0002): each pushed camera with publish credentials gets
a MediaMTX user of its own, whose one permission is to publish to its path, checked against its
argon2id hash. The reconciler owns every such user, and keeps every other user as MediaMTX's
configuration file declares it. MediaMTX never shows a password back, so the reconciler can keep
only users without one and those whose password it is given (its own, which is the API user's,
and the readers'); any other makes it refuse (`ReconcileError`). For the same reason it cannot
see a publisher's password in MediaMTX, so it remembers the hashes it last wrote, and writes
the users again when they differ, after its own start, and whenever MediaMTX's users are not
what it wrote (as after a restart of MediaMTX, which forgets everything its API was told).

MediaMTX may refuse one path (a pulled camera's source it cannot use): that path is reported in
`Changes.failed` and every other path, the removal of stale ones and the publishers are done
all the same, so one camera never costs the others theirs. A MediaMTX that cannot be reached
is not isolated this way: the reconcile raises `MediaMTXError`, because every request would fail.

A reconcile that finds MediaMTX as it should be changes nothing, so reconciling again at once is
free of effects: `Changes()` is falsy.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from pydantic import SecretStr
from visionquery.memory.cameras import Camera
from visionquery.reconciler.mediamtx import REDACTED, Json, MediaMTXClient, MediaMTXError

__all__ = ["Changes", "ReconcileError", "Reconciler", "path_config", "publisher"]


class ReconcileError(Exception):
    """MediaMTX holds something the reconciler must not overwrite."""


@dataclass(frozen=True)
class Changes:
    """What one reconcile changed in MediaMTX; falsy when it changed nothing."""

    #: Paths, by name.
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: Whether the users, and so who may publish where, were written.
    publishers: bool = False
    #: Paths MediaMTX refused to add, replace or remove, by name; the next reconcile tries
    #: again. Not a change, so it does not make `Changes` truthy.
    failed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.added or self.updated or self.removed or self.publishers)


def path_config(camera: Camera) -> Json:
    """The settings of a camera's path that differ from MediaMTX's defaults."""
    if camera.pull_url is None:
        return {"source": "publisher"}
    # RTP over TCP, as everywhere in the Deployment: over UDP it would need a port range open
    # and loses packets under load.
    return {"source": camera.pull_url.get_secret_value(), "rtspTransport": "tcp"}


def publisher(camera: Camera) -> Json | None:
    """The MediaMTX user a pushed camera publishes as, or None when it may not publish."""
    credentials = camera.publish_credentials
    if credentials is None or not camera.pushed:
        return None
    return {
        "user": credentials.user,
        "pass": f"argon2:{credentials.password_hash}",
        "ips": [],
        "permissions": [{"action": "publish", "path": camera.path}],
    }


def _attempt(failed: list[str], name: str, call: Callable[[], None]) -> bool:
    """Whether `call` succeeded. A path MediaMTX refuses is noted in `failed` and does not stop
    the rest; a MediaMTX that cannot be reached raises, because every request would fail."""
    try:
        call()
    except MediaMTXError as error:
        if not error.refused:
            raise
        failed.append(name)
        return False
    return True


def _is_publisher(user: Json) -> bool:
    """Whether a MediaMTX user is a camera's publisher, the reconciler's to write."""
    permissions: list[Json] = user.get("permissions") or []
    return (
        len(permissions) == 1
        and permissions[0].get("action") == "publish"
        and bool(permissions[0].get("path"))
    )


def _as_shown(users: Sequence[Json]) -> list[Json]:
    """Users as MediaMTX shows them back: every password but an empty one redacted."""
    return [{**user, "pass": REDACTED if user.get("pass") else ""} for user in users]


class Reconciler:
    """Makes the MediaMTX `client` reaches match the cameras it is given, one call at a time.

    `known` holds the passwords, by user, of the users the reconciler must write back unchanged
    besides the client's own (the readers').
    """

    def __init__(
        self, client: MediaMTXClient, known: Mapping[str, SecretStr] | None = None
    ) -> None:
        self._client = client
        self._known = {client.user: client.password, **(known or {})}
        #: The publishers last written, which MediaMTX never shows back; None before any write.
        self._written: list[Json] | None = None

    def reconcile(self, cameras: Sequence[Camera]) -> Changes:
        """Make MediaMTX's paths and publishers match `cameras`, those not removed."""
        current = [camera for camera in cameras if camera.removed_at is None]
        added, updated, removed, failed = self._paths(current)
        publishers = self._publishers(current)
        return Changes(tuple(added), tuple(updated), tuple(removed), publishers, tuple(failed))

    def _paths(
        self, cameras: Sequence[Camera]
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        wanted = {camera.path: path_config(camera) for camera in cameras}
        actual = self._client.path_configs()
        defaults = self._client.path_defaults()
        added: list[str] = []
        updated: list[str] = []
        removed: list[str] = []
        failed: list[str] = []
        # Stale paths go first, so that a camera's publisher never outlives its path.
        for name in sorted(set(actual) - set(wanted)):
            if _attempt(failed, name, partial(self._client.delete_path, name)):
                removed.append(name)
        for name, config in wanted.items():
            if name not in actual:
                if _attempt(failed, name, partial(self._client.add_path, name, config)):
                    added.append(name)
            elif actual[name] != {**defaults, **config, "name": name} and _attempt(
                failed, name, partial(self._client.replace_path, name, config)
            ):
                updated.append(name)
        return added, updated, removed, failed

    def _publishers(self, cameras: Sequence[Camera]) -> bool:
        """Write the users when who may publish is not what the cameras say; whether it did."""
        current = self._client.users()
        wanted = [user for camera in cameras if (user := publisher(camera)) is not None]
        kept = [self._keep(user) for user in current if not _is_publisher(user)]
        users = [*kept, *wanted]
        if self._written == wanted and _as_shown(users) == current:
            return False
        self._client.set_users(users)
        self._written = wanted
        return True

    def _keep(self, user: Json) -> Json:
        """A user that is not a publisher, as it must be written back to stay as it is."""
        if user.get("pass") != REDACTED:
            return user
        name: Any = user.get("user")
        if isinstance(name, str) and name in self._known:
            return {**user, "pass": self._known[name].get_secret_value()}
        raise ReconcileError(
            f"MediaMTX has a user {name!r} with a password, which the reconciler cannot keep "
            "when it writes the users (MediaMTX never shows a password back); give it none, "
            "or remove it from mediamtx.yml"
        )
