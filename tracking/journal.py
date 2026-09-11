"""Paper trade journal — the learning loop's memory.

Every advisor card the system emits is logged here; outcomes are resolved
later (manually or by a resolver pass). The weekly review (tracking/review.py)
reads this table and only this table — no auto-tuning, proposals only.

Paper-trading only. No orders, no credentials, no live paths.

DuckDB tables (created in the shared data/market.duckdb; the running market
sweep owns daily_bars/screen_runs/screen_scores — this module touches ONLY
its own paper_trades table):

    paper_trades(
        card_id TEXT PRIMARY KEY,
        date TEXT,            -- card session date YYYY-MM-DD (ET)
        symbol TEXT,
        side TEXT,            -- BUY (advisor mode is long-only today)
        entry REAL, stop REAL, target REAL,
        qty INTEGER,
        conviction INTEGER,
        scan_session TEXT,    -- morning | midday | late
        outcome TEXT,         -- NULL=open, else WIN | LOSS | SCRATCH | EXPIRED | CANCELLED
        exit_price REAL, exit_date TEXT,
        r_multiple REAL,      -- signed R captured
        mfe_r REAL, mae_r REAL,  -- max favorable/adverse excursion in R (optional)
        notes TEXT
    )
"""

import logging as _logging
import os as _os

LOG = _logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    card_id TEXT PRIMARY KEY,
    date TEXT,
    symbol TEXT,
    side TEXT,
    entry REAL,
    stop REAL,
    target REAL,
    qty INTEGER,
    conviction INTEGER,
    scan_session TEXT,
    outcome TEXT,
    exit_price REAL,
    exit_date TEXT,
    r_multiple REAL,
    mfe_r REAL,
    mae_r REAL,
    notes TEXT
)
"""


def _default_db_path() -> str:
    # Dedicated journal file — NEVER the sweep's market.duckdb. The whole-market
    # sweep owns data/market.duckdb and journal writes must not touch it.
    here = _os.path.dirname(_os.path.abspath(__file__))
    repo = _os.path.dirname(here)  # tracking/ -> repo root
    return _os.path.join(repo, "data", "paper_journal.duckdb")


def _connect(db_path: str | None = None):
    import duckdb

    path = db_path or _default_db_path()
    con = duckdb.connect(path)
    con.execute(_SCHEMA)
    return con


def _r_multiple(side: str, entry: float, stop: float, exit_price: float) -> float | None:
    risk = entry - stop
    if not risk or risk <= 0:
        return None
    if (side or "BUY").upper() == "BUY":
        return (exit_price - entry) / risk
    return (entry - exit_price) / risk  # short (not used in advisor mode today)


def log_card(
    card: dict,
    scan_session: str = "morning",
    date_str: str | None = None,
    db_path: str | None = None,
) -> str:
    """Persist an advisor card as an open paper trade. Returns card_id."""
    import datetime as _dt

    date_str = date_str or _dt.date.today().isoformat()
    symbol = str(card.get("symbol", "")).upper()
    base_id = f"{date_str}_{scan_session}_{symbol}"
    con = _connect(db_path)
    try:
        card_id = base_id
        suffix = 2
        while con.execute(
            "SELECT 1 FROM paper_trades WHERE card_id = ?", [card_id]
        ).fetchone():
            card_id = f"{base_id}-{suffix}"
            suffix += 1
        con.execute(
            "INSERT INTO paper_trades (card_id, date, symbol, side, entry, stop,"
            " target, qty, conviction, scan_session, outcome, notes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            [
                card_id,
                date_str,
                symbol,
                str(card.get("side", "BUY")),
                float(card.get("entry_limit") or 0),
                float(card.get("stop_loss") or 0),
                float(card.get("take_profit") or 0),
                int(card.get("quantity") or 0),
                int(card.get("conviction") or 0),
                scan_session,
                f"entry_src={card.get('entry_price_source')}",
            ],
        )
        LOG.info("journal: logged card %s", card_id)
        return card_id
    finally:
        con.close()


def log_outcome(
    card_id: str,
    exit_price: float,
    exit_date: str,
    outcome: str | None = None,
    notes: str = "",
    mfe_price: float | None = None,
    mae_price: float | None = None,
    db_path: str | None = None,
) -> dict:
    """Resolve an open card. Computes R-multiple; outcome auto-derived if omitted.

    outcome: WIN | LOSS | SCRATCH | EXPIRED | CANCELLED. If omitted, derived
    from r_multiple: >= 0.1 -> WIN, <= -0.1 -> LOSS, else SCRATCH.
    """
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT side, entry, stop FROM paper_trades WHERE card_id = ?",
            [card_id],
        ).fetchone()
        if not row:
            raise KeyError(f"journal: unknown card_id {card_id!r}")
        side, entry, stop = row[0], float(row[1] or 0), float(row[2] or 0)
        r_mult = _r_multiple(side, entry, stop, float(exit_price))
        if outcome is None:
            if r_mult is None:
                outcome = "EXPIRED"
            elif r_mult >= 0.1:
                outcome = "WIN"
            elif r_mult <= -0.1:
                outcome = "LOSS"
            else:
                outcome = "SCRATCH"
        mfe_r = mae_r = None
        if mfe_price is not None:
            mfe_r = _r_multiple(side, entry, stop, float(mfe_price))
        if mae_price is not None:
            mae_r = _r_multiple(side, entry, stop, float(mae_price))
        con.execute(
            "UPDATE paper_trades SET outcome=?, exit_price=?, exit_date=?,"
            " r_multiple=?, mfe_r=?, mae_r=?, notes=COALESCE(notes,'') || ?"
            " WHERE card_id = ?",
            [
                outcome,
                float(exit_price),
                exit_date,
                r_mult,
                mfe_r,
                mae_r,
                f" | {notes}" if notes else "",
                card_id,
            ],
        )
        LOG.info("journal: resolved %s -> %s (R=%.2f)", card_id, outcome,
                 r_mult if r_mult is not None else float("nan"))
        return {"card_id": card_id, "outcome": outcome, "r_multiple": r_mult}
    finally:
        con.close()


def open_cards(db_path: str | None = None) -> list[dict]:
    """All unresolved cards (outcome IS NULL), oldest first."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT card_id, date, symbol, side, entry, stop, target, qty,"
            " conviction, scan_session FROM paper_trades"
            " WHERE outcome IS NULL ORDER BY date, card_id"
        ).fetchall()
        keys = ["card_id", "date", "symbol", "side", "entry", "stop",
                "target", "qty", "conviction", "scan_session"]
        return [dict(zip(keys, r)) for r in rows]
    finally:
        con.close()


def get_card(card_id: str, db_path: str | None = None) -> dict | None:
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM paper_trades WHERE card_id = ?", [card_id]
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in con.description]
        return dict(zip(cols, row))
    finally:
        con.close()


def resolved_cards(db_path: str | None = None) -> list[dict]:
    """All resolved cards (outcome IS NOT NULL)."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM paper_trades WHERE outcome IS NOT NULL"
            " ORDER BY exit_date, card_id"
        ).fetchall()
        cols = [d[0] for d in con.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()
