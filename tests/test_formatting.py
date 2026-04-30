"""Tests for formatting helpers (date format selection, local TZ).

These tests stay TZ-agnostic by computing the expected output the same
way format_datetime does — `astimezone()` on a UTC timestamp — rather
than trying to pin TZ via env. Pinning TZ via monkeypatch + os.tzset
is unreliable across environments where Python has already cached the
local zone.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cc_usagemonitor import formatting


@pytest.fixture(autouse=True)
def _reset_format_each_test():
    """Module-level _DATE_FORMAT bleeds across tests if we don't reset.

    Snapshot before, run, restore — so test order doesn't matter and we
    don't pollute other modules' fixtures.
    """
    snapshot = formatting.current_date_format()
    yield
    formatting.apply_config(date_format=snapshot)


# ----- apply_config -----

def test_apply_config_accepts_known_formats():
    formatting.apply_config(date_format="YYYY-MM-DD")
    assert formatting.current_date_format() == "YYYY-MM-DD"


def test_apply_config_ignores_unknown_keys():
    formatting.apply_config(date_format="YYYY-MM-DD")
    formatting.apply_config(date_format="bogus")
    # Stays on the previous valid value rather than reverting to default
    # — corrupted config shouldn't silently flip the visible behavior.
    assert formatting.current_date_format() == "YYYY-MM-DD"


def test_apply_config_none_is_a_noop():
    formatting.apply_config(date_format="MM/DD/YYYY")
    formatting.apply_config(date_format=None)
    assert formatting.current_date_format() == "MM/DD/YYYY"


# ----- format_datetime / format_datetime_full / format_time -----

# Each test computes the expected string by replaying what the formatter
# does (UTC → local strftime). That keeps the test TZ-agnostic without
# fighting Python's cached local zone.


@pytest.mark.parametrize("fmt_key,expected_pattern", [
    ("DD-MM-YYYY", "%d-%m-%Y %H:%M"),
    ("YYYY-MM-DD", "%Y-%m-%d %H:%M"),
    ("MM/DD/YYYY", "%m/%d/%Y %H:%M"),
])
def test_format_datetime_uses_active_format(fmt_key, expected_pattern):
    formatting.apply_config(date_format=fmt_key)
    ts = datetime(2026, 4, 30, 13, 45, tzinfo=timezone.utc)
    expected = ts.astimezone().strftime(expected_pattern)
    assert formatting.format_datetime(ts) == expected


@pytest.mark.parametrize("fmt_key,expected_pattern", [
    ("DD-MM-YYYY", "%d-%m-%Y %H:%M:%S"),
    ("YYYY-MM-DD", "%Y-%m-%d %H:%M:%S"),
    ("MM/DD/YYYY", "%m/%d/%Y %H:%M:%S"),
])
def test_format_datetime_full_uses_active_format(fmt_key, expected_pattern):
    formatting.apply_config(date_format=fmt_key)
    ts = datetime(2026, 4, 30, 13, 45, 23, tzinfo=timezone.utc)
    expected = ts.astimezone().strftime(expected_pattern)
    assert formatting.format_datetime_full(ts) == expected


def test_format_time_independent_of_date_format():
    formatting.apply_config(date_format="MM/DD/YYYY")
    ts = datetime(2026, 4, 30, 7, 8, 9, tzinfo=timezone.utc)
    expected = ts.astimezone().strftime("%H:%M:%S")
    assert formatting.format_time(ts) == expected


def test_format_naive_datetime_treated_as_utc():
    # Naive timestamps come from JSONL parses where the tz was already
    # normalized; treat them as UTC rather than local-naïve.
    naive = datetime(2026, 4, 30, 13, 45)
    aware = naive.replace(tzinfo=timezone.utc)
    formatting.apply_config(date_format="DD-MM-YYYY")
    assert formatting.format_datetime(naive) == formatting.format_datetime(aware)


@pytest.mark.parametrize("fn", [
    formatting.format_datetime,
    formatting.format_datetime_full,
    formatting.format_time,
])
def test_format_none_returns_dash(fn):
    assert fn(None) == "-"
