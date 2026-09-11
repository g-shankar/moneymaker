"""DuckDB market data store for the whole-market screener.

Database: ~/workspace/trading/data/market.duckdb
Tables:
  daily_bars    - raw 1y daily OHLCV per symbol (Stage 1 sweep output)
  screen_runs   - one row per screen run (params as JSON)
  screen_scores - per-symbol factor values + composite score per run
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb

DB_PATH = Path(os.environ.get("SCREEN_DB", Path.home() / "workspace" / "trading" / "data" / "market.duckdb"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol VARCHAR NOT NULL,
    date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE
);
CREATE TABLE IF NOT EXISTS screen_runs (
    run_id VARCHAR NOT NULL,
    run_date DATE NOT NULL,
    params_json VARCHAR
);
CREATE TABLE IF NOT EXISTS screen_scores (
    run_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    volume_z_20 DOUBLE,
    rsi_14 DOUBLE,
    ret_20d DOUBLE,
    ret_60d DOUBLE,
    dist_52wk_high_pct DOUBLE,
    atr_14 DOUBLE,
    avg_dollar_vol_20 DOUBLE,
    composite_score DOUBLE
);
"""


def get_conn() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    return con


def init_db(con: duckdb.DuckDBPyConnection | None = None) -> None:
    own = con is None
    con = con or get_conn()
    try:
        con.execute(_SCHEMA)
        # DuckDB >= 0.10 supports IF NOT EXISTS on CREATE INDEX.
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_bars_sym_date "
            "ON daily_bars (symbol, date)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_scores_run "
            "ON screen_scores (run_id, composite_score)"
        )
    finally:
        if own:
            con.close()


def upsert_bars(con: duckdb.DuckDBPyConnection, symbol: str, rows: list[tuple]) -> int:
    """Replace all bars for *symbol* with *rows* of
    (date, open, high, low, close, volume). Returns row count."""
    con.execute("DELETE FROM daily_bars WHERE symbol = ?", [symbol])
    if rows:
        con.executemany(
            "INSERT INTO daily_bars (symbol, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(symbol, *r) for r in rows],
        )
    return len(rows)


def record_run(con: duckdb.DuckDBPyConnection, run_id: str, run_date: str, params: dict) -> None:
    con.execute(
        "INSERT INTO screen_runs (run_id, run_date, params_json) VALUES (?, ?, ?)",
        [run_id, run_date, json.dumps(params)],
    )


def record_scores(con: duckdb.DuckDBPyConnection, run_id: str, scores: list[dict]) -> None:
    con.executemany(
        "INSERT INTO screen_scores (run_id, symbol, volume_z_20, rsi_14, ret_20d, "
        "ret_60d, dist_52wk_high_pct, atr_14, avg_dollar_vol_20, composite_score) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                s["symbol"],
                s.get("volume_z_20"),
                s.get("rsi_14"),
                s.get("ret_20d"),
                s.get("ret_60d"),
                s.get("dist_52wk_high_pct"),
                s.get("atr_14"),
                s.get("avg_dollar_vol_20"),
                s.get("composite_score"),
            )
            for s in scores
        ],
    )
