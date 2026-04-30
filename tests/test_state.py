"""Tests for cross-run snapshot save / load.

The serialization is pickle-based; these tests verify that a
round-trip preserves the aggregator's archive and the tailer's
per-file offsets, and that schema-version mismatches drop the
snapshot rather than crashing.
"""
from __future__ import annotations

import asyncio
import os
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cc_usagemonitor import state as state_io
from cc_usagemonitor.aggregator import Aggregator
from cc_usagemonitor.parser import UsageRecord
from cc_usagemonitor.pricing import PricingTable
from cc_usagemonitor.tailer import Tailer, _FileTail


@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    # Redirect XDG_CACHE_HOME so state.save() lands in tmp_path and we
    # don't clobber the user's real ~/.cache snapshot.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    yield tmp_path


def _make_rec(ts: datetime, *, session_id: str = "s-1") -> UsageRecord:
    return UsageRecord(
        ts=ts,
        session_id=session_id,
        project_slug="-p",
        model="claude-opus-4-7",
        is_sidechain=False,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=0,
        raw_usage={},
        uuid=None,
        parent_uuid=None,
        cwd=None,
    )


def test_round_trip_preserves_aggregator_state(tmp_state_dir):
    agg = Aggregator(PricingTable())
    ts = datetime.now(tz=timezone.utc)
    agg.ingest(_make_rec(ts))
    agg.ingest(_make_rec(ts + timedelta(seconds=10)))

    queue: asyncio.Queue = asyncio.Queue()
    tailer = Tailer(queue)
    tailer._session_tails[Path("/tmp/x.jsonl")] = _FileTail(
        path=Path("/tmp/x.jsonl"), pos=12345, inode=99,
    )

    state_io.save(agg, tailer)

    # Brand-new aggregator + tailer simulating a fresh process.
    fresh_agg = Aggregator(PricingTable())
    fresh_tailer = Tailer(queue)
    snap = state_io.load()
    assert snap is not None
    fresh_agg.restore(snap.aggregator)
    fresh_tailer.restore(snap.tailer)

    assert "s-1" in fresh_agg.sessions
    assert fresh_agg.sessions["s-1"].sums.turns == 2
    assert fresh_agg.revision == agg.revision
    assert Path("/tmp/x.jsonl") in fresh_tailer._session_tails
    assert fresh_tailer._session_tails[Path("/tmp/x.jsonl")].pos == 12345
    # Restore should mark the initial scan as already done so the
    # LoadingScreen doesn't appear on a warm start.
    assert fresh_tailer.initial_scan_done is True


def test_load_returns_none_on_missing_file(tmp_state_dir):
    assert state_io.load() is None


def test_load_returns_none_on_version_mismatch(tmp_state_dir):
    path = state_io._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Pickle a payload with an obviously bogus version field.
    with path.open("wb") as fh:
        pickle.dump({"version": -999, "snapshot": None}, fh)
    assert state_io.load() is None
    # The discard-on-mismatch path should have unlinked the file so
    # the next launch doesn't keep tripping on the same bad payload.
    assert not path.exists()


def test_load_returns_none_on_corrupt_file(tmp_state_dir):
    path = state_io._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not pickle")
    assert state_io.load() is None
    assert not path.exists()


def test_discard_removes_snapshot_file(tmp_state_dir):
    agg = Aggregator(PricingTable())
    queue: asyncio.Queue = asyncio.Queue()
    tailer = Tailer(queue)
    state_io.save(agg, tailer)
    assert state_io._state_path().is_file()
    state_io.discard()
    assert not state_io._state_path().exists()


def test_discard_is_idempotent(tmp_state_dir):
    # No file → still no error.
    state_io.discard()
