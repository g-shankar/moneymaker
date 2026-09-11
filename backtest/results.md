# Backtest report — strategy1 (long-only)

Generated: 2026-09-11
Window: 2024-09-01 -> 2026-09-01
Universe (20): AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, JPM, XOM, UNH, V, MA, JNJ, PG, COST, HD, NFLX, AMD, CRM
Initial cash: $100,000
Runtime: 512.1s

## Config

```
strategy1: min_composite_score=60.0, atr_trailing_mult=3.0, take_profit_r=2.0, time_stop_days=10
risk: max_positions=8
```

## Metrics (real run numbers)

| metric | value |
|---|---|
| total_return | 0.4531 |
| sharpe | 1.399 |
| max_drawdown | -0.1565 |
| win_rate | 0.515 |
| profit_factor | 1.314 |
| n_trades | 499 |
| avg_win | 736.94 |
| avg_loss | -595.39 |
| final_equity | 145,308.96 |

## Exit reasons

| reason | count |
|---|---|
| time_stop | 335 |
| trailing_stop | 143 |
| take_profit | 13 |
| end_of_backtest | 8 |

## Top 5 winners

- AMD: 2026-05-05 -> 2026-05-08 ($351.51 -> $449.44) pnl $4,602.62 (27.9%) [take_profit]
- CRM: 2026-08-24 -> 2026-08-28 ($208.42 -> $256.78) pnl $4,062.06 (23.2%) [take_profit]
- AMD: 2025-09-25 -> 2025-10-06 ($157.14 -> $192.61) pnl $3,581.97 (22.6%) [take_profit]
- TSLA: 2024-11-01 -> 2024-11-08 ($252.04 -> $318.22) pnl $3,441.53 (26.3%) [take_profit]
- NFLX: 2026-02-26 -> 2026-03-05 ($83.2 -> $99.79) pnl $3,301.17 (19.9%) [take_profit]

## Top 5 losers

- AMD: 2026-07-23 -> 2026-07-28 ($544.74 -> $447.15) pnl $-3,123.02 (-17.9%) [trailing_stop]
- TSLA: 2025-11-07 -> 2025-11-14 ($437.92 -> $389.62) pnl $-1,835.21 (-11.0%) [trailing_stop]
- AMD: 2026-07-07 -> 2026-07-17 ($515.91 -> $465.8) pnl $-1,753.95 (-9.7%) [trailing_stop]
- AVGO: 2025-10-31 -> 2025-11-07 ($378.27 -> $339.83) pnl $-1,691.34 (-10.2%) [trailing_stop]
- AMD: 2025-11-04 -> 2025-11-07 ($250.35 -> $225.55) pnl $-1,686.72 (-9.9%) [trailing_stop]

## Assumptions & Limitations

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

