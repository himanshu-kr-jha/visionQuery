# A camera URL's path and query are secret, like its password

A camera carries its credentials wherever its maker put them.
A UniFi Protect camera's token is the path (`rtsps://192.168.1.1:7441/Xk9aQ2bLm4ZtTq1R?enableSrtp`), an XMEye camera's login is in the path (`rtsp://192.168.1.10:554/user=admin&password=x&channel=1&stream=0.sdp?`), and many cameras take a `?sig=`, `?token=`, `?key=` or `?auth=` in the query.
The log scrubber (ADR 0012) knows only a URL's user and password and a fixed list of query keys, so a rule that accepted a camera URL only when the scrubber could scrub it both leaked the secrets it did not know and refused URLs that real cameras need.

## Decision

The user, password, path and query of a pulled Camera's URL are all secret.

- The registry stores only the public part (scheme, host and port) in `pull_url`, and the secrets apart from it: the user and password in `pull_credentials`, the path and query in `pull_target`.
  Removing a Camera clears both secrets' columns (`pull_credentials` and `pull_target`), keeping its row, id, name and the public part.
- What is shown, logged or serialised is the redacted form, `rtsps://***@192.168.1.1:7441/***`: `***@` stands for the user and password and is present only when the URL has them, and `/***` stands for the path and query and is present only when the URL has any.
  The full URL is available only through `CameraUrl.get_secret_value()`, for whatever has to connect.
  A `CameraUrl` is neither pickled nor copied, because that would carry the full URL.
- The registry never logs a URL, redacted or not: a Camera is identified in logs by its id, name and path.
- A URL is no longer refused for what its path or query holds.
  It is refused only when it is malformed: not `rtsp://` or `rtsps://`, a bad host or port, a character a URL cannot hold unescaped, or an `@` outside the user and password.

## Consequences

This supersedes the "Camera URLs" section of ADR 0012, which refused camera URLs the scrubber cannot scrub.
The scrubber remains a defence in depth for everything else that is logged.
Anything that needs a URL of a Camera for display shows the redacted form, so the path and query of a pulled Camera's URL are never visible to an Operator in the interface; the Operator who needs it has it from the camera's own configuration.
The secrets are stored in plaintext, as the password already was; encrypting them at rest is a separate decision for the Deployment's setup.
