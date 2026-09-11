"""Tests for screener/intraday.py. All synthetic — no network."""

import pandas as pd
import pytest

from screener import intraday as intra


def _bars(n=30, start_price=100.0, drift=0.001, vol=1000):
    """Synthetic 5-minute bars with a steady drift."""
    idx = pd.date_range("2026-09-11 09:30", periods=n, freq="5min", tz="America/New_York")
    closes = [start_price * (1 + drift) ** i for i in range(n)]
    df = pd.DataFrame({
        "open": [c * 0.999 for c in closes],
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.998 for c in closes],
        "close": closes,
        "volume": [vol] * n,
    }, index=idx)
    return df


def test_vwap_math():
    df = pd.DataFrame({
        "high": [10.0, 12.0], "low": [8.0, 10.0], "close": [9.0, 11.0],
        "volume": [100.0, 300.0],
    })
    # typical: 9, 11 -> (9*100 + 11*300)/400 = 10.5
    assert intra.session_vwap(df) == pytest.approx(10.5)
    assert intra.session_vwap(df.iloc[0:0]) is None


def test_score_intraday_full():
    df = _bars(n=30, start_price=100.0, drift=0.002)
    out = intra.score_intraday("AAA", bars=df, avg_daily_volume=30 * 1000)
    assert out["available"] is True
    assert out["symbol"] == "AAA"
    assert out["n_bars"] == 30
    assert out["last_price"] == pytest.approx(df["close"].iloc[-1], rel=1e-3)
    # steady up-drift -> price above VWAP
    assert out["price_vs_vwap_pct"] > 0
    # 30 bars of 78 with avg daily vol 30000 -> expected 30000*30/78, cum 30000
    assert out["vol_vs_expected"] == pytest.approx(78 / 30, rel=1e-2)
    assert out["day_high"] >= out["day_low"]
    assert out["session_range_pct"] > 0


def test_breakout_flag_vs_morning_ref():
    # morning high was 101; tape rallies to ~108 on heavy volume
    df = _bars(n=40, start_price=100.0, drift=0.002, vol=5000)
    out = intra.score_intraday(
        "AAA", bars=df,
        morning_ref={"high": 101.0, "low": 99.5},
        avg_daily_volume=40 * 5000,  # avg day == this pace -> ratio 78/40
    )
    assert out["new_high_since_morning"] is True
    assert out["new_low_since_morning"] is False
    # vol_vs_expected == 78/40 = 1.95 >= 1.5 and last > 101*1.002 -> breakout
    assert out["vol_vs_expected"] == pytest.approx(78 / 40, rel=1e-2)
    assert out["breakout"] is True
    assert out["breakdown"] is False


def test_no_breakout_without_volume_surge():
    df = _bars(n=40, start_price=100.0, drift=0.002, vol=100)
    out = intra.score_intraday(
        "AAA", bars=df,
        morning_ref={"high": 101.0, "low": 99.5},
        avg_daily_volume=40 * 10000,  # thin vs average -> no surge
    )
    assert out["new_high_since_morning"] is True
    assert out["breakout"] is False  # new high alone isn't a breakout


def test_breakdown_flag():
    df = _bars(n=40, start_price=100.0, drift=-0.002, vol=5000)
    out = intra.score_intraday(
        "AAA", bars=df,
        morning_ref={"high": 101.0, "low": 99.5},
        avg_daily_volume=40 * 5000,
    )
    assert out["breakdown"] is True
    assert out["breakout"] is False
    assert out["price_vs_vwap_pct"] < 0


def test_missing_data_never_raises(monkeypatch):
    out = intra.score_intraday("AAA", bars=pd.DataFrame())
    assert out["available"] is False
    # fetch path failure -> still a dict, never an exception
    monkeypatch.setattr(intra, "get_intraday_bars", lambda *a, **k: None)
    out2 = intra.score_intraday("AAA")
    assert out2 == {"available": False, "symbol": "AAA",
                    "reason": "no intraday bars"}


def test_expected_volume_prorate():
    assert intra.expected_volume_so_far(78000, 39) == pytest.approx(39000.0)
    assert intra.expected_volume_so_far(78000, 78) == pytest.approx(78000.0)
    assert intra.expected_volume_so_far(78000, 200) == pytest.approx(78000.0)  # capped
    assert intra.expected_volume_so_far(None, 10) is None
    assert intra.expected_volume_so_far(0, 10) is None


def test_score_universe_returns_dict(monkeypatch):
    df = _bars(n=10)
    monkeypatch.setattr(intra, "get_intraday_bars", lambda *a, **k: df)
    out = intra.score_universe(["AAA", "BBB"])
    assert set(out.keys()) == {"AAA", "BBB"}
    for v in out.values():
        assert v["available"] is True
