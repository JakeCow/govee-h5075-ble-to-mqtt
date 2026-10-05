# Govee H5075 BLE → Home Assistant MQTT

Home Assistant sensors for a Govee H5075 Bluetooth hygrometer: temperature, humidity, battery,
signal. The daemon scans the sensor over Bluetooth and publishes Home Assistant-style MQTT.

[comment]: # (mac-discovery: This daemon uses D-Bus/BlueZ only, no bluetoothctl. One scan per poll cycle,
advertise timeout controlled by SCAN_WINDOW; retries with SCAN_BACKOFF; MAC latched per device name.
Keep MAC_CACHE_DIR on a stable volume or cache/machine-id.)

## What you need

- A Govee H5075 hygrometer (H5072 works too, but set `DEVICE_NAME_MATCH=h5072`).
- A Linux host that already runs Bluetooth + D-Bus. The container adds no second Bluetooth stack;
  it borrows the host's.
- Docker.
- An MQTT broker on the **same LAN** as the sensor: Mosquitto, or Home Assistant's own MQTT broker.
- Home Assistant on that LAN.
- The sensor awake. H5075s stop advertising a few minutes after their last use, so an idle sensor
  reads offline and gets retried on the next cycle.

Nothing to install on the host — the image carries its own Python and dependencies.

## 1. Build

```bash
git clone http://git.cats.fish/cat/govee-hygrometer.git
cd govee-hygrometer
docker build -t govee-hygrometer .
```

## 2. Fill in `.env`

```bash
cp .env.example .env
```

One `KEY=value` per line. Quotes are not needed, and `# comments` cannot follow a value.

Set these four at minimum:

```bash
MQTT_HOST=your-broker.local
MQTT_PORT=18883
MQTT_USERNAME=govee
MQTT_PASSWORD=keepert7-...
```

Credentials are read when you create the container, not when it starts — after changing them, use a
fresh `docker run`, not `docker kill`.

## 3. The broker account

Mosquitto refuses an empty password file, and the container must log in. Put the account file and
config drop-in under `/etc` (or `/opt`), not the repo:

```bash
sudo touch /etc/mosquitto/passwd
sudo chgrp mosquitto /etc/mosquitto/passwd
sudo chmod 0640 /etc/mosquitto/passwd
sudo mosquitto_passwd -c /etc/mosquitto/passwd govee
printf 'password_conf=%s\n' /etc/mosquitto/passwd | sudo tee /etc/mosquitto/conf.d/govee.conf
sudo chgrp mosquitto /etc/mosquitto/conf.d/govee.conf
sudo chmod 0640 /etc/mosquitto/conf.d/govee.conf
```

A root-owned unreadable file makes Mosquitto exit; a dropped-in file **overrides** the main config,
it does not merge into it.

Home Assistant ships its own broker instead — then skip the block above and use
`MQTT_PORT=9001` (`9443` for `wss`), `MQTT_TRANSPORT=ws`, and a long-lived access token as
`MQTT_PASSWORD`.

Check the credentials before starting the daemon:

```bash
sudo docker run --network=host --env-file .env --rm govee-hygrometer --probe-mqtt
```

`configured: acked` means the broker accepted it. `no CONNACK received` means auth failed.
A "Connection reset by peer" can also mean the daemon spoke MQTT v3.1.1 to a broker that only
speaks v3.1.0 (the broker log says `protocol error`, usually twice per attempt, once per retry).

## 4. Start it

```bash
sudo docker run \
  --name govee-hygrometer \
  --network=host \
  --env-file .env \
  -v /var/lib/govee-hygrometer:/var/lib/govee-hygrometer \
  -v /run/dbus:/run/dbus:ro \
  -v /var/lib/dbus:/var/lib/dbus:ro \
  govee-hygrometer
```

- `--network=host`: reach the LAN broker and the sensor from inside the container.
- the named volume: remembers the sensor across restarts.
- `/run/dbus` + `/var/lib/dbus`: borrow the host's Bluetooth stack, and keep the container's machine
  id stable so the cached identity survives.

On Ubuntu (AppArmor confines D-Bus), add `--security-opt apparmor=unconfined`. If a container starts
inside `/dev`, it will hit a tmpfs over `/dev/shm`; keep it outside `/dev`, or remount
`-o tmpfs,/dev/shm`.

## 5. Home Assistant

1. Configure an MQTT integration in HA pointing at your broker: same host, port, and account.
2. The daemon announces itself as a Home Assistant device, so the hygrometer appears on its own —
   nothing to paste.
3. Expect temperature, humidity, battery, signal, plus an availability switch that reports
   `unavailable` while the sensor is asleep.
4. If you re-pair the sensor or change `DEVICE_SLUG`, the old entities stay orphaned; delete them.

Set `DEVICE_SLUG` before the first run if you care about the URLs in HA:

```bash
DEVICE_SLUG=aquarium
```

Leave it unset to use the BLE device name. Set it when you re-pair or rename the sensor — otherwise
the MAC is baked into the HA paths, the new MAC creates a second device, and your dashboard items
go orphaned.

## Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `MQTT_HOST` `MQTT_PORT` `MQTT_USERNAME` `MQTT_PASSWORD` | — | broker; port follows the transport (18883 raw, 9001 ws, 9443 wss) |
| `MQTT_TRANSPORT` | `raw` | `raw` / `ws` / `wss`; HA's built-in broker needs `ws` |
| `MQTT_TLS_*` | — | certificates and proxy port for `wss` (see `.env.example`) |
| `DEVICE_SLUG` | BLE name | HA path/URL name; set it if the sensor can be re-paired |
| `DEVICE_NAME_MATCH` | `h5075` | which BLE name to follow |
| `DEVICE_ADDRESS` | — | pin one MAC (`AA:BB:...`); disables auto-discovery and self-healing |
| `POLL_INTERVAL` | `600` | seconds between reads; one scan then sleeps this long |
| `SCAN_WINDOW` | `30` | seconds one scan waits for the sensor to advertise itself |
| `RETRIES` `SCAN_BACKOFF` `RETRY_DELAY` | `4` `2` `5` | scan attempts per cycle, and the delays between them |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every scan outcome |
| `MQTT_BASE_TOPIC` | `sensors/<slug>` | topic prefix |
| `MQTT_DISCOVERY_PREFIX` | `ha` | where the Home Assistant announcement goes |
| `MAC_CACHE_DIR` | `/var/lib/govee-hygrometer` | identity cache; keep it inside the mounted volume |

A cycle costs `POLL_INTERVAL` plus the scan, so ~10 min is really ~10 min 5 s when the sensor answers;
a dormant sensor runs the full retry ladder first (~12.6 min). `POLL_INTERVAL=120` is about the floor;
going lower without `SCAN_WINDOW` ≥ ~2× an advertisement gap makes Home Assistant flicker
`unavailable` every cycle.

## Optional: sanity tests

`tests/` covers the payload decoder, the config validation, and the identity cache. From a source
clone: `PYTHONPATH=src python3 -m unittest discover -s tests`.
