# One runtime package every process starts with

Every VisionQuery process (recorder, Perception Workers, linker, api, Reasoner) starts through `visionquery.runtime`, which gives it three things and nothing else.
Settings come only from environment variables under the `VISIONQUERY_` prefix, declared as a pydantic-settings class the service extends with its own fields; a missing or invalid variable stops the process with an error naming the variable.
Logs are one JSON object per line on stdout, carrying the service name and level, and pass through a scrubber that removes credentials from the message, the exception text, the stack and every structured field before anything is written.
Scrubbing happens in two layers, so that no single forgotten call can leak a camera password.
A log record factory scrubs each record as it is made, which protects the records that reach handlers a library installs for itself.
It leaves a record untouched when there is nothing to remove, because formatters other libraries install may read the record's arguments.
The JSON formatter scrubs everything the runtime's own handler writes, including structured fields, and masks the value of any field whose name ends in a credential word.

Metrics are exposed in Prometheus text format on `/metrics`, on a port of the process's own, and are pulled, never pushed.

`runtime` imports none of the other packages, so every package may import it without creating a cycle; it holds no domain terms and no database driver.

## Where the record layer is installed, and what it misses

The record layer is two process-wide hooks, installed by `configure_logging`.
The record factory (`logging.setLogRecordFactory`) scrubs the message, its arguments, the exception text and the stack.
`Logger.makeRecord` is replaced on the class itself, for every logger in the process including those that already exist, to scrub the `extra=` fields, because the standard library attaches them only after the factory has run.
Both wrap whatever factory and `makeRecord` are in place when `configure_logging` is called, rather than replacing them, so a library that installed its own first (OpenTelemetry's logging instrumentor adds trace ids this way) keeps the fields it adds.
Both wrappers are marked as the runtime's, so configuring again does not wrap them a second time.
A container in `extra=` keeps its type (a namedtuple stays one), and is left as it is when there is nothing to remove.

Records that reach a handler without passing through both hooks are not scrubbed by this layer:

- records made by a `Logger` subclass that overrides `makeRecord` without calling `super()`, whose `extra=` fields are not scrubbed;
- records made by `logging.makeLogRecord`, which does not call the factory, as when records arrive over a socket from another process;
- fields that a filter adds to a record after it was made, since a filter runs when the record is handled.

On the runtime's own handler all three are still scrubbed, by the JSON formatter.
On a handler a library installed for itself, with a formatter of its own, they are not: a service that installs such a handler with such a filter or logger class must scrub what it adds.

## What never passes through logging

Three kinds of output do not go through the logging module, so neither layer sees them: the stderr of subprocesses (such as the recorder's ffmpeg), the stderr written directly by C libraries (such as grpc or NVDEC), and `print`.
The rule that follows is that a subprocess's stderr is always piped and logged line by line, never inherited, and that code never prints.

## Considered Options

Config files and per-service command-line flags were rejected: containers are configured through the environment, and one mechanism means one place to look when a service will not start.
structlog was rejected for logging because every third-party library (uvicorn, the Triton client) already logs through the standard library, and the standard library's logging is what lets the scrubber see all of it.
Scrubbing at the point of logging (as phyOS's recorder did around ffmpeg errors) was rejected as the only defence: one forgotten call leaks a camera password, so it also happens once, for every record, as it is made.

## Camera URLs

The scrubber is best-effort defence in depth, and its known limits are listed in the docstring of `visionquery.runtime.scrub`.
The camera registry (milestone 1) therefore rejects camera URLs it cannot scrub reliably, for example URLs without a scheme.
