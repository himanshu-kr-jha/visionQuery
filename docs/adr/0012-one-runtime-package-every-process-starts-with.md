# One runtime package every process starts with

Every VisionQuery process (recorder, Perception Workers, linker, api, Reasoner) starts through `visionquery.runtime`, which gives it three things and nothing else.
Settings come only from environment variables under the `VISIONQUERY_` prefix, declared as a pydantic-settings class the service extends with its own fields; a missing or invalid variable stops the process with an error naming the variable.
Logs are one JSON object per line on stdout, carrying the service name and level, and pass through a scrubber that removes credentials from every URL in the message, the exception text and every structured field before anything is written.
Metrics are exposed in Prometheus text format on `/metrics`, on a port of the process's own, and are pulled, never pushed.

`runtime` imports none of the other packages, so every package may import it without creating a cycle; it holds no domain terms and no database driver.

## Considered Options

Config files and per-service command-line flags were rejected: containers are configured through the environment, and one mechanism means one place to look when a service will not start.
structlog was rejected for logging because every third-party library (uvicorn, the Triton client) already logs through the standard library, and a single standard-library formatter at the root is what lets the scrubber see all of it.
Scrubbing at the point of logging (as phyOS's recorder did around ffmpeg errors) was rejected as the only defence: one forgotten call leaks a camera password, so it happens once, in the formatter, for everything.

## Known limits of the scrubber

The scrubber is a best-effort defence in depth, linear in the length of the text, and it has limits that are accepted.
A password containing whitespace (or one of `,;&)]}` outside a URL) leaks after that character, because plain-text values end there.
A scheme-less `user:pw@host` is not recognised, because only text after `scheme://` is treated as userinfo.
Percent-encoded URLs are not decoded before scrubbing.
Full-width or other look-alike `@` characters are not recognised.
URLs whose path or query contains an `@` lose their host in logs by design, because the userinfo is taken to end at the last `@`.
The camera registry (milestone 1) should therefore reject camera URLs it cannot scrub reliably, for example URLs without a scheme.
