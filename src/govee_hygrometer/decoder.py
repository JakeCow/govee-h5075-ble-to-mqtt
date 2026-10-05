"""Decoder for Govee H5075 BLE manufacturer advertisements.

The H5075 advertises manufacturer data under company identifier 0xEC88.
Bleak returns the value without the two-byte company identifier; some tools
include those bytes, so both representations are accepted here.

Payload layout (H5072/H5075; reference: Christian-B/ble_monitor govee.py):
    byte 0   : padding/sign byte (0x00 for the measurement record)
    bytes 1-3: big-endian 24-bit decimal-packed value: the top 3 decimal
               digits hold temperature*10 and the low 3 decimal digits hold
               humidity*10; bit 23 is the sign bit for negative temperatures
    byte 4   : battery percentage
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping


GOVEE_COMPANY_ID = 0xEC88
GOVEE_COMPANY_ID_SWAPPED = 0x88EC


@dataclass(frozen=True)
class Reading:
    temperature_c: float
    humidity_percent: float
    battery_percent: int | None
    rssi: int | None
    seen_at: datetime

    def as_state(self) -> dict[str, float | int | str | None]:
        return {
            "temperature_c": self.temperature_c,
            "humidity_percent": self.humidity_percent,
            "battery_percent": self.battery_percent,
            "rssi": self.rssi,
            "last_seen": self.seen_at.isoformat(),
        }


def decode_h5075(
    manufacturer_data: Mapping[int, bytes],
    *,
    rssi: int | None = None,
    seen_at: datetime | None = None,
) -> Reading | None:
    """Decode one Bleak manufacturer-data mapping.

    Returns None if the packet is not a Govee measurement record or its values
    fall outside plausible ranges.
    """

    raw = manufacturer_data.get(GOVEE_COMPANY_ID)
    if raw is not None:
        payload = bytes(raw)
    else:
        raw = manufacturer_data.get(GOVEE_COMPANY_ID_SWAPPED)
        if raw is None:
            return None
        payload = bytes(raw)

    # Some tools keep the little-endian company prefix (88 EC) inside the value;
    # strip it by position (never by content-matching data that may start 88 EC).
    if len(payload) >= 7 and payload[:2] == bytes((0x88, 0xEC)):
        payload = payload[2:]

    if len(payload) < 5 or payload[0] != 0x00:
        return None

    # Govee decimal-packs the reading in a big-endian 24-bit field: temp*10 in
    # the high 3 digits, humidity*10 in the low 3 digits; bit 23 carries the sign
    # for negative temperatures (ble_monitor govee.py scheme).
    packed = int.from_bytes(payload[1:4], byteorder="big")
    sign = -1.0 if packed & 0x800000 else 1.0
    magnitude = packed & 0x7FFFFF
    temperature_c = sign * (magnitude // 1000) / 10.0
    humidity_percent = (magnitude % 1000) / 10.0
    battery_raw = int(payload[4])
    battery_percent = battery_raw if 0 <= battery_raw <= 100 else None

    if not -40.0 <= temperature_c <= 85.0:
        return None
    if not 0.0 <= humidity_percent <= 100.0:
        return None

    return Reading(
        temperature_c=round(temperature_c, 2),
        humidity_percent=round(humidity_percent, 1),
        battery_percent=battery_percent,
        rssi=rssi,
        seen_at=seen_at or datetime.now(timezone.utc),
    )
