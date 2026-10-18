# MediaMTX is reconciled from the camera registry, and closed to everyone else

The camera registry in the memory is the only record of a Deployment's Cameras; MediaMTX holds no Camera in its configuration file.
A reconciler (`visionquery-reconciler`, its own package and process) makes MediaMTX match the registry every few seconds through MediaMTX's control API: one path per Camera not removed, pulled from its URL or waiting for its publisher, and every other path removed.
A whole cycle compares every setting of every path, so a change to the registry, a path changed behind its back, and a restarted MediaMTX (which forgets everything its API was told) are all put right within one cycle, and a cycle that finds nothing to change writes nothing.

MediaMTX lets nobody publish or read by default (ADR 0002).
A pushed Camera publishes only to its own path, with credentials of its own, which the registry keeps only as an argon2id hash; the reconciler gives MediaMTX one user per pushed Camera, whose one permission is to publish to that path, checked against that hash.
A pulled Camera is pulled by MediaMTX itself, so nobody may publish to its path.
Only VisionQuery's own processes read or drive the API, and as two users, so that a process that only reads can do no more:

- `visionquery-reader` has the `read` permission only.
  Every reader uses it (the recorder, the Perception Workers, the live view), and its password, `VISIONQUERY_MEDIAMTX_READ_PASSWORD`, is the one the Deployment gives them.
- `visionquery-api` has the `api` permission only.
  Only the reconciler holds its password, `VISIONQUERY_MEDIAMTX_API_PASSWORD` (Compose passes it to the reconciler and to MediaMTX, which checks it, and to nothing else).
  The reconciler also holds the readers' password, but only to write that user back unchanged when it writes MediaMTX's users; it never reads with it.

A reader that is compromised therefore cannot rewrite MediaMTX's users, nor read a pulled Camera's full URL (credentials, path and query) through the API; before this split one shared `visionquery` user held both permissions.
The loopback healthcheck and Prometheus's metrics need no password, and nothing else is granted.

A publisher's password is hashed with argon2id at 19 MiB of memory, two passes and one lane (OWASP's first recommended setting), not argon2-cffi's defaults of 64 MiB, three passes and four lanes.
MediaMTX checks that hash on every publish attempt from anyone who reaches the RTSP port, without knowing any password, so the cost of a wrong attempt is paid by MediaMTX: at 64 MiB and four lanes a few hundred concurrent attempts would take gigabytes of memory and every core of its host, while at 19 MiB and one lane they take a few hundred megabytes and one core each.
That is still safe: the password is at least 16 characters, so an attacker who obtained a hash must still search a space argon2id makes costly, and the cost is set into each hash (the parameters are part of the PHC string), so raising it later leaves the stored hashes working.

## Considered Options

Listing the paths in `mediamtx.yml` was rejected: Cameras are added by Operators at run time, the file would hold their credentials in plain text, and MediaMTX reloads it by restarting what changed.
Calling MediaMTX only when an Operator adds, changes or removes a Camera was rejected: MediaMTX forgets its API's changes when it restarts, and a missed call would leave it wrong until the next one.
MediaMTX's HTTP authentication (asking a VisionQuery endpoint about every publisher and reader) was rejected: it would keep the credentials in one place, but MediaMTX could then admit nobody, not even its own healthcheck or the reconciler, while that endpoint was down.
MediaMTX's deprecated per-path `publishUser` and `publishPass` were rejected as deprecated.

## Consequences

MediaMTX never shows a password back through its API, so the reconciler, which rewrites MediaMTX's list of users, keeps every user the configuration file declares and writes back the two passwords it is given (the API user's and the readers'), but refuses to touch the list while the file declares a user with any other password.
For the same reason it cannot see a publisher's password in MediaMTX: it remembers the hashes it last wrote, and writes the users once after it starts.
A Camera's credentials (pull URLs, publisher hashes) are readable through MediaMTX's API by the `visionquery-api` user, which only the reconciler holds, and from inside MediaMTX's container, which is where MediaMTX needs them.
MediaMTX may refuse one Camera's path (a pulled URL it cannot use): the reconciler then reports that path, logs the Camera by its id and path only, and still does everything else in the cycle, so one Camera never costs the others their paths, nor the publishers theirs.
The registry refuses the URL forms MediaMTX refuses (ADR 0014), so this is a defence for a form that slips through, and for a MediaMTX that changes its mind in a later release.
Credentials over RTSP are sent with Basic authentication, the only kind MediaMTX checks a hashed password against, so they and the video travel in the clear: MediaMTX's camera-facing ports belong on the cameras' own network.
After a restart of MediaMTX, Cameras are refused for up to one cycle, and a publisher already connected keeps publishing after its credentials change until it reconnects.
