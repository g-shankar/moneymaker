"""Extended-hours session data: pre-market (04:00-09:30 ET) and after-hours
(16:00-20:00 ET) price/volume, plus gap and move detection.

Why this exists: the 8:00 ET morning scan runs *before* the open, and
material moves happen outside regular hours (e.g. ORCL ripped ~+7%
after-hours on 2026-09-10 while its daily close print was stale). Pricing
entries off the prior close alone is wrong — the council and the advisor
must see the latest extended print.

Two sources, both optional and failure-isolated:
- yfinance intraday bars with ``prepost=True`` (no key; slow/flaky, so
  fetched in a worker thread with a timeout).
- Alpaca ``trades/latest`` / ``quotes/latest`` (needs ALPACA_API_KEY /
  ALPACA_API_SECRET env; proven working against data.alpaca.markets).

Public entry point: :func:`get_extended_hours`. Pass ``regular_close`` (and
optionally ``regular_close_date``); inject ``intraday_bars`` /
``live_print`` in tests to avoid network entirely.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

log = logging.getLogger(__name__)

try:
    _ET = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # pragma: no cover - tzdata missing
    log.warning("tzdata unavailable; extended-hours times fall back to UTC")
    _ET = None

# session windows as (start_h, start_m, end_h, end_m), ET
PREMARKET_WINDOW = (4, 0, 9, 30)
AFTERHOURS_WINDOW = (16, 0, 20, 0)

MOVE_FLAG_PCT = 2.0      # |move| beyond this gets flagged per session
GAP_RISK_HIGH_PCT = 3.0  # |move| beyond this => overnight_gap_risk "high"
VOLUME_SPIKE_MULT = 2.5  # session rel-volume beyond this is "unusual"
_FETCH_TIMEOUT_S = 20
_LOOKBACK_DAYS = 5
_INTRADAY_INTERVAL = "15m"


# ---------------------------------------------------------------------------
# session slicing helpers
# ---------------------------------------------------------------------------
def _to_et(df: pd.DataFrame) -> pd.DataFrame:
    """Return df with its index converted to America/New_York (tz-aware)."""
    idx = pd.to_datetime(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    out = df.copy()
    out.index = idx.tz_convert(_ET) if _ET is not None else idx
    return out.sort_index()


def session_slice(
    df: pd.DataFrame, window: tuple[int, int, int, int]
) -> pd.DataFrame:
    """Rows of *df* whose ET clock time falls in [start, end)."""
    if df is None or df.empty:
        return pd.DataFrame()
    sh, sm, eh, em = window
    df_et = _to_et(df)
    t = df_et.index.time
    mask = (t >= time(sh, sm)) & (t < time(eh, em))
    return df_et[mask]


def _daily_session_stats(
    df: pd.DataFrame, window: tuple[int, int, int, int]
) -> pd.DataFrame:
    """Per-ET-date session stats: last close, total volume, bar count."""
    sliced = session_slice(df, window)
    if sliced.empty:
        return pd.DataFrame(columns=["last_close", "volume", "n_bars"])
    dates = sliced.index.date
    grouped = sliced.groupby(dates)
    return pd.DataFrame(
        {
            "last_close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
            "n_bars": grouped.size(),
        }
    )


def session_relative_volume(
    df: pd.DataFrame,
    window: tuple[int, int, int, int],
    target_date: date | None = None,
    lookback_days: int = _LOOKBACK_DAYS,
) -> float | None:
    """Today's session volume vs the mean of the same session on prior days.

    Returns the multiple (1.0 = normal), or None when there is no usable
    baseline. Used for pre-market and intraday relative-volume reads.
    """
    stats = _daily_session_stats(df, window)
    if stats.empty:
        return None
    if target_date is None:
        target_date = stats.index.max()
    prior = stats[stats.index < target_date].tail(lookback_days)
    if prior.empty or float(prior["volume"].mean()) <= 0:
        return None
    today_rows = stats[stats.index == target_date]
    if today_rows.empty:
        return None
    return float(today_rows["volume"].iloc[0]) / float(prior["volume"].mean())


# ---------------------------------------------------------------------------
# fetchers (network-isolated; return None on any failure)
# ---------------------------------------------------------------------------
def fetch_intraday_yfinance(
    symbol: str,
    days: int = _LOOKBACK_DAYS,
    interval: str = _INTRADAY_INTERVAL,
    timeout_s: int = _FETCH_TIMEOUT_S,
) -> pd.DataFrame | None:
    """yfinance intraday bars with prepost=True. None on failure/timeout."""

    def _work():
        import yfinance as yf

        df = yf.download(
            symbol,
            period=f"{days}d",
            interval=interval,
            prepost=True,
            auto_adjust=False,
            progress=False,
        )
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        return df

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_work).result(timeout=timeout_s)
    except FuturesTimeout:
        log.warning("yfinance intraday timed out for %s", symbol)
    except Exception as exc:  # noqa: BLE001 - yfinance is flaky by design
        log.warning("yfinance intraday failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
    return None


def fetch_live_print_alpaca(symbol: str, timeout_s: int = 10) -> dict | None:
    """Latest extended-hours-aware print from Alpaca (trades/latest).

    Returns {"price", "time", "source": "alpaca-trades-latest"} or None when
    keys are absent or the request fails. Proven working against
    data.alpaca.markets.
    """
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    if not (key and secret):
        return None
    try:
        import requests

        resp = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            log.warning("alpaca trades/latest %s -> %s", symbol, resp.status_code)
            return None
        trade = resp.json().get("trade") or {}
        price = trade.get("p")
        if price is None:
            return None
        return {
            "price": float(price),
            "time": trade.get("t"),
            "source": "alpaca-trades-latest",
        }
    except Exception as exc:  # noqa: BLE001 - network is optional
        log.warning("alpaca live print failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def get_extended_hours(
    symbol: str,
    regular_close: float | None = None,
    regular_close_date: str | None = None,
    intraday_bars: pd.DataFrame | None = None,
    live_print: dict | None = None,
    asof: str | None = None,
) -> dict:
    """Build the extended-hours block for a symbol.

    Parameters
    ----------
    regular_close: last regular-session close (from daily bars).
    regular_close_date: ISO date of that close; after-hours bars are taken
        from this date, pre-market bars from later dates.
    intraday_bars: 15m (or finer) bars with prepost data; fetched via
        :func:`fetch_intraday_yfinance` when None and network is allowed.
        Pass an empty DataFrame to skip the fetch (tests / degraded mode).
    live_print: {"price", ...} freshest print (Alpaca trades/latest);
        wins over session bars when present.

    Never raises on data problems — returns {"available": False, ...}.
    """
    symbol = symbol.upper()
    asof = asof or datetime.now().isoformat()
    block: dict = {
        "symbol": symbol,
        "asof": asof,
        "available": False,
        "regular_close": regular_close,
        "premarket": None,
        "afterhours": None,
        "premarket_change_pct": None,
        "afterhours_change_pct": None,
        "reference_price": regular_close,
        "reference_source": "regular_close" if regular_close is not None else None,
        "overnight_gap_risk": "unknown",
        "flags": [],
    }

    try:
        if intraday_bars is None:
            intraday_bars = fetch_intraday_yfinance(symbol)
        if live_print is None:
            live_print = fetch_live_print_alpaca(symbol)

        if intraday_bars is not None and not intraday_bars.empty:
            if regular_close_date:
                ref_date = date.fromisoformat(regular_close_date)
            else:
                # best effort: the latest date with regular-session bars
                reg = session_slice(intraday_bars, (9, 30, 16, 0))
                ref_date = reg.index.max().date() if not reg.empty else None

            pm_stats = _daily_session_stats(intraday_bars, PREMARKET_WINDOW)
            ah_stats = _daily_session_stats(intraday_bars, AFTERHOURS_WINDOW)

            # pre-market: the latest session strictly after the reference close
            pm_today = None
            if not pm_stats.empty:
                later = pm_stats[pm_stats.index > ref_date] if ref_date else pm_stats
                if not later.empty:
                    pm_today = later.index.max()
                    row = later.loc[pm_today]
                    chg = _pct(float(row["last_close"]), regular_close)
                    relvol = session_relative_volume(
                        intraday_bars, PREMARKET_WINDOW, target_date=pm_today
                    )
                    block["premarket"] = {
                        "date": pm_today.isoformat(),
                        "last_price": float(row["last_close"]),
                        "volume": int(row["volume"]),
                        "rel_volume": relvol,
                        "change_pct": chg,
                        "flagged": bool(
                            (chg is not None and abs(chg) >= MOVE_FLAG_PCT)
                            or (relvol is not None and relvol >= VOLUME_SPIKE_MULT)
                        ),
                    }
                    block["premarket_change_pct"] = chg

            # after-hours: the session attached to the reference close's date
            if ref_date is not None and not ah_stats.empty:
                ah_rows = ah_stats[ah_stats.index == ref_date]
                if not ah_rows.empty:
                    row = ah_rows.iloc[0]
                    chg = _pct(float(row["last_close"]), regular_close)
                    relvol = session_relative_volume(
                        intraday_bars, AFTERHOURS_WINDOW, target_date=ref_date
                    )
                    block["afterhours"] = {
                        "date": ref_date.isoformat(),
                        "last_price": float(row["last_close"]),
                        "volume": int(row["volume"]),
                        "rel_volume": relvol,
                        "change_pct": chg,
                        "flagged": bool(
                            (chg is not None and abs(chg) >= MOVE_FLAG_PCT)
                            or (relvol is not None and relvol >= VOLUME_SPIKE_MULT)
                        ),
                    }
                    block["afterhours_change_pct"] = chg

        # freshest reference price wins: live print > pre-market > after-hours
        if live_print and live_print.get("price"):
            block["reference_price"] = float(live_print["price"])
            block["reference_source"] = live_print.get("source", "live_print")
        elif block["premarket"]:
            block["reference_price"] = block["premarket"]["last_price"]
            block["reference_source"] = "premarket"
        elif block["afterhours"]:
            block["reference_price"] = block["afterhours"]["last_price"]
            block["reference_source"] = "afterhours"

        moves = [
            c for c in (
                block["premarket_change_pct"],
                block["afterhours_change_pct"],
            )
            if c is not None
        ]
        if moves:
            worst = max(moves, key=abs)
            if abs(worst) >= GAP_RISK_HIGH_PCT:
                block["overnight_gap_risk"] = "high"
            elif abs(worst) >= MOVE_FLAG_PCT:
                block["overnight_gap_risk"] = "moderate"
            else:
                block["overnight_gap_risk"] = "low"

        flags = []
        pm, ah = block["premarket"], block["afterhours"]
        if pm and pm["flagged"]:
            flags.append(
                "premarket {0:+.2f}% (rel vol {1})".format(
                    pm["change_pct"] or 0.0,
                    f"{pm['rel_volume']:.1f}x"
                    if pm["rel_volume"] is not None else "n/a",
                )
            )
        if ah and ah["flagged"]:
            flags.append(
                "after-hours {0:+.2f}% (rel vol {1})".format(
                    ah["change_pct"] or 0.0,
                    f"{ah['rel_volume']:.1f}x"
                    if ah["rel_volume"] is not None else "n/a",
                )
            )
        block["flags"] = flags
        block["available"] = bool(pm or ah or live_print)
        return block
    except Exception as exc:  # noqa: BLE001 - never break the scan
        log.warning("extended-hours build failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
        block["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return block
