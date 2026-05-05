"""Tests for pure helpers in session_detail (no Textual import needed)."""
from __future__ import annotations

import pytest

from cc_usagemonitor.session_detail import _truncate_middle


def test_truncate_middle_below_max_returns_original():
    assert _truncate_middle("short", max_len=20) == "short"


def test_truncate_middle_at_exact_max_returns_original():
    s = "x" * 28
    assert _truncate_middle(s, max_len=28) == s


def test_truncate_middle_collapses_with_ellipsis():
    out = _truncate_middle("pr-review-toolkit:silent-failure-hunter", max_len=24)
    assert len(out) == 24
    assert "…" in out
    # Head + tail preserved — the namespace prefix and the
    # distinguishing suffix should both still be recognisable.
    assert out.startswith("pr-review")
    assert out.endswith("hunter")


def test_truncate_middle_keeps_namespace_prefix_visible():
    # User asked for middle truncation specifically so the namespace
    # before ':' stays readable. Confirm the prefix character is in
    # the output (head) for typical skill names.
    out = _truncate_middle("code-review-skill:react-component-validator", 24)
    assert "code-review" in out


def test_truncate_middle_handles_max_len_too_small():
    # Degenerate case: max_len < 4 means we can't fit head + … + tail.
    # Just slice from the start to keep behaviour deterministic.
    assert _truncate_middle("anything", max_len=3) == "any"
