"""Persist the MAC of the sensor we last latched onto.

Auto-discovery (and the self-heal re-latch) needs the chosen MAC to survive
container restarts so Home Assistant keeps one stable device. The cache lives
in a single-line file inside a mounted volume directory; writes are atomic
(tmp + rename) so a reader never sees a torn file, and a corrupt/invalid cache
is ignored rather than trusted.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


DEFAULT_CACHE_DIR = Path("/var/lib/govee-hygrometer")
MAC_RE = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")


def _normalise(value: str) -> str:
    return value.replace("-", ":").upper()


def cache_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / "mac"


def read(cache_dir: Path = DEFAULT_CACHE_DIR) -> str | None:
    """Return the cached MAC (upper-case colon form) or None if absent/corrupt.

    Any IO error or a value that is not a plausible MAC yields None, so a
    damaged volume falls back to plain name-based auto-discovery.
    """

    try:
        raw = cache_path(cache_dir).read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(f"mac cache unreadable: {exc}") from exc

    candidate = _normalise(raw.strip())
    if MAC_RE.fullmatch(candidate):
        return candidate
    return None


def write(mac: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
    """Atomically store `mac` (create parent dirs; corrupt input ignored)."""

    candidate = _normalise(mac)
    if not MAC_RE.fullmatch(candidate):
        return

    directory = cache_path(cache_dir).parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="mac.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(candidate + "\n")
        os.replace(tmp, cache_path(cache_dir))
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise OSError(f"mac cache write failed: {exc}") from exc


def clear(cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
    """Remove the cached MAC (no error when absent)."""

    try:
        cache_path(cache_dir).unlink(missing_ok=True)
    except OSError as exc:
        raise OSError(f"mac cache clear failed: {exc}") from exc
