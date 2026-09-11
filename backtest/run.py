#!/usr/bin/env python3
"""CLI for the strategy1 backtest engine.

Usage:
    python -m backtest.run --start 2024-09-01 --end 2026-09-01 --cash 100000
    python -m backtest.run --universe AAPL,MSFT,NVDA

Writes metrics to stdout and a full report (with real run numbers) to
backtest/results.md. Equity curve CSV lands in backtest/results/equity_curve.csv.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest.engine import RESULTS_DIR, run_backtest  # noqa: E402

DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO",
    "JPM", "XOM", "UNH", "V", "MA", "JNJ", "PG", "COST", "HD",
    "NFLX", "AMD", "CRM",
]

# Inline strategy config. Values and their rationale:
#   min_composite_score 60  -> score gate acting as the council proxy
#                             (no council in backtest; documented).
#   atr_trailing_mult 3.0   -> ATR trailing-stop width (stop = entry - 3*ATR,
#                             ratchets up with highest high since entry).
#   take_profit_r 2.0       -> take-profit at 2R (2x initial risk distance).
#   time_stop_days 10       -> max holding period, calendar days (matches the
#                             sibling time_stop_reached contract).
#   max_positions 8         -> portfolio cap; each position targets
#                             equity/max_positions notional, whole shares.
CONFIG = {
    "strategy1": {
        "min_composite_score": 60.0,
        "atr_trailing_mult": 3.0,
        "take_profit_r": 2.0,
        "time_stop_days": 10,
    },
    "risk": {"max_positions": 8},
}

ASSUMPTIONS = """## Assumptions & Limitations

- **No council — composite proxy.** The live pipeline routes candidates through a
  council gate; the backtest has no council. The composite-score threshold
  (`min_composite_score`) is the documented stand-in for that gate.
- **No news / social / Kalshi history.** News and social inputs are zeroed
  (neutral). Real Kalshi has no 2-year history here, so the macro gate uses a
  documented VIX proxy: `kalshi_score = 100 * min(1, VIX_close / 45)`; the live
  veto rule (score >= 70) applies unchanged.
- **Survivorship bias.** The universe is a current list of ~20 large-cap liquid
  names; delisted/acquired names that were in the index during the window are
  absent, which flatters results.
- **Fill simplifications.** Signals fire at day-t close, filled at day-t+1 open.
  Stop/TP exits assume a fill exactly at the stop/take-profit level (no gap
  through the level, no partial fills). Time-stop exits fill at the close.
  Pending orders are one-shot: if they cannot fill the next open, they lapse.
- **$0 commission, no slippage** beyond the documented fill assumptions.
- **Long-only, no margin, whole shares.** Position size targets
  `equity / max_positions` notional per trade, limited by available cash.
- **10-day time stop is calendar days** (matches the sibling contract).
- **Open positions at the end of the window** are closed at the final close
  with exit_reason `end_of_backtest` so metrics are clean.
- Data: daily bars via MarketData (Alpaca when creds exist, else yfinance),
  fetched with ~400 extra days of warmup history before `start`.
"""


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Run the strategy1 backtest.")
    p.add_argument("--start", default="2024-09-01")
    p.add_argument("--end", default="2026-09-01")
    p.add_argument("--cash", type=float, default=100000.0)
    p.add_argument(
        "--universe",
        default=None,
        help="comma-separated symbols; default is the 20-name liquid list",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    universe = (
        [s.strip().upper() for s in args.universe.split(",") if s.strip()]
        if args.universe
        else DEFAULT_UNIVERSE
    )
    print(
        f"Backtest: {len(universe)} symbols, {args.start} -> {args.end}, "
        f"cash=${args.cash:,.0f}"
    )
    t0 = time.time()
    result = run_backtest(universe, args.start, args.end, args.cash, CONFIG)
    elapsed = time.time() - t0

    m = result["metrics"]
    trades = result["trades"]
    print("\n--- metrics ---")
    for k, v in m.items():
        print(f"  {k:15s} {v}")
    print(f"\nRuntime: {elapsed:.1f}s | trades: {len(trades)}")

    wins = sorted([t for t in trades if t["pnl"] > 0], key=lambda t: t["pnl"], reverse=True)
    losses = sorted([t for t in trades if t["pnl"] <= 0], key=lambda t: t["pnl"])
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    lines = [
        "# Backtest report — strategy1 (long-only)",
        "",
        f"Generated: {date.today().isoformat()}",
        f"Window: {args.start} -> {args.end}",
        f"Universe ({len(universe)}): {', '.join(universe)}",
        f"Initial cash: ${args.cash:,.0f}",
        f"Runtime: {elapsed:.1f}s",
        "",
        "## Config",
        "",
        "```",
        f"strategy1: min_composite_score={CONFIG['strategy1']['min_composite_score']}, "
        f"atr_trailing_mult={CONFIG['strategy1']['atr_trailing_mult']}, "
        f"take_profit_r={CONFIG['strategy1']['take_profit_r']}, "
        f"time_stop_days={CONFIG['strategy1']['time_stop_days']}",
        f"risk: max_positions={CONFIG['risk']['max_positions']}",
        "```",
        "",
        "## Metrics (real run numbers)",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for k, v in m.items():
        lines.append(f"| {k} | {v} |")
    final_eq = float(result["equity_curve"]["equity"].iloc[-1])
    lines += [
        f"| final_equity | {final_eq:,.2f} |",
        "",
        "## Exit reasons",
        "",
        "| reason | count |",
        "|---|---|",
    ]
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {reason} | {count} |")
    lines += ["", "## Top 5 winners", ""]
    for t in wins[:5]:
        lines.append(
            f"- {t['symbol']}: {t['entry_date']} -> {t['exit_date']} "
            f"(${t['entry_price']} -> ${t['exit_price']}) "
            f"pnl ${t['pnl']:,.2f} ({t['pnl_pct']:.1%}) [{t['exit_reason']}]"
        )
    lines += ["", "## Top 5 losers", ""]
    for t in losses[:5]:
        lines.append(
            f"- {t['symbol']}: {t['entry_date']} -> {t['exit_date']} "
            f"(${t['entry_price']} -> ${t['exit_price']}) "
            f"pnl ${t['pnl']:,.2f} ({t['pnl_pct']:.1%}) [{t['exit_reason']}]"
        )
    lines += ["", ASSUMPTIONS]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPO_ROOT / "backtest" / "results.md"
    report_path.write_text("\n".join(lines) + "\n")
    print(f"\nReport written to {report_path}")
    print(f"Equity curve: {RESULTS_DIR / 'equity_curve.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
