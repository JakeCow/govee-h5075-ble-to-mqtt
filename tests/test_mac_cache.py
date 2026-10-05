"""Round-trip tests for the atomic MAC cache in mac_cache.py.

The cache is what keeps one stable Home Assistant device across restarts, so
"write then read must agree" and "damaged volume falls back to discovery" are
pinned here.
"""

import tempfile
import unittest
from pathlib import Path

from govee_hygrometer.mac_cache import read, write, clear, cache_path

MAC = "AA:BB:CC:DD:EE:FF"


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="govee-mac-"))

    def test_roundtrip_and_normalisation(self):
        write("aa-bb-cc-dd-ee-ff", self.dir)
        self.assertEqual(read(self.dir), MAC)
        # A stray trailing newline or spaces are tolerated on read.
        (self.dir / "mac").write_text(MAC + "\n\n")
        self.assertEqual(read(self.dir), MAC)

    def test_bad_input_is_ignored_not_stored(self):
        write("not-a-mac", self.dir)
        self.assertIsNone(read(self.dir))
        self.assertFalse((self.dir / "mac").exists())

    def test_corrupt_volume_yields_none(self):
        (self.dir / "mac").write_text("\x00\x01garbage")
        self.assertIsNone(read(self.dir))

    def test_write_leaves_no_scratch_behind(self):
        write(MAC, self.dir)
        self.assertEqual([p.name for p in self.dir.glob("mac*")], ["mac"])

    def test_clear_is_idempotent(self):
        clear(self.dir)
        clear(self.dir)
        self.assertIsNone(read(self.dir))

    def test_path_is_under_the_cache_dir(self):
        self.assertEqual(cache_path(self.dir), self.dir / "mac")


if __name__ == "__main__":
    unittest.main()
