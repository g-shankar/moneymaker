"""Quantitative features for the paper-trading system.

Pure pandas/numpy helpers over daily OHLCV frames:
momentum score, mean-reversion z-score, volatility regime, and SPY beta.
"""

import numpy as np
import pandas as pd


def _returns(close: pd.Series) -> pd.Series:
    return close.pct_change()


def momentum_score(df: pd.DataFrame, lookbacks=(20, 60, 120)) -> float:
    """Momentum score in [0, 100].

    For each lookback L, compute the L-day return and z-score it against
    that lookback's own trailing 252-day history. Map 50 + 25*z (clipped
    to [0, 100]) and average across lookbacks. A score of 50 ≈ neutral,
    >50 positive momentum, <50 negative.
    """
    close = df["close"]
    scores = []
    for lb in lookbacks:
        ret = close.pct_change(lb)
        mu = ret.rolling(252, min_periods=60).mean()
        sd = ret.rolling(252, min_periods=60).std().replace(0, np.nan)
        z = (ret.iloc[-1] - mu.iloc[-1]) / sd.iloc[-1] if len(ret) else np.nan
        if z is None or np.isnan(z):
            continue
        scores.append(float(np.clip(50 + 25 * z, 0, 100)))
    if not scores:
        return 50.0
    return float(np.mean(scores))


def mean_reversion_zscore(df: pd.DataFrame, n: int = 60) -> float:
    """(close - sma_n) / rolling_std(close, n); negative => below average."""
    close = df["close"]
    mu = close.rolling(n, min_periods=n).mean().iloc[-1]
    sd = close.rolling(n, min_periods=n).std().iloc[-1]
    if sd is None or np.isnan(sd) or sd == 0:
        return 0.0
    return float((close.iloc[-1] - mu) / sd)


def volatility_regime(df: pd.DataFrame) -> dict:
    """Annualized realized-vol regime: low (<0.15), normal, high (>0.35).

    realized_vol = std(log returns) * sqrt(252) over the available frame.
    """
    logret = np.log(df["close"] / df["close"].shift(1)).dropna()
    rv = float(logret.std() * np.sqrt(252)) if len(logret) > 1 else 0.0
    regime = "high" if rv > 0.35 else ("low" if rv < 0.15 else "normal")
    return {"regime": regime, "realized_vol": rv}


def beta_vs_spy(df: pd.DataFrame, spy_df: pd.DataFrame) -> float:
    """60-day beta of daily returns vs SPY daily bars (cov/var)."""
    r = _returns(df["close"])
    s = _returns(spy_df["close"])
    joined = pd.concat([r, s], axis=1, join="inner").dropna().tail(60)
    if len(joined) < 10:
        return 1.0
    cov = joined.iloc[:, 0].cov(joined.iloc[:, 1])
    var = joined.iloc[:, 1].var()
    if var is None or np.isnan(var) or var == 0:
        return 1.0
    return float(cov / var)
