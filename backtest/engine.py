"""Backtest engine for the autonomous paper-trading system (strategy1, long-only).

Walk-forward simulation over daily bars:

* Signals are computed at the **close** of day *t* and executed at the **open**
  of day *t+1* (one-bar execution lag — a documented, conservative fill
  assumption; no look-ahead).
* Entries: composite_score(dossier) >= config min score, no veto, position
  slots free, one position per symbol, cash available.
* Exits (checked daily against the bar's high/low):
    - ATR trailing stop (ratchets up via update_trailing_stop),
    - 2R take-profit (config take_profit_r),
    - time stop (config time_stop_days, calendar days per sibling contract).
  Stop/TP fills are assumed at the stop/TP level (documented simplification);
  time-stop exits fill at the close.
* No council in the backtest: the composite-score threshold is the documented
  council proxy (the live pipeline would route candidates to the council).
* No Kalshi history exists for a 2-year backtest, so the macro gate uses a
  documented VIX proxy: kalshi_score = 100 * min(1, VIX_close / 45); the
  sibling's >= 70 veto rule applies unchanged.
* Commission $0, no slippage beyond the documented fill assumptions.
* Position sizing: each new position targets (current equity / max_positions)
  notional, whole shares, limited by available cash. LONG-ONLY, no margin.
* Deterministic: symbols are iterated in sorted order; no randomness anywhere.

Contracts used (from sibling agents):
    data.market.MarketData(feed).get_bars(symbol, start, end) -> DataFrame
    data.indicators.latest_snapshot(df) -> dict
    data.quant.momentum_score / mean_reversion_zscore / volatility_regime
    strategies.stock_picker.composite_score / initial_stops /
        update_trailing_stop / time_stop_reached
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from data.indicators import latest_snapshot
from data.market import MarketData
from data.quant import mean_reversion_zscore, momentum_score, volatility_regime
from strategies.stock_picker import (
    composite_score,
    initial_stops,
    time_stop_reached,
    update_trailing_stop,
)

LOG = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "backtest" / "results"

WARMUP_DAYS = 400  # extra history fetched before `start` for indicator warmup
MIN_HISTORY_BARS = 230  # trading bars required before first signal (SMA-200 + momentum history)
VIX_SYMBOL = "^VIX"
VIX_DIVISOR = 45.0  # VIX level that maps to kalshi macro score 100


# --------------------------------------------------------------------------- helpers
def _norm_index(df: pd.DataFrame) -> pd.DataFrame:
    """Return df with a tz-naive, day-normalized, de-duplicated DatetimeIndex."""
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df[~df.index.duplicated(keep="last")].sort_index()


def _kalshi_series(market: MarketData, fetch_start: str, end: str, days) -> pd.Series:
    """VIX-proxy macro score per trading day (documented Kalshi stand-in)."""
    try:
        vix = _norm_index(market.get_bars(VIX_SYMBOL, fetch_start, end))
        s = (100.0 * (vix["close"] / VIX_DIVISOR)).clip(upper=100.0)
        return s.reindex(days).ffill().fillna(0.0)
    except Exception as exc:  # VIX unavailable -> neutral macro gate
        LOG.warning("VIX proxy unavailable (%s); kalshi score = 0", exc)
        return pd.Series(0.0, index=days)


def _last_close(df: pd.DataFrame, day: pd.Timestamp) -> float:
    return float(df["close"].loc[:day].iloc[-1])


# --------------------------------------------------------------------------- engine
def run_backtest(
    universe: list[str],
    start: str,
    end: str,
    initial_cash: float,
    config: dict,
    market=None,
) -> dict:
    """Run the strategy1 walk-forward backtest.

    Returns {"trades": [...], "metrics": {...}, "equity_curve": DataFrame,
             "config": config, "universe": [...]}.
    Also writes backtest/results/equity_curve.csv.
    """
    market = market or MarketData(feed="iex")
    s1 = config["strategy1"]
    risk = config["risk"]
    min_score = float(s1["min_composite_score"])
    atr_mult = float(s1["atr_trailing_mult"])
    tp_r = float(s1.get("take_profit_r", 2.0))
    time_days = int(s1.get("time_stop_days", 10))
    max_pos = int(risk["max_positions"])

    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    fetch_start = (start_ts - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    fetch_end = (end_ts + pd.Timedelta(days=1)).strftime("%Y-%m-%d")  # end-inclusive

    symbols = sorted(set(universe))
    bars: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = market.get_bars(sym, fetch_start, fetch_end)
        except Exception as exc:
            LOG.warning("skipping %s: %s", sym, exc)
            continue
        if df is None or df.empty:
            LOG.warning("skipping %s: no bars returned", sym)
            continue
        bars[sym] = _norm_index(df)

    day_set: set[pd.Timestamp] = set()
    for df in bars.values():
        idx = df.index
        day_set.update(idx[(idx >= start_ts) & (idx <= end_ts)])
    days = sorted(day_set)
    if not days:
        raise ValueError("no trading days in range for the given universe")

    kalshi = _kalshi_series(market, fetch_start, fetch_end, days)

    cash = float(initial_cash)
    equity = float(initial_cash)
    positions: dict[str, dict] = {}  # symbol -> position state
    pending: list[dict] = []  # signal-day orders to execute at next open
    trades: list[dict] = []
    equity_rows: list[tuple] = []

    for day in days:
        day_date = day.date()

        # -- 1) execute pending entry orders at today's open -----------------
        for order in pending:
            sym = order["symbol"]
            df = bars.get(sym)
            if (
                df is None
                or day not in df.index
                or sym in positions
                or len(positions) >= max_pos
            ):
                continue  # stale order: dropped (documented one-shot fill)
            open_px = float(df.loc[day, "open"])
            if not np.isfinite(open_px) or open_px <= 0:
                continue
            shares = int((equity / max_pos) // open_px)
            cost = shares * open_px
            if shares < 1 or cost > cash:
                continue
            stops = initial_stops(open_px, order["atr"], atr_mult, tp_r)
            positions[sym] = {
                "entry_date": day_date,
                "entry_price": open_px,
                "shares": shares,
                "stop": float(stops["stop"]),
                "take_profit": float(stops["take_profit"]),
                "highest_high": float(df.loc[day, "high"]),
            }
            cash -= cost
        pending = []

        # -- 2) exits -------------------------------------------------------
        for sym in list(positions):
            pos = positions[sym]
            df = bars[sym]
            if day not in df.index:
                continue
            row = df.loc[day]
            hi, lo, cl = float(row["high"]), float(row["low"]), float(row["close"])
            pos["highest_high"] = max(pos["highest_high"], hi)
            hist = df.loc[:day]
            atr_today = float(latest_snapshot(hist)["atr_14"])
            if np.isfinite(atr_today) and atr_today > 0:
                pos["stop"] = update_trailing_stop(
                    pos["stop"], pos["highest_high"], atr_today, atr_mult
                )
            exit_px, reason = None, None
            if lo <= pos["stop"]:
                # conservative: stop takes precedence if both stop and TP
                # are touched inside the same bar
                exit_px, reason = pos["stop"], "trailing_stop"
            elif hi >= pos["take_profit"]:
                exit_px, reason = pos["take_profit"], "take_profit"
            elif time_stop_reached(pos["entry_date"], day_date, time_days):
                exit_px, reason = cl, "time_stop"
            if exit_px is not None:
                cash += pos["shares"] * exit_px
                pnl = (exit_px - pos["entry_price"]) * pos["shares"]
                trades.append(
                    {
                        "symbol": sym,
                        "entry_date": pos["entry_date"].isoformat(),
                        "exit_date": day_date.isoformat(),
                        "entry_price": round(pos["entry_price"], 2),
                        "exit_price": round(exit_px, 2),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(exit_px / pos["entry_price"] - 1, 4),
                        "exit_reason": reason,
                    }
                )
                del positions[sym]

        # -- 3) entry signals at the close ----------------------------------
        for sym in symbols:
            if len(positions) + len(pending) >= max_pos:
                break
            if sym in positions:
                continue
            df = bars.get(sym)
            if df is None or day not in df.index:
                continue
            hist = df.loc[:day]
            if len(hist) < MIN_HISTORY_BARS:
                continue
            snap = latest_snapshot(hist)
            if not np.isfinite(snap["price"]) or not np.isfinite(snap["atr_14"]):
                continue
            if snap["atr_14"] <= 0:
                continue
            dossier = {
                "symbol": sym,
                "price": snap["price"],
                "indicators": snap,
                "quant": {
                    "momentum_score": momentum_score(hist),
                    "mr_zscore": mean_reversion_zscore(hist),
                    "volatility_regime": volatility_regime(hist),
                },
                "news": [],
                "social": {"score": 0.0, "message_volume": 0},
                "kalshi": {"score": float(kalshi.loc[day])},
            }
            scored = composite_score(dossier)
            if scored["veto"] or scored["score"] < min_score:
                continue
            pending.append({"symbol": sym, "atr": float(snap["atr_14"])})

        # -- 4) mark-to-market ----------------------------------------------
        equity = cash + sum(
            pos["shares"] * _last_close(bars[s], day) for s, pos in positions.items()
        )
        equity_rows.append((day, equity))

    # -- close any open positions at the final close (documented) -----------
    last_day = days[-1]
    for sym, pos in list(positions.items()):
        exit_px = _last_close(bars[sym], last_day)
        cash += pos["shares"] * exit_px
        pnl = (exit_px - pos["entry_price"]) * pos["shares"]
        trades.append(
            {
                "symbol": sym,
                "entry_date": pos["entry_date"].isoformat(),
                "exit_date": last_day.date().isoformat(),
                "entry_price": round(pos["entry_price"], 2),
                "exit_price": round(exit_px, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(exit_px / pos["entry_price"] - 1, 4),
                "exit_reason": "end_of_backtest",
            }
        )
    positions.clear()

    equity_curve = pd.DataFrame(equity_rows, columns=["date", "equity"]).set_index("date")
    equity_curve.loc[last_day, "equity"] = cash  # final cash after forced closes
    metrics = _metrics(equity_curve, trades, initial_cash)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    equity_curve.to_csv(RESULTS_DIR / "equity_curve.csv")

    return {
        "trades": trades,
        "metrics": metrics,
        "equity_curve": equity_curve,
        "config": config,
        "universe": symbols,
    }


def _metrics(equity_curve: pd.DataFrame, trades: list[dict], initial_cash: float) -> dict:
    eq = equity_curve["equity"]
    daily_ret = eq.pct_change().dropna()
    total_return = float(eq.iloc[-1] / initial_cash - 1)
    vol = float(daily_ret.std())
    sharpe = float(daily_ret.mean() / vol * np.sqrt(252)) if vol > 0 else 0.0
    roll_max = eq.cummax()
    max_dd = float(((eq - roll_max) / roll_max).min())

    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    return {
        "total_return": round(total_return, 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "profit_factor": round(profit_factor, 3) if np.isfinite(profit_factor) else "inf",
        "n_trades": n,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
    }
