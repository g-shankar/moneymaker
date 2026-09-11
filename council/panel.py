"""
Multi-vendor LLM council panel for the paper-trading pipeline.

Architecture pattern (not code) adapted from the open-source project
swingsystems/claude-council (https://github.com/swingsystems/claude-council):
a council of DIFFERENT AI vendors / model lineages reviews the same dossier
with role-differentiated prompts, and a chair synthesizes the role outputs
disagreement-first.

WARNING — DATA LEAVES THE MACHINE
----------------------------------
When a seat is filled (provider + model + API key present), calling
CouncilPanel.dispatch() / run_council() sends the rendered prompts and dossier
JSON to third-party inference providers over HTTPS. Treat dossiers as leaving
your machine: free or unpaid tiers may log/train on inputs. Review each
provider's data policy before seating them. This module never writes API keys
to files, logs, or error messages — keys are read from environment variables
only (the env var name comes from config; the value never does).

DEFAULT EXECUTION REMAINS THE SUBAGENT PATH
-------------------------------------------
With zero seats filled (no API keys set), this module dispatches nothing and
run_council() raises for a missing chair — the pipeline must fall back to the
default subagent execution path (see council/runbook.md), which needs zero
API keys. Seating the panel is strictly opt-in.

Dependencies: stdlib + `requests` only, so the sibling pipeline can
`from council.panel import CouncilPanel, run_council` lazily inside try/except
with no import-time side effects and no third-party imports beyond requests.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

ANALYST_ROLES = (
    "technical_analyst",
    "quant",
    "social_news_analyst",
    "risk_analyst",
    "devils_advocate",
)
CHAIR_ROLE = "chair"


@dataclass
class Panelist:
    """One seated council member."""

    name: str  # role, e.g. "technical_analyst"
    provider: str  # e.g. "nvidia"
    model: str  # provider model id
    lineage: str  # model family/lineage for correlation counting
    base_url: str  # provider chat-completions base URL (no trailing path)
    api_key_env: str  # name of the env var holding the key (value never stored)
    timeout_s: float = 120.0


def _render(template: str, values: dict) -> str:
    """Replace {{PLACEHOLDER}} tokens via plain string substitution.

    Uses str.replace (not str.format) so JSON payloads containing braces pass
    through untouched. Unknown placeholders are left in place.
    """
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


def _strip_fences(text: str) -> str:
    """Remove markdown code fences, returning the inner JSON text."""
    text = text.strip()
    m = FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text


class CouncilPanel:
    """Fan-out dispatcher over seated third-party LLM panelists."""

    def __init__(self, panel_config: dict):
        self.timeout_s = float(panel_config.get("timeout_s", 120))
        self.health_log = panel_config.get("health_log", "state/council_health.jsonl")
        providers = panel_config.get("providers", {}) or {}
        seats = panel_config.get("seats", {}) or {}
        self.panelists: list[Panelist] = []
        for role, seat in seats.items():
            seat = seat or {}
            provider = seat.get("provider")
            model = seat.get("model")
            if not provider or not model:
                log.warning(
                    "Council seat %r not seated (provider/model unset); "
                    "subagent path remains the default for this role.",
                    role,
                )
                continue
            prov_cfg = providers.get(provider) or {}
            base_url = (prov_cfg.get("base_url") or "").rstrip("/")
            api_key_env = prov_cfg.get("api_key_env")
            if not base_url or not api_key_env:
                log.warning(
                    "Council seat %r skipped: provider %r has no base_url/api_key_env "
                    "in council.panel.providers.",
                    role,
                    provider,
                )
                continue
            if api_key_env not in os.environ or not os.environ[api_key_env]:
                # Missing key is a warning, NOT an error: the subagent path
                # stays available for this role.
                log.warning(
                    "Council seat %r skipped: env var %s not set. "
                    "Set it to seat this role (or leave empty for the subagent path).",
                    role,
                    api_key_env,
                )
                continue
            self.panelists.append(
                Panelist(
                    name=role,
                    provider=provider,
                    model=model,
                    lineage=seat.get("lineage") or provider,
                    base_url=base_url,
                    api_key_env=api_key_env,
                    timeout_s=self.timeout_s,
                )
            )
        log.info("CouncilPanel initialised with %d seated panelist(s).", len(self.panelists))

    def panelist_for(self, role: str) -> Panelist | None:
        for p in self.panelists:
            if p.name == role:
                return p
        return None

    def _call(self, panelist: Panelist, system_prompt: str, payload: str) -> dict:
        """Single panelist call. Never raises — returns a result dict."""
        started = time.monotonic()
        api_key = os.environ.get(panelist.api_key_env)
        if not api_key:
            return {
                "ok": False,
                "output": None,
                "latency_s": round(time.monotonic() - started, 2),
                "error": f"env var {panelist.api_key_env} not set at dispatch time",
                "panelist": panelist.model,
                "lineage": panelist.lineage,
            }
        url = panelist.base_url + "/chat/completions"
        headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}

        def _post(messages: list[dict]) -> str:
            body = {
                "model": panelist.model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 2000,
            }
            resp = requests.post(url, headers=headers, json=body, timeout=panelist.timeout_s)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

        def _parse(raw: str) -> dict:
            return json.loads(_strip_fences(raw))

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": payload},
        ]
        error = None
        try:
            output = _parse(_post(messages))
        except Exception as exc:  # parse failure or transport error -> one retry
            error = f"{type(exc).__name__}: {exc}"
            log.warning("Panelist %s first attempt failed (%s); retrying once.", panelist.name, error)
            try:
                retry_messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON. "
                            "Return ONLY valid JSON, no markdown fences, no commentary."
                        ),
                    }
                ]
                output = _parse(_post(retry_messages))
                error = None
            except Exception as exc2:
                error = f"retry failed: {type(exc2).__name__}: {exc2}"
                output = None
        latency = round(time.monotonic() - started, 2)
        ok = output is not None and error is None
        if not ok:
            log.warning("Panelist %s failed: %s", panelist.name, error)
        return {
            "ok": ok,
            "output": output,
            "latency_s": latency,
            "error": error,
            "panelist": panelist.model,
            "lineage": panelist.lineage,
        }

    def dispatch(self, role_prompts: dict, payloads: dict) -> dict:
        """Parallel fan-out to seated panelists.

        Returns {role: {ok, output, latency_s, error, panelist, lineage}}.
        Roles with no seated panelist are skipped (not present in the result).
        Never raises on a single panelist failure — failures are recorded.
        """
        jobs = []
        for role, panelist in ((p.name, p) for p in self.panelists):
            prompt = role_prompts.get(role)
            if prompt is None:
                continue
            jobs.append((role, panelist, prompt, payloads.get(role, "")))
        results: dict = {}
        if not jobs:
            return results
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            future_to_role = {
                pool.submit(self._call, panelist, prompt, payload): role
                for role, panelist, prompt, payload in jobs
            }
            for future in concurrent.futures.as_completed(future_to_role):
                role = future_to_role[future]
                try:
                    results[role] = future.result()
                except Exception as exc:  # belt and braces; _call shouldn't raise
                    panelist = next(p for p in self.panelists if p.name == role)
                    results[role] = {
                        "ok": False,
                        "output": None,
                        "latency_s": 0.0,
                        "error": f"unexpected: {type(exc).__name__}: {exc}",
                        "panelist": panelist.model,
                        "lineage": panelist.lineage,
                    }
        return results


def correlated_panelists_block(results: dict) -> str:
    """Build the CORRELATED PANELISTS block injected into the chair prompt.

    Groups seated panelists by lineage so the chair reasons about *independent*
    perspectives, not headcount: agreement between two same-lineage models is
    one perspective, not two.
    """
    by_lineage: dict[str, list[str]] = {}
    for role, res in results.items():
        lineage = (res or {}).get("lineage") or "unknown"
        by_lineage.setdefault(lineage, []).append(role)
    n_lineages = len(by_lineage)
    m_panelists = sum(len(r) for r in by_lineage.values())
    lines = [
        f"{n_lineages} independent lineage(s) across {m_panelists} seated panelist(s)"
    ]
    for lineage in sorted(by_lineage):
        roles = ", ".join(sorted(by_lineage[lineage]))
        n = len(by_lineage[lineage])
        suffix = " — agreement here is one perspective, not %d" % n if n > 1 else ""
        lines.append(f"lineage {lineage}: {roles}{suffix}")
    return "\n".join(lines)


def record_run(log_path: str, panelist_name: str, ok: bool, latency_s: float, error: str | None = None) -> None:
    """Append one JSONL health record. Creates parent dirs as needed."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "panelist": panelist_name,
        "ok": bool(ok),
        "latency_s": float(latency_s),
        "error": error,
    }
    parent = os.path.dirname(os.path.abspath(log_path))
    os.makedirs(parent, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _read_health(log_path: str) -> dict:
    stats: dict[str, dict] = {}
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                s = stats.setdefault(rec.get("panelist", "?"), {"runs": 0, "ok": 0, "lat": []})
                s["runs"] += 1
                if rec.get("ok"):
                    s["ok"] += 1
                try:
                    s["lat"].append(float(rec.get("latency_s", 0.0)))
                except (TypeError, ValueError):
                    pass
    except FileNotFoundError:
        pass
    return stats


def health_report(log_path: str) -> str:
    """Render a text table of panelist health with reliability flags."""
    stats = _read_health(log_path)
    if not stats:
        return "No council health data yet."
    header = f"{'panelist':<28}{'runs':>6}{'ok%':>7}{'avg_lat_s':>10}  flags"
    lines = [header, "-" * len(header)]
    for name in sorted(stats):
        s = stats[name]
        runs, ok = s["runs"], s["ok"]
        ok_rate = ok / runs if runs else 0.0
        avg_lat = sum(s["lat"]) / len(s["lat"]) if s["lat"] else 0.0
        flags = []
        if runs >= 3 and ok_rate < 0.80:
            flags.append("UNRELIABLE")
        if avg_lat > 120:
            flags.append("TOO SLOW")
        lines.append(
            f"{name:<28}{runs:>6}{ok_rate * 100:>6.1f}{avg_lat:>10.1f}  {' '.join(flags)}"
        )
    return "\n".join(lines)


def suggest_swap(seated_lineages: list, candidates: list) -> str:
    """Rank replacement candidates, preferring lineages NOT already seated.

    Prints the config snippet to apply. Never edits config itself.
    """
    seated = set(seated_lineages or [])
    ranked = sorted(
        candidates or [],
        key=lambda c: (
            0 if (c.get("lineage") or c.get("provider")) not in seated else 1,
            str(c.get("provider", "")),
            str(c.get("model", "")),
        ),
    )
    lines = ["Candidate ranking (novel lineages first):"]
    for i, c in enumerate(ranked, 1):
        lin = c.get("lineage") or c.get("provider") or "?"
        novel = "NOVEL" if lin not in seated else "same-lineage"
        lines.append(
            f"  {i}. [{novel}] provider={c.get('provider')} model={c.get('model')} lineage={lin}"
        )
    lines.append("")
    lines.append("To seat a candidate, apply this config snippet (config.yaml),")
    lines.append("then set the provider's API key env var — config is never edited here:")
    top = ranked[0] if ranked else {}
    lines.append("  council:")
    lines.append("    panel:")
    lines.append("      seats:")
    lines.append("        <role>:")
    lines.append(f"          provider: {top.get('provider')}")
    lines.append(f"          model: {top.get('model')}")
    lines.append(f"          lineage: {top.get('lineage') or top.get('provider')}")
    return "\n".join(lines)


def _load_prompt(prompts_dir: str, role: str) -> str:
    path = os.path.join(prompts_dir, f"{role}.md")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def run_council(panel: CouncilPanel, dossiers: list, prompts_dir: str, date: str) -> dict:
    """Run the full council over a list of dossiers.

    For each dossier: render the 5 analyst prompts, dispatch in parallel,
    record health per panelist, build the correlated-lineages block, render the
    chair prompt (which receives every role output plus the correlated block),
    dispatch the chair, and assemble a decisions dict matching
    council/decisions_schema.json.
    """
    all_decisions: list[dict] = []
    for dossier in dossiers or []:
        symbol = dossier.get("symbol", "?")
        dossier_json = json.dumps(dossier, indent=2, default=str)

        # 1. Analyst fan-out.
        role_prompts, payloads = {}, {}
        for role in ANALYST_ROLES:
            try:
                template = _load_prompt(prompts_dir, role)
            except FileNotFoundError:
                log.warning("Prompt file missing for role %r; skipping.", role)
                continue
            role_prompts[role] = _render(template, {"DOSSIER_JSON": dossier_json, "DATE": date})
            payloads[role] = dossier_json
        results = panel.dispatch(role_prompts, payloads)

        # 2. Health bookkeeping.
        for role, res in results.items():
            record_run(panel.health_log, res.get("panelist") or role, res["ok"], res["latency_s"], res["error"])

        # 3. Correlated-lineages block for the chair.
        correlated_block = correlated_panelists_block(results)

        # 4. Role outputs payload for the chair prompt.
        role_outputs = {}
        for role in ANALYST_ROLES:
            res = results.get(role)
            if res is None:
                role_outputs[role] = {"ok": False, "output": None, "error": "role not seated"}
            elif res["ok"]:
                role_outputs[role] = {"ok": True, "output": res["output"]}
            else:
                role_outputs[role] = {"ok": False, "output": None, "error": res["error"]}
        role_outputs_json = json.dumps(role_outputs, indent=2, default=str)

        # 5. Chair synthesis.
        chair_panelist = panel.panelist_for(CHAIR_ROLE)
        if chair_panelist is None:
            raise RuntimeError(
                f"No seated panelist for role 'chair' (dossier {symbol}). "
                "Set council.panel.seats.chair (provider/model/lineage) in config.yaml "
                "and the provider API key env var — or run the default subagent path, "
                "see council/runbook.md."
            )
        try:
            chair_template = _load_prompt(prompts_dir, CHAIR_ROLE)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Chair prompt file missing: {prompts_dir}/{CHAIR_ROLE}.md "
                "(owned by the prompts sibling agent)."
            ) from exc
        chair_prompt = _render(
            chair_template,
            {
                "DOSSIER_JSON": dossier_json,
                "ROLE_OUTPUTS_JSON": role_outputs_json,
                "CORRELATED_PANELISTS": correlated_block,
                "DATE": date,
            },
        )
        chair_results = panel.dispatch(
            {CHAIR_ROLE: chair_prompt},
            {CHAIR_ROLE: "Synthesize the role outputs above into your final decisions JSON."},
        )
        chair_res = chair_results.get(CHAIR_ROLE) or {}
        record_run(
            panel.health_log,
            chair_res.get("panelist") or chair_panelist.model,
            chair_res.get("ok", False),
            chair_res.get("latency_s", 0.0),
            chair_res.get("error"),
        )
        if not chair_res.get("ok") or not isinstance(chair_res.get("output"), dict):
            log.warning("Chair failed for dossier %s: %s", symbol, chair_res.get("error"))
            continue

        # 6. Assemble decisions per decisions_schema.json.
        chair_out = chair_res["output"]
        decisions = chair_out.get("decisions")
        if not isinstance(decisions, list):
            log.warning("Chair output for dossier %s has no decisions list; skipping.", symbol)
            continue
        for d in decisions:
            if isinstance(d, dict):
                d.setdefault("symbol", symbol)
        all_decisions.extend([d for d in decisions if isinstance(d, dict)])

    return {"date": date, "decisions": all_decisions}
