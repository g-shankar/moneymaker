# Strategy 2 — Playbook Proposals

**Status: PROPOSALS ONLY — no code written.** Each playbook below is
independently approve / edit / reject. Nothing gets built until Gowrishankar
signs off on that playbook specifically.

All three are designed for the same paper-first, advisor-mode pipeline as
Strategy 1: council votes, advisor cards, human enters orders in Webull.
Position sizing follows the existing 1%-risk fractional model unless the
playbook states otherwise.

---

## Playbook A — Post-Earnings Drift Continuation

**Decision: [ ] APPROVE &nbsp; [ ] EDIT &nbsp; [ ] REJECT**

**Thesis.** Stocks that gap on earnings with heavy volume tend to drift in
the direction of the gap for 3–5 sessions as analysts revise models and
institutions reposition. We are not predicting earnings — we are surfing the
institutional follow-through after the result is known.

**Universe.** US-listed stocks with market cap > $5B, average daily dollar
volume > $50M, that reported earnings in the last 2 trading sessions.

**Entry trigger (exact — ALL must be true at the 8:00 AM scan):**
1. Earnings reported after prior close or before today's open (verified date,
   not rumor).
2. Stock gapped ≥ +4% (long only — we do not short earnings gaps) vs prior
   close on the pre-market print.
3. Pre-market volume ≥ 2× the 20-day average *full-session* volume
   (institutional participation, not a retail spike).
4. Price holding above VWAP on the 5-minute tape at scan time.
5. No analyst downgrade in the last 24h.

**Stop.** Below the pre-market low, or 1.5× ATR(14) below entry, whichever is
tighter. (Earnings gaps can reverse hard — the stop is non-negotiable.)

**Target.** 2R take-profit; trail with 2× ATR once +1R is banked. Time stop:
exit at the close of day 5 regardless.

**Position sizing.** Standard 1%-risk fractional sizing, then ×0.5 — earnings
drift names are jumpy; half size is the price of admission.

**Invalidation.** Gap fills back below the prior close at any point → exit
immediately, thesis dead. New material news (guidance cut, SEC probe)
overrides everything → exit.

**Expected trade frequency.** Earnings season: 3–8 setups/month. Off-season:
0–2/month. This is a seasonal weapon, not a daily driver.

**Backtest plan.** 3 years of earnings dates (announcement calendar +
verified gap > 4% + volume filter), enter at next open, apply the stop/target
rules above. Report: win rate, avg R, max adverse excursion distribution,
and performance split by market-cap quintile. Kill criterion: expectancy < 0
after costs, or win rate < 40% — the edge is the drift, not the coin flip.

---

## Playbook B — Oversold Snapback (RSI(2) Mean Reversion)

**Decision: [ ] APPROVE &nbsp; [ ] EDIT &nbsp; [ ] REJECT**

**Thesis.** Liquid large-caps that get violently oversold on no fundamental
news snap back within 1–3 days as short-term sellers exhaust. RSI(2) < 10 is
the classic quantified trigger (Connors-style); we add a liquidity and
news filter so we don't catch falling knives with real problems.

**Universe.** S&P 500 constituents only. Average daily dollar volume >
$100M. (No small-caps — snapbacks in illiquid names are traps.)

**Entry trigger (exact — ALL must be true at the scan):**
1. RSI(2) < 10 on daily bars.
2. Close is ≥ 8% below the 20-day high (it's a real washout, not noise).
3. No negative company-specific news in the last 5 sessions (earnings miss,
   downgrade, lawsuit, guidance cut → automatic disqualify).
4. Today's volume ≥ 1.5× 20-day average (capitulation leaves footprints).
5. SPY not in a >3% 5-day drawdown (no catching knives in a market flush).

**Stop.** 1× ATR(14) below entry — TIGHT. Mean reversion either works
immediately or the thesis is wrong; we don't give it room to become an
investment.

**Target.** First of: +3% from entry, or close back above the 5-day moving
average, or 3 trading days elapsed → exit at market, no exceptions.

**Position sizing.** Standard 1%-risk sizing. Small target, tight stop, high
expected win rate — the math works on frequency, not home runs.

**Invalidation.** Any new negative company news after entry → exit at market
immediately. Stop hit → gone, no re-entry for 5 sessions.

**Expected trade frequency.** 4–10 setups/month in normal markets; clusters
in selloffs (when the market filter will veto most of them — by design).

**Backtest plan.** 5 years, S&P 500 constituents point-in-time (survivorship
matters here — use historical membership, not today's list). Apply all five
filters, simulate the exact exit rules with 1¢/share + slippage model.
Report: win rate (target > 65%), avg R, profit factor, max consecutive
losses, and performance in 2020-03 / 2022 bear stress periods. Kill
criterion: win rate < 60% or any 12-month period with negative expectancy.

---

## Playbook C — Sector Rotation Momentum (ETF Trend Riding)

**Decision: [ ] APPROVE &nbsp; [ ] EDIT &nbsp; [ ] REJECT**

**Thesis.** Institutional money rotates between sectors in multi-week waves.
The top-2 sector ETFs by 20-day relative strength vs SPY capture the wave
without single-stock blowup risk. This is the slowest, calmest playbook —
fewer trades, longer holds, smallest ulcer index.

**Universe.** The 11 SPDR sector ETFs (XLK, XLF, XLE, XLV, XLI, XLP, XLU,
XLY, XLB, XLRE, XLC) + SMH. (Already in our ETF sleeve.)

**Entry trigger (exact — ALL must be true at the Monday 8:00 AM scan; this
playbook is evaluated WEEKLY, not daily):**
1. ETF ranks in the top 2 of the 12-ETF set by 20-day total return minus
   SPY's 20-day total return (relative strength).
2. ETF's 20-day relative strength > +2% (it's actually leading, not just
   "least bad").
3. Price above its 50-day moving average (trend confirmation — no catching
   a falling sector because it's "less down" than others).
4. Not currently holding that ETF from a prior rotation signal (no
   doubling; one position per sector max).

**Stop.** 2× ATR(20) below entry, trailed weekly (recomputed each Monday —
never intra-week, to avoid noise shakeouts).

**Target.** No fixed target — this is a trend ride. Exit when: ETF drops out
of the top 4 by relative strength on the Monday review, or the trailing stop
hits, or 60 trading days elapsed (quarterly reset).

**Position sizing.** Standard 1%-risk sizing, full size (not halved) — ETFs
don't gap on fraud. Max 2 sector positions at once (top-2 rule enforces
this naturally).

**Invalidation.** Kalshi macro risk ≥ 70 (veto) → no new rotation entries;
existing positions keep their trailing stops. A sector-specific shock
(e.g., XLE on an oil embargo headline) → manual review, default exit.

**Expected trade frequency.** 1–3 entries/month; average hold 3–6 weeks.
The portfolio's anchor, not its engine.

**Backtest plan.** 10 years (rotations need full cycles), weekly rebalance
on Monday closes, 0.05% cost per rebalance. Benchmark: buy-and-hold SPY and
an equal-weight 12-ETF portfolio. Report: CAGR vs benchmarks, Sharpe,
max drawdown, % of time invested, and turnover. Kill criterion:
underperforms equal-weight 12-ETF on a risk-adjusted basis over the full
period.

---

## How approval works

Reply with one line per playbook, e.g.:

- **A: approve** — build it
- **B: edit** — change the RSI(2) threshold to < 15, then build
- **C: reject** — not interested

Approved playbooks get: a `strategies/strategy2<letter>.py` implementation,
unit tests, a backtest run with the plan above, and council integration —
each gated on the backtest clearing its kill criterion before any paper
card is ever emitted.
