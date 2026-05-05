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
import shutil
import time
import urllib.request
from pathlib import Path

from . import __version__
from .logger import get_logger

log = get_logger(__name__)

PYPI_URL = "https://pypi.org/pypi/cc-monitor/json"
# 1 hour is a reasonable balance: aggressive enough that users notice
# new releases within their working session, gentle enough that we
# don't hammer PyPI on a workflow where the user launches cc-monitor
# every few minutes. 24h was tested and turned out too long for an
# actively-developed package — users who upgraded saw stale "no
# update available" answers from cache predating the new release.
CACHE_TTL_SECONDS = 60 * 60

# Installer probe order — uv first because that's what we recommend in
# the README and it's the most common path forward; pipx second for
# users who prefer it; pip last as a "we tried" fallback (often gets
# blocked by PEP 668 on managed Python distributions, but worth
# offering as a hint rather than refusing the upgrade entirely).
#
# `uv tool install --reinstall` instead of `uv tool upgrade` deliberately:
# `tool upgrade` reads from uv's HTTP-cached Simple Index, which can
# lag PyPI by several minutes after a publish and silently no-op
# ("Nothing to upgrade") even when a newer version is on PyPI. Our
# update probe hits /pypi/<pkg>/json (refreshes within seconds), so
# the modal can fire and the user accepts before uv's index catches
# up. `tool install --reinstall` re-resolves from the network, picks
# up the freshly-published wheel, and overwrites the existing tool
# install. Slightly more work than upgrade but always correct.
# `tool upgrade` itself doesn't accept --refresh as a subcommand
# flag (verified via 'unexpected argument' error in the wild).
#
# pip's analogous mechanism is `--no-cache-dir`, which bypasses the
# wheel cache. pipx doesn't expose a refresh flag directly; users on
# pipx accept the small propagation lag.
_INSTALLER_CANDIDATES: tuple[tuple[str, list[str]], ...] = (
    ("uv", ["uv", "tool", "install", "--reinstall", "cc-monitor"]),
    ("pipx", ["pipx", "upgrade", "cc-monitor"]),
    ("pip", ["pip", "install", "--upgrade", "--no-cache-dir", "cc-monitor"]),
)


def detect_installer() -> tuple[str, list[str]] | None:
    """Pick the first installer that's actually on PATH and return its
    upgrade command. Returns None when no candidate is available — the
    caller falls back to passively telling the user to reinstall.

    Detection is heuristic, not source-of-truth: a user with both `uv`
    and `pipx` on PATH might have installed via pipx but get an `uv tool
    upgrade` suggestion. Best-effort wins here — running `uv tool
    upgrade <pkg>` when the package wasn't installed via uv just errors
    out, which is fine and visible to the user."""
    for name, cmd in _INSTALLER_CANDIDATES:
        if shutil.which(name):
            return (name, cmd)
    return None


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
    """Update the cached PyPI 'latest' value while preserving any
    pending_modal field that may have been set across the same write
    window — they're independent pieces of state and shouldn't clobber
    each other."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_raw(path)
        payload = {"latest": version, "fetched_at": time.time()}
        if "pending_modal" in existing:
            payload["pending_modal"] = existing["pending_modal"]
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        log.debug("version-check cache save failed: %s", e)


def _load_raw(path: Path) -> dict:
    """Best-effort read of the whole cache JSON; returns {} on miss
    or corrupt file. Used by helpers that need to update one field
    without losing the others."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_pending_modal(version: str) -> None:
    """Persist 'this user has an unread Update? notification for
    version X' to disk. Makes the modal robust against the user
    quitting cc-monitor in the ~2-second window between async fetch
    completing and the modal actually rendering."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _load_raw(path)
        data["pending_modal"] = version
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        log.debug("set_pending_modal failed: %s", e)


def clear_pending_modal() -> None:
    """User dismissed the modal — drop the flag so we don't re-pop
    on the next launch. Idempotent: no-op when there's nothing to
    clear (missing file, missing field)."""
    path = _cache_path()
    data = _load_raw(path)
    if "pending_modal" not in data:
        return
    data.pop("pending_modal", None)
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        log.debug("clear_pending_modal failed: %s", e)


def get_pending_modal() -> str | None:
    """Returns the version string of an unread Update? modal from a
    previous launch, or None if there isn't one. Filters out cases
    where the running version has caught up to (or surpassed) the
    pending one — e.g. the user upgraded via some other route — so
    we don't pop a stale modal."""
    path = _cache_path()
    data = _load_raw(path)
    pending = data.get("pending_modal")
    if not isinstance(pending, str):
        return None
    if not _is_newer(pending, __version__):
        # User's running version already meets or exceeds the pending
        # one; clean up the stale flag so it doesn't accumulate.
        clear_pending_modal()
        return None
    return pending


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


def _cache_is_obsolete(cached_latest: str) -> bool:
    """True when our cached "latest version" is no newer than the
    version we're running — which means the user upgraded past the
    cached value and the cache is definitionally stale, regardless of
    its age. Without this guard a user who upgrades from X to X+1
    keeps seeing "no update available" until TTL expires, even when
    PyPI has X+2."""
    cached = _parse_version(cached_latest)
    running = _parse_version(__version__)
    if cached is None or running is None:
        return False
    return running >= cached


async def check_for_update() -> str | None:
    """Returns the version string of a newer release on PyPI, or None
    if no update is available (or the check failed). Never raises."""
    import asyncio

    cached = _load_cache()
    if cached is not None:
        latest, _ = cached
        if _cache_is_obsolete(latest):
            # Cached "latest" is no longer newer than us — refetch.
            log.debug(
                "version-check cache obsolete (cached=%s running=%s); refetching",
                latest, __version__,
            )
            cached = None
        else:
            log.debug("version-check using cached value: %s", latest)

    if cached is None:
        latest = await asyncio.to_thread(_fetch_pypi_latest)
        if latest is None:
            return None
        _save_cache(latest)
    else:
        latest, _ = cached

    if _is_newer(latest, __version__):
        log.info(
            "update available: running %s, PyPI has %s",
            __version__, latest,
        )
        return latest
    return None
