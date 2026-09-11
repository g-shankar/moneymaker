# Strategy 1 — Validation Report

Generated: 2026-09-11. Read-only validation: the engine, strategy, and pipeline
code were not modified. All runs use the original setup (20-symbol universe,
$100k, 2024-09-01 → 2026-09-01) with bars pre-fetched once and reused, so every
cell is bit-comparable. Baseline = ATR 3.0 / R 2.0 (the +45.3% / Sharpe 1.40 run).

## 1. Parameter sensitivity — ATR trailing-stop multiple (R fixed 2.0)

| ATR mult | total return | Sharpe | max DD | win rate | profit factor | trades |
|---|---|---|---|---|---|---|
| 2.0 | 25.93% | 0.999 | -14.30% | 44.9% | 1.187 | 606 |
| 2.25 | 27.23% | 0.987 | -14.89% | 45.5% | 1.181 | 560 |
| 2.5 | 37.76% | 1.258 | -13.22% | 49.2% | 1.247 | 533 |
| 2.75 | 29.40% | 1.002 | -15.34% | 48.6% | 1.192 | 514 |
| 3.0 | 45.31% | 1.399 | -15.65% | 51.5% | 1.314 | 499 |

Sharpe range across ATR 2.0–3.0: 0.987 – 1.399 (spread 0.412).

## 2. Parameter sensitivity — take-profit R (ATR fixed 2.5)

| R | total return | Sharpe | max DD | win rate | profit factor | trades |
|---|---|---|---|---|---|---|
| 1.5 | 33.30% | 1.157 | -13.59% | 48.5% | 1.224 | 544 |
| 2.0 | 37.76% | 1.258 | -13.22% | 49.2% | 1.247 | 533 |
| 2.5 | 33.61% | 1.135 | -14.15% | 49.0% | 1.230 | 527 |

## 3. Walk-forward — 4 half-year segments, tune ATR on IS, test on next OOS

### Fold 1: IS segment 1 → OOS segment 2

| ATR (IS) | IS Sharpe | IS trades | IS return |
|---|---|---|---|
| 3.0 | 2.107 | 133 | 17.22% |
| 2.5 | 1.970 | 143 | 16.10% |
| 2.75 | 1.857 | 137 | 14.99% |
| 2.25 | 1.775 | 147 | 13.97% |
| 2.0 | 0.961 | 164 | 6.56% |

IS winner: ATR 3.0 → OOS Sharpe 1.487, return 10.07%, 116 trades.
Fixed ATR 3.0 control → OOS Sharpe 1.487, return 10.07%, 116 trades.

### Fold 2: IS segment 2 → OOS segment 3

| ATR (IS) | IS Sharpe | IS trades | IS return |
|---|---|---|---|
| 2.0 | 2.900 | 131 | 18.07% |
| 2.5 | 2.696 | 120 | 18.17% |
| 2.25 | 2.521 | 127 | 16.04% |
| 2.75 | 2.124 | 118 | 14.59% |
| 3.0 | 1.487 | 116 | 10.07% |

IS winner: ATR 2.0 → OOS Sharpe -0.345, return -2.39%, 166 trades.
Fixed ATR 3.0 control → OOS Sharpe 0.974, return 6.27%, 128 trades.

### Fold 3: IS segment 3 → OOS segment 4

| ATR (IS) | IS Sharpe | IS trades | IS return |
|---|---|---|---|
| 3.0 | 0.974 | 128 | 6.27% |
| 2.5 | 0.353 | 143 | 1.89% |
| 2.75 | 0.226 | 134 | 1.09% |
| 2.25 | -0.317 | 151 | -2.40% |
| 2.0 | -0.345 | 166 | -2.39% |

IS winner: ATR 3.0 → OOS Sharpe 1.565, return 10.96%, 122 trades.
Fixed ATR 3.0 control → OOS Sharpe 1.565, return 10.96%, 122 trades.

## 4. Realistic costs — baseline (ATR 3.0 / R 2.0) with $0.01/share/side + 5 bps/side

| | no costs | with costs |
|---|---|---|
| total return | 45.31% | 36.94% |
| Sharpe | 1.399 | 1.156 |
| max DD | -15.65% | -16.82% |
| win rate | 51.5% | 50.3% |
| profit factor | 1.314 | 1.249 |
| trades | 499 | 499 |

Total cost drag: $8,364.11 over 499 trades (avg $16.76/trade). Costs consume 18.5% of the gross return.

Cost method: post-hoc per-trade deduction (engine has $0 commission hard-coded and
was not modified). Shares inferred as round(pnl / (exit−entry)); equity-curve Sharpe/DD
approximated by subtracting cumulative costs at exit dates. Timing/reinvestment
second-order effects ignored — directionally conservative.

## 5. Verdict

**MIXED — fixed-parameter strategy is ROBUST; in-sample parameter tuning is OVERFIT and must not be used.**

What holds up:
- Every ATR multiple from 2.0 to 3.0 is profitable over the full 2-year window (Sharpe 0.99–1.40). No cliff at nearby parameters.
- Every R multiple from 1.5 to 2.5 is profitable (Sharpe 1.14–1.26).
- Fixed ATR 3.0 was profitable in ALL THREE out-of-sample walk-forward folds (+10.07%, +6.27%, +10.96%).
- Realistic costs ($0.01/share/side + 5 bps/side) cut return 45.31% → 36.94% and Sharpe 1.40 → 1.16. Still clearly positive.

What does NOT hold up:
- Picking the ATR multiple by best in-sample Sharpe FAILED in fold 2 of 3: it selected ATR 2.0 (IS Sharpe 2.90) and lost money out-of-sample (-2.39%, Sharpe -0.345), while the fixed 3.0 default made +6.27% in the same fold. Tuning adds nothing and can subtract.

Decision:
- Strategy 1 stays champion, but ONLY with FIXED parameters — no rolling re-tuning of the ATR multiple, ever. The validated setting is ATR 3.0 / R 2.0.
- Production currently runs a 2.5x ATR trailing stop, which was NOT the backtested setting. ATR 2.5 sits on the robust plateau (full-sample Sharpe 1.258), so it is defensible, but the honest choice is to align production to the validated 3.0 or formally re-validate 2.5 as the fixed standard. Do not split the difference by tuning.
- Remaining caveats (unchanged): 20-symbol universe, 2-year window, survivorship bias in the symbol list, council approximated by score threshold, VIX proxied Kalshi. This is evidence, not proof of future performance.
