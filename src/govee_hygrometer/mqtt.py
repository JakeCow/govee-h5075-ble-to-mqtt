"""MQTT publishing and Home Assistant MQTT discovery."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import paho.mqtt.client as mqtt

from .config import Config
from .decoder import Reading


LOG = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(self, config: Config) -> None:
        self.config = config
        transport = getattr(config, "mqtt_transport", "raw") or "raw"
        # paho's 'websockets' transport speaks MQTT over a ws/wss endpoint
        # (HA's built-in MQTT integration: ws :9001, wss :9443). A plain
        # 'tcp' client there can never CONNECT — the endpoint does not take
        # a raw MQTT socket.
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="govee-h5075-collector",
            transport="websockets" if transport == "ws" else "tcp",
        )
        self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        if config.mqtt_tls_enabled:
            self.client.tls_set(
                ca_certs=config.mqtt_tls_ca_cert,
                certfile=config.mqtt_tls_client_cert,
                keyfile=config.mqtt_tls_client_key,
            )
            if config.mqtt_tls_insecure:
                self.client.tls_insecure_set(True)
        # paho 2.x: a broker can reject the CONNECT (bad auth / not really a
        # broker); the socket then closes. Without a callback that rejection is
        # silent, so every later publish() returns rc=4. Log it so the failure
        # is visible instead of looking like a daemon that publishes nothing.
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self._connect_ok = False

    def _on_connect(self, client, _server, _params, result, *_extra) -> None:
        # paho fires on_connect even for a REJECTED CONNECT (bad credentials /
        # a web port): check the CONNACK return code, otherwise a broker that
        # answers 'Not authorized' looks 'acked' and every publish then fails
        # rc=4 while we logged a false success.
        code = getattr(result, "value", result)
        if code == 0:
            self._connect_ok = True
            LOG.debug("MQTT CONNECT acked")
        else:
            LOG.warning(
                "MQTT CONNECT rejected: %s", getattr(result, "message", str(result))
            )

    def _on_disconnect(self, client, _server, _params, result_code, *_extra) -> None:
        # A rejected CONNECT tears down the socket right away; that disconnect
        # is already reported as 'CONNECT rejected', so don't double-log it.
        if result_code != 0 and self._connect_ok:
            LOG.warning("MQTT disconnected unexpectedly rc=%s", result_code)

    async def wait_connected(self, attempt: int = 0) -> bool:
        """Block until CONN_ACK or socket close; True only if CONNECT acked.

        The CONN_ACK is what paho's publish() requires, so without this a
        rejected CONNECT turns every later publish into a silent rc=4.
        """
        deadline = time.time() + 10.0
        while not self._connect_ok and time.time() < deadline:
            # Await (not time.sleep): the caller's event loop may host the
            # broker itself (tests/embedders), and blocking it would starve
            # the CONNACK dispatch that sets _connect_ok.
            await asyncio.sleep(0.05)
        if not self._connect_ok:
            # Distinguish 'broker never answered' from '_on_connect logged a
            # rejection' so the retry log states the actual cause.
            LOG.warning(
                "MQTT CONNECT not acknowledged (attempt %d): no CONNACK within "
                "10s — check credentials and that MQTT_PORT is a raw MQTT socket", attempt + 1
            )
        return self._connect_ok

    @property
    def availability_topic(self) -> str:
        return f"{self.config.mqtt_base_topic}/availability"

    @property
    def state_topic(self) -> str:
        return f"{self.config.mqtt_base_topic}/state"

    async def connect(self, stop_event: asyncio.Event | None = None) -> bool:
        """Connect to the broker, retrying forever with capped backoff.

        Transport is chosen by MQTT_TRANSPORT (default 'raw'): 'raw' needs a
        plain MQTT socket (Mosquitto default 1883); HA's built-in MQTT
        integration instead serves a WebSocket endpoint (ws on 9001, wss on
        9443), where a raw CONNECT gets 'Not authorized' or 'no CONNACK'
        because the endpoint does not speak MQTT over a bare TCP socket.

        paho's ``connect()`` can raise socket errors (and returns an error
        code for broker-level failures); both are treated as a failed
        attempt. A down or badly-configured broker is a runtime condition,
        not a startup one: the loop keeps trying (capped exponential backoff)
        so the always-on service degrades to offline state instead of
        crashing or exit-looping. ``stop_event``, when given, lets a Ctrl-C
        /SIGTERM interrupt the backoff so shutdown stays prompt and clean.
        """
        attempt = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            try:
                rc = self.client.connect(
                    self.config.mqtt_host,
                    self.config.mqtt_port,
                    keepalive=60,
                )
                # paho 2.x dispatches the CONNACK (and fires on_connect) only
                # while its network loop runs, so start the loop right after
                # sending CONNECT and before waiting for the ack.
                self.client.loop_start()
                if rc != 0:
                    LOG.warning("MQTT connect returned rc=%s (attempt %d)", rc, attempt + 1)
                    try:
                        self.client.loop_stop()
                    except Exception:
                        pass
                elif not await self.wait_connected(attempt):
                    # wait_connected() logs the reason (never-acked vs
                    # broker-rejected); just back off here.
                    pass
                else:
                    self._publish_discovery()
                    self.publish_checked(self.availability_topic, "online", retain=True)
                    return True
            except Exception as exc:  # socket / DNS / TLS errors
                LOG.warning("MQTT connect failed (attempt %d): %s", attempt + 1, exc)

            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

            attempt += 1
            delay = min(self.config.retry_delay * (2 ** (attempt - 1)), 300.0)
            LOG.warning("MQTT reconnect backoff: retrying in %.0fs", delay)
            if stop_event is None:
                time.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    def publish_checked(self, topic: str, payload: str, *, retain: bool = False) -> bool:
        """Publish and verify delivery (rc==0) rather than firing and forgetting."""
        info = self.client.publish(topic, payload, 0, retain)
        if info.rc != 0:
            LOG.warning("MQTT publish %s failed rc=%s", topic, info.rc)
            return False
        return True

    def close(self) -> None:
        """Send the final 'offline' marker, then stop the loop cleanly."""
        try:
            info = self.client.publish(self.availability_topic, "offline", 0, True)
            if info.rc != 0:
                LOG.warning("Final MQTT 'offline' publish failed rc=%s", info.rc)
            else:
                # Wait until the message is actually flushed before disconnecting.
                info.wait_for_publish(timeout=5.0)
        except Exception:  # pragma: no cover - defensive shutdown path
            LOG.debug("MQTT shutdown failed", exc_info=True)
        finally:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:  # pragma: no cover
                LOG.debug("MQTT loop stop failed", exc_info=True)

    def publish_state(self, reading: Reading) -> bool:
        return self.publish_checked(
            self.state_topic,
            json.dumps(reading.as_state(), separators=(",", ":")),
            retain=True,
        )

    def publish_online(self) -> bool:
        return self.publish_checked(self.availability_topic, "online", retain=True)

    def publish_unavailable(self) -> bool:
        return self.publish_checked(self.availability_topic, "offline", retain=True)

    def _publish_discovery(self) -> None:
        common = {
            "state_topic": self.state_topic,
            "availability_topic": self.availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": [self.config.device_slug],
                "name": self.config.device_name,
                "manufacturer": "Govee",
                "model": "H5075",
            },
        }
        entities = {
            "temperature": {
                "name": "Temperature",
                "unique_id": f"{self.config.device_slug}_temperature",
                "device_class": "temperature",
                "state_class": "measurement",
                "unit_of_measurement": "°C",
                "value_template": "{{ value_json.temperature_c }}",
            },
            "humidity": {
                "name": "Humidity",
                "unique_id": f"{self.config.device_slug}_humidity",
                "device_class": "humidity",
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "value_template": "{{ value_json.humidity_percent }}",
            },
            "battery": {
                "name": "Battery",
                "unique_id": f"{self.config.device_slug}_battery",
                "device_class": "battery",
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "value_template": "{{ value_json.battery_percent }}",
            },
            "rssi": {
                "name": "Signal strength",
                "unique_id": f"{self.config.device_slug}_rssi",
                "device_class": "signal_strength",
                "state_class": "measurement",
                "unit_of_measurement": "dBm",
                "value_template": "{{ value_json.rssi }}",
                "entity_category": "diagnostic",
            },
        }
        for component, entity in entities.items():
            payload = {**common, **entity}
            topic = (
                f"{self.config.mqtt_discovery_prefix}/sensor/"
                f"{self.config.device_slug}/{component}/config"
            )
            self.client.publish(topic, json.dumps(payload, separators=(",", ":")), 0, True)
