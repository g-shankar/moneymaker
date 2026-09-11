"""Tests for equal-risk paper sizing (screener/sizing.py) and the
freshness-aware resume in screener/screen.py."""

from datetime import date

import pytest

from screener import sizing
from screener.screen import latest_complete_trading_day


def test_risk_dollars_default():
    assert sizing.risk_dollars() == pytest.approx(1000.0)


def test_size_shares_equal_risk():
    # $4.59/share risk -> 217 shares ~= $996
    assert sizing.size_shares(48.40, 43.81) == 217
    # $33.78/share risk -> 29 shares ~= $980
    assert sizing.size_shares(416.54, 382.76) == 29
    for entry, stop in [(48.40, 43.81), (416.54, 382.76), (10.54, 9.97)]:
        qty = sizing.size_shares(entry, stop)
        risked = (entry - stop) * qty
        assert 0 < risked <= 1000.0


def test_size_shares_minimum_one():
    # per-share risk above the $1,000 budget floors to 1 share, never 0
    assert sizing.size_shares(2000.0, 500.0) == 1


def test_size_shares_rejects_bad_bracket():
    with pytest.raises(ValueError):
        sizing.size_shares(95.0, 100.0)


def test_latest_complete_trading_day_weekday():
    # Friday -> Thursday
    assert latest_complete_trading_day(date(2026, 9, 11)) == date(2026, 9, 10)
    # Monday -> Friday
    assert latest_complete_trading_day(date(2026, 9, 14)) == date(2026, 9, 11)


def test_latest_complete_trading_day_weekend():
    # Saturday -> Friday
    assert latest_complete_trading_day(date(2026, 9, 12)) == date(2026, 9, 11)
    # Sunday -> Friday
    assert latest_complete_trading_day(date(2026, 9, 13)) == date(2026, 9, 11)
