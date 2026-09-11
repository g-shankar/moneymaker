# moneymaker

A paper-first, advisor-mode trading system. It scans 940 large-cap U.S. stocks
(≥$10B market cap) three times a day, ranks them on a validated momentum
composite, and emits 10 diversified paper trade cards — no broker keys, no
order execution, ever. The human enters cards by hand in their brokerage app.

## How it works

1. **Universe** — `data/universe_symbols.txt`: 940 Nasdaq-listed stocks ≥$10B,
   rebuilt from the Nasdaq screener API (`data/universe_raw.json`).
2. **Data** — daily OHLCV via a split pipe: Yahoo Finance (`screener/screen.py`)
   + Nasdaq historical API (`screener/fetch_nasdaq.py`), stored in DuckDB.
3. **Signal** — `screener/screen.py::score_frame`: volume z-score, RSI-14,
   20d/60d returns, distance from 52-week high → composite score.
   Strategy 1 validated 2026-09-11: ATR 3.0 stop / 2R target, Sharpe 1.40
   baseline, profitable in all OOS folds (`reports/validation_2026-09-11.md`).
4. **Diversification** — max 2 names per sector in the final 10
   (`screener/build_ten.py`). Lesson learned 2026-09-11: an uncapped screen
   put 4/5 picks in oil.
5. **Cards** — entry = last price, stop = entry − 3×ATR(14),
   target = entry + 6×ATR(14). Logged to the paper journal
   (`tracking/journal.py`); resolved at the close as WIN/LOSS/SCRATCH.
6. **Evidence gate** — `tracking/verify_scan.py`: a scan only reports success
   if 10 valid cards exist in the journal. A clean exit with no cards is
   reported as failure, never success.

## Scans

| Scan | Schedule (ET, weekdays) | Session |
|------|------------------------|---------|
| Morning | 8:00 AM | `morning` |
| Midday | 12:12 PM | `midday` |
| Late | 3:00 PM | `late` |

## Advisor mode (the only mode)

- No broker credentials are stored anywhere. No orders are ever placed.
- `pipeline/advisor.py` never touches `broker/`.
- A legacy autonomous `--execute` path (simulated/Alpaca paper) remains for
  backward compatibility but is NOT the selected mode.

## Quickstart

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m screener.screen        # download + score the 940
./.venv/bin/python -m screener.build_ten     # rank -> 10 diversified cards
./.venv/bin/python -m tracking.verify_scan morning
```

## Layout

- `screener/` — universe screen, Nasdaq alt-pipe fetcher, 10-card builder
- `tracking/` — paper journal (`log_card`/`log_outcome`) + evidence gate
- `data/` — indicators, universe lists (DuckDB files are local-only, gitignored)
- `backtest/` `strategies/` `risk/` — validation + position logic
- `council/` — LLM review panel (design reference; deep review of top
  candidates runs before cards are treated as cleared)
- `reports/` — validation report + dated scan outputs
- `tests/` — 89 unit tests

## Status

Paper trading only. No real capital until the paper track record earns it.
