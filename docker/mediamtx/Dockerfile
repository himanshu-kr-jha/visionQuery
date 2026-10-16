# MediaMTX, unchanged, on Alpine.
#
# The official image is the bare binary on `scratch`, so a healthcheck has nothing to run;
# this adds a shell and BusyBox's wget around the same binary, and runs it as a user of its
# own. It is also where a patch would go if RTMP cameras are ever needed (see mediamtx.yml).

FROM bluenviron/mediamtx:1.21.1@sha256:5ce2a948eb68df06ce2e13870db8df8e30d516ac4dc40e04bfe8aee3bdf7be40 AS mediamtx

FROM alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx
RUN addgroup -S -g 10001 mediamtx && adduser -S -D -H -u 10001 -G mediamtx mediamtx
USER mediamtx:mediamtx
WORKDIR /tmp
ENTRYPOINT ["mediamtx"]
CMD ["/etc/mediamtx/mediamtx.yml"]
