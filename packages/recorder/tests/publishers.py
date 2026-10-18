"""Opening cameras' paths in a real MediaMTX, as the reconciler does, for the recorder's tests.

The recorder never configures MediaMTX (the reconciler does, ADR 0015), so its tests do it
themselves: a path per camera waiting for its publisher, and that publisher's own user.
"""

from real_mediamtx import MediaMTX

__all__ = ["PASSWORD", "open_paths"]

#: Every test camera's publish password; each publishes as the user named for its path.
PASSWORD = "camera-pa55word"


def open_paths(server: MediaMTX, *paths: str) -> None:
    """Let each of `paths` be published to by the user of its name, with PASSWORD, alone."""
    for path in paths:
        server.api("POST", f"/v3/config/paths/add/{path}", {"source": "publisher"})
    # The configuration file's users, as the reconciler writes them back: each with its password.
    kept = server.with_known_passwords(server.users())
    publishers = [
        {
            "user": path,
            "pass": PASSWORD,
            "ips": [],
            "permissions": [{"action": "publish", "path": path}],
        }
        for path in paths
    ]
    server.api("PATCH", "/v3/config/global/patch", {"authInternalUsers": [*kept, *publishers]})
