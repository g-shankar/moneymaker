#!/usr/bin/env python3
"""Fetch daily OHLCV from the Nasdaq historical API (alt pipe to Yahoo).

Usage: SCREEN_DB=data/market_nasdaq.duckdb ./.venv/bin/python -m screener.fetch_nasdaq <symbols.txt>
Writes daily_bars via upsert_bars. Threaded; polite 0.3s stagger.
"""
import json
import logging
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from screener.store import get_conn, init_db, upsert_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fetch_nasdaq")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
}
WORKERS = 3


def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except Exception:
        return 0.0


def fetch_one(symbol: str) -> list[tuple] | None:
    fromdate = (date.today() - timedelta(days=400)).isoformat()
    url = (f"https://api.nasdaq.com/api/quote/{symbol}/historical"
           f"?assetclass=stocks&fromdate={fromdate}&limit=9999")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        rows = d["data"]["tradesTable"]["rows"]
        out = []
        for rw in rows:
            # date format: MM/DD/YYYY
            m, dd, yy = rw["date"].split("/")
            out.append((f"{yy}-{m}-{dd}", _num(rw["open"]), _num(rw["high"]),
                        _num(rw["low"]), _num(rw["close"]), _num(rw["volume"])))
        return out
    except Exception as exc:
        log.warning("%s failed: %s", symbol, exc)
        return None


def main() -> None:
    symfile = Path(sys.argv[1])
    symbols = [s.strip() for s in symfile.read_text().splitlines() if s.strip()]
    con = get_conn()
    init_db(con)
    done = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily_bars").fetchall()}
    todo = [s for s in symbols if s not in done]
    log.info("nasdaq pipe: %d symbols, %d already in DB, %d to fetch", len(symbols), len(done), len(todo))

    from concurrent.futures import as_completed
    ok, failed = 0, []
    futures = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for sym in todo:  # submit all; gentle stagger keeps Nasdaq happy
            futures[ex.submit(fetch_one, sym)] = sym
            time.sleep(0.15)
        total = len(futures)
        for j, fut in enumerate(as_completed(futures), 1):
            sym = futures[fut]
            try:
                bars = fut.result()
            except Exception as exc:
                log.warning("%s raised: %s", sym, exc)
                bars = None
            if bars:
                upsert_bars(con, sym, bars)
                ok += 1
            else:
                failed.append(sym)
            if j % 25 == 0:
                log.info("nasdaq pipe: %d/%d ok=%d failed=%d", j, total, ok, len(failed))
    log.info("nasdaq pipe DONE: ok=%d failed=%d %s", ok, len(failed), failed[:10])
    con.close()


if __name__ == "__main__":
    main()
