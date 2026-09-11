"""Per-symbol research dossier: one JSON-serializable bundle of everything
the data layer knows about a symbol — price, technical indicators,
quantitative scores, news, social sentiment, and macro risk.

``build_dossier(symbol, market, spy_bars=None, asof=None)`` assembles and
returns the dict. Pass ``kalshi`` (the result of ``get_macro_risk()``)
when scanning many symbols so the Kalshi API is hit ONCE per scan;
omitting it makes the dossier call ``get_macro_risk()`` itself (fine
for standalone/one-off use).
"""

import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from data.indicators import latest_snapshot
from data.kalshi import get_macro_risk
from data.market import MarketData
from data.news import get_news
from data.quant import (
    beta_vs_spy,
    mean_reversion_zscore,
    momentum_score,
    volatility_regime,
)
from data.social import get_sentiment

log = logging.getLogger(__name__)


def _extended_block(symbol: str, price: float | None,
                    price_date: str | None, intraday_bars) -> dict:
    """Best-effort extended-hours block; never raises."""
    try:
        from data.extended import fetch_live_print_alpaca, get_extended_hours

        return get_extended_hours(
            symbol,
            regular_close=price,
            regular_close_date=price_date,
            intraday_bars=intraday_bars,
            live_print=fetch_live_print_alpaca(symbol),
        )
    except Exception as exc:  # noqa: BLE001 - never break the scan
        log.warning("extended-hours block failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
        return {"available": False, "error": f"{type(exc).__name__}"[:120]}


def _unusual_block(symbol: str, daily_bars, intraday_bars) -> dict:
    """Best-effort unusual-activity block; never raises."""
    try:
        from data.unusual import scan_unusual_activity

        return scan_unusual_activity(
            symbol, daily_bars=daily_bars, intraday_bars=intraday_bars
        )
    except Exception as exc:  # noqa: BLE001 - never break the scan
        log.warning("unusual-activity block failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
        return {"flagged": False, "error": f"{type(exc).__name__}"[:120]}


def _catalysts_block(symbol: str, is_etf: bool = False) -> dict:
    """Best-effort catalysts block; never raises."""
    try:
        from data.catalysts import scan_catalysts

        return scan_catalysts(symbol, is_etf=is_etf)
    except Exception as exc:  # noqa: BLE001 - never break the scan
        log.warning("catalysts block failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
        return {"hot": False, "error": f"{type(exc).__name__}"[:120]}


def _intraday_block(symbol: str) -> dict:
    """Best-effort intraday tape block (screener/intraday.py); never raises."""
    try:
        from screener.intraday import score_intraday

        return score_intraday(symbol)
    except Exception as exc:  # noqa: BLE001 - never break the scan
        log.warning("intraday block failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
        return {"available": False, "symbol": symbol,
                "reason": f"{type(exc).__name__}"[:120]}


def _clean(value):
    """Recursively convert numpy/Timestamp objects to JSON-safe values."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    return value


def build_dossier(
    symbol: str,
    market: MarketData,
    spy_bars: pd.DataFrame = None,
    asof: str = None,
    kalshi: dict = None,
    include_extended: bool = True,
    include_unusual: bool = True,
    include_catalysts: bool = True,
    etf_symbols: set | None = None,
    include_intraday: bool = False,
) -> dict:
    """Assemble the full data dossier for *symbol*.

    symbol price history is pulled for the trailing 400 days so 200-day
    indicators and 252-day momentum histories have enough data. Everything
    is JSON-serializable.

    include_intraday (default False): merge today's 5-minute tape read
    (screener/intraday.py) under dossier["intraday"]. Additive — when the
    tape is unavailable the block is {"available": False} and nothing else
    changes. Intended for the midday / 3pm scans.

    Extended-hours, unusual-activity, and catalyst blocks are best-effort:
    each is isolated so a failure degrades to a stub block instead of
    killing the scan. Disable with the include_* flags (tests / degraded
    mode). *etf_symbols* marks ETF symbols so the catalysts block skips
    the earnings-date check (no single-stock earnings risk for ETFs).
    """
    symbol = symbol.upper()
    asof = asof or datetime.now(timezone.utc).isoformat()
    today_d = datetime.now(timezone.utc).date()
    start = (today_d - timedelta(days=730)).isoformat()
    today = today_d.isoformat()
    is_etf = bool(etf_symbols) and symbol in etf_symbols

    df = market.get_bars(symbol, start, today)
    if df.empty:
        log.warning("No bars for %s; returning skeleton dossier", symbol)
        return _clean(
            {
                "symbol": symbol,
                "asof": asof,
                "price": None,
                "indicators": {},
                "quant": {},
                "news": get_news(symbol),
                "social": get_sentiment(symbol),
                "kalshi": kalshi if kalshi is not None else get_macro_risk(),
                "extended_hours": {"available": False},
                "unusual_activity": {"flagged": False},
                "catalysts": {"hot": False},
                "is_etf": is_etf,
            }
        )

    snap = latest_snapshot(df)
    if kalshi is None:
        kalshi = get_macro_risk()

    quant = {
        "momentum_score": momentum_score(df),
        "mr_zscore": mean_reversion_zscore(df),
        "volatility_regime": volatility_regime(df),
        "beta_vs_spy": beta_vs_spy(df, spy_bars)
        if spy_bars is not None and not spy_bars.empty
        else None,
    }

    # One intraday fetch shared by the extended-hours and unusual-activity
    # blocks (each degrades independently on failure).
    intraday_bars = None
    if include_extended or include_unusual:
        try:
            from data.extended import fetch_intraday_yfinance

            intraday_bars = fetch_intraday_yfinance(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("intraday fetch failed for %s: %s: %s",
                        symbol, type(exc).__name__, exc)

    price = snap.get("price")
    try:
        price_date = pd.Timestamp(df.index[-1]).date().isoformat()
    except Exception:  # noqa: BLE001
        price_date = None

    return _clean(
        {
            "symbol": symbol,
            "asof": asof,
            "price": price,
            "indicators": snap,
            "quant": quant,
            "news": get_news(symbol),
            "social": get_sentiment(symbol),
            "kalshi": kalshi,
            "extended_hours": (
                _extended_block(symbol, price, price_date, intraday_bars)
                if include_extended else {"available": False}
            ),
            "unusual_activity": (
                _unusual_block(symbol, df, intraday_bars)
                if include_unusual else {"flagged": False}
            ),
            "catalysts": (
                _catalysts_block(symbol, is_etf=is_etf) if include_catalysts else {"hot": False}
            ),
            "intraday": (
                _intraday_block(symbol) if include_intraday else {"available": False}
            ),
            "is_etf": is_etf,
        }
    )
