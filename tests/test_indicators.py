"""Indicator unit tests (data/indicators.py).

Pure-pandas/numpy math — no network, no fixtures beyond synthetic frames.
"""

import numpy as np
import pandas as pd

from data.indicators import atr, bollinger, rsi, sma, volume_zscore


def _rising_close(n: int = 20) -> pd.Series:
    """Steadily rising with small pullbacks (keeps avg_loss > 0 so Wilder's
    RSI is well-defined)."""
    price, vals = 100.0, []
    for i in range(n):
        price += 1.0 if i % 3 else -0.4
        vals.append(price)
    return pd.Series(vals)


def _falling_close(n: int = 20) -> pd.Series:
    price, vals = 100.0, []
    for i in range(n):
        price -= 1.0 if i % 3 else -0.4
        vals.append(price)
    return pd.Series(vals)


def test_rsi_rising_series_above_70():
    assert rsi(_rising_close()).iloc[-1] > 70


def test_rsi_falling_series_below_30():
    assert rsi(_falling_close()).iloc[-1] < 30


def test_atr_matches_hand_computed_true_range():
    # period=1 -> ATR collapses to the raw true range each row.
    df = pd.DataFrame(
        {
            "open": [101, 104, 107],
            "high": [105, 107, 110],
            "low": [100, 103, 106],
            "close": [102, 106, 108],
            "volume": [1000, 1000, 1000],
        }
    )
    # row0: 105-100 = 5
    # row1: max(107-103=4, |107-102|=5, |103-102|=1) = 5
    # row2: max(110-106=4, |110-106|=4, |106-106|=0) = 4
    expected = [5.0, 5.0, 4.0]
    got = list(atr(df, period=1))
    assert got == expected


def test_bollinger_mid_equals_sma20():
    close = pd.Series(np.linspace(100, 130, 25))
    assert bollinger(close)["mid"].iloc[-1] == sma(close, 20).iloc[-1]


def test_volume_zscore_flat_volume_is_nan():
    # Flat volume -> zero rolling std -> NaN (handled as missing, not 0).
    z = volume_zscore(pd.Series([1000] * 25))
    assert pd.isna(z.iloc[-1])


def test_volume_zscore_finite_on_varying_volume():
    vol = pd.Series([1000 + (i % 5) * 200 for i in range(25)])
    z = volume_zscore(vol)
    assert np.isfinite(z.iloc[-1])
