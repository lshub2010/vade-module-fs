# Build from the Vade REPOSITORY ROOT: the module depends on `sdk/python`.
#   docker build -f modules/fs/Dockerfile -t vade/fs:dev .
#
# No packaging step on purpose: the image only needs the two source trees on
# PYTHONPATH plus the two runtime wheels. `pip install .` would drag a build
# backend into an offline builder for no benefit — the module is run as
# `python -m fs_module`, not imported from site-packages.
#
# `/data/workspace` is the ONLY writable area the module will ever touch: both
# FS_DATA_DIR and FS_ROOTS point at it, and the container runs as uid 10001
# with no writable layer outside `/data`. Do not widen FS_ROOTS to `/` — that
# turns the module into an unconfined filesystem client (see README).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/opt/vade-sdk \
    MODULE_KEY_PATH=/data/module.key \
    VADE_DATA_DIR=/data \
    FS_DATA_DIR=/data/workspace \
    FS_ROOTS=/data/workspace

WORKDIR /app

RUN pip install --no-cache-dir "websockets>=13" "cryptography>=42"

COPY sdk/python /opt/vade-sdk
COPY modules/fs/src /app/src

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin vade \
    && mkdir -p /data/workspace && chown -R 10001:10001 /data

USER 10001:10001
VOLUME ["/data"]

# The Noise binding (vade_protocol) is not in this image: mount it at
# /opt/vade-sdk/vade_protocol.py + libvade_protocol.so, or rebuild the image
# with `protocol/core` present. Without it only --standalone works, which is
# intentional: a container that cannot authenticate must say so, not fall back.
STOPSIGNAL SIGTERM

# ENTRYPOINT, not just CMD: `docker run vade/fs:dev --standalone fs_list ...`
# has to append to `python -m fs_module`, not replace it (a bare CMD makes
# docker exec `--standalone` as a binary).
ENTRYPOINT ["python", "-m", "fs_module"]
CMD ["--core", "wss://core:8443"]
