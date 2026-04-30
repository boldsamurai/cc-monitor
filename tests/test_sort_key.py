"""Tests for the DataTable sort-key factory.

The factory has three jobs:
  1. Pin empty cells to the visible *bottom* regardless of direction.
  2. Recognize duration tokens ('2h 4m') and order them by total seconds.
  3. Recognize numbers with optional $-sign / K/M/B/T suffix and order
     them by magnitude.

Importing the helpers requires a Textual-free path so the test suite
can run without spinning up an App.
"""
from __future__ import annotations

import pytest

from cc_usagemonitor.sort_key import (
    parse_duration_seconds as _parse_duration_seconds,
    sort_key_factory as _sort_key_factory,
)


# ----- _parse_duration_seconds -----

@pytest.mark.parametrize("raw,want", [
    ("30s", 30),
    ("5m", 300),
    ("2h", 7200),
    ("1d", 86400),
    ("2h 4m", 2 * 3600 + 4 * 60),
    ("1d 5h", 86400 + 5 * 3600),
    ("1d 5h 30m 15s", 86400 + 5 * 3600 + 30 * 60 + 15),
])
def test_parse_duration_seconds(raw, want):
    assert _parse_duration_seconds(raw) == want


@pytest.mark.parametrize("raw", ["", "-", "5", "$22.50", "12K", "abc"])
def test_parse_duration_seconds_returns_none_for_non_durations(raw):
    assert _parse_duration_seconds(raw) is None


# ----- _sort_key_factory: empty pinning -----

def test_empties_at_bottom_ascending():
    key = _sort_key_factory(reverse=False)
    items = ["10", "-", "5", "", None, "20"]
    items.sort(key=key)
    # Non-empty in ascending order, then empties last.
    assert items[:3] == ["5", "10", "20"]
    assert all(x in (None, "", "-") for x in items[3:])


def test_empties_at_bottom_descending():
    key = _sort_key_factory(reverse=True)
    items = ["10", "-", "5", "", None, "20"]
    # When you do reverse=True, Python sorts asc by key then reverses
    # the whole list. The factory compensates by putting empties at
    # rank -1 so they end up at the bottom *after* the reverse.
    items.sort(key=key, reverse=True)
    assert items[:3] == ["20", "10", "5"]
    assert all(x in (None, "", "-") for x in items[3:])


# ----- _sort_key_factory: numeric magnitude -----

def test_numeric_with_suffixes_orders_by_magnitude():
    key = _sort_key_factory(reverse=False)
    items = ["1.5K", "200", "3M", "9", "1.2B"]
    items.sort(key=key)
    assert items == ["9", "200", "1.5K", "3M", "1.2B"]


def test_currency_strings_strip_dollar_and_commas():
    key = _sort_key_factory(reverse=False)
    items = ["$22,252.04", "$5.42", "$1,000.00"]
    items.sort(key=key)
    assert items == ["$5.42", "$1,000.00", "$22,252.04"]


def test_percent_treated_as_numeric():
    # The %-suffix isn't in the magnitude table — _SORT_NUMERIC_RX still
    # captures the leading number, so '99%' < '120%'.
    key = _sort_key_factory(reverse=False)
    items = ["99%", "5%", "120%"]
    items.sort(key=key)
    assert items == ["5%", "99%", "120%"]


# ----- _sort_key_factory: duration sorting -----

def test_durations_sorted_by_total_seconds():
    key = _sort_key_factory(reverse=False)
    items = ["1d 5h", "30m", "2h 4m", "5h"]
    items.sort(key=key)
    assert items == ["30m", "2h 4m", "5h", "1d 5h"]


def test_durations_beat_pure_numerics_at_same_text_length():
    # Without duration handling, '5h' would parse as numeric 5 and
    # outrank '60m' (numeric 60). With duration handling, '5h' = 18000s
    # is the bigger of the two.
    key = _sort_key_factory(reverse=False)
    items = ["5h", "60m"]  # 18000s vs 3600s
    items.sort(key=key)
    assert items == ["60m", "5h"]


# ----- _sort_key_factory: text fallback -----

def test_non_numeric_strings_fall_back_to_lowercase():
    key = _sort_key_factory(reverse=False)
    items = ["Bash", "alpha", "Charlie"]
    items.sort(key=key)
    assert items == ["alpha", "Bash", "Charlie"]


def test_numbers_outrank_strings_in_mixed_columns():
    # Type rank: numerics (0, magnitude) sort before strings (1, lower).
    key = _sort_key_factory(reverse=False)
    items = ["abc", "100", "xyz", "5"]
    items.sort(key=key)
    assert items[:2] == ["5", "100"]
    assert items[2:] == ["abc", "xyz"]
