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
    assert cmd == ["uv", "tool", "upgrade", "cc-monitor"]


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
    assert cmd == ["pip", "install", "--upgrade", "cc-monitor"]
