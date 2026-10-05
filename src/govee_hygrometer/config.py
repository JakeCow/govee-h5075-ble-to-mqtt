"""Environment-backed collector configuration."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

LOG_LEVELS = {
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
}

# 'raw' = plain MQTT socket (Mosquitto default 1883); 'ws' = websocket endpoint
# (HA's built-in integration: ws 9001, wss 9443).
MQTT_TRANSPORTS = {"raw", "ws"}


def _env(name: str, default: str | None = None) -> str | None:
    """Read an env var, tolerating quotes a loader left in the value.

    docker --env-file keeps surrounding quotes as literal characters (unlike a
    shell), so DEVICE_NAME="Govee H5075" arrives as '\"Govee H5075\"' and Home
    Assistant shows the quotes.  A systemd EnvironmentFile or a sourced shell
    file strips them, so unwrap one matched pair of outer quotes either way.
    """
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        value = value[1:-1].strip()
    return value


def _positive_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw or "")
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive, finite number")
    return value


@dataclass(frozen=True)
class Config:
    device_address: str
    device_name: str
    device_name_match: str
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_base_topic: str
    mqtt_discovery_prefix: str
    device_slug: str
    poll_interval: float
    scan_window: float
    # BlueZ's advertisement monitor is not reliably re-served when it is
    # rebuilt between attempts (the device must redisplay its LE ADD, which
    # Govee sensors do on their own cadence); retry back-to-back just re-logs
    # 'Auto-discovered' per attempt, so the NO_DATA path holds off longer.
    retries: int
    retry_delay: float
    between_attempts: float
    log_level: str
    # 'raw' = plain MQTT socket (Mosquitto default 1883); 'ws' = WebSocket
    # endpoint (HA's built-in MQTT integration: ws 9001, wss 9443).
    mqtt_transport: str = "raw"
    mqtt_tls_enabled: bool = False
    mqtt_tls_ca_cert: str | None = None
    mqtt_tls_client_cert: str | None = None
    mqtt_tls_client_key: str | None = None
    mqtt_tls_insecure: bool = False
    mac_cache_dir: Path = Path("/var/lib/govee-hygrometer")

    @classmethod
    def from_environment(cls) -> "Config":
        # DEVICE_ADDRESS is optional: leave it unset to auto-discover the Govee
        # sensor by BLE name + reading signature. When set it must be a MAC and
        # is matched exactly.
        address = (_env("DEVICE_ADDRESS") or "").upper().replace("-", ":")
        if address and not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
            raise ValueError("DEVICE_ADDRESS must be a Bluetooth MAC address")

        mqtt_host = (_env("MQTT_HOST") or "").strip()
        if not mqtt_host:
            raise ValueError("MQTT_HOST is required (LAN address of your MQTT server)")
        try:
            mqtt_port = int(_env("MQTT_PORT", "1883") or "1883")
            retries = int(_env("RETRIES", "4") or "4")
        except ValueError as exc:
            raise ValueError("MQTT_PORT and RETRIES must be integers") from exc
        if not 1 <= mqtt_port <= 65535:
            raise ValueError("MQTT_PORT must be between 1 and 65535")
        if retries < 0:
            raise ValueError("RETRIES cannot be negative")
        username = _env("MQTT_USERNAME")
        password = _env("MQTT_PASSWORD")
        if not username or not password:
            raise ValueError("MQTT_USERNAME and MQTT_PASSWORD are required")

        base_topic = (_env("MQTT_BASE_TOPIC") or "sensors/govee_h5075").strip("/")
        if not _valid_topic_path(base_topic):
            raise ValueError(
                "MQTT_BASE_TOPIC may only contain letters, digits, _ and / "
                "(MQTT topic characters)"
            )

        hex_only = address.replace(":", "")
        # With auto-discovery the MAC is not known up-front, so a slug derived
        # from it is only possible when DEVICE_ADDRESS is set. Otherwise fall
        # back to a fixed default (overridable via DEVICE_SLUG).
        default_slug = (
            f"govee_h5075_{hex_only[-4:].lower()}"
            if hex_only
            else "govee_h5075"
        )
        device_slug = (_env("DEVICE_SLUG") or default_slug).strip().lower()
        if not _valid_topic_path(device_slug):
            raise ValueError(
                "DEVICE_SLUG may only contain letters, digits and _ "
                "(it becomes part of MQTT topic paths)"
            )

        discovery_prefix = (
            _env("MQTT_DISCOVERY_PREFIX", "homeassistant") or "homeassistant"
        ).strip("/")
        if not _valid_topic_path(discovery_prefix):
            raise ValueError(
                "MQTT_DISCOVERY_PREFIX may only contain letters, digits, _ and / "
                "(MQTT topic characters)"
            )

        log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
        if log_level not in LOG_LEVELS:
            raise ValueError("LOG_LEVEL must be one of DEBUG INFO WARNING ERROR CRITICAL")

        # An unrecognised value is refused rather than quietly treated as 'raw',
        # which would turn a websocket-only broker into a lost-CONNACK mystery.
        transport = (_env("MQTT_TRANSPORT") or "raw").strip().lower()
        if transport not in MQTT_TRANSPORTS:
            raise ValueError(
                "MQTT_TRANSPORT must be 'raw' (plain MQTT socket, e.g. Mosquitto "
                "1883) or 'ws' (websocket endpoint, e.g. HA's 9001/9443)"
            )

        tls_enabled = (_env("MQTT_TLS_ENABLED") or "").upper() in {"1", "TRUE", "YES", "ON"}
        ca_cert = _env("MQTT_TLS_CA_CERT")
        client_cert = _env("MQTT_TLS_CLIENT_CERT")
        client_key = _env("MQTT_TLS_CLIENT_KEY")
        if tls_enabled and not ca_cert:
            raise ValueError("MQTT_TLS_CA_CERT is required when MQTT_TLS_ENABLED is set")

        return cls(
            device_address=address,
            device_name=_env("DEVICE_NAME", "Govee H5075") or "Govee H5075",
            # Govee BLE names look like 'GVH5075_105F' (model token first), so
            # the default match is the model string 'h5075', not 'Govee'.
            device_name_match=(_env("DEVICE_NAME_MATCH", "h5075") or "h5075").strip(),
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            mqtt_username=username,
            mqtt_password=password,
            mqtt_base_topic=base_topic,
            mqtt_discovery_prefix=discovery_prefix,
                mqtt_transport=transport,
            device_slug=device_slug,
            poll_interval=_positive_float("POLL_INTERVAL", 600),
            scan_window=_positive_float("SCAN_WINDOW", 30),
            retries=retries,
            retry_delay=_positive_float("RETRY_DELAY", 5),
            # BlueZ must tear down and reopen its advertisement monitor between
            # attempts (a kept-open monitor never replays the LE ADD), so each
            # attempt waits for the device to redisplay its announcement.
            between_attempts=_positive_float("SCAN_BACKOFF", 2.0),
            log_level=log_level,
            mqtt_tls_enabled=tls_enabled,
            mqtt_tls_ca_cert=ca_cert,
            mqtt_tls_client_cert=client_cert,
            mqtt_tls_client_key=client_key,
            mqtt_tls_insecure=(_env("MQTT_TLS_INSECURE") or "").upper() in {"1", "TRUE", "YES", "ON"},
            # MAC cache lives in a mounted volume dir so restarts keep one
            # stable Home Assistant device (see mac_cache.py, README docker run).
            mac_cache_dir=Path(_env("MAC_CACHE_DIR", "/var/lib/govee-hygrometer") or "/var/lib/govee-hygrometer"),
        )


def _valid_topic_path(value: str) -> bool:
    """True if every path component is a safe MQTT topic fragment.

    MQTT forbids the NUL, space, and the reserved wildcard characters in topic
    names; HA additionally treats '/' as the topic separator, so each component
    must match [A-Za-z0-9_]+.
    """
    return bool(value) and all(
        re.fullmatch(r"[A-Za-z0-9_]+", part) for part in value.split("/")
    )
