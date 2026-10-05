"""Poll a Govee H5075 BLE advertisement and publish it over MQTT.

Scanning strategy (the container keeps the D-Bus scan latched across polling
cycles so Home Assistant does not see the device flip offline every poll):

- pin mode (explicit DEVICE_ADDRESS): exact MAC match only.
- latched mode (MAC cache present, from a previous run or an earlier latch):
  the cached MAC is an implicit pin AND a name-valid-Govee device is accepted
  as a self-heal candidate, so a re-paired sensor whose MAC drifted re-latches
  instead of going offline forever. The self-heal candidate is only recorded
  when the pinned scan produced no valid reading; a healthy pin never re-latches.
- discover mode (nothing known yet): name match (or unresolved name) plus a
  valid Govee decode, and the winner's MAC is cached so restarts keep one
  stable Home Assistant device.

The scanner object itself is kept open across polling cycles: restarting a
BleakScanner per cycle would reconnect the D-Bus monitor, and BlueZ reports
that as a reset which makes Home Assistant drop the device every cycle.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import signal
import sys
from datetime import datetime, timezone
from enum import Enum

from bleak import BleakScanner, BleakError
from bleak.args.bluez import OrPatternLike
from bleak.assigned_numbers import AdvertisementDataType

from .config import Config
from .decoder import Reading, decode_h5075
from .mac_cache import (
    cache_path,
    read as read_mac,
    write as write_mac,
)
from .mqtt import MqttPublisher


LOG = logging.getLogger(__name__)


class ScanOutcome(Enum):
    """Result of one BLE scan attempt."""

    OK = "ok"
    NO_DATA = "no_data"
    BACKEND_ERROR = "backend_error"


def _normalise_address(value: str) -> str:
    return value.replace("-", ":").upper()


def _name_key(value: str) -> str:
    """Lower-case alphanumerics only, so 'h5075' matches 'GVH5075_105F',
    'Govee H5075', etc. Default DEVICE_NAME_MATCH is the model string 'h5075'
    (Govee BLE names look like 'GVH5075_105F', not 'Govee'), which also keeps
    the H6072 (key 'goveeh6072...') from matching.
    """
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _matches(device, config: Config, *, latched: str | None) -> bool:
    """Decide whether an advertised device is the one we want to read.

    With DEVICE_ADDRESS set this is an exact MAC match (pin mode). In latched
    mode (cached MAC from a previous run or an earlier latch) the cached MAC is
    an implicit pin AND a name-valid-Govee device is accepted as a self-heal
    candidate, but only used when the pinned scan produced no valid reading
    (see the caller). Discover mode (nothing known yet) accepts a name match
    or an unresolved name — the Govee decode in the caller is authoritative
    either way, so an empty name can never accept a foreign device on its own.
    """

    pin = config.device_address or latched
    if pin:
        mac = _normalise_address(device.address)
        if mac == pin:
            return True
        if not config.device_address:
            # Latched mode only: keep a plausible replacement around; a healthy
            # pin never reaches here with a foreign device.
            name = (getattr(device, "name", "") or "").strip()
            if name:
                needle = _name_key(config.device_name_match)
                return needle != "" and needle in _name_key(name)
        return False

    name = (getattr(device, "name", "") or "").strip()
    if name == "":
        return True  # name unresolved; decode check in caller is authoritative
    needle = _name_key(config.device_name_match)
    return needle == "" or needle in _name_key(name)


def _ad_patterns(config: Config) -> list[OrPatternLike] | None:
    """BlueZ advertisement-monitor pattern for the sensor's advertised name.

    The H5075 advertises its name (e.g. 'GVH5075_...') under the COMPLETE_LOCAL
    _NAME type; matching the model token narrows the BlueZ monitor to our
    sensor. Returns None (no filter) when DEVICE_NAME_MATCH is empty or when
    DEVICE_ADDRESS pins a specific MAC — the address itself is not part of the
    payload, so a pin alone cannot use a name pattern.
    """

    if config.device_address:
        return None
    token = "".join(
        ch for ch in config.device_name_match if ch.isalnum()
    ).lower()
    # device_name_match is already the model token in practice; only keep
    # letters/digits because BlueZ patterns match raw advertised bytes.
    if not token:
        return None
    return [(0, int(AdvertisementDataType.COMPLETE_LOCAL_NAME), token.encode())]


class LatchedScanner:
    """One BleakScanner whose lifecycle scan_once drives per attempt.

    BlueZ's advertisement monitor must be torn down and reopened between
    attempts: a monitor kept open across attempts does not replay the LE ADD
    (back-to-back scans then log 'Auto-discovered' but never read the sensor).
    scan_once stops (if open), retargets, and starts a fresh monitor each
    attempt; because the teardown is D-Bus-side, callers space the attempts
    apart so BlueZ's own monitor session has closed before the next start
    (a premature reopen gets org.bluez.Error.InProgress, which is transient
    — treat it as soft, not a BACKEND_ERROR). Callers still own the final
    teardown via stop_if_started().

    The scanner is built with a placeholder callback and rebuilt per scan by
    ``scan_once``: the detection callback lives in each scan's own closure
    (result, heal, latch), and BleakScanner registers callbacks at construction
    time, so a scanner built once for the whole run could never route
    detections into a later scan's state.
    """

    def __init__(self, config: Config, on_detection) -> None:
        bluez: dict[str, list[OrPatternLike]] = {}
        patterns = _ad_patterns(config)
        if patterns:
            bluez["or_patterns"] = patterns
        self.bluez = bluez
        self.on_detection = on_detection
        self.scanner = BleakScanner(detection_callback=on_detection, bluez=bluez)
        self.started = False

    async def ensure_started(self) -> None:
        if not self.started:
            # A previous attempt scheduled a fire-and-forget teardown; wait
            # for it to finish before reopening the monitor, otherwise BlueZ
            # refuses the new session with org.bluez.Error.InProgress.
            stop = getattr(self, "_stop_task", None)
            if stop is not None and not stop.done():
                await asyncio.wait([stop])
            await self.scanner.start()
            self.started = True

    async def stop_if_started(self) -> None:
        if self.started:
            self.started = False
            await self.scanner.stop()

    def stop_if_started_nowait(self) -> None:
        """Synchronous counterpart: schedule the stop and mark closed.

        The D-Bus monitor teardown is fire-and-forget; callers that rebuild
        right after rely on the next start() waiting for the new monitor.
        """
        if self.started:
            self.started = False
            self._stop_task = asyncio.create_task(self.scanner.stop())
            self._stop_task.add_done_callback(self._on_stopped)

    def _on_stopped(self, task) -> None:
        task.result()

    def retarget(self, on_detection) -> None:
        """Rebuild the scanner around a new scan's detection callback."""
        self.on_detection = on_detection
        self.scanner = BleakScanner(detection_callback=on_detection, bluez=self.bluez)


async def scan_once(
    scanner_latch: LatchedScanner,
    config: Config,
    latched: str | None,
    cache_dir,
) -> tuple[ScanOutcome, Reading | None, str | None]:
    """Listen for the configured H5075 until a valid packet is received.

    Returns (outcome, reading, mac). `mac` is set when a sensor was latched or
    self-healed this scan (the MAC to persist); the caller updates the cache
    and the mode. The scanner stays open on success and after NO_DATA (the
    monitor just produced nothing); it is closed on BACKEND_ERROR so the next
    cycle re-opens a fresh monitor.
    """

    result: Reading | None = None
    latched_mac: str | None = None
    heal: Reading | None = None
    heal_mac: str | None = None
    received = asyncio.Event()

    def on_detection(device, advertisement_data) -> None:
        nonlocal result, latched_mac, heal, heal_mac
        if not _matches(device, config, latched=latched):
            return
        reading = decode_h5075(
            advertisement_data.manufacturer_data,
            rssi=advertisement_data.rssi,
            seen_at=datetime.now(timezone.utc),
        )
        if reading is None:
            return
        mac = _normalise_address(device.address)
        if config.device_address:
            if mac == config.device_address:
                result = reading
                received.set()
            return
        if latched:
            if mac == latched:
                result = reading
                received.set()
            elif heal is None:
                # Name-valid Govee device that is not the cached pin: candidate
                # replacement, used only if the pinned scan comes up dry.
                heal = reading
                heal_mac = mac
            return
        # Discover mode: any device that matched the name filter and decoded
        # is our sensor.
        result = reading
        latched_mac = mac
        received.set()
        LOG.info(
            "Auto-discovered sensor at %s (name %r); pin DEVICE_ADDRESS if "
            "this is not your sensor",
            mac,
            getattr(device, "name", "") or "",
        )

    outcome = ScanOutcome.NO_DATA
    try:
        # The scanner registers callbacks at construction time, so point it at
        # this scan's callback before starting it. BlueZ's advertisement
        # monitor must be torn down and reopened between attempts: a monitor
        # kept open across attempts does not replay the LE ADD, and back-to-
        # back scans then only ever log 'Auto-discovered' without ever reading
        # the sensor. So: stop first (if open), retarget, start fresh.
        scanner_latch.stop_if_started_nowait()
        scanner_latch.retarget(on_detection)
        await scanner_latch.ensure_started()
        try:
            await asyncio.wait_for(received.wait(), timeout=config.scan_window)
        except asyncio.TimeoutError:
            pass
    except (BleakError, OSError) as exc:
        if "InProgress" in str(exc):
            # The D-Bus teardown from the previous attempt had not finished
            # when this scan reopened the monitor: BlueZ refuses with
            # org.bluez.Error.InProgress. That is transient, so retry (the
            # caller sleeps between_attempts); do not count it as a hard
            # backend failure, and reopen by leaving the latch stopped.
            await scanner_latch.stop_if_started()
            return ScanOutcome.NO_DATA, None, None
        LOG.warning("BLE scan failed: %s", exc)
        await scanner_latch.stop_if_started()
        return ScanOutcome.BACKEND_ERROR, None, None

    if heal is not None and result is None:
        LOG.warning(
            "Pinned/latched sensor %s produced no reading; self-healing to "
            "name-valid Govee sensor at %s (name %r)",
            latched or config.device_address,
            heal_mac,
            "",
        )
        result, latched_mac = heal, heal_mac

    if result is not None:
        return ScanOutcome.OK, result, latched_mac
    return outcome, None, None


async def run(config: Config) -> int:
    """Run the polling loop. Returns a process exit code.

    A broker that is unreachable never exits the process any more: the
    publisher retries with capped backoff forever, and the loop stays
    responsive to stop signals (fail-soft, Docker-friendly).
    """

    publisher = MqttPublisher(config)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows fallback
            pass
    connected = asyncio.create_task(publisher.connect(stop_event))

    # MAC cache mode for the run: an explicit DEVICE_ADDRESS pin wins; otherwise
    # a cached MAC from a previous run (or an earlier latch) makes an implicit
    # pin plus self-heal candidate; with neither, plain name-based discovery.
    mac_cache_dir = config.mac_cache_dir
    pin = bool(config.device_address)
    latched_mac: str | None = read_mac(mac_cache_dir) if not pin else None
    if latched_mac:
        LOG.info(
            "Using cached MAC %s from %s; a name-valid Govee sensor is accepted as "
            "a self-heal replacement", latched_mac, cache_path(mac_cache_dir),
        )

    scanner_latch = LatchedScanner(config, lambda device, adv: None)
    exit_code = 0
    try:
        while not stop_event.is_set():
            # Broker connection is a first-class runtime concern: never exit the
            # process on a down broker. While it reconnects (capped backoff), mark
            # the device unavailable and keep polling; publishes become no-ops
            # until CONNECT is acked.
            if connected.done():
                if connected.exception() is not None:
                    LOG.error("MQTT connection failed fatally: %s", connected.exception())
                    exit_code = 3
                    break
                connected = asyncio.create_task(publisher.connect(stop_event))

            reading: Reading | None = None
            outcome = ScanOutcome.NO_DATA
            backend_failures = 0
            stopped = False
            for attempt in range(config.retries + 1):
                # Interrupt the scan promptly if a stop signal arrives while it is
                # in flight.
                scan = asyncio.create_task(
                    scan_once(scanner_latch, config, latched_mac, mac_cache_dir)
                )
                stop_task = asyncio.create_task(stop_event.wait())
                done, pending = await asyncio.wait(
                    [scan, stop_task], return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    scan.cancel()
                    for t in pending:
                        if t is not scan:
                            t.cancel()
                    stopped = True
                    break
                stop_task.cancel()
                outcome, reading, mac = scan.result()
                if mac and not config.device_address:
                    # New latch (discover) or self-heal: persist + switch mode.
                    write_mac(mac, mac_cache_dir)
                    latched_mac = mac
                if outcome is ScanOutcome.OK:
                    break
                if outcome is ScanOutcome.BACKEND_ERROR:
                    backend_failures += 1
                if outcome is ScanOutcome.NO_DATA:
                    # Give BlueZ time to re-emit the announcement after the
                    # monitor is torn down and reopened; back-to-back scans
                    # never see the LE ADD again.
                    await asyncio.sleep(config.between_attempts)
                elif attempt < config.retries:
                    await asyncio.sleep(config.retry_delay)
            if stopped:
                break

            if outcome is ScanOutcome.OK:
                assert reading is not None
                LOG.info(
                    "Temperature %.1f °C, humidity %.1f %%, battery %s, RSSI %s",
                    reading.temperature_c,
                    reading.humidity_percent,
                    f"{reading.battery_percent}%"
                    if reading.battery_percent is not None
                    else "unknown",
                    reading.rssi if reading.rssi is not None else "unknown",
                )
                if not publisher.publish_state(reading):
                    publisher.publish_unavailable()
                else:
                    publisher.publish_online()
            elif outcome is ScanOutcome.NO_DATA:
                LOG.warning("No valid H5075 advertisement received; marking unavailable")
                publisher.publish_unavailable()
            else:  # ScanOutcome.BACKEND_ERROR
                LOG.warning(
                    "No reading received and the BLE backend failed %d times; "
                    "marking unavailable", backend_failures,
                )
                publisher.publish_unavailable()

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.poll_interval)
            except asyncio.TimeoutError:
                pass

            # On stop, make sure HA sees the device as unavailable.
            if stop_event.is_set():
                publisher.publish_unavailable()
    finally:
        scanner_latch.started = False
        await scanner_latch.stop_if_started()
        publisher.close()
    return exit_code


async def probe_credentials(config: Config) -> int:
    """One-shot credential probe for the MQTT broker (diagnostic mode).

    Tries the configured credentials, then anonymous. For each it reports one
    of: acked / CONNACK rejected / socket error, so the config fix is obvious:
    creds, or wrong port (web/TLS/websocket vs raw). Exits 0 if any acks.
    """

    candidates = [
        ("configured", config.mqtt_username, config.mqtt_password),
        ("anonymous", None, None),
    ]
    seen = set()
    for label, user, password in candidates:
        key = (user or "", password or "")
        if key in seen:
            continue
        seen.add(key)
        cfg = dataclasses.replace(config, mqtt_username=user or "", mqtt_password=password or "")
        pub = MqttPublisher(cfg)
        # The publisher retries forever with backoff; cut one attempt here so
        # the probe stays bounded (one CONNECT round-trip per combination).
        connected = asyncio.create_task(pub.connect(None))
        try:
            acked = await asyncio.wait_for(connected, timeout=12)
        except asyncio.TimeoutError:
            connected.cancel()
            acked = False
        try:
            pub.client.loop_stop()
            pub.client.disconnect()
        except Exception:
            pass
        print(f"{label}: {'acked' if acked else 'not acked (see CONNECT lines)'}")
        if acked:
            print(f"\nUse MQTT_USERNAME={user!r} MQTT_PASSWORD={password!r}")
            return 0
    print("\nNothing acked — fix credentials or the port (must be a raw "
          "MQTT socket, not TLS or the web port).")
    return 1


def main() -> None:
    if sys.argv[1:2] == ["--probe-mqtt"]:
        # One-shot diagnostic: try configured creds, then anonymous, print
        # one line per attempt (acked / not acked). Exits 0 on first ack so
        # the user can confirm creds without running the polling loop.
        try:
            config = Config.from_environment()
        except ValueError as exc:
            print(f"govee-hygrometer: invalid configuration: {exc}", file=sys.stderr)
            raise SystemExit(2)
        logging.basicConfig(
            level=getattr(logging, config.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        raise SystemExit(asyncio.run(probe_credentials(config)))
    try:
        config = Config.from_environment()
    except ValueError as exc:
        # Bad/missing config is a startup error, not a runtime failure: exit 2
        # (the systemd unit treats it as non-retryable via
        # RestartPreventionExitCodes) and say what is wrong.
        print(f"govee-hygrometer: invalid configuration: {exc}", file=sys.stderr)
        raise SystemExit(2)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(run(config)) or 0)


if __name__ == "__main__":
    main()
