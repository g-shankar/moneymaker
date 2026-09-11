"""Technical indicators, implemented with pandas/numpy only (no TA-Lib).

All functions take a DataFrame (or Series) with columns
open/high/low/close/volume and return Series/DataFrames aligned to the
input index. ``latest_snapshot`` rolls every indicator up into a dict of
plain Python floats for downstream JSON use.
"""

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's), 0-100 scale."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def ema(close: pd.Series, n: int) -> pd.Series:
    """Exponential moving average with span *n*."""
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(close: pd.Series, n: int) -> pd.Series:
    """Simple moving average with window *n*."""
    return close.rolling(n, min_periods=n).mean()


def macd(close: pd.Series) -> pd.DataFrame:
    """MACD(12, 26, 9): columns macd, signal, hist."""
    fast = ema(close, 12)
    slow = ema(close, 26)
    line = fast - slow
    signal = line.ewm(span=9, adjust=False, min_periods=9).mean()
    return pd.DataFrame({"macd": line, "signal": signal, "hist": line - signal})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder's smoothing on true range)."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2) -> pd.DataFrame:
    """Bollinger Bands: columns upper, mid, lower."""
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std()
    return pd.DataFrame({"upper": mid + k * std, "mid": mid, "lower": mid - k * std})


def vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative typical-price VWAP over the frame."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).cumsum()
    vv = df["volume"].cumsum().replace(0, np.nan)
    return pv / vv


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def volume_zscore(volume: pd.Series, n: int = 20) -> pd.Series:
    """Z-score of volume vs its own *n*-period rolling window."""
    mu = volume.rolling(n, min_periods=n).mean()
    sd = volume.rolling(n, min_periods=n).std().replace(0, np.nan)
    return (volume - mu) / sd


def _f(x) -> float:
    """Best-effort float conversion of a scalar (numpy -> Python float)."""
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return float("nan")
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def latest_snapshot(df: pd.DataFrame) -> dict:
    """Compute every indicator and return the LAST row as plain floats.

    Keys: rsi_14, ema_20, ema_50, sma_200, macd, macd_signal, macd_hist,
    atr_14, bb_upper, bb_mid, bb_lower, bb_pct (0-1 close position within
    the bands, NaN when bands degenerate), vwap, obv, volume_zscore,
    price (last close).
    """
    close = df["close"]
    m = macd(close)
    bb = bollinger(close)
    span = (bb["upper"] - bb["lower"]).iloc[-1]
    bb_pct = (
        (close.iloc[-1] - bb["lower"].iloc[-1]) / span if span and span != 0 else np.nan
    )
    return {
        "rsi_14": _f(rsi(close).iloc[-1]),
        "ema_20": _f(ema(close, 20).iloc[-1]),
        "ema_50": _f(ema(close, 50).iloc[-1]),
        "sma_200": _f(sma(close, 200).iloc[-1]),
        "macd": _f(m["macd"].iloc[-1]),
        "macd_signal": _f(m["signal"].iloc[-1]),
        "macd_hist": _f(m["hist"].iloc[-1]),
        "atr_14": _f(atr(df).iloc[-1]),
        "bb_upper": _f(bb["upper"].iloc[-1]),
        "bb_mid": _f(bb["mid"].iloc[-1]),
        "bb_lower": _f(bb["lower"].iloc[-1]),
        "bb_pct": _f(bb_pct),
        "vwap": _f(vwap(df).iloc[-1]),
        "obv": _f(obv(df).iloc[-1]),
        "volume_zscore": _f(volume_zscore(df["volume"]).iloc[-1]),
        "price": _f(close.iloc[-1]),
    }
