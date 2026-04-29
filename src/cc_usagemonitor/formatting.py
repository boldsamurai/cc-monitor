"""Config-aware datetime formatting.

The TUI's date format and time-zone preferences live in config.json
and are editable from the Settings screen. Threading config through
every caller would be invasive, so we keep the active settings in
module-level state — apply_config() updates them, the format helpers
read them on each call. The defaults match the historical hard-coded
behavior (local TZ, DD-MM-YYYY) so unconfigured installs render
exactly like before.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .logger import get_logger

log = get_logger(__name__)

# Active format settings — defaults match the historical hard-coded
# behavior. apply_config() rewrites them from config.json on startup
# and from the Settings screen on every change.
_DATE_FORMAT = "DD-MM-YYYY"
_TIME_ZONE = "Local"  # 'Local', 'UTC', or an IANA name e.g. 'Europe/Warsaw'

# Tuple per format key: (date+minutes pattern, date+seconds pattern).
# Time-only display is fixed at HH:MM:SS regardless of date format.
DATE_FORMATS: dict[str, tuple[str, str]] = {
    "DD-MM-YYYY": ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S"),
    "YYYY-MM-DD": ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"),
    "MM/DD/YYYY": ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S"),
}


def apply_config(
    date_format: str | None = None,
    time_zone: str | None = None,
) -> None:
    """Update active format settings. Pass None to leave a field as-is.

    Invalid date_format keys are silently ignored (kept on previous
    value) so a corrupted config can't crash startup. Invalid time-zone
    strings ARE accepted here — _to_display_tz handles the fallback at
    render time so the user sees a working clock instead of a blank
    screen while they correct their typo in Settings.
    """
    global _DATE_FORMAT, _TIME_ZONE
    if date_format and date_format in DATE_FORMATS:
        _DATE_FORMAT = date_format
    if time_zone:
        _TIME_ZONE = time_zone


def current_date_format() -> str:
    return _DATE_FORMAT


def current_time_zone() -> str:
    return _TIME_ZONE


def _to_display_tz(ts: datetime) -> datetime:
    """Convert ts to whichever zone the user picked in Settings.

    Falls back to local time on invalid IANA names — better a
    working clock in the wrong zone than a crashed render path.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if _TIME_ZONE == "UTC":
        return ts.astimezone(timezone.utc)
    if _TIME_ZONE == "Local":
        return ts.astimezone()
    try:
        from zoneinfo import ZoneInfo
        return ts.astimezone(ZoneInfo(_TIME_ZONE))
    except Exception as e:
        log.warning(
            "invalid time_zone %r: %s — falling back to local",
            _TIME_ZONE, e,
        )
        return ts.astimezone()


def format_datetime(ts: datetime | None) -> str:
    """Date + minutes (e.g. '29-04-2026 13:45') — used in DataTable cells."""
    if ts is None:
        return "-"
    return _to_display_tz(ts).strftime(DATE_FORMATS[_DATE_FORMAT][0])


def format_datetime_full(ts: datetime | None) -> str:
    """Date + seconds (e.g. '29-04-2026 13:45:23') — used in detail-screen
    info blocks where extra precision actually helps debugging."""
    if ts is None:
        return "-"
    return _to_display_tz(ts).strftime(DATE_FORMATS[_DATE_FORMAT][1])


def format_time(ts: datetime | None) -> str:
    """Time-of-day only ('13:45:23') — independent of date format."""
    if ts is None:
        return "-"
    return _to_display_tz(ts).strftime("%H:%M:%S")
