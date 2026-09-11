#!/usr/bin/env python3
"""Backtest the production 940-universe momentum strategy.

Mirrors production as closely as daily bars allow:
  - screener/screen.py::score_frame  (exact composite + liquidity gates)
  - screener/build_ten.py            (top-40 -> max-2-per-sector -> top 10)
  - screener/sizing.py               (1% risk on $100k paper: floor(1000/(entry-stop)))

DOCUMENTED DEVIATIONS FROM PRODUCTION
  D1. Weekly (Friday-close) rebalance instead of 3x/day: only daily bars exist.
  D2. Entry at the NEXT trading day's open. Production enters at scan-time
      price (effectively the prior close); the next open is the tradable proxy.
  D3. Per-position notional capped at 25% of equity (defensive). Production's
      fixed-100-share sizing has no such cap, and the 1%-risk sizer can imply
      outsized notional on low-ATR expensive names.
  D4. Staleness guard: a symbol whose latest bar is >7 calendar days before
      the rebalance date is unscorable (the merged DB mixes Yahoo/Nasdaq
      sources with different vintages).
  D5. Benchmark (a) costs use the clean-room bps convention (1bp + 5bps);
      the strategy uses $1 flat + 5bps per the task spec.

NO-LOOKAHEAD: every score uses only bars with date <= rebalance date.
Read-only on data/*.duckdb. Never fabricates bars: missing history -> skip.

Exit conventions (stated, deterministic):
  - Stop/target checked on each daily bar; stop wins on ambiguous days.
  - Stop exit fill:  min(open, stop)   (gap-through filled at the worse open)
  - Target exit fill: max(open, target) (gap-up fills at the better open)
  - A name dropped at rebalance exits at the next open, regardless of that
    day's bar. A name kept in the top 10 keeps its ORIGINAL brackets.
  - Costs: $1 per fill (entry and exit) + 5 bps slippage on notional, each way.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from screener.screen import score_frame  # noqa: E402
from backtest.vectorized import (  # noqa: E402
    rank_to_weights,
    simulate as vec_simulate,
    stats as vec_stats,
)

DB = BASE / "data" / "market.duckdb"
UNIVERSE_FILE = BASE / "data" / "universe_symbols.txt"
SECTOR_CACHE_FILE = BASE / "data" / "sector_cache.json"
SPY_DB = Path("/tmp/spy_benchmark.duckdb")

EQUITY0 = 100_000.0
RISK_DOLLARS = 1_000.0          # 1% of paper equity per trade
COMMISSION_FLAT = 1.0           # $1 per fill
SLIPPAGE_BPS = 5.0              # 5 bps each way
MAX_NOTIONAL_PCT = 0.25         # D3 defensive cap
STALE_DAYS = 7                  # D4
MIN_BARS = 65


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def load_frames(symbols: list[str]) -> dict[str, dict]:
    """{symbol: {date: (open, high, low, close, volume)}} — read-only."""
    con = duckdb.connect(str(DB), read_only=True)
    try:
        ph = ", ".join(["?"] * len(symbols))
        df = con.execute(
            "SELECT symbol, date, open, high, low, close, volume "
            f"FROM daily_bars WHERE symbol IN ({ph}) ORDER BY symbol, date",
            symbols,
        ).fetchdf()
    finally:
        con.close()
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    frames: dict[str, dict] = {}
    for sym, g in df.groupby("symbol", sort=False):
        g = g.sort_values("date")
        bars = {}
        for r in g.itertuples():
            d = r.date.date() if hasattr(r.date, "date") else r.date
            o, h, l, c, v = r.open, r.high, r.low, r.close, r.volume
            if not (np.isfinite(o) and np.isfinite(h) and np.isfinite(l)
                    and np.isfinite(c) and c > 0):
                continue
            bars[d] = (float(o), float(h), float(l), float(c),
                       float(v) if np.isfinite(v) and v > 0 else 0.0)
        frames[sym] = bars
    return frames


def load_spy(dates: list[date]) -> pd.Series:
    con = duckdb.connect(str(SPY_DB), read_only=True)
    try:
        df = con.execute("SELECT date, close FROM spy ORDER BY date").fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    s = pd.Series(df["close"].to_numpy(dtype=float),
                  index=pd.to_datetime(df["date"]))
    idx = pd.to_datetime(dates)
    out = s.reindex(idx)
    missing = out.isna().sum()
    if missing:
        raise RuntimeError(f"SPY missing {missing} of {len(idx)} window dates")
    return out


# ---------------------------------------------------------------------------
# Scoring (production mirror)
# ---------------------------------------------------------------------------
def select_top10(t: date, frames: dict, sector_cache: dict) -> tuple[list, dict]:
    """Score every symbol on bars <= t. Returns (top10 cards, diag)."""
    scored, n_insufficient, n_gate, n_stale = [], 0, 0, 0
    for sym, bars in frames.items():
        sub_dates = [d for d in bars if d <= t]
        if len(sub_dates) < MIN_BARS:
            n_insufficient += 1
            continue
        if (t - sub_dates[-1]).days > STALE_DAYS:
            n_stale += 1
            continue
        sub = pd.DataFrame(
            [bars[d] for d in sub_dates],
            columns=["open", "high", "low", "close", "volume"],
        )
        s = score_frame(sym, sub)
        if s is None:
            n_gate += 1
            continue
        scored.append(s)
    scored.sort(key=lambda r: r["composite_score"], reverse=True)
    top40 = scored[:40]
    known = sum(1 for c in top40
                if sector_cache.get(c["symbol"], "Unknown") != "Unknown")
    ten, counts = [], {}
    for c in top40:
        if len(ten) >= 10:
            break
        sec = sector_cache.get(c["symbol"], "Unknown")
        if counts.get(sec, 0) >= 2:
            continue
        counts[sec] = counts.get(sec, 0) + 1
        c = dict(c)
        c["sector"] = sec
        ten.append(c)
    diag = {
        "n_scored": len(scored),
        "n_insufficient_history": n_insufficient,
        "n_gate_fail": n_gate,
        "n_stale": n_stale,
        "top40_sector_coverage_pct": round(100.0 * known / max(1, len(top40)), 1),
        "n_top10": len(ten),
        "sector_counts": counts,
    }
    return ten, diag


# ---------------------------------------------------------------------------
# Strategy simulation
# ---------------------------------------------------------------------------
def run_strategy(frames, dates, rebalance_dates, sector_cache):
    cash = EQUITY0
    held: dict[str, dict] = {}   # sym -> entry_date, entry, shares, stop, target
    trades: list[dict] = []
    equity_curve: list[float] = []
    eq_dates: list[date] = []
    diags: list[dict] = []
    counters = {"entry_no_bar": 0, "entry_cash_skip": 0, "entry_bad_bracket": 0,
                "entry_cap_hit": 0}
    turnover_events: list[tuple[date, float]] = []  # (action date, notional)

    date_idx = {d: i for i, d in enumerate(dates)}
    action_for = {}  # action date -> rebalance date
    for t in rebalance_dates:
        i = date_idx[t]
        if i + 1 < len(dates):
            action_for[dates[i + 1]] = t

    last_close: dict[str, float] = {}

    def mark_to_market(d):
        val = cash
        for sym, p in held.items():
            px = last_close.get(sym, p["entry"])
            val += p["shares"] * px
        return val

    slippage = SLIPPAGE_BPS / 10_000.0
    start = date_idx[rebalance_dates[0]] + 1  # first action day

    for di in range(start, len(dates)):
        d = dates[di]
        traded_notional = 0.0

        # --- 1. rebalance actions at the open (dropped names exit, new enter)
        if d in action_for:
            t = action_for[d]
            top10, diag = select_top10(t, frames, sector_cache)
            diag["rebalance_date"] = t.isoformat()
            diags.append(diag)
            new_syms = [c["symbol"] for c in top10]

            for sym in list(held):
                if sym not in new_syms:
                    bar = frames[sym].get(d)
                    if bar is None:
                        continue  # cannot exit without a print; try tomorrow
                    o = bar[0]
                    p = held.pop(sym)
                    px = o
                    cost = COMMISSION_FLAT + slippage * p["shares"] * px
                    cash += p["shares"] * px - cost
                    traded_notional += p["shares"] * px
                    trades.append(_close_trade(p, sym, d, px, cost, "rebalance_drop"))

            for c in top10:
                sym = c["symbol"]
                if sym in held:
                    continue
                bar = frames[sym].get(d)
                if bar is None:
                    counters["entry_no_bar"] += 1
                    continue
                entry = bar[0]
                atr14 = c["atr_14"] or 0.0
                stop = entry - 3.0 * atr14
                target = entry + 6.0 * atr14
                if not (np.isfinite(stop) and np.isfinite(target)
                        and entry > stop and target > entry):
                    counters["entry_bad_bracket"] += 1
                    continue
                shares = max(1, int(RISK_DOLLARS // (entry - stop)))
                cap = int((MAX_NOTIONAL_PCT * mark_to_market(d)) // entry)
                if cap < shares:
                    counters["entry_cap_hit"] += 1
                    shares = max(0, cap)
                cost_in = COMMISSION_FLAT + slippage * shares * entry
                if shares < 1 or cash < shares * entry + cost_in:
                    counters["entry_cash_skip"] += 1
                    continue
                cash -= shares * entry + cost_in
                traded_notional += shares * entry
                held[sym] = {"entry_date": d, "entry": entry, "shares": shares,
                             "stop": stop, "target": target,
                             "cost_in": cost_in}

        # --- 2. stop/target scan on today's bar (stop first on ambiguous days)
        for sym in list(held):
            bar = frames[sym].get(d)
            if bar is None:
                continue
            o, h, l, _c, _v = bar
            p = held[sym]
            exit_px, reason = None, None
            if l <= p["stop"]:
                exit_px = min(o, p["stop"])
                reason = "stop"
            elif h >= p["target"]:
                exit_px = max(o, p["target"])
                reason = "target"
            if exit_px is not None:
                held.pop(sym)
                cost = COMMISSION_FLAT + slippage * p["shares"] * exit_px
                cash += p["shares"] * exit_px - cost
                traded_notional += p["shares"] * exit_px
                trades.append(_close_trade(p, sym, d, exit_px, cost, reason))

        # --- 3. mark to market at the close
        for sym in held:
            bar = frames[sym].get(d)
            if bar is not None:
                last_close[sym] = bar[3]
        eq = mark_to_market(d)
        equity_curve.append(eq)
        eq_dates.append(d)
        if traded_notional:
            turnover_events.append((d, traded_notional))

    return {
        "equity": np.array(equity_curve),
        "dates": eq_dates,
        "trades": trades,
        "diags": diags,
        "counters": counters,
        "turnover_events": turnover_events,
        "open_positions": {s: dict(p) for s, p in held.items()},
        "start_cash": EQUITY0,
    }


def _close_trade(p, sym, exit_date, exit_px, cost_out, reason):
    gross = (exit_px - p["entry"]) * p["shares"]
    net = gross - p["cost_in"] - cost_out
    risk = (p["entry"] - p["stop"]) * p["shares"]
    return {
        "symbol": sym,
        "entry_date": p["entry_date"].isoformat(),
        "exit_date": exit_date.isoformat(),
        "entry": round(p["entry"], 2),
        "exit": round(exit_px, 2),
        "shares": p["shares"],
        "reason": reason,
        "hold_days": (exit_date - p["entry_date"]).days,
        "pnl_net": round(net, 2),
        "r_multiple": round(net / risk, 3) if risk > 0 else 0.0,
        "costs": round(p["cost_in"] + cost_out, 2),
    }


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------
def gate_matrix(frames, dates, symbols):
    """(T, S) bool: production liquidity gates, point-in-time."""
    T = len(dates)
    gate = np.zeros((T, len(symbols)), dtype=bool)
    for j, sym in enumerate(symbols):
        bars = frames[sym]
        c = np.array([bars[d][3] if d in bars else np.nan for d in dates])
        v = np.array([bars[d][4] if d in bars else np.nan for d in dates])
        valid = np.isfinite(c) & (c > 0)
        nbars = np.cumsum(valid) >= MIN_BARS
        price_ok = np.isfinite(c) & (c >= 2.0)
        dvol = pd.Series(np.nan_to_num(c) * np.nan_to_num(v)).rolling(
            20, min_periods=20).mean().to_numpy() >= 5_000_000
        gate[:, j] = valid & nbars & price_ok & dvol
    return gate


def run_benchmark_eqweight(frames, dates, symbols):
    """Equal-weight buy-and-hold of the scored universe, monthly rebalance."""
    gate = gate_matrix(frames, dates, symbols)
    months = {}
    for d in dates:
        months.setdefault((d.year, d.month), []).append(d)
    month_ends = sorted(max(v) for v in months.values())
    # need 65 trading days of history before the first rebalance
    month_ends = [d for d in month_ends if dates.index(d) >= MIN_BARS]
    r_idx = np.array([dates.index(d) for d in month_ends])

    T = len(dates)
    S = len(symbols)
    closes = np.full((T, S), np.nan)
    volumes = np.zeros((T, S))
    for j, sym in enumerate(symbols):
        bars = frames[sym]
        for i, d in enumerate(dates):
            if d in bars:
                closes[i, j] = bars[d][3]
                volumes[i, j] = bars[d][4]

    ranks = np.where(gate[r_idx], 1.0, np.nan)
    weights = rank_to_weights(ranks, top_n=S, weighting="equal")
    res = vec_simulate(closes, volumes, weights, r_idx,
                       initial_equity=EQUITY0,
                       commission_bps=1.0, slippage_bps=SLIPPAGE_BPS)
    return res, month_ends


def run_benchmark_spy(dates):
    spy = load_spy(dates)
    px = spy.to_numpy(dtype=float)
    shares = (EQUITY0 - COMMISSION_FLAT) / (px[0] * (1 + SLIPPAGE_BPS / 10_000.0))
    equity = shares * px
    return equity


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def trade_stats(trades):
    n = len(trades)
    if n == 0:
        return {"n_trades": 0}
    pnls = np.array([t["pnl_net"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gw, gl = wins.sum(), -losses.sum()
    return {
        "n_trades": n,
        "win_rate": round(float((pnls > 0).mean()), 4),
        "profit_factor": round(float(gw / gl), 3) if gl > 0 else float("inf"),
        "avg_hold_days": round(float(np.mean([t["hold_days"] for t in trades])), 1),
        "total_costs": round(float(sum(t["costs"] for t in trades)), 2),
        "exits": {r: sum(1 for t in trades if t["reason"] == r)
                  for r in ("stop", "target", "rebalance_drop")},
    }


def main():
    symbols = [s.strip() for s in UNIVERSE_FILE.read_text().splitlines()
               if s.strip()]
    sector_cache = json.loads(SECTOR_CACHE_FILE.read_text()) \
        if SECTOR_CACHE_FILE.exists() else {}
    print(f"universe: {len(symbols)} symbols", flush=True)

    frames = load_frames(symbols)
    all_dates = sorted({d for bars in frames.values() for d in bars})
    print(f"data: {all_dates[0]} -> {all_dates[-1]} "
          f"({len(all_dates)} trading days)", flush=True)

    # weekly rebalance dates: last trading day of each ISO week, >=65 bars in
    weeks = {}
    for d in all_dates:
        weeks.setdefault(d.isocalendar()[:2], []).append(d)
    rebalance_dates = sorted(max(v) for v in weeks.values())
    rebalance_dates = [d for d in rebalance_dates
                       if all_dates.index(d) >= MIN_BARS]
    print(f"rebalances: {len(rebalance_dates)}, first {rebalance_dates[0]}, "
          f"last {rebalance_dates[-1]}", flush=True)

    print("running strategy simulation...", flush=True)
    strat = run_strategy(frames, all_dates, rebalance_dates, sector_cache)
    s_dates = strat["dates"]
    n_years = len(s_dates) / 252.0

    # turnover: traded notional / initial equity, annualized
    total_notional = sum(n for _, n in strat["turnover_events"])
    turnover_ann = total_notional / EQUITY0 / n_years if n_years else 0.0
    per_reb_turnover = []
    ev_by_date = {d: n for d, n in strat["turnover_events"]}
    for t in rebalance_dates:
        i = all_dates.index(t) + 1
        if i < len(all_dates):
            d = all_dates[i]
            pre = strat["equity"][s_dates.index(d) - 1] if s_dates.index(d) > 0 \
                else EQUITY0
            per_reb_turnover.append(ev_by_date.get(d, 0.0) / pre)

    s_stats = vec_stats(strat["equity"],
                        dates=np.array(s_dates, dtype="datetime64[D]"),
                        turnover=np.array(per_reb_turnover))
    t_stats = trade_stats(strat["trades"])

    print("running benchmarks...", flush=True)
    bench, month_ends = run_benchmark_eqweight(frames, all_dates, symbols)
    b_start = s_dates[0]
    b0 = all_dates.index(b_start)
    b_stats = vec_stats(bench["equity"][b0:],
                        dates=np.array(all_dates[b0:], dtype="datetime64[D]"),
                        turnover=bench["turnover"])
    spy_eq = run_benchmark_spy(s_dates)
    spy_stats = vec_stats(spy_eq,
                          dates=np.array(s_dates, dtype="datetime64[D]"))

    # ---------------- report ----------------
    L = []
    A = L.append
    A("# Backtest: production 940-universe momentum strategy\n")
    A(f"_Run {date.today().isoformat()}. Read-only on data/*.duckdb; "
      "no bars fabricated._\n")
    A("## Setup\n")
    A(f"- Universe: {len(symbols)} symbols from data/universe_symbols.txt "
      "(all present in DB)")
    A(f"- Data window: {all_dates[0]} -> {all_dates[-1]} "
      f"({len(all_dates)} trading days; ~2y requested, "
      f"~{len(all_dates) / 252:.1f}y available)")
    A(f"- Strategy window: {s_dates[0]} -> {s_dates[-1]} "
      f"({len(s_dates)} days, {n_years:.2f} years)")
    A(f"- Rebalance: weekly at Friday close ({len(rebalance_dates)} rebalances); "
      "entry/exit at next open (deviation D1/D2 documented in script header)")
    A("- Signal: production score_frame composite "
      "(3.0*Vz + 2.0*M20 + 1.5*M60 + 1.5*H + 1.0*R) with gates "
      "(20d avg $vol >= $5M, price >= $2, 65+ bars), no lookahead")
    A("- Portfolio: top-40 -> max 2/sector -> top 10; "
      "stop = entry - 3xATR(14), target = entry + 6xATR(14) (2R)")
    A("- Sizing: $100k paper, shares = floor(1000 / (entry - stop)), "
      "25% notional cap (D3)")
    A("- Costs: $1/fill + 5 bps slippage each way")
    A("- Exits: stop checked before target on ambiguous days; "
      "dropped names exit at next open; kept names hold original brackets")
    A("- Prices: unadjusted closes (splits/dividends not modeled); "
      "survivorship bias present (universe = today's large caps)")
    A("")
    A("## Scoring diagnostics (per-rebalance averages)\n")
    diags = strat["diags"]
    A(f"- Avg symbols scored: "
      f"{np.mean([d['n_scored'] for d in diags]):.0f}")
    A(f"- Avg skipped: insufficient history "
      f"{np.mean([d['n_insufficient_history'] for d in diags]):.0f}, "
      f"gate fail {np.mean([d['n_gate_fail'] for d in diags]):.0f}, "
      f"stale {np.mean([d['n_stale'] for d in diags]):.0f}")
    A(f"- Top-40 sector-cache coverage: "
      f"{np.mean([d['top40_sector_coverage_pct'] for d in diags]):.1f}% "
      f"(cache holds {len(sector_cache)} symbols; missing -> 'Unknown', "
      "also capped at 2)")
    A(f"- Avg top-10 selected: "
      f"{np.mean([d['n_top10'] for d in diags]):.1f}")
    A(f"- Entry skips: {strat['counters']}")
    A(f"- Positions still open at end: {len(strat['open_positions'])} "
      "(unrealized P&L is in the equity curve; trade stats cover closed "
      "trades only)")
    A("")
    A("## Results\n")
    A("| Metric | Strategy (top-10) | Equal-weight universe (monthly) "
      "| SPY buy-and-hold |")
    A("|---|---|---|---|")
    A(f"| Total return | {s_stats['total_return']:.2%} | "
      f"{b_stats['total_return']:.2%} | {spy_stats['total_return']:.2%} |")
    A(f"| CAGR | {s_stats['cagr']:.2%} | {b_stats['cagr']:.2%} | "
      f"{spy_stats['cagr']:.2%} |")
    A(f"| Sharpe | {s_stats['sharpe']:.3f} | {b_stats['sharpe']:.3f} | "
      f"{spy_stats['sharpe']:.3f} |")
    A(f"| Max drawdown | {s_stats['max_drawdown']:.2%} | "
      f"{b_stats['max_drawdown']:.2%} | {spy_stats['max_drawdown']:.2%} |")
    A(f"| Annualized turnover | {s_stats['annualized_turnover']:.2f}x | "
      f"{b_stats['annualized_turnover']:.2f}x | 0.00x |")
    A("")
    A("### Strategy trade-level stats (closed trades only)\n")
    A(f"- Trades: {t_stats['n_trades']}")
    A(f"- Win rate: {t_stats['win_rate']:.1%}")
    A(f"- Profit factor: {t_stats['profit_factor']}")
    A(f"- Avg holding: {t_stats['avg_hold_days']} days")
    A(f"- Exits: {t_stats['exits']}")
    A(f"- Total costs paid: ${t_stats['total_costs']:,.2f}")
    A(f"- Total traded notional: ${total_notional:,.0f} "
      f"({turnover_ann:.2f}x equity/year)")
    A("")
    A("## Benchmark notes\n")
    A("- Equal-weight: all gate-passing symbols, rebalanced monthly at the "
      "close via the clean-room vectorized engine (1bp commission + 5bps "
      "slippage, both sides; deviation D5).")
    A("- SPY: Yahoo Finance daily closes 2025-08-01..2026-09-10, single "
      "buy at the first window close, $1 + 5bps entry cost, held.")
    A("")

    # verdict
    strat_tr = s_stats["total_return"]
    bench_tr = b_stats["total_return"]
    spy_tr = spy_stats["total_return"]
    beats_eq = strat_tr > bench_tr
    beats_spy = strat_tr > spy_tr
    verdict = (
        f"The ranked top-10 strategy returned {strat_tr:.1%} "
        f"(CAGR {s_stats['cagr']:.1%}, Sharpe {s_stats['sharpe']:.2f}) vs "
        f"{bench_tr:.1%} for equal-weight and {spy_tr:.1%} for SPY, net of "
        f"${t_stats['total_costs']:,.0f} in costs across {t_stats['n_trades']} "
        f"closed trades. "
    )
    if beats_eq and beats_spy:
        verdict += ("It beat both benchmarks net of costs — the ranking "
                    "signal adds value over naive large-cap exposure in this "
                    "window. Caveats before trusting it: ~1 year of data, "
                    "survivorship bias (today's large caps), unadjusted "
                    "prices, and weekly (not 3x/day) rebalancing.")
    elif beats_eq:
        verdict += ("It beat equal-weight but not SPY — the ranking adds "
                    "value over naive stock-picking within large caps, but "
                    "a passive index still won. The strategy's edge, if any, "
                    "is stock selection, not market timing.")
    else:
        verdict += ("It did NOT beat equal-weight net of costs — the "
                    "composite ranking, sector cap, and 3xATR/2R brackets "
                    "as implemented do not earn their turnover in this "
                    "window. Do not trade this on real capital; the next "
                    "step is diagnosing whether the signal, the sizing, or "
                    "the exit rules are the leak.")
    A("## Verdict\n")
    A(verdict)
    A("")

    report = "\n".join(L)
    out_path = BASE / "reports" / f"backtest_940_{date.today().isoformat()}.md"
    out_path.write_text(report)
    print(report)
    print(f"\nreport -> {out_path}")


if __name__ == "__main__":
    main()
