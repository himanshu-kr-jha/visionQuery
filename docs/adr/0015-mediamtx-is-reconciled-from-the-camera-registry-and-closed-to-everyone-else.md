# MediaMTX is reconciled from the camera registry, and closed to everyone else

The camera registry in the memory is the only record of a Deployment's Cameras; MediaMTX holds no Camera in its configuration file.
A reconciler (`visionquery-reconciler`, its own package and process) makes MediaMTX match the registry every few seconds through MediaMTX's control API: one path per Camera not removed, pulled from its URL or waiting for its publisher, and every other path removed.
A whole cycle compares every setting of every path, so a change to the registry, a path changed behind its back, and a restarted MediaMTX (which forgets everything its API was told) are all put right within one cycle, and a cycle that finds nothing to change writes nothing.

MediaMTX lets nobody publish or read by default (ADR 0002).
A pushed Camera publishes only to its own path, with credentials of its own, which the registry keeps only as an argon2id hash; the reconciler gives MediaMTX one user per pushed Camera, whose one permission is to publish to that path, checked against that hash.
A pulled Camera is pulled by MediaMTX itself, so nobody may publish to its path.
Only VisionQuery's own processes read, as one `visionquery` user whose password the Deployment chooses; the loopback healthcheck and Prometheus's metrics need no password, and nothing else is granted.

## Considered Options

Listing the paths in `mediamtx.yml` was rejected: Cameras are added by Operators at run time, the file would hold their credentials in plain text, and MediaMTX reloads it by restarting what changed.
Calling MediaMTX only when an Operator adds, changes or removes a Camera was rejected: MediaMTX forgets its API's changes when it restarts, and a missed call would leave it wrong until the next one.
MediaMTX's HTTP authentication (asking a VisionQuery endpoint about every publisher and reader) was rejected: it would keep the credentials in one place, but MediaMTX could then admit nobody, not even its own healthcheck or the reconciler, while that endpoint was down.
MediaMTX's deprecated per-path `publishUser` and `publishPass` were rejected as deprecated.

## Consequences

MediaMTX never shows a password back through its API, so the reconciler, which rewrites MediaMTX's list of users, keeps every user the configuration file declares and writes back its own password, but refuses to touch the list while the file declares a user with any other password.
For the same reason it cannot see a publisher's password in MediaMTX: it remembers the hashes it last wrote, and writes the users once after it starts.
A Camera's credentials (pull URLs, publisher hashes) are readable through MediaMTX's API by the `visionquery` user and from inside MediaMTX's container, which is where MediaMTX needs them.
Credentials over RTSP are sent with Basic authentication, the only kind MediaMTX checks a hashed password against, so they and the video travel in the clear: MediaMTX's camera-facing ports belong on the cameras' own network.
After a restart of MediaMTX, Cameras are refused for up to one cycle, and a publisher already connected keeps publishing after its credentials change until it reconnects.
