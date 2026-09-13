"""Display-time helpers: storage is UTC, rendering is the configured zone."""
from datetime import datetime, timezone

import pytest

from app import timefmt


@pytest.fixture(autouse=True)
def _fresh_zone_cache():
    """display_tz() is lru_cached; clear it around every test so a zone set
    by monkeypatch never leaks into the next test."""
    timefmt.display_tz.cache_clear()
    yield
    timefmt.display_tz.cache_clear()


def _set_zone(monkeypatch, name: str) -> None:
    monkeypatch.setattr(timefmt.get_settings(), "dashboard_timezone", name)


def test_aware_utc_renders_in_display_zone(monkeypatch):
    _set_zone(monkeypatch, "Asia/Kolkata")
    value = datetime(2026, 9, 13, 12, 58, 29, tzinfo=timezone.utc)
    assert timefmt.format_ts(value) == "2026-09-13 18:28:29"


def test_naive_is_treated_as_utc(monkeypatch):
    _set_zone(monkeypatch, "Asia/Kolkata")
    value = datetime(2026, 9, 13, 12, 58, 29)
    assert timefmt.format_ts(value) == "2026-09-13 18:28:29"


def test_none_renders_as_dash():
    assert timefmt.format_ts(None) == "\u2013"


def test_utc_setting_keeps_old_behaviour(monkeypatch):
    _set_zone(monkeypatch, "UTC")
    value = datetime(2026, 9, 13, 12, 58, 29, tzinfo=timezone.utc)
    assert timefmt.format_ts(value) == "2026-09-13 12:58:29"


def test_bad_zone_name_falls_back_to_utc(monkeypatch):
    _set_zone(monkeypatch, "Not/AZone")
    value = datetime(2026, 9, 13, 12, 58, 29, tzinfo=timezone.utc)
    assert timefmt.format_ts(value) == "2026-09-13 12:58:29"
    assert timefmt.display_tz().key == "UTC"
