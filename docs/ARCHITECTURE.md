# Architecture — council-trader

**User-selected mode: ADVISOR.** The 8:00 ET scan builds dossiers, the LLM
council decides BUY/PASS, and `pipeline/advisor.py` converts approved
decisions into copy-paste manual-execution trade cards
(`reports/advisor_<date>.md`) the user enters by hand (Webull). The system
never executes and stores no API keys. The legacy autonomous path
(`--execute` → simulated/Alpaca paper brokers) remains for backward
compatibility but is not the selected mode.

Data flows one way: market data → dossiers → ranked candidates → council
decisions → advisor cards → user. Risk is a choke point, not a suggestion.

## Data flow

```
                        ┌─────────────────────────────┐
                        │        config.yaml          │  universe,
                        │  (tunables, no secrets)     │  etf_universe, advisor,
                        └──────────────┬──────────────┘  risk, strategy, council
                                       │
┌──────────────┐   ┌────────────────────▼────────────────────┐
│ Alpaca Data  │   │        pipeline/daily.py --scan-only     │
│ API (iex)    │──▶│  MarketData.get_bars  (1y x 40 symbols: │
└──────┬───────┘   │   24 stocks + 16-ETF sleeve, deduped)    │
       │ yfinance fallback └────────────────────┬────────────────────┘
       │                                        ▼
┌──────▼───────┐   ┌─────────────────────────────────────────┐
│ Kalshi public│   │  data/dossier.py :: build_dossier()     │
│ API (no key) │──▶│  indicators + quant + news + social +   │
└──────────────┘   │  kalshi + extended_hours +              │
                   │  unusual_activity + catalysts ->        │
                   │  one JSON-serializable dict             │
                   └────────────────────┬────────────────────┘
                                        ▼
                   ┌─────────────────────────────────────────┐
                   │ strategies/stock_picker.py              │
                   │ rank_candidates(): composite score      │
                   │ 0.35*technicals + 0.25*quant +           │
                   │ 0.25*social_news + 0.15*kalshi;         │
                   │ macro-risk veto at kalshi.score >= 70;  │
                   │ catalyst_boost: hot-catalyst dossiers   │
                   │ always get a council look               │
                   └────────────────────┬────────────────────┘
                                        ▼
                   ┌─────────────────────────────────────────┐
                   │ reports/dossiers/<date>.json            │
                   │ {date, macro_risk, ranked:[...]}        │
                   └────────────────────┬────────────────────┘
                                        ▼
                   ┌─────────────────────────────────────────┐
                   │ COUNCIL (the seam)                      │
                   │ subagent path: council/runbook.md  ─┐   │
                   │ panel path: council/panel.py      ─┘   │
                   │ -> reports/decisions/<date>.json       │
                   │    council/decisions_schema.json       │
                   └────────────────────┬────────────────────┘
                                        ▼
                   ┌─────────────────────────────────────────┐
                   │ pipeline/daily.py --advisor             │
                   │  (ADVISOR MODE — never touches broker/) │
                   │  build_advisor_cards: limit entry off   │
                   │  extended_hours.reference_price, sizing │
                   │  mirrors risk math vs assumed equity,   │
                   │  kill-switch probe                      │
                   └────────────────────┬────────────────────┘
                                        ▼
                   ┌─────────────────────────────────────────┐
                   │ reports/advisor_<date>.md/.json         │
                   │ reported in chat; user enters tickets   │
                   │ manually (Webull). Nothing executes.    │
                   └─────────────────────────────────────────┘
```

`--full-advisor` runs the scan and prints the advisor next step (the council
step is completed by the scheduled agent per `council/runbook.md`, then
`--advisor` consumes the decisions file). The legacy `--full` /
`--execute` path (scan → council resolution → RiskManager gates → broker)
is unchanged for backward compatibility.

Free-feed honesty: extended-hours prints (yfinance prepost, 15m), options
chains, and news are free, delayed, and sparse — every fetcher is
thread-timeout isolated with skip-and-log, and every dossier degrades to
stub blocks instead of failing the scan. Paper fills differ from real fills;
advisor cards are guidance, not guarantees.

## Module responsibilities

| Module | Owns |
|---|---|
| `pipeline/daily.py` | CLI orchestration: scan / execute / full; never holds strategy logic |
| `pipeline/common.py` | Repo-root resolution, `config.yaml` loading, ET date handling |
| `data/market.py` | OHLCV bars + latest price; Alpaca first, yfinance fallback |
| `data/indicators.py` | Pure pandas/numpy indicators (RSI/EMA/SMA/MACD/ATR/Bollinger/VWAP/OBV/volume z-score) |
| `data/dossier.py` | `build_dossier()`: the single per-symbol research bundle (JSON-serializable; NaN → null). Always includes `extended_hours`, `unusual_activity`, `catalysts`, and `is_etf` blocks (stub on failure — never breaks the scan) |
| `data/extended.py` | Pre-market (04:00–09:30 ET) / after-hours (16:00–20:00 ET) prints via yfinance `prepost=True` 15m bars + Alpaca `trades/latest` live print (env keys only). Per-session volume rel-volume vs 5-day baseline; move flags at 2%, gap risk high at 3%. Freshest print wins: live > premarket > afterhours > regular close. Thread-timeout isolated |
| `data/unusual.py` | Unusual volume: daily z-score (20d), intraday rel-volume, pre-market rel-volume (flag > 2.5σ/2.5×), accumulation/distribution read from the spike bar's close position. Unusual options: yfinance chains (nearest 2 expiries), contracts with volume > 2× OI flagged, call/put premium tilt, top 3 contracts. Thread-timeout isolated |
| `data/catalysts.py` | Overnight news sweep (since prior close), news velocity (> 3× 10-day avg article count), earnings calendar via yfinance (skipped for ETFs), analyst upgrade/downgrade headline classification. Top-3 catalysts per symbol with impact tags; `hot` when velocity spikes or a high-impact item lands |
| `pipeline/advisor.py` | **Advisor mode**: decisions → manual-execution cards. Never imports `broker/`. Entry off `extended_hours.reference_price`; sizing mirrors the risk-manager formula vs `advisor.assumed_equity` using the card's actual stop distance; kill-switch probe; writes `reports/advisor_<date>.md/.json` |
| `data/kalshi.py` | Macro-risk gauge from Kalshi prediction markets (public API, heuristic — see below) |
| `data/news.py`, `data/social.py`, `data/quant.py` | News items, social sentiment, quant scores feeding the dossier |
| `strategies/stock_picker.py` | Strategy 1: composite scoring, macro veto, `initial_stops`, `update_trailing_stop` ratchet, time stop |
| `strategies/strategy2_slot.py` | Disabled interface + spec template awaiting user definition |
| `risk/manager.py` | Kill switch, fractional position sizing scaled by conviction, pre-trade gates, PDT guard, persisted day-trade log |
| `broker/simulated.py` | In-memory paper broker; instant fills; stop/TP evaluated on closes |
| `broker/alpaca_adapter.py` | Alpaca **paper** adapter; paper URL hard-coded and enforced; retries with backoff; never logs secrets |
| `council/runbook.md` | Subagent council procedure (default path) + advisor card generation (Step 6) |
| `council/prompts/` | Per-role prompts: extended-hours rules (risk_analyst), extended-price + unusual-activity + ETF sector-rotation rules (chair), unusual-activity analysis (quant), catalysts/velocity/unusual-activity rules (social_news_analyst) |
| `council/panel.py` | Opt-in in-process multi-vendor LLM panel |
| `council/decisions_schema.json` | Contract between council and `--execute` |
| `reports/generator.py` | EOD markdown: account, positions, decisions, risk status, macro risk |
| `backtest/` | Historical simulation engine (sibling workstream) |

## Council designSix roles: `technical_analyst`, `quant`, `social_news_analyst`,
`risk_analyst`, `devils_advocate`, `chair`. Analysts score the dossier in
parallel from `council/prompts/<role>.md`; the devil's advocate attacks the
four analyst outputs (disagreement-first); the chair synthesizes into
BUY/PASS decisions that must include `conviction`, `disagreement_notes`
(never empty), `independent_lineages_considered`, and `reasoning`.

Two execution paths, selected by `council.execution`:

- **subagent** (default): the council runs as parallel subagents following
  `council/runbook.md`; the pipeline writes a `PENDING_COUNCIL` stub and the
  operator completes the step, then re-runs `--execute`.
- **panel**: `council/panel.py` dispatches each role to a configured
  provider/model/lineage (`config.yaml` → `council.panel.seats`), records
  per-panelist health (latency, ok/error) to `state/council_health.jsonl`,
  builds a CORRELATED PANELISTS block so the chair can see which analysts
  share a lineage, and emits the decisions file directly.

The design follows the multi-agent council pattern from
[swingsystems/claude-council](https://github.com/swingsystems/claude-council):
role-specialized analysts, an explicit devil's advocate, lineage-aware
correlation tracking, and health logging so degraded panelists can be
identified and swapped.

**Honest limitation:** all panel seats ship unconfigured (`provider/model/lineage: null`),
so a panel run today collapses to a single default model — analysts with
correlated blind spots agreeing with themselves. Real disagreement requires
seating distinct vendor lineages (roadmap below).

## Advisor mode

The user-selected operating mode. `pipeline/advisor.py`:

- reads `reports/decisions/<date>.json` (council output) and the dossiers
  file (for extended-hours entry pricing),
- builds one card per BUY decision with conviction ≥
  `advisor.min_conviction` (default 60), capped at
  `advisor.max_cards_per_day` (highest conviction first),
- prices the limit entry off `extended_hours.reference_price` — the latest
  live/pre-market/after-hours print — never the stale prior close,
- sizes with the risk-manager's fractional-risk formula against
  `advisor.assumed_equity`, using the card's actual stop distance; the
  chair's stop is used when sane (below entry for longs), else an ATR
  fallback,
- writes `reports/advisor_<date>.md` (surfaced in chat for manual entry)
  and `reports/advisor_<date>.json` (`executed: false`),
- probes the kill switch first: active → zero cards with a note.

Guarantees: no import of `broker/` anywhere in the module (asserted in
tests), no API keys, no orders. A card is valid for
`advisor.session_validity` ("next session only").

## ETF sleeve

`config.yaml` → `etf_universe`: SPY, QQQ, IWM, DIA, XLK, XLF, XLE, XLV, XLI,
XLP, XLU, XLY, XLB, XLRE, SMH, ARKK. The scan merges it with the stock
universe (deduped; SPY/QQQ already appear in both). ETFs are just symbols:
technicals, quant, extended-hours, unusual volume/options, catalysts, the
council, and advisor cards all treat them identically, with three
adjustments:

1. **Earnings check skipped** (`catalysts.earnings.skipped: "ETF"`) — no
   single-stock earnings risk; macro event risk (Fed/CPI) still covered by
   the news/Kalshi layer.
2. **Social is neutral when thin** — sector ETFs trade on rotation, not
   chatter; the composite scorer already maps missing social to 50
   (neutral), never negative.
3. **Sector-rotation logic in the chair prompt** — ETF candidates are judged
   on relative sector strength (e.g. XLK vs XLU) and macro-regime fit, not
   single-name narratives.

Deliberately excluded: leveraged/inverse ETFs (daily-reset decay vs
multi-day holds) and single-stock ETFs. Documented in `config.yaml`.

## Risk model

- **Kill switch**: `state/KILL_SWITCH` sentinel file → all new trades blocked.
- **Sizing**: `risk$ = equity × max_risk_per_trade_pct × (0.5 + 0.5 × conviction/100)`;
  `shares = floor(risk$ / (ATR × atr_trailing_mult))`. Conviction 80 on
  $100k equity with 1% risk and 2.5× ATR stop ≈ 180 shares at ATR 2.0.
- **Gates** (in order): kill switch → daily-loss halt (3% of equity) →
  max positions (8) → buying power → PDT guard (≤3 day trades / trailing 5d).
- **Exits**: 2.5× ATR initial stop, 2R take-profit, ATR trailing-stop ratchet
  (20-day highest high, only ever tightens), 10-day time stop.
- **Day PnL**: tracked via `state/last_equity.json` (prior-run equity snapshot;
  resets to 0 on a new day).

## Backtest methodology + limitations

The backtest engine (sibling workstream, `backtest/`) replays the strategy-1
rules over historical bars. Known limitations, stated plainly:

- Simulated fills have no slippage model; bracket triggers evaluate on
  **closes**, not intraday high/low — stop-outs and take-profits are
  optimistic vs. reality.
- No borrow costs, no dividends, no corporate-action handling beyond what the
  data feed adjusts.
- The Kalshi macro gauge is a **heuristic** (average binary uncertainty
  across matched macro events + hawkish tilt), not calibrated to realized
  volatility; backtests that include the veto gate should be read as
  qualitative.
- Survivorship: the universe is today's 24 large-caps; historical runs
  overstate by ignoring delisted constituents.

## Scheduling plan

Advisor-mode entries, America/New_York (no cron jobs are created by this repo):

```
0 8 * * 1-5  cd ~/workspace/trading && python -m pipeline.daily --scan-only >> pipeline.log 2>&1
# council step: scheduled agent follows council/runbook.md, writes reports/decisions/<date>.json
# advisor step: python -m pipeline.daily --advisor reports/decisions/<date>.json --dossiers reports/dossiers/<date>.json
```

The pipeline is headless-safe: stdlib logging only, argparse CLI, exit 0 on
the expected stub path, non-zero with a logged traceback on real failure.
Keys arrive as env vars injected by the scheduler's Secure Vault integration.

## Roadmap

Shipped 2026-09-11 (workstreams 1–5): Webull advisor mode (`pipeline/advisor.py`,
`--advisor`/`--full-advisor`, advisor cards + reports), extended-hours
pricing (`data/extended.py`, wired into dossiers/council/advisor),
unusual activity detection (`data/unusual.py`: volume anomalies + unusual
options flow), comprehensive catalyst scanning (`data/catalysts.py`:
overnight sweep, news velocity, earnings calendar, analyst actions,
missing-trade guard via `catalyst_boost`), and the 16-ETF sleeve
(`etf_universe`, ETF adjustments in catalysts/chair/composite scoring).

1. **Multi-vendor panel activation** — seat distinct lineages per role
   (NVIDIA / OpenRouter / Together), wire `council.panel.candidates`, and use
   the health log + `suggest_swap` to rotate degraded panelists.
2. **Live order-replace for trailing stops** — today the Alpaca path logs the
   recommended stop update; v2 replaces the stop order in place.
3. **Strategy 2** — implement from the user's definition in
   `strategies/strategy2_slot.py`.
4. **Saga/world-tour structure** — queued behind the quality phase; not
   started.
