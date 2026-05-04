"""Background check for newer cc-monitor releases on PyPI.

On launch the app fires off an asyncio task that:

  1. Checks ~/.cache/cc-monitor/version-check.json. If the cached
     answer is younger than 24h, use it.
  2. Otherwise GET https://pypi.org/pypi/cc-monitor/json, extract the
     latest non-yanked stable version, cache it, and compare.
  3. If the latest version on PyPI is strictly newer than the running
     version, expose it via Aggregator.update_available so the TUI can
     show a one-shot toast on first paint.

Network failures, malformed payloads, and parser errors are all
swallowed silently — startup never blocks on PyPI being slow / down /
unreachable. The cache file itself is best-effort: corrupt JSON triggers
a re-fetch on next launch instead of a crash.

Privacy note: every uncached call sends one GET to PyPI with the user's
IP and Python's default User-Agent. The 24h cache keeps that to ~1
request per day per machine. Users who prefer no network traffic at all
can disable the check via --no-update-check or the Settings screen.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from . import __version__
from .logger import get_logger

log = get_logger(__name__)

PYPI_URL = "https://pypi.org/pypi/cc-monitor/json"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 1 day


def _cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "cc-monitor" / "version-check.json"


def _parse_version(s: str) -> tuple[int, ...] | None:
    """Best-effort tuple-of-ints parse. None on anything fancy
    (pre-release labels, dev suffixes, etc.) so the caller skips
    comparison rather than crashing on a malformed PyPI entry."""
    try:
        return tuple(int(part) for part in s.split("."))
    except ValueError:
        return None


def _is_newer(remote: str, local: str) -> bool:
    r = _parse_version(remote)
    l = _parse_version(local)
    if r is None or l is None:
        return False
    return r > l


def _load_cache() -> tuple[str, float] | None:
    """Returns (latest_version, fetched_at_epoch) or None on miss /
    corrupt / stale."""
    path = _cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = raw.get("latest")
    fetched = raw.get("fetched_at")
    if not isinstance(version, str) or not isinstance(fetched, (int, float)):
        return None
    if time.time() - fetched > CACHE_TTL_SECONDS:
        return None
    return version, float(fetched)


def _save_cache(version: str) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"latest": version, "fetched_at": time.time()}
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        log.debug("version-check cache save failed: %s", e)


def _fetch_pypi_latest() -> str | None:
    """One blocking HTTP GET. Caller is expected to run this off the
    event loop (asyncio.to_thread). Returns the stable latest version,
    or None on any failure."""
    try:
        req = urllib.request.Request(
            PYPI_URL,
            headers={"User-Agent": f"cc-monitor/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.debug("version-check fetch failed: %s", e)
        return None
    info = payload.get("info") or {}
    latest = info.get("version")
    if isinstance(latest, str) and latest:
        return latest
    return None


async def check_for_update() -> str | None:
    """Returns the version string of a newer release on PyPI, or None
    if no update is available (or the check failed). Never raises."""
    import asyncio

    cached = _load_cache()
    if cached is not None:
        latest, _ = cached
        log.debug("version-check using cached value: %s", latest)
    else:
        latest = await asyncio.to_thread(_fetch_pypi_latest)
        if latest is None:
            return None
        _save_cache(latest)

    if _is_newer(latest, __version__):
        log.info(
            "update available: running %s, PyPI has %s",
            __version__, latest,
        )
        return latest
    return None
