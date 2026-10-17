# The fake cameras of the `dev` profile: fake_cameras.py, on Python, driving ffmpeg.
#
# ffmpeg is the static build published as mwader/static-ffmpeg, pinned to its version and
# digest like every other image here; only the ffmpeg binary is copied, not ffprobe or the
# documentation. Alpine's own ffmpeg package would be smaller but cannot be pinned: Alpine keeps
# only the latest build of a package, so a pinned version stops installing at its next update.
# The script needs nothing beyond Python's standard library.

FROM mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 AS ffmpeg

FROM python:3.14.8-alpine3.24@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --chmod=0755 fake_cameras.py /usr/local/bin/fake-cameras
# /cache is where prepared samples are kept (a named volume, which takes this ownership).
RUN addgroup -S -g 10001 cameras && adduser -S -D -H -u 10001 -G cameras cameras \
    && mkdir /cache && chown cameras:cameras /cache
USER cameras:cameras
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /tmp
ENTRYPOINT ["fake-cameras"]
CMD ["run"]
