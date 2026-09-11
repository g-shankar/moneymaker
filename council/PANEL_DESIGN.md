# Council Panel Design — Multi-Vendor LLM Council for Paper Trading

## 1. Why a multi-vendor council

A single model reviewing its own trade idea inherits its own blind spots: the
model that generated the bias is the same one being asked to check it. The
council pattern — borrowed from
[swingsystems/claude-council](https://github.com/swingsystems/claude-council),
whose design was itself validated by an earlier LLM — attacks this with two
levers:

1. **Different vendors, different blind spots.** A model from vendor A and a
   model from vendor B share far less training-data and alignment DNA than two
   calls to the same model. Decorrelated errors are the whole point: if two
   independent lineages converge on BUY, that convergence means more than any
   one of them saying BUY twice.
2. **Role-differentiated prompts.** Five analyst roles
   (`technical_analyst`, `quant`, `social_news_analyst`, `risk_analyst`,
   `devils_advocate`) each see the same dossier through a different lens, plus
   a `chair` that synthesizes. Role prompts live in
   `council/prompts/{role}.md` and are owned by a sibling agent; `panel.py`
   only guarantees the chair *receives* everything it needs.

## 2. The key finding we built around: disagreements beat consensus

The reference project's central empirical finding was that **disagreements
were more useful than consensus** — and, more sharply, that two
**same-lineage** models made the *same* error, so a naive majority vote would
have *promoted* the error instead of catching it.

Two consequences in this implementation:

- **Lineage counting, not headcount.** `correlated_panelists_block()` groups
  seated panelists by lineage and injects a block into the chair prompt of the
  form:

  ```
  2 independent lineage(s) across 3 seated panelist(s)
  lineage llama: quant, technical_analyst — agreement here is one perspective, not 2
  lineage qwen: risk_analyst
  ```

  The chair is thereby told, in plain text, that same-lineage agreement is one
  perspective, not N. Independent-lineage agreement is weighted as genuine
  convergence.

- **Disagreement-first synthesis.** The chair prompt (sibling-owned) is
  required to instruct disagreement-first synthesis: the chair must surface
  *where the analysts disagree and why* before rendering a verdict, because
  the disagreement is where the signal lives. `run_council()` guarantees the
  chair receives all five role outputs plus the correlated block; the prompt
  text itself is the sibling's contract.

## 3. Seat model and lineage

`config.yaml → council.panel`:

```yaml
council:
  panel:
    timeout_s: 120
    health_log: "state/council_health.jsonl"
    providers:
      nvidia:     {base_url: "https://integrate.api.nvidia.com/v1", api_key_env: "NVIDIA_API_KEY"}
      openrouter: {base_url: "https://openrouter.ai/api/v1",       api_key_env: "OPENROUTER_API_KEY"}
      together:   {base_url: "https://api.together.xyz/v1",        api_key_env: "TOGETHER_API_KEY"}
    seats:
      technical_analyst: {provider: nvidia, model: meta/llama-3.1-70b-instruct, lineage: llama}
      chair:             {provider: openrouter, model: anthropic/claude-opus-4, lineage: claude}
      quant:             {provider: null, model: null, lineage: null}   # not seated
    candidates: []
```

Rules:

- A seat with `provider: null` (or model null) is **not seated** — that role
  falls back to the default **subagent execution path** (`council/runbook.md`),
  which needs zero API keys. The panel is opt-in per seat, not all-or-nothing.
- A seat whose `api_key_env` env var is missing is **skipped with a logged
  warning, not an error** — the pipeline keeps running on the subagent path.
- `lineage` defaults to the provider name when unset. Lineage is a semantic
  label ("llama", "claude", "qwen", "gpt", …) used for correlation counting;
  set it deliberately when two providers serve the same model family.

## 4. Dispatch mechanics

- `CouncilPanel.dispatch(role_prompts, payloads)` fans out over
  `ThreadPoolExecutor(max_workers=6)`; each call is a plain HTTPS
  `POST {base_url}/chat/completions` with `{model, messages:
  [system=prompt, user=payload], temperature: 0.2, max_tokens: 2000}` and
  `Authorization: Bearer <key from env>`.
- Response parsing strips markdown code fences, then `json.loads`. On parse
  failure the panelist is retried **once** with a "return ONLY valid JSON"
  nudge.
- **Never raises on a single panelist failure** — failures are recorded as
  `{ok: False, error: ...}` and the council proceeds with whoever answered.
- Per-call timeout from `timeout_s` (default 120s). No CLIs, no subprocesses,
  stdlib + `requests` only, headless-safe. The module imports cleanly with
  zero third-party deps so the pipeline can `from council.panel import
  CouncilPanel, run_council` lazily in try/except.

`run_council(panel, dossiers, prompts_dir, date)` per dossier: renders the 5
analyst prompts with `{{DOSSIER_JSON}}` and `{{DATE}}` → parallel dispatch →
records health per panelist → builds the correlated block → renders the chair
prompt with `{{DOSSIER_JSON}}`, `{{ROLE_OUTPUTS_JSON}}` (all 5 role outputs),
`{{CORRELATED_PANELISTS}}` → dispatches the chair → assembles
`{date, decisions: [...]}` matching `council/decisions_schema.json`. If no
panelist is seated for `chair`, it raises `RuntimeError` pointing at
`council/runbook.md` — an empty chair seat means the multi-vendor path is not
configured, and the pipeline should use the subagent path instead.

## 5. Health tracking and swap mechanics

- `record_run(log_path, panelist_name, ok, latency_s, error=None)` appends one
  JSONL record `{ts, panelist, ok, latency_s, error}` to
  `state/council_health.jsonl` (default). Keys are never written — only the
  model name and outcome.
- `health_report(log_path)` renders a text table (runs / ok-rate /
  avg-latency) and flags:
  - **UNRELIABLE** — <80% success over ≥3 runs
  - **TOO SLOW** — average latency >120s
- `suggest_swap(seated_lineages, candidates)` ranks candidate
  provider/model/lineage entries from `council.panel.candidates`, **preferring
  lineages not already seated** (decorrelation is the point of a swap), and
  prints the config snippet to apply. It never edits config itself — the user
  applies the snippet and sets the key.

Swap loop: `health_report` flags a seat → add alternatives to `candidates` →
`suggest_swap` recommends the most decorrelated one → user edits
`config.yaml` → seat improves or gets replaced.

## 6. Activation steps

1. **Add keys to the Secure Vault** (never to files): set
   `NVIDIA_API_KEY` / `OPENROUTER_API_KEY` / `TOGETHER_API_KEY` in the
   environment via the vault-backed mechanism. `panel.py` reads them only via
   `os.environ`; they never appear in files, logs, or error strings.
2. **Fill seats in `config.yaml`**: for each role set
   `provider`, `model`, and `lineage` under `council.panel.seats`. Leave a
   seat at `provider: null` to keep that role on the subagent path.
3. **Run the pipeline with `--full`**: `run_council` auto-dispatches every
   seated role in parallel, synthesizes through the chair, and emits decisions
   matching `decisions_schema.json`. Roles without keys keep working through
   the subagent path — mixed councils are supported by design.

## 7. Privacy trade-off

Sending dossiers to third-party inference providers means **your data leaves
the machine**: prompts, dossier JSON, and role outputs transit to vendor APIs.
Free or unpaid tiers may log inputs and train on them — seating a provider is
the user's call after reviewing that provider's data policy. Mitigations built
in:

- Keys live only in environment variables; nothing secret is persisted.
- Per-seat opt-in: you can seat just one role (or none) and the rest of the
  pipeline stays local.
- Dossiers contain market data and derived features; keep account identifiers
  and credentials out of dossiers at the source.

## 8. Credit

Architecture pattern (not code) adapted from the open-source project
[swingsystems/claude-council](https://github.com/swingsystems/claude-council)
— a council of different AI vendors reviewing the same input with
role-differentiated prompts — applied here to trading decisions. Its key
findings (disagreements > consensus; same-lineage models share errors) shaped
the lineage-counting and disagreement-first synthesis design above.
