"""Stage-1 whole-market quant sweep.

Pipeline:
  1. Read the deduped US-listed universe from /tmp/universe_symbols.txt.
  2. Batch-download 1 year of daily bars via yfinance (batch=200, polite
     sleep between batches), write raw bars to DuckDB (screener/store.py).
  3. Liquidity filter: 20-day average dollar volume >= $5M AND last price >= $2.
  4. Per-symbol factors: volume_z_20, rsi_14, ret_20d, ret_60d,
     dist_52wk_high_pct, atr_14.
  5. Composite score -> top 40 -> reports/screen_2026-09-11-market.json.

COMPOSITE FORMULA (higher = stronger breakout/momentum candidate):

    composite = 3.0*Vz + 2.0*M20 + 1.5*M60 + 1.5*H + 1.0*R

    Vz = clamp(volume_z_20 / 5, -1, 1)        # unusual volume (5 sigma = max)
    M20 = clamp(ret_20d / 0.20, -1, 1)        # 1-month momentum (20% = max)
    M60 = clamp(ret_60d / 0.40, -1, 1)        # 3-month momentum (40% = max)
    H  = 1 - clamp(dist_52wk_high_pct / 30, 0, 1)
         # proximity to 52-week high: at the high -> 1, 30%+ below -> 0
    R  = 1 - min(abs(rsi_14 - 60) / 30, 1)    # RSI sweet spot centered on 60

Rationale: unusual volume plus positive momentum near the highs is the
classic swing-breakout signature; the RSI term keeps it out of parabolic
(>90) and dead-money (<30) names. Weights sum to 9.0; theoretical range
[-9, 9].

Run: cd ~/workspace/trading && python -m screener.screen
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from data.indicators import atr, rsi, volume_zscore
from screener.store import get_conn, init_db, record_run, record_scores, upsert_bars

log = logging.getLogger("screener")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

UNIVERSE_FILE = Path(os.environ.get("SCREEN_UNIVERSE", Path(__file__).resolve().parent.parent / "data" / "universe_symbols.txt"))
BATCH_SIZE = 200
BATCH_SLEEP_S = 2.0
RETRY_SLEEP_S = 15.0
MIN_DOLLAR_VOL_20 = 5_000_000
MIN_PRICE = 2.0
MIN_BARS = 65  # need 61+ for ret_60d
TOP_N = 40
REPORT_PATH = Path.home() / "workspace" / "trading" / "reports" / "screen_2026-09-11-market.json"


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def composite(volume_z_20, rsi_14, ret_20d, ret_60d, dist_52wk_high_pct) -> float:
    vz = _clamp(volume_z_20 / 5.0, -1.0, 1.0)
    m20 = _clamp(ret_20d / 0.20, -1.0, 1.0)
    m60 = _clamp(ret_60d / 0.40, -1.0, 1.0)
    h = 1.0 - _clamp(dist_52wk_high_pct / 30.0, 0.0, 1.0)
    r = 1.0 - min(abs(rsi_14 - 60.0) / 30.0, 1.0)
    return 3.0 * vz + 2.0 * m20 + 1.5 * m60 + 1.5 * h + 1.0 * r


def _norm_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep].dropna(subset=["close"])


def download_batch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Return {symbol: normalized OHLCV frame}. Raises on total failure."""
    raw = yf.download(
        tickers=" ".join(symbols),
        period="1y",
        interval="1d",
        group_by="ticker",
        threads=True,
        progress=False,
        auto_adjust=False,
    )
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    if isinstance(raw.columns, pd.MultiIndex):
        for sym in symbols:
            try:
                sub = raw[sym]
            except KeyError:
                continue
            sub = sub.dropna(how="all")
            if not sub.empty:
                out[sym] = _norm_frame(sub)
    else:  # single ticker came back flat
        if len(symbols) == 1:
            out[symbols[0]] = _norm_frame(raw)
    return out


def score_frame(symbol: str, df: pd.DataFrame) -> dict | None:
    """Factor + composite for one symbol, or None if it fails the gate."""
    if len(df) < MIN_BARS:
        return None
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float).fillna(0.0)

    last_price = float(close.iloc[-1])
    if not np.isfinite(last_price) or last_price < MIN_PRICE:
        return None
    dollar_vol_20 = float((close.tail(20) * vol.tail(20)).mean())
    if not np.isfinite(dollar_vol_20) or dollar_vol_20 < MIN_DOLLAR_VOL_20:
        return None

    vz = volume_zscore(vol, 20)
    volume_z_20 = float(vz.iloc[-1]) if len(vz) else 0.0
    if not np.isfinite(volume_z_20):
        volume_z_20 = 0.0
    rsi_s = rsi(close, 14)
    rsi_14 = float(rsi_s.iloc[-1]) if len(rsi_s) else 50.0
    if not np.isfinite(rsi_14):
        rsi_14 = 50.0
    atr_s = atr(df.rename(columns=str.lower)[["open", "high", "low", "close"]].astype(float), 14)
    atr_14 = float(atr_s.iloc[-1]) if len(atr_s) else 0.0

    ret_20d = float(close.iloc[-1] / close.iloc[-21] - 1.0)
    ret_60d = float(close.iloc[-1] / close.iloc[-61] - 1.0)
    high_52w = float(high.tail(252).max())
    dist_52wk_high_pct = float((high_52w - last_price) / high_52w * 100.0) if high_52w > 0 else 100.0

    vals = [volume_z_20, rsi_14, ret_20d, ret_60d, dist_52wk_high_pct]
    if any(not np.isfinite(v) for v in vals):
        return None

    return {
        "symbol": symbol,
        "last_price": round(last_price, 2),
        "volume_z_20": round(volume_z_20, 2),
        "rsi_14": round(rsi_14, 1),
        "ret_20d": round(ret_20d * 100, 2),
        "ret_60d": round(ret_60d * 100, 2),
        "dist_52wk_high_pct": round(dist_52wk_high_pct, 1),
        "atr_14": round(atr_14, 2) if np.isfinite(atr_14) else None,
        "avg_dollar_vol_20": round(dollar_vol_20),
        "composite_score": round(composite(volume_z_20, rsi_14, ret_20d, ret_60d, dist_52wk_high_pct), 3),
    }


def main() -> None:
    run_id = uuid.uuid4().hex[:12]
    run_date = date.today().isoformat()
    symbols = [s.strip() for s in UNIVERSE_FILE.read_text().splitlines() if s.strip()]
    log.info("universe: %d symbols, run_id=%s", len(symbols), run_id)

    con = get_conn()
    init_db(con)
    # Resume: skip symbols already stored (reboots kill the sweep; upsert makes
    # re-runs safe, but skipping avoids re-downloading finished symbols).
    try:
        done = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily_bars").fetchall()}
    except Exception:
        done = set()
    skipped = [s for s in symbols if s in done]
    symbols = [s for s in symbols if s not in done]
    log.info("resume: %d symbols already in DB, %d remaining", len(skipped), len(symbols))
    record_run(con, run_id, run_date, {
        "batch_size": BATCH_SIZE,
        "min_dollar_vol_20": MIN_DOLLAR_VOL_20,
        "min_price": MIN_PRICE,
        "top_n": TOP_N,
        "formula": "3.0*clamp(vz/5)+2.0*clamp(ret20/0.20)+1.5*clamp(ret60/0.40)+1.5*(1-clamp(dist52hi/30))+1.0*(1-min(|rsi-60|/30,1))",
    })

    failures: list[str] = []
    scored: list[dict] = []
    downloaded_ok = 0

    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    for bi, batch in enumerate(batches):
        frames: dict[str, pd.DataFrame] = {}
        try:
            frames = download_batch(batch)
        except Exception as exc:  # noqa: BLE001 - one retry, then mark failed
            log.warning("batch %d/%d failed (%s); retrying once", bi + 1, len(batches), exc)
            time.sleep(RETRY_SLEEP_S)
            try:
                frames = download_batch(batch)
            except Exception as exc2:  # noqa: BLE001
                log.error("batch %d/%d failed twice (%s)", bi + 1, len(batches), exc2)
        for sym in batch:
            df = frames.get(sym)
            if df is None or df.empty:
                failures.append(sym)
                continue
            downloaded_ok += 1
            rows = [
                (idx.date().isoformat(), float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"]), float(r["volume"] or 0.0))
                for idx, r in df.iterrows()
            ]
            upsert_bars(con, sym, rows)
            s = score_frame(sym, df)
            if s:
                scored.append(s)
        log.info("batch %d/%d: ok=%d scored=%d", bi + 1, len(batches), downloaded_ok, len(scored))
        time.sleep(BATCH_SLEEP_S)

    record_scores(con, run_id, scored)
    con.close()

    scored.sort(key=lambda s: s["composite_score"], reverse=True)
    top = scored[:TOP_N]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps({
        "run_id": run_id,
        "run_date": run_date,
        "universe_size": len(symbols),
        "downloaded_ok": downloaded_ok,
        "download_failures": len(failures),
        "download_failure_symbols": failures,
        "liquidity_survivors": len(scored),
        "formula": "composite = 3.0*clamp(volume_z_20/5,-1,1) + 2.0*clamp(ret_20d/0.20,-1,1) + "
                   "1.5*clamp(ret_60d/0.40,-1,1) + 1.5*(1-clamp(dist_52wk_high_pct/30,0,1)) + "
                   "1.0*(1-min(|rsi_14-60|/30,1))",
        "top40": top,
    }, indent=1))
    log.info("done: %d/%d downloaded, %d survived liquidity, top40 -> %s",
             downloaded_ok, len(symbols), len(scored), REPORT_PATH)
    for s in top:
        log.info("  %-8s score=%6.3f vz=%5.2f r20=%6.2f%% rsi=%4.1f dist52hi=%5.1f%% $vol20=%s",
                 s["symbol"], s["composite_score"], s["volume_z_20"], s["ret_20d"],
                 s["rsi_14"], s["dist_52wk_high_pct"], f"{s['avg_dollar_vol_20']:,}")


if __name__ == "__main__":
    main()
