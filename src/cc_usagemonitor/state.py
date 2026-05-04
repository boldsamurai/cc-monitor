"""Persistent snapshot of aggregator + tailer state across runs.

On clean shutdown the app pickles its in-memory archive plus per-file
tail offsets to ~/.cache/cc-monitor/state.pickle. On startup the
snapshot is loaded and the tailer resumes from the saved offsets, so a
second launch only parses lines that landed since the last quit. For a
user with hundreds of sessions this turns multi-second startup into
sub-second.

Versioning: the top-level dict carries a SCHEMA_VERSION. A mismatch (or
any unpickling error) drops the snapshot silently and falls back to a
full replay — no crash on schema evolution. Bump SCHEMA_VERSION when
removing/renaming fields on Aggregator or Tailer state.

Atomicity: writes go to state.pickle.tmp first, then rename — readers
never see a half-written file.
"""
from __future__ import annotations

import os
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .logger import get_logger

log = get_logger(__name__)


# Bump on incompatible changes to Aggregator / Tailer state shape. Old
# snapshots that don't match are dropped silently.
SCHEMA_VERSION = 1


def _state_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "cc-monitor" / "state.pickle"


@dataclass
class Snapshot:
    """Bundle of everything we need to restore on startup."""

    aggregator: dict[str, Any]
    tailer: dict[str, Any]


def save(aggregator, tailer) -> None:
    """Pickle aggregator + tailer state to disk. Best-effort — logs a
    warning on failure but never raises (a missing snapshot just means
    next launch does a full replay)."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        snap = Snapshot(
            aggregator=aggregator.snapshot(),
            tailer=tailer.snapshot(),
        )
        payload = {"version": SCHEMA_VERSION, "snapshot": snap}
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
        log.info(
            "state snapshot saved: %d sessions, %d archive records",
            len(aggregator.sessions),
            len(aggregator._long_window),
        )
    except Exception as e:
        log.warning("state snapshot save failed: %s", e)


def discard() -> None:
    """Remove the saved snapshot file.

    Used by Settings → Force re-scan so a subsequent crash before the
    next clean quit can't bring the cleared state back from a stale
    snapshot. Best-effort — missing file is fine.
    """
    path = _state_path()
    try:
        path.unlink(missing_ok=True)
        log.info("state snapshot discarded")
    except OSError as e:
        log.warning("state snapshot discard failed: %s", e)


def load() -> Snapshot | None:
    """Load a previously-saved snapshot. Returns None on miss, version
    mismatch, or any unpickling error (caller falls back to full
    replay). Stale or corrupt snapshots are removed so they don't keep
    failing on every launch."""
    path = _state_path()
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            payload = pickle.load(fh)
    except Exception as e:
        log.warning("state snapshot load failed: %s — discarding", e)
        try:
            path.unlink()
        except OSError:
            pass
        return None

    version = payload.get("version") if isinstance(payload, dict) else None
    if version != SCHEMA_VERSION:
        log.info(
            "state snapshot schema mismatch (got %s, want %s) — discarding",
            version, SCHEMA_VERSION,
        )
        try:
            path.unlink()
        except OSError:
            pass
        return None

    snap = payload.get("snapshot")
    if not isinstance(snap, Snapshot):
        log.warning("state snapshot missing or malformed — discarding")
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return snap
