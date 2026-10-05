"""Startup-validation tests for config.py.

Every case below is a value someone can plausibly type in `.env`, so the point
under test is which error message you get back (or, for the accepted ones, that
the loader normalised what docker/systemd/shell hand over differently).
"""

import os
import unittest

from govee_hygrometer.config import Config

BASE = {
    "MQTT_HOST": "10.0.0.1",
    "MQTT_USERNAME": "govee_collector",
    "MQTT_PASSWORD": "long-random-pw",
}


class Accepted(unittest.TestCase):
    def load(self, **over):
        old = dict(os.environ)
        os.environ.update({**BASE, **over})
        try:
            return Config.from_environment()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_defaults(self):
        cfg = self.load()
        self.assertEqual(cfg.mqtt_port, 1883)
        self.assertEqual(cfg.mqtt_transport, "raw")
        self.assertEqual(cfg.device_slug, "govee_h5075")
        self.assertEqual(cfg.poll_interval, 600.0)

    def test_docker_env_file_quotes_are_unwrapped(self):
        # docker --env-file keeps the quotes literal; a shell strips them.
        cfg = self.load(**{"DEVICE_NAME": '"Govee H5075"'})
        self.assertEqual(cfg.device_name, "Govee H5075")

    def test_websocket_transport(self):
        cfg = self.load(MQTT_TRANSPORT="WS", MQTT_PORT="9001")
        self.assertEqual(cfg.mqtt_transport, "ws")
        self.assertEqual(cfg.mqtt_port, 9001)

    def test_dashed_address_normalises_and_drives_the_slug(self):
        cfg = self.load(DEVICE_ADDRESS="aa-bb-cc-dd-ee-ff")
        self.assertEqual(cfg.device_address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(cfg.device_slug, "govee_h5075_eeff")

    def test_topic_path_with_slash(self):
        cfg = self.load(MQTT_BASE_TOPIC="/sensors/govee_h5075/")
        self.assertEqual(cfg.mqtt_base_topic, "sensors/govee_h5075")

    def test_discovery_prefix_slashes_are_stripped(self):
        cfg = self.load(MQTT_DISCOVERY_PREFIX="/homeassistant/")
        self.assertEqual(cfg.mqtt_discovery_prefix, "homeassistant")


class Rejected(unittest.TestCase):
    def expect(self, message, **over):
        old = dict(os.environ)
        os.environ.update({**BASE, **over})
        try:
            with self.assertRaises(ValueError) as raised:
                Config.from_environment()
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertIn(message, str(raised.exception))

    def test_transport_garbage_is_refused_not_treated_as_raw(self):
        # The classic docker trap: a trailing '# note' survives in the value.
        self.expect("MQTT_TRANSPORT", **{"MQTT_TRANSPORT": "raw  # raw = plain socket"})

    def test_log_level_comment_survives_in_the_value(self):
        self.expect("LOG_LEVEL", **{"LOG_LEVEL": "INFO  # DEBUG | INFO"})

    def test_port_out_of_band(self):
        self.expect("MQTT_PORT", MQTT_PORT="70000")

    def test_non_integer_port(self):
        self.expect("integers", MQTT_PORT="1883/tcp")

    def test_negative_retries(self):
        self.expect("RETRIES", RETRIES="-1")

    def test_topic_with_space(self):
        self.expect("MQTT_BASE_TOPIC", MQTT_BASE_TOPIC="sensors/govee h5075")

    def test_slug_that_is_not_a_topic_fragment(self):
        self.expect("DEVICE_SLUG", DEVICE_SLUG="Govee H5075")

    def test_interval_must_be_a_number(self):
        self.expect("POLL_INTERVAL", POLL_INTERVAL="10min")


if __name__ == "__main__":
    unittest.main()
