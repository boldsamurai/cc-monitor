"""Tests for aggregator pure-functional bits.

Covers the percentile helper, sums_in_window / sums_in_range against a
hand-built _long_window, and the ingest happy path so the cost+token
totals match the input records.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from cc_usagemonitor.aggregator import (
    Aggregator,
    _iter_blocks,
    _percentile,
    _top_of_hour,
)
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
    is_sidechain: bool = False,
) -> UsageRecord:
    return UsageRecord(
        ts=ts,
        session_id=session_id,
        project_slug="-p",
        model="claude-opus-4-7",
        is_sidechain=is_sidechain,
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


# ----- block boundary detection -----

def test_top_of_hour_truncates_minutes_seconds_micros():
    ts = datetime(2026, 4, 30, 14, 35, 42, 123456, tzinfo=timezone.utc)
    assert _top_of_hour(ts) == datetime(
        2026, 4, 30, 14, 0, 0, tzinfo=timezone.utc,
    )


def test_iter_blocks_anchors_start_to_top_of_hour():
    # First message at 14:35 → block 14:00–19:00 (Maciek-roboblog rule).
    base = datetime(2026, 4, 30, 14, 35, tzinfo=timezone.utc)
    records = [
        (base, _make_rec(base), 0.1),
        (base + timedelta(minutes=10), _make_rec(base), 0.1),
    ]
    blocks = list(_iter_blocks(records))
    assert len(blocks) == 1
    start, end, indices = blocks[0]
    assert start == datetime(2026, 4, 30, 14, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 4, 30, 19, 0, tzinfo=timezone.utc)
    assert indices == [0, 1]


def test_iter_blocks_splits_when_message_lands_after_end():
    # 14:00 block ends at 19:00. Message at 19:30 starts a new block.
    # Crucially, our old gap-based detection would miss this because
    # the gap between 18:30 and 19:30 is only 1h.
    msgs = [
        datetime(2026, 4, 30, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc),
        datetime(2026, 4, 30, 19, 30, tzinfo=timezone.utc),
    ]
    records = [(ts, _make_rec(ts), 0.1) for ts in msgs]
    blocks = list(_iter_blocks(records))
    assert len(blocks) == 2
    # First block: 14:00–19:00 with the 14:00 and 18:30 messages.
    assert blocks[0][0] == datetime(2026, 4, 30, 14, 0, tzinfo=timezone.utc)
    assert blocks[0][1] == datetime(2026, 4, 30, 19, 0, tzinfo=timezone.utc)
    assert blocks[0][2] == [0, 1]
    # Second block: 19:00–24:00 with the 19:30 message.
    assert blocks[1][0] == datetime(2026, 4, 30, 19, 0, tzinfo=timezone.utc)
    assert blocks[1][1] == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    assert blocks[1][2] == [2]


def test_iter_blocks_huge_gap_still_anchors_correctly():
    # 6h+ gap → unambiguously a new block. Top-of-hour anchoring
    # still applies on both ends.
    msgs = [
        datetime(2026, 4, 30, 14, 35, tzinfo=timezone.utc),
        datetime(2026, 4, 30, 22, 12, tzinfo=timezone.utc),  # +7h 37m
    ]
    records = [(ts, _make_rec(ts), 0.1) for ts in msgs]
    blocks = list(_iter_blocks(records))
    assert len(blocks) == 2
    assert blocks[0][0].minute == 0 and blocks[1][0].minute == 0


def test_iter_blocks_empty():
    assert list(_iter_blocks([])) == []


def test_block_info_uses_top_of_hour_end(agg):
    # Single message 4 hours ago, anchored to its top-of-hour. The
    # block_end exposed via BlockInfo must match top-of-hour + 5h —
    # that's what aligns the projection 'by HH:MM' line with the
    # API's '5h resets HH:MM' line.
    now = datetime.now(tz=timezone.utc)
    msg_ts = now - timedelta(hours=4)
    agg._long_window.append((msg_ts, _make_rec(msg_ts), 0.5))
    info = agg.block_info()
    assert info is not None
    expected_start = _top_of_hour(msg_ts)
    assert info.start == expected_start
    assert info.end == expected_start + timedelta(hours=5)


def test_block_info_returns_none_when_block_already_ended(agg):
    # Single message 6 hours ago — its block ended an hour ago, no
    # current block until the next message arrives.
    now = datetime.now(tz=timezone.utc)
    msg_ts = now - timedelta(hours=6)
    agg._long_window.append((msg_ts, _make_rec(msg_ts), 0.5))
    assert agg.block_info() is None


# ----- session JSONL combined helper -----

def _write_session_jsonl(tmp_path, project_slug: str, session_id: str, lines: list[dict]) -> None:
    """Write a fake session JSONL where the parser will find it.

    Aggregator's compute helpers use PROJECTS_DIR / slug / {session}.jsonl
    so the test has to put the file there. tests redirect PROJECTS_DIR
    via monkeypatch on the paths module.
    """
    import json
    proj_dir = tmp_path / project_slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines))


def test_session_jsonl_stats_extracts_reads_writes_and_tools(
    agg, tmp_path, monkeypatch,
):
    # Redirect PROJECTS_DIR so _compute_session_jsonl_stats reads our
    # fixture file instead of the user's ~/.claude/projects.
    from cc_usagemonitor import paths as paths_module
    monkeypatch.setattr(paths_module, "PROJECTS_DIR", tmp_path)
    # aggregator caches a reference at import time inside its helper,
    # so monkeypatch the module-level too if it's already bound.
    import cc_usagemonitor.aggregator as agg_mod
    monkeypatch.setattr(agg_mod, "PROJECTS_DIR", tmp_path, raising=False)

    project_slug = "-fake-proj"
    session_id = "sess-abc"
    # Register the session in agg so _compute_* finds it.
    ts = datetime.now(tz=timezone.utc)
    rec = _make_rec(ts, session_id=session_id)
    rec.project_slug = project_slug
    agg.ingest(rec)

    _write_session_jsonl(tmp_path, project_slug, session_id, [
        # Read 'a.py' twice with different result sizes.
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu1", "name": "Read",
             "input": {"file_path": "/tmp/a.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": "x" * 400},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu2", "name": "Read",
             "input": {"file_path": "/tmp/a.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu2",
             "content": "y" * 200},
        ]}},
        # Write 'b.py' once.
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu3", "name": "Write",
             "input": {"file_path": "/tmp/b.py", "content": "hello"}},
        ]}},
        # Edit 'a.py' once.
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu4", "name": "Edit",
             "input": {"file_path": "/tmp/a.py",
                       "new_string": "fix"}},
        ]}},
        # Bash tool just to populate counts.
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu5", "name": "Bash",
             "input": {"command": "ls"}},
        ]}},
    ])

    reads = agg.count_file_reads_in_session(session_id)
    assert "/tmp/a.py" in reads
    assert reads["/tmp/a.py"]["reads"] == 2
    assert reads["/tmp/a.py"]["chars"] == 600
    assert reads["/tmp/a.py"]["tokens_est"] == 150

    writes = agg.count_file_writes_in_session(session_id)
    assert writes["/tmp/b.py"]["writes"] == 1
    assert writes["/tmp/b.py"]["chars"] == 5
    assert writes["/tmp/a.py"]["edits"] == 1
    assert writes["/tmp/a.py"]["chars"] == 3

    tools = agg.count_tools_in_session(session_id)
    # 2 Read + 1 Write + 1 Edit + 1 Bash
    assert tools == {"Read": 2, "Write": 1, "Edit": 1, "Bash": 1}


def test_session_jsonl_stats_uses_single_pass_cache(
    agg, tmp_path, monkeypatch,
):
    # The three count_*_in_session methods now share a cache key, so
    # calling all three after one ingest should compute exactly once.
    import cc_usagemonitor.aggregator as agg_mod
    monkeypatch.setattr(agg_mod, "PROJECTS_DIR", tmp_path, raising=False)

    project_slug = "-fake-proj"
    session_id = "sess-xyz"
    ts = datetime.now(tz=timezone.utc)
    rec = _make_rec(ts, session_id=session_id)
    rec.project_slug = project_slug
    agg.ingest(rec)
    _write_session_jsonl(tmp_path, project_slug, session_id, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t", "name": "Bash",
             "input": {}},
        ]}},
    ])

    # Wrap _compute_session_jsonl_stats so we can count invocations.
    calls = {"n": 0}
    real = agg._compute_session_jsonl_stats

    def counted(sid):
        calls["n"] += 1
        return real(sid)

    monkeypatch.setattr(agg, "_compute_session_jsonl_stats", counted)

    agg.count_file_reads_in_session(session_id)
    agg.count_file_writes_in_session(session_id)
    agg.count_tools_in_session(session_id)
    assert calls["n"] == 1


# ----- per-session cache -----

def test_session_cache_returns_same_object_until_revision_bump(agg):
    # Build a session by ingesting once.
    ts = datetime.now(tz=timezone.utc)
    agg.ingest(_make_rec(ts))

    calls = []
    result_a = {"x": 1}
    result_b = {"x": 2}

    def fake_compute():
        calls.append("c")
        return result_b if calls else result_a

    # First call computes and caches.
    out1 = agg._cached_for_session("s-1", "k", lambda: result_a)
    # Second call hits the cache — fn isn't even invoked.
    out2 = agg._cached_for_session(
        "s-1", "k", lambda: (_ for _ in ()).throw(AssertionError("recomputed")),
    )
    assert out1 is result_a and out2 is result_a

    # Bump the session's revision via another ingest; the next call
    # should recompute.
    agg.ingest(_make_rec(ts + timedelta(seconds=1)))
    out3 = agg._cached_for_session("s-1", "k", lambda: result_b)
    assert out3 is result_b


def test_session_cache_unknown_session_does_not_cache(agg):
    # No SessionState for 'ghost' → fn runs every time, no caching.
    counter = {"n": 0}

    def fn():
        counter["n"] += 1
        return counter["n"]

    agg._cached_for_session("ghost", "k", fn)
    agg._cached_for_session("ghost", "k", fn)
    assert counter["n"] == 2


def test_reset_state_clears_session_cache(agg):
    ts = datetime.now(tz=timezone.utc)
    agg.ingest(_make_rec(ts))
    sentinel = object()
    agg._cached_for_session("s-1", "k", lambda: sentinel)
    assert ("s-1", "k") in agg._session_cache
    agg.reset_state()
    assert agg._session_cache == {}


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


# ----- sidechain context filter (Bug A from session detail context dive) -----


def test_sidechain_records_dont_overwrite_main_chain_context(agg):
    base = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    # Main turn lands first with a fat context.
    agg.ingest(_make_rec(
        base, cache_read=50_000, input_tokens=10_000,
    ))
    sess = agg.sessions["s-1"]
    assert sess.last_context_tokens == 60_000
    main_max = sess.max_context_tokens

    # Sub-agent runs with a small context — must NOT overwrite the
    # main chain's last/max values. This is the bug that caused the
    # context % chart to dive whenever a Task tool fired.
    agg.ingest(_make_rec(
        base + timedelta(seconds=5),
        cache_read=500, input_tokens=200, is_sidechain=True,
    ))
    assert sess.last_context_tokens == 60_000
    assert sess.max_context_tokens == main_max


def test_sidechain_records_still_count_in_cost_sums(agg):
    # Cost / token sums DO need to include sidechain — sub-agents
    # cost real money. Only context tracking ignores them.
    base = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    agg.ingest(_make_rec(base, input_tokens=100, is_sidechain=False))
    agg.ingest(_make_rec(
        base + timedelta(seconds=1), input_tokens=200, is_sidechain=True,
    ))
    sess = agg.sessions["s-1"]
    assert sess.sums.input == 300  # both turns
    assert sess.sums_main.input == 100
    assert sess.sums_sidechain.input == 200


# ----- session_in_current_block helper (per-session 5h aggregation) -----


def test_session_in_current_block_returns_none_for_no_active_block(agg):
    # Empty long-window → no block at all.
    assert agg.session_in_current_block("s-1") is None


def test_session_in_current_block_returns_none_for_unrelated_session(agg):
    # Active block exists but only contains other sessions.
    now = datetime.now(tz=timezone.utc)
    agg._long_window.append((now, _make_rec(now, session_id="other"), 1.0))
    assert agg.session_in_current_block("s-1") is None


def test_session_in_current_block_sums_only_target_session(agg):
    # Two sessions in the same active block; helper returns sums for
    # one without leaking the other's tokens/cost.
    now = datetime.now(tz=timezone.utc)
    agg._long_window.append(
        (now, _make_rec(now, session_id="target", input_tokens=100), 0.5)
    )
    agg._long_window.append(
        (now, _make_rec(now, session_id="other", input_tokens=999), 0.9)
    )
    sums = agg.session_in_current_block("target")
    assert sums is not None
    assert sums.input == 100
    assert sums.cost_usd == pytest.approx(0.5)
