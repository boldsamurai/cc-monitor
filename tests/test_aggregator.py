"""Tests for aggregator pure-functional bits.

Covers the percentile helper, sums_in_window / sums_in_range against a
hand-built _long_window, and the ingest happy path so the cost+token
totals match the input records.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from cc_usagemonitor.aggregator import Aggregator, _percentile
from cc_usagemonitor.parser import UsageRecord
from cc_usagemonitor.pricing import PricingTable


# ----- _percentile -----

def test_percentile_empty_is_zero():
    assert _percentile([], 90) == 0.0


def test_percentile_single_value():
    assert _percentile([42.0], 90) == 42.0


def test_percentile_p90_matches_numpy_default():
    # numpy.percentile([1..10], 90) → 9.1 (linear interpolation)
    values = [float(x) for x in range(1, 11)]
    assert _percentile(values, 90) == pytest.approx(9.1)


def test_percentile_p50_is_median():
    # Even-length list: linear interpolation gives midpoint.
    assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)


# ----- sums helpers -----

def _make_rec(
    ts: datetime,
    *,
    session_id: str = "s-1",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
) -> UsageRecord:
    return UsageRecord(
        ts=ts,
        session_id=session_id,
        project_slug="-p",
        model="claude-opus-4-7",
        is_sidechain=False,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_5m_tokens=cache_write_5m,
        cache_write_1h_tokens=cache_write_1h,
        raw_usage={},
        uuid=None,
        parent_uuid=None,
        cwd=None,
    )


@pytest.fixture
def agg() -> Aggregator:
    # Use the real pricing table; tests don't assert specific costs,
    # only shape and bucket boundaries.
    return Aggregator(PricingTable())


def test_sums_in_range_inclusive_start_exclusive_end(agg):
    base = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    # Three records: at 12:00, 13:00, 14:00 UTC.
    for offset in (0, 1, 2):
        ts = base + timedelta(hours=offset)
        agg._long_window.append((ts, _make_rec(ts), 1.0))

    # [12:00, 14:00) → captures the first two only.
    sums = agg.sums_in_range(base, base + timedelta(hours=2))
    assert sums.turns == 2
    assert sums.input == 200
    assert sums.cost_usd == pytest.approx(2.0)


def test_sums_in_range_end_defaults_to_now(agg):
    # Pin records to the recent past so 'end defaults to now' captures
    # both regardless of when the test happens to run.
    now = datetime.now(tz=timezone.utc)
    base = now - timedelta(hours=4)
    for offset in (0, 1):
        ts = base + timedelta(hours=offset)
        agg._long_window.append((ts, _make_rec(ts), 0.5))
    sums = agg.sums_in_range(base)
    assert sums.turns == 2


def test_sums_in_window_uses_now_minus_window(agg):
    now = datetime.now(tz=timezone.utc)
    # One in-window, one stale.
    agg._long_window.append((now - timedelta(minutes=10), _make_rec(now), 1.0))
    agg._long_window.append((now - timedelta(days=10), _make_rec(now), 1.0))
    sums = agg.sums_in_window(timedelta(hours=1))
    assert sums.turns == 1


def test_sums_in_range_handles_naive_timestamps(agg):
    base_aware = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    base_naive = datetime(2026, 4, 30, 13, 0)  # treated as UTC
    agg._long_window.append((base_naive, _make_rec(base_aware), 1.0))
    sums = agg.sums_in_range(
        base_aware, base_aware + timedelta(hours=2),
    )
    assert sums.turns == 1


# ----- ingest happy path -----

def test_ingest_assigns_session_and_sums(agg):
    ts = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    rec = _make_rec(ts, input_tokens=10, output_tokens=20, cache_read=30)
    agg.ingest(rec)
    sess = agg.sessions["s-1"]
    assert sess.sums.input == 10
    assert sess.sums.output == 20
    assert sess.sums.cache_read == 30
    assert sess.sums.turns == 1
    # by_model entry created with the normalized model id.
    assert "claude-opus-4-7" in sess.by_model
    assert sess.by_model["claude-opus-4-7"].turns == 1


def test_ingest_updates_first_and_last_seen(agg):
    # Aggregator assumes records arrive in chronological order (the
    # Tailer streams them that way). first_seen latches on the first
    # ingest; last_seen overwrites on every later ingest.
    earlier = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    later = datetime(2026, 4, 30, 14, 0, tzinfo=timezone.utc)
    agg.ingest(_make_rec(earlier))
    agg.ingest(_make_rec(later))
    sess = agg.sessions["s-1"]
    assert sess.first_seen == earlier
    assert sess.last_seen == later
