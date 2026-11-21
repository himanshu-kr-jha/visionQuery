# No credentials in a process's argv

No VisionQuery process starts another with a credential on its command line.
A command line is readable by every user of the host: `ps` and `/proc/<pid>/cmdline` show it, unless the host mounts `/proc` with `hidepid`, and a container's processes are processes of the host.
A password there is a password published to everyone with a shell on the machine, whatever the container's own isolation.

The credential that matters most is the MediaMTX `visionquery-reader` password (ADR 0015).
It can read every Camera's live video, so exposing it is the exposure of Visual Data that ADR 0002 closes the Deployment against.

## Decision

A credential reaches a child process by a channel only that process's user can read: a file in a directory of mode 0700, itself mode 0600, created exclusively, on the process's own tmpfs, removed when the child ends.
`visionquery.runtime.private_input.private_ffmpeg_input` is the one way to do it for ffmpeg, for every process that reads a URL with credentials (the recorder today, the Perception Workers next): it writes an ffconcat list holding the URL and the input's options (`rtsp_transport`, `timeout`) and returns the arguments `-f concat -safe 0 -protocol_whitelist file,rtsp,rtsps,tcp,tls,crypto -i <file>`.
ffmpeg opens the URL itself, so the command line names the file and never the URL, user or host.
The protocol whitelist lets the list lead nowhere but the one RTSP source.

The recorder tests prove it three ways: no command line the recorder builds holds the password or the user; a `ps` and `/proc/<pid>/cmdline` read while ffmpeg reads a real MediaMTX camera hold neither; and the file's options take effect on a live camera (TCP, with ffmpeg allowed nothing else, and the silence timeout that makes it give up on a camera that stopped sending).

A new process that must pass a secret to a child uses the same kind of channel, or the child's environment, which only its own user can read; it never adds the secret to argv.

## Considered Options

Having MediaMTX trust readers by network (a Compose network with a fixed subnet and `ips:` on `visionquery-reader`) was rejected: it amends ADR 0015, ties the Deployment's security to its network layout, and gives nothing against a process inside that network.
Accepting the risk and requiring `hidepid=2` on the host's `/proc` was rejected: it moves a security property from the code, where tests keep it, to a host setting the Deployment cannot check.
ffmpeg's `-/i <file>` (an option read from a file) was tried and is refused by ffmpeg 9.
A credential in the child's environment was rejected for ffmpeg because it reads none: its input is a URL, and a URL holds the credential.

## Consequences

Reading a camera costs one small file per ffmpeg, written on the tmpfs, and the list, not argv, is where an RTSP source's options now go: an option that needs quoting is refused rather than guessed at, since ffmpeg takes a quoted option literally and silently ignores it.
A recorder killed before it can clean up leaves its file only its own user can read, on a tmpfs that holds nothing once the container stops.
ffmpeg still repeats the URL in its error messages, so whatever logs them scrubs them (ADR 0012).

Publishing is not covered: the dev profile's fake cameras give ffmpeg their publish password in its output URL, which has no input file to carry it.
It is development-only, publishes with a per-camera user that can publish to its own path and nothing else (ADR 0015), and stays as it is until it has a channel of its own.
