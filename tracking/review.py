"""Weekly self-review generator — the learning loop's report card.

Reads ONLY tracking/journal.paper_trades. Produces reports/review_<week>.md
with performance stats and exactly 3 concrete tuning PROPOSALS. Nothing is
auto-tuned: every proposal needs Gowrishankar's approve/edit/reject.

Usage: python -m tracking.review [--week 2026-W37] [--db PATH] [--reports PATH]
"""

import argparse as _argparse
import datetime as _dt
import logging as _logging
import os as _os

from . import journal as _journal

LOG = _logging.getLogger(__name__)

_MIN_TRADES_FOR_CALIBRATION = 5


def _iso_week_label(day: _dt.date | None = None) -> str:
    day = day or _dt.date.today()
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _pct(n: float, d: float) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def _bucket(conviction: int) -> str:
    c = int(conviction or 0)
    if c >= 90:
        return "90+"
    if c >= 80:
        return "80-89"
    if c >= 70:
        return "70-79"
    return "60-69"


def _stats(trades: list[dict]) -> dict:
    """Core stats over resolved trades."""
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    wins = [r for r in rs if r >= 0.1]
    losses = [r for r in rs if r <= -0.1]
    n = len(rs)
    win_rate = len(wins) / n if n else 0.0
    avg_r = sum(rs) / n if n else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    # expectancy per trade in R
    expectancy = avg_r
    profit_factor = (
        abs(sum(wins) / sum(losses)) if losses and sum(losses) else float("inf") if wins else 0.0
    )
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_r": avg_r,
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        "expectancy_r": expectancy,
        "profit_factor": profit_factor,
        "total_r": sum(rs),
    }


def _proposals(trades: list[dict], by_session: dict, by_bucket: dict,
               by_symbol: dict) -> list[dict]:
    """Generate exactly 3 concrete tuning proposals from the data.

    Rule-based and conservative: proposals only fire on enough evidence;
    otherwise the proposal is a data-collection action. Never auto-tunes.
    """
    props: list[dict] = []

    def add(title: str, evidence: str, change: str):
        props.append({"title": title, "evidence": evidence,
                      "proposed_change": change, "status": "pending approval"})

    # P1: conviction calibration — is the min-conviction gate earning its keep?
    low = by_bucket.get("60-69", {})
    if low.get("n", 0) >= _MIN_TRADES_FOR_CALIBRATION:
        if low["win_rate"] < 0.45:
            add(
                "Raise min_conviction 60 → 70",
                f"60-69 bucket: {low['n']} trades, {_pct(low['wins'], low['n'])} win rate, "
                f"{low['avg_r']:+.2f}R avg — below the 45% bar.",
                "advisor.min_conviction: 60 → 70",
            )
        else:
            add(
                "Hold min_conviction at 60",
                f"60-69 bucket: {low['n']} trades, {_pct(low['wins'], low['n'])} win rate — "
                "gate is earning its keep.",
                "no change; re-check next week",
            )
    else:
        add(
            "Resolve more cards before judging the 60-69 bucket",
            f"only {low.get('n', 0)} resolved trades in the 60-69 bucket "
            f"(need {_MIN_TRADES_FOR_CALIBRATION}).",
            "no parameter change; resolve open cards via journal.log_outcome()",
        )

    # P2: worst symbol — consistent loser gets benched.
    losers = [
        (s, st) for s, st in by_symbol.items()
        if st["n"] >= 3 and st["avg_r"] < -0.5
    ]
    if losers:
        sym, st = min(losers, key=lambda kv: kv[1]["avg_r"])
        add(
            f"Bench {sym} for 2 weeks",
            f"{sym}: {st['n']} trades, {st['avg_r']:+.2f}R avg, "
            f"{_pct(st['wins'], st['n'])} win rate — consistently negative.",
            f"add {sym} to a temporary exclusion list (revert after 2 green weeks)",
        )
    else:
        add(
            "No symbol bench warranted",
            "no symbol with ≥3 trades averages worse than −0.50R.",
            "no change",
        )

    # P3: session comparison — is a scan slot underperforming?
    weak = [
        (s, st) for s, st in by_session.items()
        if st["n"] >= _MIN_TRADES_FOR_CALIBRATION and st["expectancy_r"] < 0
    ]
    if weak:
        sess, st = min(weak, key=lambda kv: kv[1]["expectancy_r"])
        add(
            f"Halve size on {sess} session cards",
            f"{sess}: {st['n']} trades, expectancy {st['expectancy_r']:+.2f}R — "
            "negative expectancy at this slot.",
            f"apply 0.5× size multiplier to {sess} cards until expectancy turns positive",
        )
    else:
        sessions = ", ".join(
            f"{s} ({st['n']} trades, {st['expectancy_r']:+.2f}R)"
            for s, st in sorted(by_session.items())
        ) or "no resolved trades yet"
        add(
            "Keep all three scan sessions",
            f"no session shows negative expectancy on ≥{_MIN_TRADES_FOR_CALIBRATION} "
            f"trades. Sessions: {sessions}.",
            "no change",
        )

    return props[:3]


def generate_review(
    week_label: str | None = None,
    db_path: str | None = None,
    reports_dir: str | None = None,
) -> str:
    """Build the weekly review markdown. Returns the report path."""
    week_label = week_label or _iso_week_label()
    if reports_dir is None:
        here = _os.path.dirname(_os.path.abspath(__file__))
        reports_dir = _os.path.join(_os.path.dirname(here), "reports")
    _os.makedirs(reports_dir, exist_ok=True)

    trades = _journal.resolved_cards(db_path)
    open_n = len(_journal.open_cards(db_path))
    overall = _stats(trades)

    # group resolved trades
    groups: dict[str, dict[str, list[dict]]] = {
        "session": {}, "bucket": {}, "symbol": {},
    }
    for t in trades:
        groups["session"].setdefault(str(t.get("scan_session") or "?"), []).append(t)
        groups["bucket"].setdefault(_bucket(t.get("conviction")), []).append(t)
        groups["symbol"].setdefault(str(t.get("symbol") or "?"), []).append(t)
    by_session = {k: _stats(v) for k, v in groups["session"].items()}
    by_bucket = {k: _stats(v) for k, v in groups["bucket"].items()}
    by_symbol = {k: _stats(v) for k, v in groups["symbol"].items()}

    proposals = _proposals(trades, by_session, by_bucket, by_symbol)

    L: list[str] = []
    L.append(f"# Weekly trading review — {week_label}")
    L.append("")
    L.append("_Paper-trade journal self-review. Proposals need human approval — nothing auto-tunes._")
    L.append("")
    L.append("## Headline")
    L.append(f"- Resolved trades: **{overall['n']}** ({open_n} still open)")
    L.append(f"- Win rate: **{_pct(overall['wins'], overall['n'])}** "
             f"({overall['wins']}W / {overall['losses']}L)")
    L.append(f"- Avg R-multiple: **{overall['avg_r']:+.2f}R** "
             f"(avg win {overall['avg_win_r']:+.2f}R, avg loss {overall['avg_loss_r']:+.2f}R)")
    L.append(f"- Expectancy: **{overall['expectancy_r']:+.2f}R per trade**")
    L.append(f"- Total: **{overall['total_r']:+.2f}R** banked")
    pf = overall["profit_factor"]
    L.append(f"- Profit factor: **{pf:.2f}**" if pf != float("inf") else "- Profit factor: **∞** (no losses)")
    L.append("")
    L.append("## By scan session")
    if by_session:
        L.append("| session | n | win rate | avg R | expectancy |")
        L.append("|---|---|---|---|---|")
        for s in sorted(by_session):
            st = by_session[s]
            L.append(f"| {s} | {st['n']} | {_pct(st['wins'], st['n'])} | "
                     f"{st['avg_r']:+.2f}R | {st['expectancy_r']:+.2f}R |")
    else:
        L.append("_no resolved trades yet_")
    L.append("")
    L.append("## Conviction calibration (do high-conviction cards win more?)")
    if by_bucket:
        L.append("| conviction | n | win rate | avg R |")
        L.append("|---|---|---|---|")
        for b in ["60-69", "70-79", "80-89", "90+"]:
            if b in by_bucket:
                st = by_bucket[b]
                L.append(f"| {b} | {st['n']} | {_pct(st['wins'], st['n'])} | {st['avg_r']:+.2f}R |")
    else:
        L.append("_no resolved trades yet_")
    L.append("")
    L.append("## Best / worst symbols (≥2 trades)")
    syms = [(s, st) for s, st in by_symbol.items() if st["n"] >= 2]
    if syms:
        syms.sort(key=lambda kv: kv[1]["avg_r"], reverse=True)
        L.append("| symbol | n | win rate | avg R | total R |")
        L.append("|---|---|---|---|---|")
        for s, st in syms:
            L.append(f"| {s} | {st['n']} | {_pct(st['wins'], st['n'])} | "
                     f"{st['avg_r']:+.2f}R | {st['total_r']:+.2f}R |")
    else:
        L.append("_not enough data_")
    L.append("")
    L.append("## Tuning proposals (need your approval)")
    for i, p in enumerate(proposals, 1):
        L.append(f"### P{i}: {p['title']}")
        L.append(f"- Evidence: {p['evidence']}")
        L.append(f"- Proposed change: `{p['proposed_change']}`")
        L.append(f"- Status: **{p['status']}**")
        L.append("")
    L.append("---")
    L.append("_Generated by tracking/review.py from the paper-trade journal. "
             "Approve, edit, or reject each proposal — nothing changes until you say so._")

    path = _os.path.join(reports_dir, f"review_{week_label}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    LOG.info("review written to %s (%d resolved trades)", path, overall["n"])
    return path


def main(argv: list[str] | None = None) -> int:
    ap = _argparse.ArgumentParser(description="weekly paper-trade self-review")
    ap.add_argument("--week", default=None, help="week label like 2026-W37 (default: current ISO week)")
    ap.add_argument("--db", default=None, help="DuckDB path (default: data/paper_journal.duckdb)")
    ap.add_argument("--reports", default=None, help="reports dir (default: reports/)")
    args = ap.parse_args(argv)
    path = generate_review(args.week, args.db, args.reports)
    print(f"review written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
