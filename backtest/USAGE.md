# Vectorized backtester — usage

`backtest/vectorized.py` is a clean-room, numpy/pandas portfolio simulator.
It answers **"does ranking by the screener's composite score beat
equal-weight?"** over thousands of symbols in seconds. It is a
ranking-validation tool; `backtest/engine.py` remains the authority for
strategy-level validation (whole shares, intrabar stop/TP logic).

## The standard experiment

```python
import sys
sys.path.insert(0, "/home/hatch/workspace/trading")

from backtest.vectorized import load_panel, rank_to_weights, simulate, stats
from screener.store import DB_PATH
import numpy as np

# 1. Load the market panel (dates x symbols) from DuckDB.
panel = load_panel(DB_PATH)          # or load_panel(DB_PATH, symbols=[...])
closes, volumes = panel["close"], panel["volume"]

# 2. Build point-in-time rank scores, one row per rebalance date.
#    Example: composite score recomputed monthly from the screener.
#    MUST be point-in-time (no future data in the ranks).
rebalance_idx = np.arange(21, len(panel["dates"]), 21)   # monthly-ish
ranks = ...  # shape (len(rebalance_idx), n_symbols), higher = better, NaN = ineligible

# 3a. Rank-weighted portfolio: top 20 by composite, best rank gets most weight.
w_rank = rank_to_weights(ranks, top_n=20, weighting="rank")
res_rank = simulate(closes, volumes, w_rank, rebalance_idx,
                    commission_bps=1.0, slippage_bps=5.0)

# 3b. Equal-weight baseline: same top 20, flat weights.
w_eq = rank_to_weights(ranks, top_n=20, weighting="equal")
res_eq = simulate(closes, volumes, w_eq, rebalance_idx,
                  commission_bps=1.0, slippage_bps=5.0)

# 4. Compare.
for name, res in [("rank-weighted", res_rank), ("equal-weight", res_eq)]:
    s = stats(res["equity"], dates=panel["dates"], turnover=res["turnover"])
    print(f"{name}: ret={s['total_return']:.2%}  CAGR={s['cagr']:.2%}  "
          f"Sharpe={s['sharpe']:.2f}  maxDD={s['max_drawdown']:.2%}  "
          f"turnover={s['annualized_turnover']:.1f}x/yr  costs=${res['total_costs']:,.0f}")

# If rank-weighted Sharpe / return is not better than equal-weight,
# the composite score is not adding value over the raw screen.
```

## API notes

- `load_panel(db_path, symbols=None, start=None, end=None)` — read-only;
  safe to call while the Stage-1 sweep is writing (DuckDB snapshot read).
  Returns `close`, `volume`, `valid` mask; missing data never crashes.
- `rank_to_weights(ranks, top_n, weighting="equal"|"rank")` — NaN ranks are
  ineligible; rows sum to 1.0 (or 0.0 when nothing is eligible).
- `simulate(...)` — trades execute at the **close** of each rebalance date;
  costs = `(commission_bps + slippage_bps)` on traded notional, both sides,
  funded out of the rebalance (documented one-step fixed-point; cash never
  goes negative). Long-only, fractional shares, no margin.
- `stats(equity_curve, dates=None, turnover=None)` — total return, CAGR,
  annualized Sharpe, max drawdown (+ peak/trough dates), win rate and
  profit factor on daily returns, annualized turnover.

## Cost-model assumptions (read before quoting numbers)

1. Commission 1 bp + slippage 5 bps per side on traded notional — a
   conservative retail estimate, not a measurement.
2. No bid/ask spread beyond the slippage term; no market-impact model.
3. Close-price execution; the ranking inputs must be point-in-time or the
   result has lookahead bias the engine cannot detect.
4. Fractional shares; the event engine's whole-share rounding typically
   moves results by < 0.01% at $100k equity (verified by cross-check).
