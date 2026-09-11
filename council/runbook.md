# Council Runbook — LLM Paper-Trading Council

The council sits between the pipeline's scan and the advisor/execute steps.
Scan produces candidate dossiers; the council turns each dossier into a
BUY/PASS decision with audited reasoning.

**USER-SELECTED MODE: ADVISOR.** The council's decisions are consumed by
`--advisor`, which builds manual-execution trade cards for the user to enter
by hand (e.g. in Webull). The system NEVER executes automatically and stores
no API keys. The scheduled agent generates advisor cards and reports the
markdown summary in chat. Advisor mode never executes — the legacy
`--execute` path is documented for backward compatibility only.

Seam (advisor mode):
- `pipeline/daily.py --scan-only` → writes `reports/dossiers/<date>.json`
- council (this runbook) → writes `reports/decisions/<date>.json`
- `pipeline/daily.py --advisor reports/decisions/<date>.json --dossiers reports/dossiers/<date>.json`
  → writes `reports/advisor_<date>.md` + `reports/advisor_<date>.json`
  (report the .md in chat; the user enters the tickets manually)

Seam (legacy autonomous mode, NOT the user-selected mode):
- `pipeline/daily.py --execute` → reads `reports/decisions/<date>.json`

## Step 0 — Morning scan checklist (8:00 ET)

The scan already covers the FULL universe for news (every stock + ETF
sleeve symbol gets a dossier with `news`, `catalysts`, `unusual_activity`,
and `extended_hours` blocks — even when technicals are quiet). Before
starting the council, spot-check the dossiers file:

- Every universe symbol has a dossier (skips logged, never fabricated).
- `extended_hours.available` — note which symbols have fresh pre-market
  prints; the chair prices entries off `extended_hours.reference_price`.
- `catalysts.hot` entries are flagged `catalyst_boost` in the ranked list —
  these arrive below the score gate on catalyst strength alone; judge the
  catalyst on its merits (rule: real + dated = valid edge, vague = PASS).
- `unusual_activity.flagged` entries — volume spikes and unusual options
  flow the council must address in reasoning.
- `earnings.within_window` — earnings inside 5 trading days must be a
  deliberate avoid-or-play call, never an accident. Skipped for ETFs.

## Step 1 — Read the dossiers

Read `reports/dossiers/<date>.json`. Expected shape:

```json
{
  "date": "2026-09-10",
  "macro_risk": "neutral",
  "ranked": [
    {"symbol": "AAPL", "score": 81, "dossier": { "...inline dossier..." }},
    {"symbol": "MSFT", "score": 77, "dossier_path": "reports/dossiers/aapl.json"}
  ]
}
```

Each candidate has `symbol`, a scan `score`, and either an inline `dossier`
object or a `dossier_path` to load. The dossier contains everything the five
analyst prompts need: price, indicators, quant, social, news, kalshi. If a
candidate lacks a dossier, skip it and log the skip — never fabricate inputs.

Also load the portfolio context for the risk analyst (open positions, day
P&L, equity) from wherever the pipeline keeps it; render it as
`{{PORTFOLIO_JSON}}`.

## Step 2 — Run the five analyst roles (parallel)

For each candidate, spawn 5 subagents in parallel, each with:
- the role prompt from `council/prompts/<role>.md`
- placeholders rendered: `{{DOSSIER_JSON}}` → the candidate's dossier JSON,
  `{{DATE}}` → session date, `{{PORTFOLIO_JSON}}` → portfolio context (risk
  analyst only)

Devil's advocate runs AFTER the other four for the same candidate: its prompt
needs `{{ROLE_OUTPUTS_JSON}}` (the four validated outputs). So per candidate
the pattern is: 4 in parallel → validate → devil's advocate → validate.

Role → prompt file:
- technical_analyst → `council/prompts/technical_analyst.md`
- quant → `council/prompts/quant.md`
- social_news_analyst → `council/prompts/social_news_analyst.md`
- risk_analyst → `council/prompts/risk_analyst.md`
- devils_advocate → `council/prompts/devils_advocate.md`

Each role returns STRICT JSON. If a subagent returns prose around the JSON,
extract the JSON object and re-validate; if it cannot be parsed, re-run that
role once before failing the candidate.

## Step 3 — Validate roles, build the CORRELATED PANELISTS block

1. Validate each role output: parses as JSON, has the right `role` value, all
   required fields present, scores within 0–100, `veto` is boolean.
2. Build the `{{CORRELATED_PANELISTS}}` text block. This is the correlated-
   panelists guard: role-differentiated prompts running on one model share
   blind spots, so same-lineage agreement counts once.

   **Default (subagent execution): all five analysts are ONE lineage.**
   Use this block verbatim unless `council/panel.py` (multi-vendor) exists
   and was used:

   ```
   CORRELATED PANELISTS — all five analyst roles (technical_analyst, quant,
   social_news_analyst, risk_analyst, devils_advocate) were executed as
   subagents on a single model lineage ("muse-subagents"). They share blind
   spots. Agreement among them counts as ONE independent perspective, not
   five. independent_lineages_considered = 1 unless panel.py reports more.
   ```

   If `council/panel.py` ran a multi-vendor panel, replace the block with its
   lineage report (which roles ran on which vendor/model).

## Step 4 — Run the chair per candidate

For each candidate, run one chair subagent with `council/prompts/chair.md`,
rendering `{{DOSSIER_JSON}}`, `{{ROLE_OUTPUTS_JSON}}` (all five validated
role outputs), `{{CORRELATED_PANELISTS}}` (the block from Step 3), and
`{{DATE}}`.

Enforce the disagreement rules from the chair prompt when validating output:
- `risk_analyst.veto == true` and chair said BUY → reject, force PASS.
- `devils_advocate.devil_score > 60` and chair conviction > 40 → reject,
  cap at 40 (→ PASS).
- BUY with conviction < 60 → reject.
- BUY with `position_size_pct` not in {0.5, 1.0, 1.5} or > 2 → reject.
- `disagreement_notes` empty → reject; send back to the chair.

Write the validated chair outputs to `reports/decisions/<date>.json`
matching `council/decisions_schema.json`:

```json
{"date": "2026-09-10", "decisions": [ {chair output fields...}, ... ]}
```

## Step 5 — Validation checklist

Before handing off to `--advisor` (or legacy `--execute`), confirm ALL of:
- [ ] `reports/decisions/<date>.json` parses as JSON and validates against
      `council/decisions_schema.json`
- [ ] One decision per candidate from the dossiers file (skips logged)
- [ ] No BUY where `risk_analyst.veto == true` for that candidate
- [ ] Every BUY has conviction >= 60 and position_size_pct in {0.5, 1.0, 1.5}
- [ ] Every decision has non-empty `disagreement_notes`
- [ ] `independent_lineages_considered` matches the Step 3 block (default: 1)
- [ ] Any `unusual_activity.flagged` or `catalysts.hot` candidate has its
      signal NAMED in the chair's `reasoning` (not just in the dossier)
- [ ] Entry pricing references `extended_hours.reference_price` where
      available (no stale-close anchoring)

If any check fails, do not write the decisions file — surface the failure to
the orchestrator. A missing decisions file means `--advisor` produces no
cards, which is the safe failure mode.

## Step 6 — Generate advisor cards (scheduled agent)

After the decisions file validates, build the advisor cards:

```bash
python -m pipeline.daily --advisor reports/decisions/<date>.json \
    --dossiers reports/dossiers/<date>.json --date <date>
```

This writes `reports/advisor_<date>.md` and `reports/advisor_<date>.json`
WITHOUT touching broker/ — nothing executes. The scheduled agent then
reports the markdown in chat: one card per approved trade (symbol, side,
limit entry + zone, stop, take-profit, quantity, conviction, 2–3 line
reasoning summary, invalidation, time horizon, validity), with an
"Unusual activity" line and a "Catalyst" line whenever the dossier flags
them. If the kill switch is active, no cards are generated — say so.

## Known limitation — single-model execution shares blind spots

Design input from the reference project (swingsystems/claude-council, a
cross-vendor code-review council) found two things that shaped this council:

(a) **Disagreements were more useful than consensus.** The chair prompt is
built disagreement-first: the strongest disagreement is quoted in every
report and hard rules (veto → PASS, devil_score > 60 → conviction cap) force
it to move the needle instead of being averaged away.

(b) **Correlated panelists.** Role-differentiated prompts running on ONE
model share blind spots — two same-lineage panelists agreeing is one
perspective counted twice. Hence the `{{CORRELATED_PANELISTS}}` block and
the `independent_lineages_considered` field in every chair output: the
council counts independent lineages, not seats.

Honest statement of where this stands: as specified here, all five analysts
run as subagents on a single model lineage, so every report's
`independent_lineages_considered` will be 1 and the council's "five analysts"
are really one perspective wearing five hats. The disagreement-first
synthesis is the mitigation that works even on one model — forced dissent
(devil's advocate, veto power) still surfaces failure modes a single pass
would miss. The real fix is the multi-vendor panel (`council/panel.py`,
roadmap): route roles across genuinely different model vendors/lineages so
agreement means something. Until that exists, treat high-conviction BUYs
from this council as single-perspective judgments and size accordingly
(the 0.5% starting tier reflects this).
