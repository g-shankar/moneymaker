"""EOD report generation: markdown daily reports for the paper portfolio.

``generate_daily_report`` renders the markdown; ``write_report`` persists it
under the daily reports directory. No secrets are ever included — inputs are
account/position/decision dicts only.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _fmt_money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _positions_table(positions: list[dict]) -> str:
    if not positions:
        return "_No open positions._"
    lines = [
        "| Symbol | Qty | Avg entry | Current | Unrealized P&L | Market value |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for p in positions:
        lines.append(
            "| {symbol} | {qty:g} | {entry} | {current} | {upl} | {mv} |".format(
                symbol=p.get("symbol", "?"),
                qty=float(p.get("qty", 0) or 0),
                entry=_fmt_money(p.get("avg_entry_price")),
                current=_fmt_money(p.get("current_price")),
                upl=_fmt_money(p.get("unrealized_pl")),
                mv=_fmt_money(p.get("market_value")),
            )
        )
    return "\n".join(lines)


def _decisions_section(decisions: dict) -> str:
    if not isinstance(decisions, dict):
        return "_No decisions data._"
    if decisions.get("status") == "PENDING_COUNCIL":
        return (
            "_Council step pending._ "
            f"{decisions.get('note', 'Run the council per council/runbook.md.')}"
        )
    rows = decisions.get("decisions") or []
    if not rows:
        return "_No decisions recorded._"
    lines = []
    for d in rows:
        if not isinstance(d, dict):
            continue
        lines.append(
            f"### {d.get('symbol', '?')} — {d.get('decision', '?')}\n"
            f"- Conviction: {d.get('conviction', 'n/a')}\n"
            f"- Position size: {d.get('position_size_pct', 'n/a')}% of equity\n"
            f"- Stop loss: {d.get('stop_loss', 'n/a')} | "
            f"Take-profit: {d.get('take_profit_r', 'n/a')}R | "
            f"Trailing ATR mult: {d.get('trailing_atr_mult', 'n/a')}\n"
            f"- Independent lineages considered: "
            f"{d.get('independent_lineages_considered', 'n/a')}\n"
            f"- Disagreement: {d.get('disagreement_notes', 'n/a')}\n"
            f"- Reasoning: {d.get('reasoning', 'n/a')}"
        )
    return "\n\n".join(lines)


def generate_daily_report(
    date: str,
    account: dict,
    positions: list,
    decisions: dict,
    risk_status: dict,
    macro_risk: dict,
) -> str:
    """Render the end-of-day markdown report. Returns the markdown string."""
    account = account or {}
    risk_status = risk_status or {}
    macro_risk = macro_risk or {}

    lines = [
        f"# Daily Report — {date}",
        "",
        "## Account summary",
        "",
        f"- Equity: {_fmt_money(account.get('equity'))}",
        f"- Cash: {_fmt_money(account.get('cash'))}",
        f"- Buying power: {_fmt_money(account.get('buying_power'))}",
        f"- Realized P&L: {_fmt_money(account.get('realized_pl'))}",
        f"- Unrealized P&L: {_fmt_money(account.get('unrealized_pl'))}",
        "",
        "## Positions",
        "",
        _positions_table(positions or []),
        "",
        "## Council decisions",
        "",
        _decisions_section(decisions),
        "",
        "## Risk status",
        "",
        f"- Kill switch active: {risk_status.get('kill_switch_active', 'n/a')}",
        f"- Daily loss halt: {risk_status.get('daily_loss_halted', 'n/a')} "
        f"(day P&L {_fmt_money(risk_status.get('day_pnl'))})",
        f"- Open positions: {risk_status.get('n_open', 'n/a')} / "
        f"{risk_status.get('max_positions', 'n/a')}",
        "",
        "## Macro risk (Kalshi)",
        "",
        f"- Score: {macro_risk.get('score', 'n/a')} / 100",
        f"- Reasoning: {macro_risk.get('reasoning', 'n/a')}",
    ]
    events = macro_risk.get("events") or []
    if events:
        lines.append("- Events:")
        for e in events[:10]:
            lines.append(
                f"  - {e.get('ticker', '?')}: p={e.get('probability', 'n/a')} — "
                f"{e.get('title', '')}".rstrip()
            )
    lines.append("")
    return "\n".join(lines)


def write_report(date: str, markdown: str, daily_dir: str | Path) -> Path:
    """Write the markdown report to <daily_dir>/<date>.md. Returns the path."""
    daily_dir = Path(daily_dir)
    daily_dir.mkdir(parents=True, exist_ok=True)
    out_path = daily_dir / f"{date}.md"
    out_path.write_text(markdown, encoding="utf-8")
    log.info("wrote daily report %s", out_path)
    return out_path
