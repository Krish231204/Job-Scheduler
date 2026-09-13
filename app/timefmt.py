"""Display-time helpers for the dashboard.

Storage is UTC everywhere (``DateTime(timezone=True)`` columns); nothing here
changes that. These helpers only decide how a stored instant is *shown*,
using ``settings.dashboard_timezone`` (an IANA name, default Asia/Kolkata).
"""
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import get_settings

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


@lru_cache(maxsize=1)
def display_tz() -> ZoneInfo:
    """The configured display zone. A bad name falls back to UTC rather
    than taking the dashboard down over a typo in an env file."""
    try:
        return ZoneInfo(get_settings().dashboard_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_display(value: datetime) -> datetime:
    """Convert a stored instant to the display zone. Naive values are
    treated as UTC, which is what every column in this app holds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(display_tz())


def format_ts(value: datetime | None, fmt: str = TS_FORMAT) -> str:
    """Compact, second-precision timestamp in the display zone; ``None``
    renders as an en dash so empty table cells stay quiet."""
    if value is None:
        return "\u2013"
    return to_display(value).strftime(fmt)
