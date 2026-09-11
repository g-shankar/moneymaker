"""Advisor mode: turn final council decisions into copy-paste trade cards
for manual execution (e.g. in the user's Webull app).

The user chose ADVISOR MODE: no API keys are stored anywhere and the system
never executes. This module NEVER imports or instantiates ``broker/`` — it
only reads decisions + dossiers and writes human/machine-readable cards.

Position sizing reuses ``risk.manager.RiskManager.position_size_shares``
(the same fractional-risk math as autonomous mode) against a configurable
assumed equity (``config.yaml`` → ``advisor.assumed_equity``). The
RiskManager is pointed at a throwaway temp dir so advisor runs never touch
real risk state (no day-trade logging — nothing executes).

Entry pricing uses the dossier's ``extended_hours.reference_price`` (latest
extended-hours print) — never the stale prior close. See data/extended.py.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from risk import portfolio_guards as _pg

log = logging.getLogger(__name__)


class CardList(list):
    """A list of advisor cards carrying ``guard_notes``.

    Portfolio-guard actions (correlation rejects, regime sizing, heat
    scaling) are reported as human-readable strings on
    ``cards.guard_notes``. Empty when no portfolio context was passed.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard_notes: list[str] = []

_REQUIRED_CARD_FIELDS = [
    "symbol", "side", "order_type", "entry_limit", "entry_zone_low",
    "entry_zone_high", "stop_loss", "take_profit", "take_profit_r",
    "quantity", "assumed_equity", "risk_dollars", "conviction",
    "reasoning_summary", "invalidation", "time_horizon", "validity",
    "asof",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sentences(text: str, n: int) -> str:
    """First *n* sentences of *text*, single-spaced."""
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(p for p in parts if p)[:1200] if n <= 0 else " ".join(
        p for p in parts[:n] if p
    )


def _dossier_lookup(dossiers_data: dict | None) -> dict:
    lookup: dict[str, dict] = {}
    if not isinstance(dossiers_data, dict):
        return lookup
    for entry in dossiers_data.get("ranked") or []:
        if isinstance(entry, dict) and entry.get("symbol"):
            lookup[entry["symbol"]] = entry.get("dossier") or {}
    return lookup


def _reference_price(dossier: dict) -> tuple[float | None, str]:
    """Latest extended print, else dossier price. Returns (price, source)."""
    ext = dossier.get("extended_hours") or {}
    ref = ext.get("reference_price")
    if ref:
        return float(ref), str(ext.get("reference_source") or "extended_hours")
    price = dossier.get("price")
    if price:
        return float(price), "regular_close"
    return None, "none"


def _atr(dossier: dict) -> float | None:
    try:
        atr = (dossier.get("indicators") or {}).get("atr_14")
        return float(atr) if atr and float(atr) > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# card building
# ---------------------------------------------------------------------------
def build_advisor_cards(
    decisions: list[dict],
    dossiers_by_symbol: dict,
    cfg: dict,
    date_str: str,
    portfolio_context: dict | None = None,
) -> list[dict]:
    """Build one advisor card per qualifying BUY decision.

    Qualifying: decision == "BUY", conviction >= advisor.min_conviction.
    Capped at advisor.max_cards_per_day, highest conviction first.

    portfolio_context (optional, additive): {
        "open_positions": [{"symbol": str, "risk_dollars": float}, ...],
        "macro_risk_score": float | None,   # Kalshi macro risk (higher = worse)
        "vix": float | None,                # VIX proxy, optional
        "returns_fn": callable | None,      # symbol -> list of daily returns
    }
    When provided, three portfolio guards engage: correlation cap (reject a
    card too correlated with an open position), regime-adaptive sizing
    (halve/zero sizes in elevated macro risk), and portfolio heat (scale new
    cards so total risk stays under the heat cap). When omitted, sizing is
    exactly the legacy per-card fractional-risk math.

    Guard actions are reported on the returned list as ``cards.guard_notes``
    (list of human-readable strings).
    """
    advisor_cfg = cfg.get("advisor") or {}
    assumed_equity = float(advisor_cfg.get("assumed_equity", 100000))
    min_conviction = int(advisor_cfg.get("min_conviction", 60))
    max_cards = int(advisor_cfg.get("max_cards_per_day", 5))
    validity = str(advisor_cfg.get("session_validity", "next session only"))
    zone_pct = float(advisor_cfg.get("entry_zone_pct", 0.3)) / 100.0
    time_stop_days = int(cfg.get("strategy1", {}).get("time_stop_days", 10))
    atr_mult_default = float(
        cfg.get("strategy1", {}).get("atr_trailing_mult", 2.5)
    )

    # NOTE: sizing mirrors RiskManager.position_size_shares' fractional-risk
    # formula (max_risk_per_trade_pct scaled by conviction) but uses the
    # card's ACTUAL stop distance (entry − stop_loss), not the ATR trailing
    # multiple — for a hand-entered ticket, risk honesty beats internal
    # consistency. A throwaway RiskManager is still used in cmd_advisor for
    # the kill-switch probe (state dir = temp, never real risk state).
    max_risk_pct = float((cfg.get("risk") or {}).get("max_risk_per_trade_pct", 1.0))

    def _size_shares(entry: float, stop: float, conviction: int) -> int:
        stop_distance = entry - stop
        if stop_distance <= 0 or entry <= 0:
            return 0
        risk_dollars = (
            assumed_equity
            * (max_risk_pct / 100)
            * (0.5 + 0.5 * max(0.0, min(100.0, conviction)) / 100)
        )
        return int(risk_dollars / stop_distance)

    buys = [
        d for d in decisions
        if isinstance(d, dict)
        and d.get("decision") == "BUY"
        and int(d.get("conviction", 0) or 0) >= min_conviction
        and d.get("symbol")
    ]
    buys.sort(key=lambda d: int(d.get("conviction", 0) or 0), reverse=True)
    buys = buys[: max(0, max_cards)]

    # ---- portfolio guards (additive: engage only when caller passes context)
    guards_on = portfolio_context is not None
    ctx = portfolio_context or {}
    open_positions = ctx.get("open_positions") or []
    open_symbols = [
        str(p.get("symbol", "")).upper() for p in open_positions
        if p.get("symbol")
    ]
    open_risks = [float(p.get("risk_dollars") or 0) for p in open_positions]
    risk_cfg = cfg.get("risk") or {}
    corr_cap = float(risk_cfg.get("correlation_cap", 0.7))
    heat_cap = float(risk_cfg.get("portfolio_heat_cap_pct", 6.0))
    regime_mult = _pg.regime_size_multiplier(
        macro_risk_score=ctx.get("macro_risk_score"),
        vix=ctx.get("vix"),
        halve_score=float(risk_cfg.get("regime_halve_score", 55.0)),
    )
    returns_fn = ctx.get("returns_fn")
    guard_notes: list[str] = []

    def _assemble(prep: dict, qty: int) -> dict:
        d = prep["d"]
        symbol = prep["symbol"]
        dossier = prep["dossier"]
        conviction = prep["conviction"]
        entry = prep["entry"]
        entry_src = prep["entry_src"]
        stop = prep["stop"]
        risk_dollars = round(qty * (entry - stop), 2)

        reasoning = _sentences(d.get("reasoning", ""), 2)
        pushback = _sentences(d.get("disagreement_notes", ""), 1)
        summary = reasoning
        if pushback:
            summary += f"\nKey pushback weighed: {pushback}"

        unusual_line = None
        unusual = dossier.get("unusual_activity") or {}
        if unusual.get("flagged") and unusual.get("summary"):
            unusual_line = f"Unusual activity: {unusual['summary']}"

        catalyst_line = None
        catalysts = dossier.get("catalysts") or {}
        items = catalysts.get("items") or []
        if items:
            top = items[0]
            catalyst_line = "Catalyst [{0}]: {1} ({2})".format(
                top.get("impact_tag", "general"),
                top.get("headline", ""),
                top.get("source", ""),
            ).strip()

        invalidation = (
            f"Invalid if {symbol} trades below ${stop:.2f} (stop) before entry, "
            f"prints a >3% adverse extended-hours move, or a council veto "
            f"condition triggers (earnings <2d, macro risk-off)."
        )

        card = {
            "symbol": symbol,
            "side": "BUY",
            "order_type": "limit",
            "entry_limit": round(entry, 2),
            "entry_zone_low": round(entry * (1 - zone_pct), 2),
            "entry_zone_high": round(entry * (1 + zone_pct), 2),
            "entry_price_source": entry_src,
            "stop_loss": stop,
            "take_profit": prep["take_profit"],
            "take_profit_r": prep["r_mult"],
            "quantity": int(qty),
            "assumed_equity": assumed_equity,
            "risk_dollars": risk_dollars,
            "conviction": conviction,
            "reasoning_summary": summary,
            "invalidation": invalidation,
            "time_horizon": f"up to {time_stop_days} trading days",
            "validity": f"valid for {validity}",
            "unusual_activity": unusual_line,
            "catalyst": catalyst_line,
            "asof": date_str,
        }
        for field in _REQUIRED_CARD_FIELDS:
            if field not in card:
                raise ValueError(f"advisor card missing required field {field!r}")
        return card

    preps: list[dict] = []
    for d in buys:
        symbol = d["symbol"]
        dossier = dossiers_by_symbol.get(symbol) or {}
        conviction = int(d.get("conviction", 0) or 0)

        entry, entry_src = _reference_price(dossier)
        if entry is None or entry <= 0:
            log.warning("advisor: no usable price for %s — skipping", symbol)
            continue
        atr = _atr(dossier)

        # Stop: chair's level when sane (below entry for a long),
        # else fall back to ATR-based so the card is always tradeable.
        stop = d.get("stop_loss")
        try:
            stop = float(stop) if stop else None
        except (TypeError, ValueError):
            stop = None
        atr_mult = float(d.get("trailing_atr_mult") or atr_mult_default)
        if stop is None or stop >= entry:
            if atr:
                stop = entry - atr_mult * atr
                log.info("advisor %s: chair stop unusable; ATR fallback %.2f",
                         symbol, stop)
            else:
                log.warning("advisor: no stop and no ATR for %s — skipping",
                            symbol)
                continue
        stop = round(stop, 2)

        r_mult = float(d.get("take_profit_r") or 2.0)
        take_profit = round(entry + r_mult * (entry - stop), 2)

        qty = _size_shares(entry, stop, conviction)
        if guards_on:
            if regime_mult <= 0:
                guard_notes.append(
                    f"{symbol}: blocked \u2014 regime veto level (macro risk \u2265 70)")
                log.warning("advisor %s: regime veto \u2014 skipping", symbol)
                continue
            if regime_mult < 1.0:
                qty = int(qty * regime_mult)
                guard_notes.append(
                    f"{symbol}: sized \u00d7{regime_mult:.1f} for elevated macro risk")
            if returns_fn is not None and open_symbols:
                rejected, corr, offender = _pg.correlation_check(
                    symbol, open_symbols, returns_fn, threshold=corr_cap)
                if rejected:
                    guard_notes.append(
                        f"{symbol}: rejected \u2014 "
                        f"{corr:.2f} 60-day correlation with open {offender} "
                        f"(cap {corr_cap:.2f})")
                    log.warning("advisor %s: correlation reject vs %s (%.2f)",
                                symbol, offender, corr if corr else 0.0)
                    continue
        if qty <= 0:
            log.warning("advisor: non-positive size for %s — skipping", symbol)
            continue
        risk_dollars = round(qty * (entry - stop), 2)
        preps.append({
            "d": d, "symbol": symbol, "dossier": dossier,
            "conviction": conviction, "entry": entry, "entry_src": entry_src,
            "stop": stop, "take_profit": take_profit, "r_mult": r_mult,
            "qty": qty, "risk_dollars": risk_dollars,
        })

    cards: CardList = CardList()
    if guards_on and preps:
        scales = _pg.scale_sizes_to_heat(
            [p["risk_dollars"] for p in preps], open_risks,
            assumed_equity, heat_cap)
        for prep, scale in zip(preps, scales):
            if scale <= 0:
                guard_notes.append(
                    f"{prep['symbol']}: blocked \u2014 portfolio heat cap "
                    f"({heat_cap:.1f}%) exhausted by open positions")
                log.warning("advisor %s: heat cap exhausted \u2014 skipping",
                            prep["symbol"])
                continue
            if scale < 1.0:
                guard_notes.append(
                    f"{prep['symbol']}: sized down \u00d7{scale:.2f} to fit "
                    f"{heat_cap:.1f}% heat cap")
            final_qty = int(prep["qty"] * scale)
            if final_qty <= 0:
                guard_notes.append(
                    f"{prep['symbol']}: dust size after heat scaling \u2014 skipped")
                continue
            cards.append(_assemble(prep, final_qty))
    else:
        for prep in preps:
            cards.append(_assemble(prep, prep["qty"]))
    cards.guard_notes = guard_notes
    return cards


# ---------------------------------------------------------------------------
# report rendering
# ---------------------------------------------------------------------------
def render_advisor_markdown(
    cards: list[dict], date_str: str, note: str = ""
) -> str:
    """Human-readable markdown — this is what gets surfaced in chat."""
    lines = [
        f"# Advisor cards — {date_str}",
        "",
        "_Manual-execution cards. No orders were placed — copy each ticket "
        "into your brokerage app (e.g. Webull) by hand. Prices are limits; "
        "respect the stop on every card._",
        "",
    ]
    if note:
        lines += [f"> {note}", ""]
    if not cards:
        lines.append("_No qualifying BUY decisions today — no cards._\n")
        return "\n".join(lines)
    for i, c in enumerate(cards, 1):
        lines += [
            f"## {i}. {c['side']} {c['symbol']} — conviction {c['conviction']}",
            "",
            "```",
            (f"{c['side']} {c['quantity']} {c['symbol']} @ LIMIT "
             f"${c['entry_limit']:.2f}  (zone ${c['entry_zone_low']:.2f}–"
             f"${c['entry_zone_high']:.2f})"),
            "```",
            f"- Stop-loss: ${c['stop_loss']:.2f} | "
            f"Take-profit: ${c['take_profit']:.2f} ({c['take_profit_r']}R)",
            f"- Risk: ${c['risk_dollars']:.2f} on "
            f"${c['assumed_equity']:,.0f} assumed equity "
            f"(entry priced off {c['entry_price_source']})",
        ]
        if c.get("unusual_activity"):
            lines.append(f"- {c['unusual_activity']}")
        if c.get("catalyst"):
            lines.append(f"- {c['catalyst']}")
        lines += [
            f"- Why: {c['reasoning_summary']}",
            f"- Invalidation: {c['invalidation']}",
            f"- Time horizon: {c['time_horizon']} | {c['validity'].capitalize()}",
            "",
        ]
    return "\n".join(lines)


def write_advisor_reports(
    cards: list[dict], reports_dir: Path, date_str: str, note: str = ""
) -> tuple[Path, Path]:
    """Write reports/advisor_<date>.md and .json. Returns both paths."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"advisor_{date_str}.md"
    json_path = reports_dir / f"advisor_{date_str}.json"
    md_path.write_text(
        render_advisor_markdown(cards, date_str, note), encoding="utf-8"
    )
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "date": date_str,
                "mode": "advisor",
                "executed": False,
                "note": note,
                "cards": cards,
            },
            fh,
            indent=2,
        )
    log.info("wrote advisor reports %s and %s", md_path, json_path)
    return md_path, json_path


# ---------------------------------------------------------------------------
# pipeline entry
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a JSON object")
    return data


def _duckdb_returns_fn():
    """Best-effort daily-returns loader from the DuckDB market store.

    READ-ONLY (read_only=True) so the running market sweep is never
    disturbed. Returns None when the DB or table isn't available — the
    correlation guard then abstains instead of blocking.
    """
    try:
        import duckdb
        import os as _os

        from risk.portfolio_guards import daily_returns

        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        db_path = _os.path.join(repo, "data", "market.duckdb")
        if not _os.path.exists(db_path):
            return None

        def _rets(symbol: str) -> list[float]:
            con = duckdb.connect(db_path, read_only=True)
            try:
                rows = con.execute(
                    "SELECT close FROM daily_bars WHERE symbol = ? ORDER BY date",
                    [symbol.upper()],
                ).fetchall()
            finally:
                con.close()
            closes = [float(r[0]) for r in rows if r[0]]
            return daily_returns(closes)

        return _rets
    except Exception as exc:  # noqa: BLE001 - correlation is best-effort
        log.warning("advisor: returns source unavailable (%s) — "
                    "correlation guard abstains", exc)
        return None


def _advisor_portfolio_context(cfg: dict, dossiers_data: dict | None,
                               journal_db: str | None = None) -> dict:
    """Build the portfolio-guard context for build_advisor_cards.

    Open positions come from the paper-trade journal (advisor mode has no
    broker positions); macro score from the dossiers' Kalshi block.
    Everything is best-effort — a failure yields an empty context, never
    a crash. `journal_db` overrides the journal path (tests use a tmp DB so
    real open positions never leak into unit tests).
    """
    open_positions: list[dict] = []
    try:
        from tracking import journal as _journal

        for c in _journal.open_cards(journal_db):
            risk = float(c.get("qty") or 0) * max(
                0.0, float(c.get("entry") or 0) - float(c.get("stop") or 0))
            open_positions.append(
                {"symbol": str(c.get("symbol", "")).upper(),
                 "risk_dollars": round(risk, 2)})
    except Exception as exc:  # noqa: BLE001
        log.warning("advisor: journal unreadable (%s) — no open positions "
                    "for guards", exc)

    macro_score = None
    try:
        for r in (dossiers_data or {}).get("ranked") or []:
            k = (r.get("dossier") or {}).get("kalshi") or {}
            if k.get("score") is not None:
                macro_score = float(k["score"])
                break
        if macro_score is None:
            k = (dossiers_data or {}).get("kalshi") or {}
            if k.get("score") is not None:
                macro_score = float(k["score"])
    except Exception:  # noqa: BLE001
        macro_score = None

    return {
        "open_positions": open_positions,
        "macro_risk_score": macro_score,
        "returns_fn": _duckdb_returns_fn(),
    }


def cmd_advisor(
    cfg: dict,
    decisions_path: str,
    dossiers_path: str | None,
    date_str: str,
    scan_session: str = "morning",
    journal_db: str | None = None,
) -> tuple[Path, Path]:
    """Build advisor cards from a decisions file. Never touches broker/."""
    from pipeline.common import configured_path

    data = _load_json(Path(decisions_path))
    if data.get("status") == "PENDING_COUNCIL":
        note = "Council step pending — no decisions to turn into cards."
        log.info("advisor: %s", note)
        return write_advisor_reports(
            [], Path(cfg["paths"]["reports_dir"]), date_str, note
        )
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError(f"{decisions_path} has no 'decisions' list")

    dossiers_data = _load_json(Path(dossiers_path)) if dossiers_path else None
    dossiers_by_symbol = _dossier_lookup(dossiers_data)

    note = ""
    try:
        from risk.manager import RiskManager
        import tempfile as _tf

        probe = RiskManager(cfg, _tf.mkdtemp(prefix="advisor_probe_"))
        if probe.kill_switch_active():
            note = "Kill switch ACTIVE — no cards generated."
            log.warning("advisor: %s", note)
            return write_advisor_reports(
                [], Path(cfg["paths"]["reports_dir"]), date_str, note
            )
    except Exception as exc:  # noqa: BLE001 - kill-switch check is best-effort
        log.warning("advisor kill-switch probe failed: %s", exc)

    cards = build_advisor_cards(
        decisions, dossiers_by_symbol, cfg, date_str,
        portfolio_context=_advisor_portfolio_context(cfg, dossiers_data,
                                                     journal_db),
    )
    guard_notes = getattr(cards, "guard_notes", None) or []
    if guard_notes:
        note = (note + "\n" if note else "") + "Portfolio guards:\n- " + \
            "\n- ".join(guard_notes)
    md_path, json_path = write_advisor_reports(
        cards, configured_path(cfg, "reports_dir"), date_str, note
    )
    # Journal: log every emitted card as an open paper trade (best-effort;
    # a journal failure must never break the advisor report).
    try:
        from tracking import journal as _journal

        for card in cards:
            _journal.log_card(card, scan_session=scan_session,
                              date_str=date_str)
    except Exception as exc:  # noqa: BLE001
        log.warning("advisor journal logging failed: %s", exc)
    print(md_path.read_text(encoding="utf-8"))
    return md_path, json_path
