"""Vectorized portfolio backtesting engine (clean-room implementation).

Purpose: answer "does ranking by the screener's composite score beat
equal-weight?" by simulating rank-driven portfolios over thousands of
symbols in seconds. This is a *ranking-validation* tool, not a trade-level
simulator — the event-driven `backtest/engine.py` remains the authority for
strategy-level validation (whole-share fills, stop/TP intrabar logic).

Execution model
---------------
* Rebalance dates are integer indices into the time axis. Rankings must be
  **point-in-time**: computed from information available at or before the
  close of the rebalance date. The engine itself cannot detect lookahead in
  its inputs — that is the caller's responsibility.
* Trades execute at the **close** of each rebalance date.
* Between rebalances, positions are buy-and-hold: weights drift with prices,
  no trading, no costs.

Transaction-cost model
----------------------
* Costs are charged in basis points on traded notional, both sides:
      cost = |trade_notional| * (commission_bps + slippage_bps) / 10_000
  Defaults: 1.0 bp commission + 5.0 bps slippage (conservative retail
  estimate; institutional equity slippage is often modeled at 1–10 bps).
* Costs are funded out of the rebalance itself via a one-step fixed-point
  approximation: target notionals are scaled by (V - raw_costs) / V so the
  portfolio ends fully invested with non-negative cash. The residual error
  is second-order in the cost rate (~1e-9 relative) and documented here
  rather than hidden.
* No bid/ask spread model beyond the slippage term; no market-impact model
  (the `volumes` argument is accepted for API compatibility and reserved
  for future liquidity-aware extensions — it does not affect fills today).

Position model
--------------
* Long-only, no margin, no leverage: target-weight rows summing above 1.0
  are renormalized to 1.0 (documented, defensive).
* Fractional shares allowed (ranking-validation simplification; whole-share
  rounding lives in the event engine).
* Missing data: closes are forward-filled for *valuation* only. A symbol
  without a valid (finite, positive) print on a rebalance date cannot be
  traded — its target weight is skipped and the residual stays in cash.
  Existing holdings in such symbols are carried at the last valid price.
  Nothing in the hot path loops over symbols in Python; all per-symbol work
  is numpy broadcasting.

Shapes
------
* closes, volumes: (T, S) float64 — T trading days, S symbols.
* target_weights: (R, S) float64 — R rebalance dates.
* rebalance_dates: (R,) int — indices into the time axis, ascending.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_panel(db_path, symbols=None, start=None, end=None) -> dict:
    """Read daily_bars from the DuckDB market store into aligned numpy arrays.

    Returns {"dates", "symbols", "close", "volume", "valid"} where close and
    volume are (T, S) float64 arrays and valid is a (T, S) bool mask
    (finite, positive close). Duplicate (symbol, date) rows keep the last.
    Suspended/delisted symbols simply have False in `valid` where data is
    absent — never a crash.
    """
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        clauses, params = [], []
        if symbols is not None:
            symbols = list(symbols)
            placeholders = ", ".join(["?"] * len(symbols))
            clauses.append(f"symbol IN ({placeholders})")
            params.extend(symbols)
        if start is not None:
            clauses.append("date >= ?")
            params.append(str(start))
        if end is not None:
            clauses.append("date <= ?")
            params.append(str(end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        df = con.execute(
            f"SELECT symbol, date, close, volume FROM daily_bars {where} "
            "ORDER BY date, symbol",
            params,
        ).fetchdf()
    finally:
        con.close()

    if df.empty:
        return {
            "dates": np.array([], dtype="datetime64[D]"),
            "symbols": [],
            "close": np.zeros((0, 0)),
            "volume": np.zeros((0, 0)),
            "valid": np.zeros((0, 0), dtype=bool),
        }

    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    syms = list(symbols) if symbols is not None else sorted(df["symbol"].unique())

    close = (
        df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
        .reindex(columns=syms)
        .sort_index()
    )
    volume = (
        df.pivot_table(index="date", columns="symbol", values="volume", aggfunc="last")
        .reindex(columns=syms)
        .sort_index()
        .reindex(index=close.index)
    )
    close_m = close.to_numpy(dtype=np.float64)
    vol_m = np.nan_to_num(volume.to_numpy(dtype=np.float64), nan=0.0)
    valid = np.isfinite(close_m) & (close_m > 0)
    return {
        "dates": close.index.to_numpy(dtype="datetime64[D]"),
        "symbols": syms,
        "close": close_m,
        "volume": vol_m,
        "valid": valid,
    }


# ---------------------------------------------------------------------------
# Ranking -> weights
# ---------------------------------------------------------------------------
def rank_to_weights(ranks, top_n: int, weighting: str = "equal") -> np.ndarray:
    """Convert per-rebalance rank scores into target portfolio weights.

    ranks: (R, S) float64, higher = better, NaN = ineligible/unranked.
    top_n: number of names to hold per rebalance.
    weighting: "equal" -> 1/k each; "rank" -> linear rank weights
               (k, k-1, ..., 1) normalized, best rank gets the most.
    Returns (R, S) float64; each row sums to 1.0 when at least one symbol is
    eligible, else 0.0. Ties broken deterministically (stable sort).
    """
    ranks = np.asarray(ranks, dtype=np.float64)
    if ranks.ndim != 2:
        raise ValueError("ranks must be 2-D (rebalances x symbols)")
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if weighting not in ("equal", "rank"):
        raise ValueError("weighting must be 'equal' or 'rank'")

    R, S = ranks.shape
    weights = np.zeros((R, S), dtype=np.float64)
    # NaN -> -inf so ineligible symbols sort last under a stable argsort.
    key = np.where(np.isfinite(ranks), ranks, -np.inf)
    order = np.argsort(-key, axis=1, kind="stable")
    eligible = np.isfinite(ranks)

    for i in range(R):
        row_order = order[i]
        # row_order is sorted best-first; take the eligible prefix.
        k = min(top_n, int(eligible[i].sum()))
        if k == 0:
            continue
        picks = row_order[:k]
        # picks are the top-k by construction, but guard anyway:
        picks = picks[eligible[i, picks]]
        k = len(picks)
        if k == 0:
            continue
        if weighting == "equal":
            weights[i, picks] = 1.0 / k
        else:
            lin = np.arange(k, 0, -1, dtype=np.float64)
            weights[i, picks] = lin / lin.sum()
    return weights


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
def _ffill_closes(closes: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Forward-fill closes for valuation; leading gaps become 0.0.

    Positions can only exist where a valid print was seen, so a 0.0 price
    never multiplies a nonzero share count.
    """
    filled = pd.DataFrame(np.where(valid, closes, np.nan)).ffill().to_numpy(
        dtype=np.float64
    )
    return np.nan_to_num(filled, nan=0.0)


def simulate(
    closes,
    volumes,
    target_weights,
    rebalance_dates,
    initial_equity: float = 100_000.0,
    commission_bps: float = 1.0,
    slippage_bps: float = 5.0,
) -> dict:
    """Vectorized long-only portfolio simulation.

    Returns {"equity": (T,) float64, "turnover": (R,) float64,
             "costs": (R,) float64, "total_costs": float,
             "n_rebalances": int}.
    turnover[r] = traded notional / pre-trade equity at rebalance r.
    """
    closes = np.asarray(closes, dtype=np.float64)
    target_weights = np.asarray(target_weights, dtype=np.float64)
    rebalance_dates = np.asarray(rebalance_dates, dtype=np.int64)
    # volumes: accepted for API compatibility / future liquidity extensions.
    _ = volumes

    if closes.ndim != 2:
        raise ValueError("closes must be 2-D (days x symbols)")
    T, S = closes.shape
    R = len(rebalance_dates)
    if target_weights.shape != (R, S):
        raise ValueError(
            f"target_weights shape {target_weights.shape} != "
            f"(n_rebalances={R}, n_symbols={S})"
        )
    if R and (rebalance_dates.min() < 0 or rebalance_dates.max() >= T):
        raise ValueError("rebalance_dates out of range")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")

    # Canonicalize rebalance order; merge duplicate dates (last wins is
    # meaningless here — duplicates are collapsed).
    if R:
        order = np.argsort(rebalance_dates, kind="stable")
        rebalance_dates = rebalance_dates[order]
        target_weights = target_weights[order]
        keep = np.ones(R, dtype=bool)
        keep[:-1] = rebalance_dates[1:] != rebalance_dates[:-1]
        rebalance_dates = rebalance_dates[keep]
        target_weights = target_weights[keep]
        R = len(rebalance_dates)

    valid = np.isfinite(closes) & (closes > 0)
    close_val = _ffill_closes(closes, valid)
    cost_rate = (commission_bps + slippage_bps) / 10_000.0

    shares = np.zeros(S, dtype=np.float64)
    cash = float(initial_equity)
    equity = np.full(T, initial_equity, dtype=np.float64)
    turnover = np.zeros(R, dtype=np.float64)
    costs = np.zeros(R, dtype=np.float64)

    prev_t = 0
    for i, t in enumerate(rebalance_dates):
        # Buy-and-hold drift up to (not including) the rebalance close.
        if t > prev_t:
            equity[prev_t:t] = cash + close_val[prev_t:t] @ shares

        price = closes[t]
        tradeable = valid[t]
        w = np.nan_to_num(target_weights[i], nan=0.0)
        w[~tradeable] = 0.0
        w_sum = w.sum()
        if w_sum > 1.0:  # defensive: never lever
            w = w / w_sum

        equity_pre = cash + float(close_val[t] @ shares)
        if equity_pre > 0 and w_sum > 0:
            px = np.where(tradeable, price, 0.0)
            current = shares * px
            desired = w * equity_pre
            trade = desired - current
            raw_cost = np.abs(trade) * cost_rate
            # One-step fixed-point: fund costs out of the rebalance so cash
            # never goes negative; residual is 2nd-order in the cost rate.
            scale = (equity_pre - raw_cost.sum()) / equity_pre
            desired = desired * scale
            trade = desired - current
            cost = np.abs(trade) * cost_rate
            with np.errstate(divide="ignore", invalid="ignore"):
                dshares = np.where(tradeable & (px > 0), trade / px, 0.0)
            shares = shares + dshares
            cash = cash - float(trade.sum()) - float(cost.sum())
            turnover[i] = float(np.abs(trade).sum() / equity_pre)
            costs[i] = float(cost.sum())
        elif equity_pre > 0 and w_sum == 0 and (shares != 0).any():
            # Explicit all-cash target: liquidate everything tradeable.
            px = np.where(tradeable, price, 0.0)
            trade = -shares * px
            cost = np.abs(trade) * cost_rate
            with np.errstate(divide="ignore", invalid="ignore"):
                dshares = np.where(tradeable & (px > 0), trade / px, 0.0)
            shares = shares + dshares
            cash = cash - float(trade.sum()) - float(cost.sum())
            turnover[i] = float(np.abs(trade).sum() / equity_pre)
            costs[i] = float(cost.sum())

        equity[t] = cash + float(close_val[t] @ shares)
        prev_t = t + 1

    if prev_t < T:
        equity[prev_t:] = cash + close_val[prev_t:] @ shares

    return {
        "equity": equity,
        "turnover": turnover,
        "costs": costs,
        "total_costs": float(costs.sum()),
        "n_rebalances": R,
    }


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def stats(equity_curve, dates=None, turnover=None) -> dict:
    """Performance statistics for an equity curve.

    equity_curve: array-like or pd.Series of portfolio values, one per day.
    dates: optional array-like of dates (for max-drawdown peak/trough).
    turnover: optional per-rebalance turnover array (annualized in output).
    """
    if isinstance(equity_curve, pd.Series):
        if dates is None:
            dates = equity_curve.index.to_numpy()
        eq = equity_curve.to_numpy(dtype=np.float64)
    else:
        eq = np.asarray(equity_curve, dtype=np.float64)
    if eq.ndim != 1 or len(eq) < 2:
        raise ValueError("equity_curve must have at least 2 points")
    if not np.all(np.isfinite(eq)) or np.any(eq < 0):
        raise ValueError("equity_curve must be finite and non-negative")

    rets = eq[1:] / eq[:-1] - 1.0
    n = len(eq)
    years = n / TRADING_DAYS_PER_YEAR

    total_return = float(eq[-1] / eq[0] - 1.0)
    cagr = float((eq[-1] / eq[0]) ** (TRADING_DAYS_PER_YEAR / n) - 1.0)
    vol = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    sharpe = float(rets.mean() / vol * np.sqrt(TRADING_DAYS_PER_YEAR)) if vol > 0 else 0.0

    roll_max = np.maximum.accumulate(eq)
    dd = (eq - roll_max) / np.where(roll_max > 0, roll_max, 1.0)
    trough = int(np.argmin(dd))
    peak = int(np.argmax(eq[: trough + 1]))
    max_drawdown = float(dd[trough])

    wins = rets[rets > 0]
    losses = rets[rets < 0]
    win_rate = float(len(wins) / len(rets)) if len(rets) else 0.0
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    out = {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "n_days": n,
    }
    if dates is not None:
        dates = np.asarray(dates)
        out["max_dd_peak"] = dates[peak] if peak < len(dates) else None
        out["max_dd_trough"] = dates[trough] if trough < len(dates) else None
    if turnover is not None:
        turnover = np.asarray(turnover, dtype=np.float64)
        out["annualized_turnover"] = (
            float(turnover.sum() / years) if years > 0 else 0.0
        )
    return out
