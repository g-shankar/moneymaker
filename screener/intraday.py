"""Intraday edge for the midday / 3pm scans.

For each of the 40 watchlist symbols: today's 5-minute bars -> session VWAP,
price vs VWAP, intraday volume vs expected (prorated 20-day average), day
high/low, new highs/lows since the morning scan, and fresh breakout /
breakdown flags.

ADDITIVE BY DESIGN: score_intraday() never raises on missing data — it
returns {"available": False, "reason": ...} and the daily pipeline works
unchanged. The dossier builder merges the dict under dossier["intraday"]
when include_intraday=True (default False).

Paper context only. No orders, no credentials.
"""

import datetime as _dt
import logging as _logging

LOG = _logging.getLogger(__name__)

_BARS_PER_SESSION = 78  # 6.5h * 12 five-minute bars
_BREAKOUT_PCT = 0.002   # 0.2% beyond the morning extreme
_VOLUME_SURGE = 1.5     # 1.5x expected prorated volume


def get_intraday_bars(symbol: str, interval: str = "5m"):
    """Today's intraday bars via yfinance, normalized.

    Returns a DataFrame with DatetimeIndex and lowercase columns
    open/high/low/close/volume, or None when unavailable. Never raises.
    """
    try:
        import yfinance as _yf
        import pandas as _pd

        df = _yf.download(symbol, period="1d", interval=interval,
                          progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, _pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        need = {"open", "high", "low", "close", "volume"}
        if not need.issubset(df.columns):
            return None
        df = df[["open", "high", "low", "close", "volume"]]
        df.index = _pd.to_datetime(df.index)
        df = df.sort_index().dropna()
        # keep only today's session (ET)
        today = _dt.date.today().isoformat()
        df = df[df.index.strftime("%Y-%m-%d") == today]
        return df if not df.empty else None
    except Exception as exc:  # noqa: BLE001 - intraday is best-effort
        LOG.warning("intraday bars unavailable for %s: %s", symbol, exc)
        return None


def session_vwap(df) -> float | None:
    """Cumulative VWAP over the bars: sum(typical*vol) / sum(vol)."""
    if df is None or df.empty or float(df["volume"].sum()) <= 0:
        return None
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    return float((typical * df["volume"]).sum() / df["volume"].sum())


def expected_volume_so_far(avg_daily_volume: float | None,
                           elapsed_bars: int) -> float | None:
    """Prorated expected cumulative volume at this point in the session."""
    if not avg_daily_volume or avg_daily_volume <= 0 or elapsed_bars <= 0:
        return None
    frac = min(1.0, elapsed_bars / _BARS_PER_SESSION)
    return float(avg_daily_volume) * frac


def score_intraday(
    symbol: str,
    morning_ref: dict | None = None,
    avg_daily_volume: float | None = None,
    bars=None,
) -> dict:
    """Intraday score dict for one symbol. Never raises; missing data ->
    {"available": False, ...}.

    morning_ref: {"high": float, "low": float} — the day's high/low as of the
    morning scan, so we can flag NEW extremes since then.
    avg_daily_volume: 20-day average daily volume (for the volume surge read).
    bars: pre-fetched 5m DataFrame (for tests); fetched when None.
    """
    symbol = symbol.upper()
    try:
        df = bars if bars is not None else get_intraday_bars(symbol)
        if df is None or df.empty:
            return {"available": False, "symbol": symbol,
                    "reason": "no intraday bars"}
        last = float(df["close"].iloc[-1])
        vwap = session_vwap(df)
        day_high = float(df["high"].max())
        day_low = float(df["low"].min())
        cum_vol = float(df["volume"].sum())
        exp_vol = expected_volume_so_far(avg_daily_volume, len(df))
        vol_vs_expected = (cum_vol / exp_vol) if exp_vol else None

        new_high = new_low = None
        breakout = breakdown = False
        if morning_ref:
            mh, ml = morning_ref.get("high"), morning_ref.get("low")
            if mh:
                new_high = day_high > float(mh)
                breakout = (
                    last > float(mh) * (1 + _BREAKOUT_PCT)
                    and vol_vs_expected is not None
                    and vol_vs_expected >= _VOLUME_SURGE
                )
            if ml:
                new_low = day_low < float(ml)
                breakdown = (
                    last < float(ml) * (1 - _BREAKOUT_PCT)
                    and vol_vs_expected is not None
                    and vol_vs_expected >= _VOLUME_SURGE
                )

        session_range_pct = (
            (day_high - day_low) / day_low * 100.0 if day_low else None)
        return {
            "available": True,
            "symbol": symbol,
            "asof": df.index[-1].isoformat(),
            "n_bars": len(df),
            "last_price": round(last, 2),
            "vwap": round(vwap, 2) if vwap else None,
            "price_vs_vwap_pct": (
                round((last - vwap) / vwap * 100.0, 2) if vwap else None),
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "session_range_pct": (
                round(session_range_pct, 2)
                if session_range_pct is not None else None),
            "cum_volume": int(cum_vol),
            "vol_vs_expected": (
                round(vol_vs_expected, 2) if vol_vs_expected else None),
            "new_high_since_morning": new_high,
            "new_low_since_morning": new_low,
            "breakout": breakout,
            "breakdown": breakdown,
        }
    except Exception as exc:  # noqa: BLE001 - additive means never fatal
        LOG.warning("score_intraday failed for %s: %s", symbol, exc)
        return {"available": False, "symbol": symbol, "reason": str(exc)}


def score_universe(symbols: list[str],
                   morning_refs: dict | None = None,
                   avg_volumes: dict | None = None) -> dict:
    """score_intraday() for a watchlist. Returns {symbol: score_dict}."""
    morning_refs = morning_refs or {}
    avg_volumes = avg_volumes or {}
    return {
        s: score_intraday(s,
                          morning_ref=morning_refs.get(s),
                          avg_daily_volume=avg_volumes.get(s))
        for s in symbols
    }
