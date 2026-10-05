"""Probe an MQTT broker for working credentials — stdlib only, no deps.

Works with any python3: host python, the project venv, or the image.

    python3 probe-mqtt.py                          # reads MQTT_* env
    python3 probe-mqtt.py mini.lan 1883            # + creds from env
    python3 probe-mqtt.py mini.lan 1883 user pass  # explicit creds
    python3 probe-mqtt.py --port 9001              # override port only

For each credential set (configured, then anonymous) it prints one verdict:

    acked                  -> use these creds; exit 0
    CONNACK rc=N           -> broker answered and denied (N: 1 bad user/pass,
                              2 unauthorized, 5 connection refused...)
    no CONNACK             -> the endpoint never answered: not a raw MQTT socket
                              (TLS port / HA web port / websocket endpoint), or
                              the broker hung up before replying
    connect error          -> address not resolvable or socket-level failure

Exits 0 if any set acked, 1 otherwise.

The CONNECT is sent as MQTT protocol version 3 (3.1.1), which both old and new
Mosquitto accept.  Sending 0 instead makes Mosquitto 2.x log 'protocol error'
and close the socket *before* it checks the account, so a credential problem
showed up as 'no CONNACK' and pointed at the port instead of the password.
"""

import argparse
import struct
from socket import socket

USAGE = "usage: probe-mqtt.py [host [port [user pass]]]"


def _fields(*vals):
    out = b""
    for v in vals:
        if v is not None:
            out += b"\x00" + struct.pack(">H", len(v)) + v
    return out


def conack(host: str, port: int, user=None, pw=None):
    s = socket()
    s.settimeout(8)
    s.connect((host, port))
    flags = (0x02 if user is not None else 0) | (0x01 if pw is not None else 0)
    # Protocol version 3 = MQTT 3.1.1; see the module docstring for why 0 is wrong.
    body = bytes([flags, 3]) + _fields(b"probe", user, pw)
    s.sendall(bytes([0x40 | flags, len(body)]) + body)
    r = s.recv(8)
    s.close()
    if len(r) >= 2 and r[0] == 0xC0:
        return r[1]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("pos", nargs="*")
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    a = ap.parse_args()
    if a.pos and not a.host:
        a.host = a.pos[0]
        if len(a.pos) > 1:
            try:
                a.port = int(a.pos[1])
            except ValueError:
                print(USAGE)
                return 2
        if len(a.pos) >= 4:
            a.user, a.password = a.pos[2], a.pos[3]
    host = a.host or __import__("os").environ.get("MQTT_HOST", "")
    port = a.port or int(__import__("os").environ.get("MQTT_PORT", "1883"))
    user = a.user or __import__("os").environ.get("MQTT_USERNAME")
    pw = a.password or __import__("os").environ.get("MQTT_PASSWORD")
    user = user.encode() if user else None
    pw = pw.encode() if pw else None
    if not host:
        print("MQTT_HOST not set and no host given.\n" + USAGE)
        return 2
    seen = set()
    for label, u, p in [("configured", user, pw), ("anonymous", None, None)]:
        if (u or "", p or "") in seen:
            continue
        seen.add((u or "", p or ""))
        try:
            rc = conack(host, port, u, p)
        except Exception as exc:
            print(f"{label}: connect error: {exc}")
            continue
        if rc == 0:
            print(f"{label}: acked -> use these creds in .env (user={u!r} pass={p!r})")
            return 0
        if rc is None:
            print(
                f"{label}: no CONNACK -> {host}:{port} is not a raw MQTT socket "
                "(websocket / TLS / web port) or the broker closed the connection"
            )
        else:
            print(f"{label}: CONNACK rc={rc} -> creds or broker deny it")
    print("\nNothing acked: check the account exists on the broker and "
          "MQTT_PORT is a raw MQTT socket (Mosquitto default 1883).")
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
