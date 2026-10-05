# Govee H5075 BLE -> MQTT collector, container build.
#
# The daemon talks to BlueZ over D-Bus, so the container must mount the HOST's
# /run/dbus and /var/lib/dbus (README's docker run line). Do not start a second
# bluetoothd inside: one BlueZ stack per machine, shared with the host.
#
# Layer order: slow-changing system packages first so rebuilds are cheap; the
# repo COPY (src/, pyproject.toml) sits at the bottom because it changes on
# every code edit.
FROM debian:bookworm-slim

# BlueZ userland + D-Bus (bleak's bluez backend speaks over it); python3 and
# python3-venv come from the distro. Plain -y: Ubuntu 24.04's apt rejects
# --no-recommends ("not understood in combination with the other options"),
# which broke the build on the user's box. Recommends add a few MB; acceptable.
RUN apt-get update && apt-get install -y \
    dbus \
    bluetooth \
    python3 \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Default location for the MAC cache volume (see README's docker run line).
RUN mkdir -p /var/lib/govee-hygrometer

WORKDIR /app

# Deps (bleak, paho-mqtt) rarely change; repo source changes every edit — so
# source is copied AFTER the deps layer and the venv keeps the interpreter
# path identical to the host install.
RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir \
        "bleak>=0.22,<2" "paho-mqtt>=2,<3"

COPY src /app/src
COPY pyproject.toml /app/pyproject.toml

# Sensible container defaults; required MQTT_* values still come from --env.
ENV MQTT_BASE_TOPIC=sensors/govee_h5075 \
    DEVICE_NAME_MATCH=h5075 \
    POLL_INTERVAL=600 \
    SCAN_WINDOW=30 \
    LOG_LEVEL=INFO \
    PYTHONPATH=/app/src

# The daemon never exits on a down broker (fail-soft) and traps SIGTERM to
# publish 'offline', so a plain docker stop degrades gracefully.
ENTRYPOINT ["/app/.venv/bin/python", "-m", "govee_hygrometer.collector"]
