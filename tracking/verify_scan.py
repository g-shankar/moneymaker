#!/usr/bin/env python3
"""Evidence gate for scan crons: success requires 10 verified cards in the journal.

Usage: ./.venv/bin/python -m tracking.verify_scan <scan_session> [date]
Exit 0 + prints VERIFIED when >=10 open cards with valid brackets exist for
the session/date. Exit 1 + prints REFUSAL:<reason> otherwise.
A scan that cannot pass this gate must report failure, never success.
"""
import datetime as _dt
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from tracking import journal


def main() -> int:
    session = sys.argv[1] if len(sys.argv) > 1 else "morning"
    date_str = sys.argv[2] if len(sys.argv) > 2 else _dt.date.today().isoformat()
    con = journal._connect(None)
    try:
        rows = con.execute(
            "SELECT card_id, symbol, entry, stop, target FROM paper_trades "
            "WHERE date = ? AND scan_session = ? AND outcome IS NULL",
            [date_str, session],
        ).fetchall()
    finally:
        con.close()
    if len(rows) < 10:
        print(f"REFUSAL: session={session} date={date_str} has {len(rows)} open cards, need 10. "
              f"Scan did not produce its evidence. Report failure, not success.")
        return 1
    bad = [r[0] for r in rows if not (r[2] > 0 and r[3] > 0 and r[4] > 0 and r[3] < r[2] < r[4])]
    if bad:
        print(f"REFUSAL: session={session} date={date_str} cards with invalid brackets: {bad}. "
              f"Report failure, not success.")
        return 1
    syms = sorted({r[1] for r in rows})
    print(f"VERIFIED: session={session} date={date_str} {len(rows)} open cards, brackets valid. "
          f"Symbols: {','.join(syms)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
