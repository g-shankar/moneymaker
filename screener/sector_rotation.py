#!/usr/bin/env python3
"""Monthly sector-rotation paper system (dual momentum on US sector ETFs).

Runs on month-end data, rebalanced in the first days of the new month:

  1. Universe: 11 SPDR sector ETFs + SPY (benchmark) + SGOV (cash leg).
  2. Entry: 6-month relative strength vs SPY (must be positive) AND price
     above the 200-day SMA (absolute trend filter).
  3. Take the top 4 qualifiers, sized by INVERSE 60-day volatility
     (equal risk, not equal dollars), single-sector cap 40%.
  4. Churn buffer: a newcomer only displaces a held sector if it beats the
     weakest held qualifier by >2pp of relative strength.
  5. Regime: if SPY itself is below its 200-day SMA, sector exposure is
     capped at 50%, remainder to SGOV. Partial defense, never binary.
  6. Empty slots (fewer than 4 qualify) go to SGOV.

Paper-only: SECTOR_PAPER_EQUITY is a separate $100k paper sleeve, tracked
independently from the stock momentum book. No broker, no orders, ever.

Usage: ./.venv/bin/python -m screener.sector_rotation [--date YYYY-MM-DD]
Appends to data/sector_rotation_ledger.json and updates
state/sector_rotation.json (last holdings, for the churn buffer).
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data.indicators import sma

BASE = Path(__file__).resolve().parent.parent
SECTORS = ["XLK", "XLF", "XLV", "XLI", "XLE", "XLU", "XLP", "XLY", "XLB", "XLRE", "XLC"]
BENCH = "SPY"
CASH = "SGOV"
ALL = SECTORS + [BENCH, CASH]

SECTOR_PAPER_EQUITY = 100_000.0
TOP_N = 4
RS_BUFFER_PP = 2.0
SINGLE_SECTOR_CAP = 0.40
REGIME_EQUITY_CAP = 0.50
LOOKBACK_RS = 126   # ~6 months of trading days
LOOKBACK_VOL = 60
SMA_LONG = 200

STATE_FILE = BASE / "state" / "sector_rotation.json"
LEDGER_FILE = BASE / "data" / "sector_rotation_ledger.json"


def month_end(ref: date) -> date:
    """Last calendar day of the month containing ref."""
    nxt = date(ref.year + (ref.month == 12), ref.month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def load_prices() -> dict[str, pd.Series]:
    out = {}
    for sym in ALL:
        try:
            df = yf.download(sym, period="2y", interval="1d", progress=False,
                             auto_adjust=True)
            if df is None or df.empty:
                continue
            close = df["Close"][sym] if sym in df["Close"].columns else df["Close"]
            out[sym] = close.dropna()
        except Exception as exc:  # noqa: BLE001
            print(f"  download failed for {sym}: {exc}", file=sys.stderr)
    return out


def signals(prices: dict[str, pd.Series], asof: pd.Timestamp):
    """Compute RS vs SPY, 200-day filter, 60-day vol as of `asof`."""
    if BENCH not in prices:
        raise RuntimeError("no SPY data — cannot compute relative strength")
    out = {}
    for sym in SECTORS:
        px = prices.get(sym)
        if px is None or len(px) < SMA_LONG + 5:
            continue
        hist = px.loc[:asof]
        if len(hist) < SMA_LONG + 5:
            continue
        bench = prices[BENCH].loc[:asof]
        rs = hist.iloc[-1] / hist.iloc[-1 - LOOKBACK_RS] - 1.0
        rs_b = bench.iloc[-1] / bench.iloc[-1 - LOOKBACK_RS] - 1.0
        rel = (rs - rs_b) * 100.0
        above = hist.iloc[-1] > sma(hist, SMA_LONG).iloc[-1]
        rets = hist.pct_change().iloc[-LOOKBACK_VOL:]
        vol = float(rets.std() * np.sqrt(252)) if len(rets) >= 20 else float("nan")
        out[sym] = {"rel_strength_pp": round(rel, 2),
                    "above_200d": bool(above),
                    "vol_ann": round(vol, 4),
                    "price": round(float(hist.iloc[-1]), 2)}
    spy_hist = prices[BENCH].loc[:asof]
    regime_ok = bool(spy_hist.iloc[-1] > sma(spy_hist, SMA_LONG).iloc[-1])
    return out, regime_ok


def select(sig: dict, prev_holdings: list[str]):
    """Top-4 qualifiers with churn buffer against previous holdings."""
    qual = {s: v for s, v in sig.items()
            if v["rel_strength_pp"] > 0 and v["above_200d"]
            and np.isfinite(v["vol_ann"]) and v["vol_ann"] > 0}
    ranked = sorted(qual, key=lambda s: qual[s]["rel_strength_pp"], reverse=True)
    final: list[str] = []
    # incumbency: keep previously held qualifiers first
    for s in prev_holdings:
        if s in qual and len(final) < TOP_N and s not in final:
            final.append(s)
    for s in ranked:
        if s in final:
            continue
        if len(final) < TOP_N:
            final.append(s)
        else:
            weakest = min(final, key=lambda x: qual[x]["rel_strength_pp"])
            if qual[s]["rel_strength_pp"] - qual[weakest]["rel_strength_pp"] > RS_BUFFER_PP:
                final.remove(weakest)
                final.append(s)
    return final, qual


def weight(holdings: list[str], qual: dict, regime_ok: bool):
    """Inverse-vol weights with slot scaling: each of the 4 slots is 25% of
    the book, so unfilled slots (fewer than 4 qualifiers) go to SGOV.
    40% single-sector cap (excess redistributed to the other sectors),
    50% regime cap when SPY is below its 200-day SMA. SGOV takes the rest."""
    w: dict[str, float] = {}
    if holdings:
        inv = {s: 1.0 / qual[s]["vol_ann"] for s in holdings}
        tot = sum(inv.values())
        slot_scale = len(holdings) / TOP_N
        w = {s: inv[s] / tot * slot_scale for s in holdings}
        for s in list(w):
            if w[s] > SINGLE_SECTOR_CAP:
                excess = w[s] - SINGLE_SECTOR_CAP
                w[s] = SINGLE_SECTOR_CAP
                rest = [x for x in w if x != s]
                if rest:
                    rtot = sum(w[x] for x in rest)
                    for x in rest:
                        w[x] += excess * (w[x] / rtot)
    equity_w = sum(w.values())
    if not regime_ok and equity_w > 0:
        scale = min(1.0, REGIME_EQUITY_CAP / equity_w)
        w = {s: v * scale for s, v in w.items()}
        equity_w = sum(w.values())
    cash = 1.0 - equity_w
    if cash > 0.0005:
        w[CASH] = round(cash, 4)
    return {s: round(v, 4) for s, v in w.items() if v > 0.0005}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="signal date YYYY-MM-DD (default: last month-end)")
    args = ap.parse_args()
    ref = date.fromisoformat(args.date) if args.date else date.today()
    # default: signals as of the last completed month-end
    asof = month_end(args.date and ref or (date(ref.year, ref.month, 1) - timedelta(days=1)))
    print(f"sector rotation signals as of month-end {asof.isoformat()}")

    prices = load_prices()
    asof_ts = pd.Timestamp(asof)
    # use last available bar at or before month-end
    sig, regime_ok = signals(prices, asof_ts)
    print(f"  sectors with data: {len(sig)}/11, SPY above 200d: {regime_ok}")

    prev = []
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text()).get("holdings", [])
        except Exception:
            prev = []
    holdings, qual = select(sig, prev)
    weights = weight(holdings, qual, regime_ok)

    # build paper tickets
    tickets = []
    for sym, wt in sorted(weights.items(), key=lambda kv: -kv[1]):
        dollars = SECTOR_PAPER_EQUITY * wt
        if sym == CASH:
            price, qty = 100.0, round(dollars / 100.0, 2)
        else:
            price = qual[sym]["price"]
            qty = int(dollars // price)
        tickets.append({"symbol": sym, "weight": wt,
                        "dollars": round(dollars, 2),
                        "price": price, "qty": qty,
                        "rel_strength_pp": qual.get(sym, {}).get("rel_strength_pp"),
                        "vol_ann": qual.get(sym, {}).get("vol_ann")})

    entry = {"signal_date": asof.isoformat(), "run_date": date.today().isoformat(),
             "regime_spy_above_200d": regime_ok,
             "prev_holdings": prev, "holdings": holdings,
             "weights": weights, "tickets": tickets,
             "paper_equity": SECTOR_PAPER_EQUITY,
             "notes": "Paper sleeve, separate from the stock momentum book. No orders placed."}
    ledger = json.loads(LEDGER_FILE.read_text()) if LEDGER_FILE.exists() else []
    ledger.append(entry)
    LEDGER_FILE.write_text(json.dumps(ledger, indent=1))
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"holdings": holdings,
                                      "asof": asof.isoformat()}, indent=1))

    print(f"  holdings: {holdings} (prev: {prev})")
    for t in tickets:
        print(f"    {t['symbol']:5s} {t['weight']*100:5.1f}%  ${t['dollars']:>9,.0f}  "
              f"qty={t['qty']} @ {t['price']}")
    print(f"ledger -> {LEDGER_FILE}")


if __name__ == "__main__":
    main()
