"""Pure-functional sort helpers used by every DataTable in the app.

Lives in its own module so the test suite can import it without
pulling in Textual. The factory has three jobs:

  1. Pin empty cells to the visible *bottom* regardless of direction.
     Python's reverse=True flips the result of an ascending sort, so
     a single key tuple can't put empties at the bottom in both
     directions — we have to swap the empty-rank based on direction.
  2. Recognize duration tokens ('2h 4m', '1d 5h', '30s') and order by
     total seconds, not by raw string lex order.
  3. Recognize numbers with optional $-sign / K/M/B/T suffix and order
     by magnitude. Falls through to lower-cased text otherwise.
"""
from __future__ import annotations

import re


_SORT_NUMERIC_RX = re.compile(r"([-+]?\d+(?:[.,]\d+)*)\s*([KMBT]?)")
_SORT_K_FACTOR = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}

# Duration tokens like '1d 5h', '2h 4m', '30m', '45s' produced by
# _fmt_duration. Lowercase d/h/m/s are duration suffixes; uppercase M
# is millions and stays for the numeric branch.
_SORT_DURATION_RX = re.compile(r"(\d+)\s*([dhms])\b")
_SORT_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration_seconds(s: str) -> int | None:
    """Parse a Duration cell ('2h 4m', '1d 5h', '30s') into total
    seconds for numeric sorting. Returns None when the string has no
    duration tokens (lets the caller fall back to the regular numeric
    branch).
    """
    matches = _SORT_DURATION_RX.findall(s)
    if not matches:
        return None
    total = 0
    for n, unit in matches:
        try:
            total += int(n) * _SORT_DURATION_UNITS[unit]
        except (ValueError, KeyError):
            continue
    return total


def sort_key_factory(reverse: bool):
    """Build a sort-key callable that pins empty/dash cells to the
    bottom of the *visible* list regardless of direction.

    Order of attempts for non-empty cells:
      1. Duration string ('2h 4m', '30s', …) → total seconds.
      2. Numeric string ('$22,252.04', '5.42M', '12%') → magnitude.
      3. Anything else → lowercase string.
    Numeric cells return (0, number); textual cells (1, string); the
    type rank keeps mixed columns sane (numbers before text).
    """
    empty_rank = -1 if reverse else 2

    def _key(value):
        if value is None:
            return (empty_rank, "")
        s = str(value).strip()
        if not s or s == "-":
            return (empty_rank, "")
        dur = parse_duration_seconds(s)
        if dur is not None:
            return (0, dur)
        m = _SORT_NUMERIC_RX.match(s.lstrip("$"))
        if m:
            try:
                base = float(m.group(1).replace(",", ""))
                suffix = m.group(2).upper()
                return (0, base * _SORT_K_FACTOR.get(suffix, 1.0))
            except ValueError:
                pass
        return (1, s.lower())

    return _key


# Backwards-compatible alias for the ascending case so any existing
# table.sort(...) call without the factory keeps the legacy behavior
# (empties at bottom in ascending only, which is fine for most uses).
sort_key = sort_key_factory(False)
