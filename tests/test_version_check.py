"""Unit tests for version_check helpers (parse, compare, cache)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cc_usagemonitor import version_check as vc


def test_parse_version_simple():
    assert vc._parse_version("0.1.2") == (0, 1, 2)
    assert vc._parse_version("1.0.0") == (1, 0, 0)


def test_parse_version_two_parts():
    assert vc._parse_version("0.1") == (0, 1)


def test_parse_version_pre_release_returns_none():
    # We deliberately reject anything we can't tuple-of-int parse.
    assert vc._parse_version("0.1.2.dev0") is None
    assert vc._parse_version("0.1.2-rc1") is None
    assert vc._parse_version("not-a-version") is None


def test_is_newer_basic():
    assert vc._is_newer("0.1.2", "0.1.1") is True
    assert vc._is_newer("0.2.0", "0.1.99") is True
    assert vc._is_newer("1.0.0", "0.99.99") is True


def test_is_newer_same_or_older():
    assert vc._is_newer("0.1.1", "0.1.1") is False
    assert vc._is_newer("0.1.0", "0.1.1") is False


def test_is_newer_unparseable_is_safe():
    # Garbage in either side → no notification (don't crash, don't lie).
    assert vc._is_newer("garbage", "0.1.1") is False
    assert vc._is_newer("0.1.1", "garbage") is False


def test_load_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "_cache_path", lambda: tmp_path / "missing.json")
    assert vc._load_cache() is None


def test_load_cache_corrupt(tmp_path, monkeypatch):
    p = tmp_path / "cache.json"
    p.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(vc, "_cache_path", lambda: p)
    assert vc._load_cache() is None


def test_load_cache_stale_returns_none(tmp_path, monkeypatch):
    p = tmp_path / "cache.json"
    payload = {
        "latest": "0.2.0",
        "fetched_at": time.time() - vc.CACHE_TTL_SECONDS - 60,
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(vc, "_cache_path", lambda: p)
    assert vc._load_cache() is None


def test_load_cache_fresh_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "_cache_path", lambda: tmp_path / "cache.json")
    vc._save_cache("0.2.0")
    cached = vc._load_cache()
    assert cached is not None
    version, fetched = cached
    assert version == "0.2.0"
    assert time.time() - fetched < 5  # written just now


def test_detect_installer_returns_none_when_nothing_on_path(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda name: None)
    assert vc.detect_installer() is None


def test_detect_installer_prefers_uv(monkeypatch):
    monkeypatch.setattr(
        vc.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in ("uv", "pipx", "pip") else None,
    )
    result = vc.detect_installer()
    assert result is not None
    name, cmd = result
    assert name == "uv"
    assert cmd == [
        "uv", "--refresh", "tool", "install", "--reinstall", "cc-monitor",
    ]


def test_detect_installer_falls_back_to_pipx(monkeypatch):
    monkeypatch.setattr(
        vc.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in ("pipx", "pip") else None,
    )
    result = vc.detect_installer()
    assert result is not None
    name, cmd = result
    assert name == "pipx"
    assert cmd == ["pipx", "upgrade", "cc-monitor"]


def test_detect_installer_falls_back_to_pip(monkeypatch):
    monkeypatch.setattr(
        vc.shutil, "which",
        lambda name: "/usr/bin/pip" if name == "pip" else None,
    )
    result = vc.detect_installer()
    assert result is not None
    name, cmd = result
    assert name == "pip"
    assert cmd == [
        "pip", "install", "--upgrade", "--no-cache-dir", "cc-monitor",
    ]


def test_cache_obsolete_when_running_newer_than_cached(monkeypatch):
    # User on 0.1.13, cache says latest=0.1.11 from a previous session.
    # Cache is stale even if it's only 30 minutes old.
    monkeypatch.setattr(vc, "__version__", "0.1.13")
    assert vc._cache_is_obsolete("0.1.11") is True


def test_cache_obsolete_when_running_equals_cached(monkeypatch):
    # User on 0.1.13, cache says latest=0.1.13. Cache is "obsolete"
    # in the sense that we already know we're not newer than ourselves
    # — we should refetch to find anything beyond. But _is_newer will
    # then return False if PyPI agrees, so no false-positive modal.
    monkeypatch.setattr(vc, "__version__", "0.1.13")
    assert vc._cache_is_obsolete("0.1.13") is True


def test_cache_fresh_when_running_older_than_cached(monkeypatch):
    # User on 0.1.10, cache says latest=0.1.13. Cache is correct, no
    # need to refetch — _is_newer will pick up the update.
    monkeypatch.setattr(vc, "__version__", "0.1.10")
    assert vc._cache_is_obsolete("0.1.13") is False


def test_cache_obsolete_safe_with_unparseable_version(monkeypatch):
    # Garbage in either side → don't claim obsolete. Caller falls
    # through to the cached value, which is the safest behavior
    # (worst case: one extra cycle of "no update").
    monkeypatch.setattr(vc, "__version__", "0.1.13")
    assert vc._cache_is_obsolete("garbage") is False
    monkeypatch.setattr(vc, "__version__", "garbage")
    assert vc._cache_is_obsolete("0.1.13") is False


# ----- pending_modal helpers -----


def test_pending_modal_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(vc, "__version__", "0.1.13")
    assert vc.get_pending_modal() is None  # nothing to read
    vc.set_pending_modal("0.1.20")
    assert vc.get_pending_modal() == "0.1.20"


def test_pending_modal_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(vc, "__version__", "0.1.13")
    vc.set_pending_modal("0.1.20")
    vc.clear_pending_modal()
    assert vc.get_pending_modal() is None


def test_pending_modal_cleared_when_user_caught_up(tmp_path, monkeypatch):
    # User had pending modal for 0.1.18 but somehow upgraded to 0.1.20
    # via another route. get_pending_modal must NOT pop a stale modal
    # for a version we're already past.
    monkeypatch.setattr(vc, "_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(vc, "__version__", "0.1.13")
    vc.set_pending_modal("0.1.18")
    monkeypatch.setattr(vc, "__version__", "0.1.20")
    assert vc.get_pending_modal() is None
    # Should also have proactively cleaned the stale flag.
    monkeypatch.setattr(vc, "__version__", "0.1.13")  # back-step
    assert vc.get_pending_modal() is None  # confirmed gone


def test_pending_modal_preserves_latest_field(tmp_path, monkeypatch):
    # set_pending_modal must not stomp on the latest/fetched_at
    # half of the cache file — these are independent pieces of state.
    monkeypatch.setattr(vc, "_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(vc, "__version__", "0.1.13")
    vc._save_cache("0.1.20")
    vc.set_pending_modal("0.1.20")
    cached = vc._load_cache()
    assert cached is not None
    version, _ = cached
    assert version == "0.1.20"
    assert vc.get_pending_modal() == "0.1.20"


def test_save_cache_preserves_pending_modal(tmp_path, monkeypatch):
    # The reverse: a fresh _save_cache (e.g. after PyPI refetch) must
    # not wipe a pending_modal that another code path just set.
    monkeypatch.setattr(vc, "_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(vc, "__version__", "0.1.13")
    vc.set_pending_modal("0.1.20")
    vc._save_cache("0.1.20")
    assert vc.get_pending_modal() == "0.1.20"


def test_clear_pending_modal_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "_cache_path", lambda: tmp_path / "cache.json")
    # No file → no-op.
    vc.clear_pending_modal()
    # Empty file → no-op.
    (tmp_path / "cache.json").write_text("{}", encoding="utf-8")
    vc.clear_pending_modal()
