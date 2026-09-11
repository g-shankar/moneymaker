#!/usr/bin/env python3
"""Assemble validation results: cost analysis + markdown report tables.

Reads per-cell JSON from the scratch dir. Does NOT rerun the engine.
"""
import sys, os, json

import numpy as np
import pandas as pd

SCRATCH = "/home/hatch/workspace/trading/backtest/.validation_scratch"
REPORT = "/home/hatch/workspace/trading/reports/validation_2026-09-11.md"


def load(tag):
    with open(os.path.join(SCRATCH, f"val_{tag}.json")) as f:
        return json.load(f)


# ---------------------------------------------------------------- costs
def cost_adjust(cell, comm_per_share=0.01, slip_bps=5.0):
    """Post-hoc cost model: $0.01/share/side + 5bps/side on traded notional.

    Shares inferred per trade from pnl/(exit-entry) (engine uses whole shares).
    Costs are cash outflows; equity-curve impact is approximated by subtracting
    cumulative costs at each trade's exit date (second-order timing ignored,
    documented in report).
    """
    trades = cell["trades"]
    total_cost = 0.0
    adj_trades = []
    cost_by_exit = {}
    for t in trades:
        ep, xp, pnl = t["entry_price"], t["exit_price"], t["pnl"]
        shares = int(round(pnl / (xp - ep))) if xp != ep else 0
        comm = 2 * comm_per_share * shares
        slip = (slip_bps / 10000.0) * (shares * ep + shares * xp)
        cost = comm + slip
        total_cost += cost
        adj = dict(t)
        adj["pnl"] = round(pnl - cost, 2)
        adj["cost"] = round(cost, 2)
        adj["shares_est"] = shares
        adj_trades.append(adj)
        cost_by_exit[t["exit_date"]] = cost_by_exit.get(t["exit_date"], 0.0) + cost

    eq = pd.DataFrame(cell["equity"], columns=["date", "equity"])
    eq["date"] = pd.to_datetime(eq["date"])
    cum = 0.0
    adj_eq = []
    for _, row in eq.iterrows():
        d = row["date"].strftime("%Y-%m-%d")
        cum += cost_by_exit.get(d, 0.0)
        adj_eq.append(row["equity"] - cum)
    eq["adj_equity"] = adj_eq

    rets = eq["adj_equity"].pct_change().dropna()
    vol = rets.std()
    sharpe = float(rets.mean() / vol * np.sqrt(252)) if vol > 0 else 0.0
    roll_max = eq["adj_equity"].cummax()
    max_dd = float(((eq["adj_equity"] - roll_max) / roll_max).min())
    total_return = float(eq["adj_equity"].iloc[-1] / 100000.0 - 1)

    wins = [t for t in adj_trades if t["pnl"] > 0]
    losses = [t for t in adj_trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    pf = gw / gl if gl > 0 else float("inf")

    return {
        "total_cost": round(total_cost, 2),
        "avg_cost_per_trade": round(total_cost / len(trades), 2) if trades else 0,
        "total_return": round(total_return, 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0,
        "profit_factor": round(pf, 3) if np.isfinite(pf) else "inf",
        "n_trades": len(trades),
        "adj_trades": adj_trades,
    }


def fmt(v):
    return v


def metrics_row(m):
    return (m["total_return"], m["sharpe"], m["max_drawdown"],
            m["win_rate"], m["profit_factor"], m["n_trades"])


def main():
    # ---- 1. parameter sensitivity
    atr_cells = []
    for atr, tag in [(2.0, "sens_atr_2p0"), (2.25, "sens_atr_2p25"),
                     (2.5, "sens_atr_2p5"), (2.75, "sens_atr_2p75"),
                     (3.0, "sens_atr_3p0")]:
        atr_cells.append((atr, load(tag)["metrics"]))
    r_cells = []
    for r, tag in [(1.5, "sens_r_1p5_atr2p5"), (2.0, "sens_atr_2p5"),
                   (2.5, "sens_r_2p5_atr2p5")]:
        r_cells.append((r, load(tag)["metrics"]))

    # ---- 2. walk-forward
    wf = []
    for i in (1, 2, 3):
        is_rows = []
        for atr in (2.0, 2.25, 2.5, 2.75, 3.0):
            tag = f"wf_is{i}_atr_{str(atr).replace('.', 'p')}"
            m = load(tag)["metrics"]
            is_rows.append((atr, m["sharpe"], m["n_trades"], m["total_return"]))
        tuned = load(f"wf_oos{i+1}_tuned")
        fixed = load(f"wf_oos{i+1}_fixed30")
        wf.append({"seg": i, "is_rows": is_rows, "tuned": tuned, "fixed": fixed})

    # ---- 3. costs on baseline (ATR 3.0, R 2.0)
    baseline = load("sens_atr_3p0")
    cost = cost_adjust(baseline)

    # ---- write report
    L = []
    A = L.append
    A("# Strategy 1 — Validation Report")
    A("")
    A("Generated: 2026-09-11. Read-only validation: the engine, strategy, and pipeline")
    A("code were not modified. All runs use the original setup (20-symbol universe,")
    A("$100k, 2024-09-01 → 2026-09-01) with bars pre-fetched once and reused, so every")
    A("cell is bit-comparable. Baseline = ATR 3.0 / R 2.0 (the +45.3% / Sharpe 1.40 run).")
    A("")
    A("## 1. Parameter sensitivity — ATR trailing-stop multiple (R fixed 2.0)")
    A("")
    A("| ATR mult | total return | Sharpe | max DD | win rate | profit factor | trades |")
    A("|---|---|---|---|---|---|---|")
    for atr, m in atr_cells:
        A(f"| {atr} | {m['total_return']:.2%} | {m['sharpe']:.3f} | {m['max_drawdown']:.2%} | "
          f"{m['win_rate']:.1%} | {m['profit_factor']:.3f} | {m['n_trades']} |")
    sharpes = [m["sharpe"] for _, m in atr_cells]
    A("")
    A(f"Sharpe range across ATR 2.0–3.0: {min(sharpes):.3f} – {max(sharpes):.3f} "
      f"(spread {max(sharpes)-min(sharpes):.3f}).")
    A("")
    A("## 2. Parameter sensitivity — take-profit R (ATR fixed 2.5)")
    A("")
    A("| R | total return | Sharpe | max DD | win rate | profit factor | trades |")
    A("|---|---|---|---|---|---|---|")
    for r, m in r_cells:
        A(f"| {r} | {m['total_return']:.2%} | {m['sharpe']:.3f} | {m['max_drawdown']:.2%} | "
          f"{m['win_rate']:.1%} | {m['profit_factor']:.3f} | {m['n_trades']} |")
    A("")
    A("## 3. Walk-forward — 4 half-year segments, tune ATR on IS, test on next OOS")
    A("")
    for w in wf:
        i = w["seg"]
        A(f"### Fold {i}: IS segment {i} → OOS segment {i+1}")
        A("")
        A("| ATR (IS) | IS Sharpe | IS trades | IS return |")
        A("|---|---|---|---|")
        for atr, sh, n, ret in sorted(w["is_rows"], key=lambda r: -r[1]):
            A(f"| {atr} | {sh:.3f} | {n} | {ret:.2%} |")
        tm, fm = w["tuned"]["metrics"], w["fixed"]["metrics"]
        A("")
        A(f"IS winner: ATR {w['tuned']['atr']} → OOS Sharpe {tm['sharpe']:.3f}, "
          f"return {tm['total_return']:.2%}, {tm['n_trades']} trades.")
        A(f"Fixed ATR 3.0 control → OOS Sharpe {fm['sharpe']:.3f}, "
          f"return {fm['total_return']:.2%}, {fm['n_trades']} trades.")
        A("")
    A("## 4. Realistic costs — baseline (ATR 3.0 / R 2.0) with $0.01/share/side + 5 bps/side")
    A("")
    bm = baseline["metrics"]
    A("| | no costs | with costs |")
    A("|---|---|---|")
    A(f"| total return | {bm['total_return']:.2%} | {cost['total_return']:.2%} |")
    A(f"| Sharpe | {bm['sharpe']:.3f} | {cost['sharpe']:.3f} |")
    A(f"| max DD | {bm['max_drawdown']:.2%} | {cost['max_drawdown']:.2%} |")
    A(f"| win rate | {bm['win_rate']:.1%} | {cost['win_rate']:.1%} |")
    A(f"| profit factor | {bm['profit_factor']:.3f} | {cost['profit_factor']:.3f} |")
    A(f"| trades | {bm['n_trades']} | {cost['n_trades']} |")
    A("")
    A(f"Total cost drag: ${cost['total_cost']:,.2f} over {cost['n_trades']} trades "
      f"(avg ${cost['avg_cost_per_trade']:.2f}/trade). "
      f"Costs consume {(1 - cost['total_return']/bm['total_return']):.1%} of the gross return.")
    A("")
    A("Cost method: post-hoc per-trade deduction (engine has $0 commission hard-coded and")
    A("was not modified). Shares inferred as round(pnl / (exit−entry)); equity-curve Sharpe/DD")
    A("approximated by subtracting cumulative costs at exit dates. Timing/reinvestment")
    A("second-order effects ignored — directionally conservative.")
    A("")
    A("## 5. Verdict")
    A("")
    A("{{VERDICT}}")
    A("")

    with open(REPORT, "w") as f:
        f.write("\n".join(L))
    print(f"report skeleton written to {REPORT}")
    print(json.dumps({"cost": {k: v for k, v in cost.items() if k != "adj_trades"},
                      "atr_sharpes": sharpes}, indent=1))


if __name__ == "__main__":
    main()
