#!/usr/bin/env python3
"""Full 940 large-cap ranking -> 10 diversified paper cards (sector cap max 2).

Usage: ./.venv/bin/python -m screener.build_ten
"""
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data.indicators import atr
from screener.screen import score_frame
from screener.sizing import PAPER_EQUITY, RISK_PCT, risk_dollars, size_shares
from screener.store import get_conn

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("build_ten")

BASE = Path(__file__).resolve().parent.parent
SECTOR_CACHE = BASE / "data" / "sector_cache.json"


def load_sectors(symbols):
    cache = json.loads(SECTOR_CACHE.read_text()) if SECTOR_CACHE.exists() else {}
    missing = [s for s in symbols if s not in cache]
    for i, s in enumerate(missing):
        try:
            info = yf.Ticker(s).info
            cache[s] = info.get("sector", "Unknown") or "Unknown"
        except Exception:
            cache[s] = "Unknown"
        if i:
            time.sleep(0.4)
        if (i + 1) % 10 == 0:
            SECTOR_CACHE.write_text(json.dumps(cache))
    SECTOR_CACHE.write_text(json.dumps(cache))
    return cache


def main():
    uni = [s.strip() for s in (BASE / "data" / "universe_symbols.txt").read_text().splitlines() if s.strip()]
    con = get_conn()
    scored = []
    for s in uni:
        rows = con.execute(
            "SELECT date, open, high, low, close, volume FROM daily_bars WHERE symbol=? ORDER BY date", [s]
        ).fetchall()
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        sc = score_frame(s, df)
        if sc:
            scored.append(sc)
    con.close()
    scored.sort(key=lambda r: r["composite_score"], reverse=True)
    print(f"scored {len(scored)}/{len(uni)}")

    cands = scored[:40]
    sectors = load_sectors([c["symbol"] for c in cands])
    for c in cands:
        c["sector"] = sectors.get(c["symbol"], "Unknown")

    # Diversified 10: max 2 per sector
    ten, counts = [], {}
    for c in cands:
        if len(ten) >= 10:
            break
        sec = c["sector"]
        if counts.get(sec, 0) >= 2:
            continue
        counts[sec] = counts.get(sec, 0) + 1
        ten.append(c)

    out = []
    for c in ten:
        entry = round(c["last_price"], 2)
        a = c["atr_14"]
        stop = round(entry - 3.0 * a, 2)
        tgt = round(entry + 2 * (entry - stop), 2)
        # Equal-risk sizing: every card risks ~$1,000 of the $100k paper book.
        qty = size_shares(entry, stop)
        risk = round((entry - stop) * qty, 2)
        out.append({
            "symbol": c["symbol"], "sector": c["sector"],
            "score": round(c["composite_score"], 3),
            "ret_20d": c["ret_20d"], "rsi_14": c["rsi_14"],
            "dist_52wk_high_pct": c["dist_52wk_high_pct"],
            "entry": entry, "stop": stop, "target": tgt,
            "qty": qty, "risk": risk,
        })
    rep_path = BASE / "reports" / f"full940_ten_{date.today().isoformat()}.json"
    rep_path.write_text(json.dumps({
        "date": date.today().isoformat(), "universe": len(uni), "scored": len(scored),
        "sector_counts": counts,
        "sizing": {"paper_equity": PAPER_EQUITY, "risk_pct": RISK_PCT,
                   "risk_dollars_per_trade": risk_dollars()},
        "cards": out,
        "top40": [{"symbol": c["symbol"], "sector": c["sector"],
                   "score": round(c["composite_score"], 3)} for c in cands],
    }, indent=1))
    print(f"wrote {rep_path}")
    for o in out:
        print(f"  {o['symbol']:6s} {o['sector'][:22]:22s} score={o['score']:.3f} "
              f"entry={o['entry']} stop={o['stop']} tgt={o['target']} qty={o['qty']} risk=${o['risk']:.0f}")


if __name__ == "__main__":
    main()
