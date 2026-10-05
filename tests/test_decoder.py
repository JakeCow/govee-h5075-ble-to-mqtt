"""Vectors for the packed-payload rules in decoder.py.

The README says the decimal-packing scheme is ported and unverified against a
capture, so the rules that used to hide in the field are pinned here: temp*10
in the high three decimals, humidity*10 in the low three, bit 23 = sign.

Run with the stdlib runner, no pytest and no BLE deps needed:

    PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

import unittest

from govee_hygrometer.decoder import decode_h5075, GOVEE_COMPANY_ID

# bytes 1-3 = 236718 -> 236 / 718 -> 23.6 C, 71.8 %
MEASUREMENT = bytes([0x00, 0x03, 0x9C, 0xAE, 0x5C])


class DecodeTest(unittest.TestCase):
    def test_measurement_record(self):
        got = decode_h5075({GOVEE_COMPANY_ID: MEASUREMENT}, rssi=-70)
        self.assertEqual((got.temperature_c, got.humidity_percent), (23.6, 71.8))
        self.assertEqual(got.battery_percent, 92)
        self.assertEqual(got.rssi, -70)

    def test_negative_temperature_sign_bit(self):
        # 23.6 C, 71.8 % with bit 23 set -> the sign flips, humidity stays.
        signed = bytes([0x83, 0x9C, 0xAE])  # 0x800000 | 236718
        got = decode_h5075({GOVEE_COMPANY_ID: b"\x00" + signed + b"\x5c"})
        self.assertEqual((got.temperature_c, got.humidity_percent), (-23.6, 71.8))

    def test_implausible_temperature_is_rejected(self):
        # 900718 -> 90.0 C: outside the -40..85 band, so no reading is published.
        self.assertIsNone(decode_h5075({GOVEE_COMPANY_ID: bytes([0x00, 0x0D, 0xBE, 0x4E, 0x5C])}))

    def test_packing_split_cannot_produce_100_percent(self):
        # The high/low 3-decimal split caps humidity at 99.9, so the 100.0
        # ceiling in decoder.py is unreachable in practice.
        self.assertEqual(decode_h5075({GOVEE_COMPANY_ID: bytes([0x00, 0x03, 0x9D, 0xC7, 0x5C])}).humidity_percent, 99.9)

    def test_blank_record_looks_like_a_real_reading(self):
        # Trap documented here: an all-zero value decodes as 0.0 C / 0.0 % rather
        # than being dropped, so a zeroed advertisement can publish a plausible
        # freezing-dry reading.
        blank = decode_h5075({GOVEE_COMPANY_ID: bytes(5)})
        self.assertEqual((blank.temperature_c, blank.humidity_percent), (0.0, 0.0))

    def test_battery_out_of_band_keeps_the_reading(self):
        got = decode_h5075({GOVEE_COMPANY_ID: MEASUREMENT[:4] + bytes([200])})
        self.assertEqual((got.temperature_c, got.humidity_percent), (23.6, 71.8))
        self.assertIsNone(got.battery_percent)

    def test_company_prefix_included_by_other_tools(self):
        # ble_monitor-style tools keep the little-endian company prefix in the
        # value; stripping happens by position, and only for >= 7 bytes.
        got = decode_h5075({0x88EC: bytes([0x88, 0xEC]) + MEASUREMENT})
        self.assertEqual((got.temperature_c, got.humidity_percent), (23.6, 71.8))

    def test_non_measurement_record_is_rejected(self):
        self.assertIsNone(decode_h5075({GOVEE_COMPANY_ID: MEASUREMENT[:4]}))  # short
        self.assertIsNone(decode_h5075({GOVEE_COMPANY_ID: b"\x01" + MEASUREMENT[1:]}))  # pad byte
        self.assertIsNone(decode_h5075({0x5134: MEASUREMENT}))  # not Govee
        # A 5-byte value that happens to start 88 EC must not be mistaken for a
        # company prefix.
        self.assertIsNone(decode_h5075({0x88EC: bytes([0x88, 0xEC, 0x01, 0x02, 0x03])}))


if __name__ == "__main__":
    unittest.main()
