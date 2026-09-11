# moneymaker

A paper-first, advisor-mode trading system. It scans 940 large-cap U.S. stocks
(≥$10B market cap) three times a day, ranks them on a validated momentum
composite, and emits 10 diversified, equal-risk paper trade cards — no broker
keys, no order execution, ever. The human enters cards by hand in their
brokerage app. A second, monthly sector-rotation sleeve runs alongside it.

## How it works

1. **Universe** — `data/universe_symbols.txt`: 940 Nasdaq-listed stocks ≥$10B,
   rebuilt from the Nasdaq screener API (`data/universe_raw.json`).
2. **Data** — daily OHLCV via a split pipe: Yahoo Finance (`screener/screen.py`)
   + Nasdaq historical API (`screener/fetch_nasdaq.py`), stored in DuckDB.
   `screen.py` uses freshness-aware resume: symbols are refetched only when
   their latest bar is older than the last completed trading day.
3. **Signal** — `screener/screen.py::score_frame`: volume z-score, RSI-14,
   20d/60d returns, distance from 52-week high → composite score.
   Strategy 1 validated 2026-09-11: ATR 3.0 stop / 2R target, Sharpe 1.40
   baseline, profitable in all OOS folds (`reports/validation_2026-09-11.md`).
4. **Diversification** — max 2 names per sector in the final 10
   (`screener/build_ten.py`). Lesson learned 2026-09-11: an uncapped screen
   put 4/5 picks in oil.
5. **Equal-risk sizing** — `screener/sizing.py`: every card risks $1,000
   (1% of the $100k paper book), shares = $1,000 ÷ (entry − stop).
   Never fixed share counts.
6. **Cards** — entry = last price, stop = entry − 3×ATR(14),
   target = entry + 6×ATR(14). Logged to the paper journal
   (`tracking/journal.py`); resolved only on stop/target/invalidation —
   multi-day positions, never force-closed at the bell.
7. **Deterministic review** — `tracking/review_cards.py`: trend, RSI, 52w
   distance, ATR-bracket sanity, volume, and portfolio heat checks on every
   open card. No LLM theater.
8. **Evidence gate** — `tracking/verify_scan.py`: a scan only reports success
   if 10 valid cards exist in the journal. A clean exit with no cards is
   reported as failure, never success.

## Scans

| Scan | Schedule (ET) | Session |
|------|---------------|---------|
| Morning stocks | Weekdays 8:00 AM | `morning` |
| Midday stocks | Weekdays 12:12 PM | `midday` |
| Late stocks | Weekdays 3:00 PM | `late` |
| Sector rotation | Monthly, 3rd 9:00 AM | `sector_monthly` |

## Sector rotation sleeve (separate $100k paper book)

`screener/sector_rotation.py` — dual momentum on 11 SPDR sector ETFs:
6-month relative strength vs SPY must be positive AND price above the
200-day SMA; top 4 qualifiers sized by inverse 60-day volatility (40%
single-sector cap); 2pp churn buffer against last month's holdings; SPY
below its 200-day SMA caps equity at 50% (rest SGOV); unfilled slots go to
SGOV. Ledger: `data/sector_rotation_ledger.json` (local-only).

## Backtest verdict (2026-09-11, `reports/backtest_940_2026-09-11.md`)

Full 940-universe backtest of the production strategy, weekly rebalance,
~10 months: **13.5% return, Sharpe 1.40, max drawdown −5.8%** — beats SPY
(11.2%) with lower drawdown, but does NOT beat equal-weight of the same
universe (16.5%) net of costs. Two known leaks: sector cache covers only
~9% of weekly top-40s (throttling selection below 10 names), and weekly
rebalancing churns positions out before stops/targets resolve (135 of 148
exits were rebalance drops). Do not trade on real capital; fix the leaks
first.

## Advisor mode (the only mode)

- No broker credentials are stored anywhere. No orders are ever placed.
- `pipeline/advisor.py` never touches `broker/`.
- A legacy autonomous `--execute` path (simulated/Alpaca paper) remains for
  backward compatibility but is NOT the selected mode.

## Quickstart

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m screener.screen          # download + score the 940
./.venv/bin/python -m screener.build_ten       # rank -> 10 diversified, sized cards
./.venv/bin/python -m tracking.review_cards    # deterministic review of open cards
./.venv/bin/python -m tracking.verify_scan morning
./.venv/bin/python -m screener.sector_rotation # monthly ETF sleeve
./.venv/bin/python backtest/backtest_940.py    # full-universe backtest
./.venv/bin/python -m pytest tests/ -q         # 95 tests
```

## Layout

- `screener/` — universe screen, Nasdaq alt-pipe fetcher, 10-card builder,
  equal-risk sizing, monthly sector rotation
- `tracking/` — paper journal (`log_card`/`log_outcome`), evidence gate,
  deterministic card review
- `data/` — indicators, universe lists (DuckDB files are local-only, gitignored)
- `backtest/` `strategies/` `risk/` — validation + position logic
- `council/` — LLM review panel (design reference)
- `reports/` — validation report, 940 backtest, dated scan outputs
- `tests/` — 95 unit tests

## Status

Paper trading only. No real capital until the paper track record earns it.
