#!/usr/bin/env python3
"""Fetch the full US-listed stock universe from the Nasdaq screener API.

Correct pagination (limit/offset), dedupe, drop warrants/units/rights/
preferred via name heuristics, normalize symbols for yfinance (BRK.B -> BRK-B).

Output (durable, never /tmp):
  ~/workspace/trading/data/universe_symbols.txt  - one symbol per line
  ~/workspace/trading/data/universe_raw.json     - raw rows for audit
"""
import json
import re
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_TXT = DATA_DIR / "universe_symbols.txt"
OUT_RAW = DATA_DIR / "universe_raw.json"

BASE = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit={limit}&offset={offset}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
}
LIMIT = 500

DROP_PATTERNS = [
    (re.compile(r"\bwarrants?\b", re.I), "warrant"),
    (re.compile(r"\bunits?\b", re.I), "unit"),
    (re.compile(r"\brights?\b", re.I), "right"),
    (re.compile(r"preferred", re.I), "preferred"),
]
# Keep rules: ADR phrasing "representing the right to receive" = ordinary ADR,
# and "Common Units" = the primary tradable security of an MLP.
KEEP_PATTERNS = [
    re.compile(r"representing the right to receive", re.I),
    re.compile(r"common units", re.I),
]


def fetch_page(offset):
    url = BASE.format(limit=LIMIT, offset=offset)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        d = json.load(resp)
    return d["data"]["table"]["rows"]


def main():
    rows = []
    offset = 0
    while True:
        page = fetch_page(offset)
        if not page:
            break
        rows.extend(page)
        print(f"offset={offset} rows={len(page)} total={len(rows)}", flush=True)
        if len(page) < LIMIT:
            break
        offset += LIMIT

    # Dedupe by symbol (first occurrence wins)
    seen, uniq = set(), []
    dupes = 0
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        if not sym or sym in seen:
            dupes += 1
            continue
        seen.add(sym)
        uniq.append(r)

    dropped = {}
    kept = []
    for r in uniq:
        sym = r["symbol"].strip()
        name = r.get("name", "") or ""
        reason = None
        if "^" in sym:
            reason = "preferred-caret"
        elif any(p.search(name) for p in KEEP_PATTERNS):
            reason = None  # legitimate security, keep
        else:
            for pat, label in DROP_PATTERNS:
                if pat.search(name):
                    reason = label
                    break
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        # Normalize for yfinance: BRK.B -> BRK-B, BRK/A -> BRK-A
        kept.append(sym.replace(".", "-").replace("/", "-"))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(kept) + "\n")
    OUT_RAW.write_text(json.dumps(uniq, indent=1))

    print(f"raw_rows={len(rows)} unique={len(uniq)} dupes={dupes}")
    print(f"dropped={dropped} total_dropped={sum(dropped.values())}")
    print(f"kept={len(kept)} -> {OUT_TXT}")


if __name__ == "__main__":
    main()
