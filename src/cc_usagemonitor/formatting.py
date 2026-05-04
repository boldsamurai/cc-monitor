"""Config-aware datetime formatting.

The TUI's date format preference lives in config.json and is editable
from the Settings screen. Threading config through every caller would
be invasive, so we keep the active setting in module-level state —
apply_config() updates it, the format helpers read it on each call.
The default matches the historical hard-coded behavior so unconfigured
installs render exactly like before.

Time zone is intentionally not a setting: timestamps render in the
system's local zone via datetime.astimezone(). Users running on a
foreign-TZ host who want a different display zone can set the TZ
environment variable when launching cc-monitor.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Active format setting — defaults match the historical hard-coded
# behavior. apply_config() rewrites it from config.json on startup
# and from the Settings screen on every change.
_DATE_FORMAT = "DD-MM-YYYY"

# Tuple per format key: (date+minutes pattern, date+seconds pattern).
# Time-only display is fixed at HH:MM:SS regardless of date format.
DATE_FORMATS: dict[str, tuple[str, str]] = {
    "DD-MM-YYYY": ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S"),
    "YYYY-MM-DD": ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"),
    "MM/DD/YYYY": ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S"),
}


def apply_config(date_format: str | None = None) -> None:
    """Update active format setting. Pass None to leave it unchanged.

    Invalid date_format keys are silently ignored (kept on previous
    value) so a corrupted config can't crash startup.
    """
    global _DATE_FORMAT
    if date_format and date_format in DATE_FORMATS:
        _DATE_FORMAT = date_format


def current_date_format() -> str:
    return _DATE_FORMAT


def _to_local(ts: datetime) -> datetime:
    """Convert ts to the system's local TZ. Naive timestamps are
    treated as UTC (matches the project's JSONL ISO-8601 convention)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone()


def format_datetime(ts: datetime | None) -> str:
    """Date + minutes (e.g. '29-04-2026 13:45') — used in DataTable cells."""
    if ts is None:
        return "-"
    return _to_local(ts).strftime(DATE_FORMATS[_DATE_FORMAT][0])


def format_datetime_full(ts: datetime | None) -> str:
    """Date + seconds (e.g. '29-04-2026 13:45:23') — used in detail-screen
    info blocks where extra precision actually helps debugging."""
    if ts is None:
        return "-"
    return _to_local(ts).strftime(DATE_FORMATS[_DATE_FORMAT][1])


def format_time(ts: datetime | None) -> str:
    """Time-of-day only ('13:45:23') — independent of date format."""
    if ts is None:
        return "-"
    return _to_local(ts).strftime("%H:%M:%S")
