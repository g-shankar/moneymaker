#!/usr/bin/env python3
"""Validation suite driver for Strategy 1 — READ-ONLY vs engine/strategy code.

Calls backtest.engine.run_backtest with varying configs only. Pre-fetches all
bars once into a scratch dir (survives restarts) and serves slices, so repeated
runs are CPU-only. Writes per-cell JSON to the scratch dir. Idempotent: cells
with existing output are skipped.
"""
import sys, os, json, pickle, time, argparse

sys.path.insert(0, "/home/hatch/workspace/trading")
os.chdir("/home/hatch/workspace/trading")

import numpy as np
import pandas as pd

from backtest.engine import run_backtest
from data.market import MarketData

UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO",
    "JPM", "XOM", "UNH", "V", "MA", "JNJ", "PG", "COST", "HD",
    "NFLX", "AMD", "CRM",
]
SCRATCH = "/home/hatch/workspace/trading/backtest/.validation_scratch"
os.makedirs(SCRATCH, exist_ok=True)
CACHE_PKL = os.path.join(SCRATCH, "bars_cache.pkl")
GSTART, GEND = "2023-07-01", "2026-09-12"
FULL = ("2024-09-01", "2026-09-01")
SEGMENTS = [
    ("2024-09-01", "2025-03-01"),
    ("2025-03-01", "2025-09-01"),
    ("2025-09-01", "2026-03-01"),
    ("2026-03-01", "2026-09-01"),
]
ATR_GRID = [2.0, 2.25, 2.5, 2.75, 3.0]


def build_cache():
    m = MarketData(feed="iex")
    cache = {}
    for sym in UNIVERSE + ["^VIX"]:
        try:
            df = m.get_bars(sym, GSTART, GEND)
        except Exception as e:  # noqa: BLE001
            print(f"fetch fail {sym}: {e}", flush=True)
            df = pd.DataFrame()
        cache[sym] = df
        print(f"cached {sym}: {len(df)} bars", flush=True)
    with open(CACHE_PKL, "wb") as f:
        pickle.dump(cache, f)
    return cache


def load_cache():
    if os.path.exists(CACHE_PKL):
        with open(CACHE_PKL, "rb") as f:
            return pickle.load(f)
    return build_cache()


class SliceMarket:
    """Serve [start:end] slices from one pre-fetched global window."""

    def __init__(self, cache):
        self.cache = cache

    def get_bars(self, symbol, start, end):
        df = self.cache.get(symbol)
        if df is None or df.empty:
            return pd.DataFrame()
        return df.loc[start:end]


def make_config(atr, r):
    return {
        "strategy1": {
            "min_composite_score": 60.0,
            "atr_trailing_mult": atr,
            "take_profit_r": r,
            "time_stop_days": 10,
        },
        "risk": {"max_positions": 8},
    }


def run_cell(tag, start, end, atr, r, market):
    out_path = os.path.join(SCRATCH, f"val_{tag}.json")
    if os.path.exists(out_path):
        print(f"{tag}: SKIPPED (already done)", flush=True)
        with open(out_path) as f:
            return json.load(f)
    t0 = time.time()
    res = run_backtest(UNIVERSE, start, end, 100000.0, make_config(atr, r), market=market)
    dt = time.time() - t0
    eq = res["equity_curve"]
    out = {
        "tag": tag, "start": start, "end": end, "atr": atr, "r": r,
        "metrics": res["metrics"], "trades": res["trades"],
        "equity": [
            (d.strftime("%Y-%m-%d"), float(v))
            for d, v in zip(eq.index, eq["equity"].to_numpy())
        ],
        "runtime_s": round(dt, 1),
    }
    with open(out_path, "w") as f:
        json.dump(out, f)
    print(f"{tag}: {res['metrics']}  ({dt:.0f}s)", flush=True)
    return out


def sensitivity(market, skip_atr300_if_verified=True):
    # Reuse the standalone verify cell as the ATR-3.0 data point when present
    vpath = os.path.join(SCRATCH, "val_verify_atr30.json")
    spath = os.path.join(SCRATCH, "val_sens_atr_3p0.json")
    if skip_atr300_if_verified and os.path.exists(vpath) and not os.path.exists(spath):
        import shutil
        shutil.copy(vpath, spath)
        print("sens_atr_3p0: reused verify cell", flush=True)
    jobs = []
    for atr in ATR_GRID:
        if atr == 3.0 and os.path.exists(spath):
            continue
        jobs.append((f"sens_atr_{str(atr).replace('.', 'p')}", FULL[0], FULL[1], atr, 2.0))
    jobs.append(("sens_r_1p5_atr2p5", FULL[0], FULL[1], 2.5, 1.5))
    jobs.append(("sens_r_2p5_atr2p5", FULL[0], FULL[1], 2.5, 2.5))
    for tag, s, e, atr, r in jobs:
        run_cell(tag, s, e, atr, r, market)


def walkforward(market):
    is_results = {}
    for i in range(3):
        s, e = SEGMENTS[i]
        for atr in ATR_GRID:
            tag = f"wf_is{i+1}_atr_{str(atr).replace('.', 'p')}"
            out = run_cell(tag, s, e, atr, 2.0, market)
            is_results.setdefault(i, []).append(
                (atr, out["metrics"]["sharpe"], out["metrics"]["n_trades"])
            )
    for i in range(3):
        s, e = SEGMENTS[i + 1]
        cands = sorted(is_results[i], key=lambda t: (-t[1], -t[2]))
        winner_atr = cands[0][0]
        print(f"segment {i+1} IS winner: ATR={winner_atr} sharpe={cands[0][1]} n={cands[0][2]}", flush=True)
        run_cell(f"wf_oos{i+2}_tuned", s, e, winner_atr, 2.0, market)
        run_cell(f"wf_oos{i+2}_fixed30", s, e, 3.0, 2.0, market)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["sensitivity", "walkforward", "verify", "all"])
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    cache = load_cache()
    market = SliceMarket(cache)
    if args.mode == "verify":
        run_cell("verify_atr30", FULL[0], FULL[1], 3.0, 2.0, market)
    elif args.mode == "sensitivity":
        if args.only:
            mapping = {
                "atr300": (3.0, 2.0), "atr275": (2.75, 2.0), "atr250": (2.5, 2.0),
                "atr225": (2.25, 2.0), "atr200": (2.0, 2.0),
                "r15": (2.5, 1.5), "r25": (2.5, 2.5),
            }
            atr, r = mapping[args.only]
            run_cell(f"sens_{args.only}", FULL[0], FULL[1], atr, r, market)
        else:
            sensitivity(market)
    elif args.mode == "walkforward":
        walkforward(market)
    elif args.mode == "all":
        sensitivity(market)
        walkforward(market)


if __name__ == "__main__":
    main()
