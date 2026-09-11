#!/usr/bin/env python3
"""Deterministic deep review of open paper cards.

No LLM, no personas — just verifiable checks against daily bars:
trend (price vs SMA50/200), RSI-14 extremes, distance from 52-week high,
ATR-multiple sanity of the bracket, volume z-score, per-card risk dollars,
and portfolio-level heat + sector concentration.

Usage: ./.venv/bin/python -m tracking.review_cards
Writes reports/card_review_<date>.json and prints a verdict per card.
Exit 0 always (a review reports; it never blocks).
"""
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data.indicators import atr, rsi, sma, volume_zscore
from screener.store import get_conn
from tracking import journal

BASE = Path(__file__).resolve().parent.parent

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check("trend")
def _trend(df, card):
    c = df["close"]
    s50 = sma(c, 50).iloc[-1]
    s200 = sma(c, 200).iloc[-1] if len(c) >= 200 else float("nan")
    last = c.iloc[-1]
    if last > s50 and (pd.isna(s200) or last > s200):
        return "PASS", f"above SMA50 ({s50:.2f})" + (f" and SMA200 ({s200:.2f})" if not pd.isna(s200) else "")
    if last < s50:
        return "FLAG", f"below SMA50 ({s50:.2f}) — buying weakness, not strength"
    return "PASS", f"above SMA50 ({s50:.2f}), below SMA200 ({s200:.2f})"


@check("rsi")
def _rsi(df, card):
    v = rsi(df["close"], 14).iloc[-1]
    if v >= 80:
        return "FLAG", f"RSI-14 {v:.1f} — parabolic, chase risk"
    if v >= 70:
        return "NOTE", f"RSI-14 {v:.1f} — hot but not extreme"
    if v <= 25:
        return "FLAG", f"RSI-14 {v:.1f} — washed out, no momentum"
    return "PASS", f"RSI-14 {v:.1f}"


@check("distance_from_high")
def _dist(df, card):
    hi = df["high"].tail(252).max()
    d = (hi - df["close"].iloc[-1]) / hi * 100
    if d > 30:
        return "FLAG", f"{d:.1f}% below 52w high — deep hole for a momentum card"
    return "PASS", f"{d:.1f}% below 52w high"


@check("bracket_atr")
def _bracket(df, card):
    a = atr(df.rename(columns=str.lower)[["open", "high", "low", "close"]].astype(float), 14).iloc[-1]
    if not a or a <= 0:
        return "FLAG", "ATR unavailable — bracket unverifiable"
    mult = (card["entry"] - card["stop"]) / a
    tgt_mult = (card["target"] - card["entry"]) / a
    if abs(mult - 3.0) > 0.25:
        return "FLAG", f"stop is {mult:.2f}x ATR, expected ~3.0x"
    return "PASS", f"stop {mult:.2f}x ATR / target {tgt_mult:.2f}x ATR"


@check("volume")
def _vol(df, card):
    z = volume_zscore(df["volume"], 20).iloc[-1]
    if z >= 3:
        return "NOTE", f"volume z {z:.1f} — unusual activity, confirm catalyst"
    if z <= -1.5:
        return "NOTE", f"volume z {z:.1f} — thin interest"
    return "PASS", f"volume z {z:.1f}"


@check("intraday_position")
def _pos(df, card):
    """Where is the last close vs the card's entry/stop/target?"""
    last = df["close"].iloc[-1]
    e, s, t = card["entry"], card["stop"], card["target"]
    if last <= s:
        return "STOPPED", f"last {last:.2f} at/below stop {s:.2f} — resolve LOSS"
    if last >= t:
        return "TARGET", f"last {last:.2f} at/above target {t:.2f} — resolve WIN"
    r = (last - e) / (e - s) if e != s else 0
    return "PASS", f"last {last:.2f} = {r:+.2f}R vs entry"


def main():
    cards = journal.open_cards()
    con = get_conn()
    results = []
    for card in cards:
        sym = card["symbol"]
        rows = con.execute(
            "SELECT date, open, high, low, close, volume FROM daily_bars WHERE symbol=? ORDER BY date",
            [sym],
        ).fetchall()
        entry = {"card_id": card["card_id"], "symbol": sym, "sector": "?",
                 "verdict": "FLAG", "risk_dollars": 0.0, "checks": {}}
        if len(rows) < 65:
            entry["checks"]["data"] = ["FLAG", f"only {len(rows)} bars — insufficient history"]
            results.append(entry)
            continue
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        worst = "PASS"
        for name, fn in CHECKS:
            try:
                status, detail = fn(df, card)
            except Exception as exc:  # noqa: BLE001
                status, detail = "FLAG", f"check error: {exc}"
            entry["checks"][name] = [status, detail]
            if status in ("FLAG", "STOPPED", "TARGET") and worst == "PASS":
                worst = status
            elif status == "NOTE" and worst == "PASS":
                worst = "NOTE"
        entry["verdict"] = worst
        entry["risk_dollars"] = round((card["entry"] - card["stop"]) * card["qty"], 2)
        results.append(entry)
    con.close()

    # portfolio-level
    sectors = {}
    try:
        cache = json.loads((BASE / "data" / "sector_cache.json").read_text())
        for r in results:
            r["sector"] = cache.get(r["symbol"], "Unknown")
            sectors[r["sector"]] = sectors.get(r["sector"], 0) + 1
    except Exception:
        pass
    total_risk = round(sum(r.get("risk_dollars", 0) for r in results), 2)
    breachers = [s for s, n in sectors.items() if n > 2]

    report = {
        "date": date.today().isoformat(),
        "open_cards": len(results),
        "total_risk_dollars": total_risk,
        "sector_counts": sectors,
        "sector_cap_breaches": breachers,
        "cards": results,
    }
    out = BASE / "reports" / f"card_review_{date.today().isoformat()}.json"
    out.write_text(json.dumps(report, indent=1))

    for r in results:
        print(f"{r['symbol']:6s} {r['sector'][:20]:20s} {r['verdict']:8s} risk=${r.get('risk_dollars', 0):,.0f}")
        for name, (st, detail) in r["checks"].items():
            if st != "PASS":
                print(f"    [{st}] {name}: {detail}")
    print(f"\n{len(results)} open cards, total stop-risk ${total_risk:,.0f}")
    if breachers:
        print(f"SECTOR CAP BREACH: {breachers}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
