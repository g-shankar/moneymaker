"""Unusual activity detection: volume anomalies + unusual options flow.

Informed money often shows up in volume and options *before* it shows up in
price or headlines, so this is the earliest signal in the dossier. Two
halves:

1. **Volume** — daily volume z-score (20d), intraday relative volume (vs the
   same clock-time average — see data.extended.session_relative_volume), and
   pre-market relative volume. Flagged at >2.5 sigma / 2.5x. The accompanying
   price action is read as accumulation (spike + strong close) or
   distribution (spike + weak close).
2. **Options** — yfinance option chains for the nearest 2 expiries. A
   contract is unusual when volume > 2x open interest (with a minimum-volume
   floor to kill noise) or when its volume is extreme for the chain.
   Aggregates to a symbol-level call/put premium tilt; the top 3 unusual
   contracts are surfaced.

yfinance options endpoints are slow and flaky: every network call runs in a
worker thread with a timeout, per-symbol try/except, skip-and-log. A bad
symbol never kills the scan.

Entry point: :func:`scan_unusual_activity`. Inject ``daily_bars``,
``intraday_bars`` and ``chain_fetcher`` in tests to avoid network.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

import pandas as pd

log = logging.getLogger(__name__)

VOLUME_Z_FLAG = 2.5
RELVOL_FLAG = 2.5
MIN_CONTRACT_VOLUME = 100      # noise floor for the vol > 2x OI flag
BIG_CONTRACT_VOLUME = 1000    # absolute "extreme" threshold
CHAIN_TIMEOUT_S = 30
_N_EXPIRIES = 2
_TOP_CONTRACTS = 3


# ---------------------------------------------------------------------------
# volume half
# ---------------------------------------------------------------------------
def _daily_volume_stats(daily_bars: pd.DataFrame) -> dict:
    """daily_z and relvol for the last bar vs the trailing 20 (excl. last)."""
    out = {"daily_z": None, "daily_relvol": None}
    try:
        vols = daily_bars["volume"].astype(float)
        if len(vols) < 21:
            return out
        baseline = vols.iloc[-21:-1]
        mean, std = float(baseline.mean()), float(baseline.std())
        last = float(vols.iloc[-1])
        if mean > 0:
            out["daily_relvol"] = last / mean
        if std > 0:
            out["daily_z"] = (last - mean) / std
    except Exception as exc:  # noqa: BLE001 - defensive stats
        log.debug("daily volume stats failed: %s", exc)
    return out


def _accumulation_read(daily_bars: pd.DataFrame) -> str | None:
    """Read the last daily bar: accumulation / distribution / neutral.

    A volume-spike bar closing in the top 40% of its range with a green
    close reads as accumulation (buying into strength); bottom 40% with a
    red close reads as distribution. Otherwise neutral.
    """
    try:
        bar = daily_bars.iloc[-1]
        high, low = float(bar["high"]), float(bar["low"])
        rng = high - low
        if rng <= 0:
            return None
        pos = (float(bar["close"]) - low) / rng
        green = float(bar["close"]) >= float(bar["open"])
        if pos >= 0.6 and green:
            return "accumulation"
        if pos <= 0.4 and not green:
            return "distribution"
        return "neutral"
    except Exception as exc:  # noqa: BLE001 - defensive stats
        log.debug("accumulation read failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# options half
# ---------------------------------------------------------------------------
def _fetch_chains_yfinance(symbol: str, timeout_s: int = CHAIN_TIMEOUT_S):
    """[(expiry, calls_df, puts_df)] for the nearest 2 expiries, or None."""

    def _work():
        import yfinance as yf

        tk = yf.Ticker(symbol)
        expiries = list(tk.options or [])[:_N_EXPIRIES]
        out = []
        for exp in expiries:
            oc = tk.option_chain(exp)
            out.append((exp, oc.calls, oc.puts))
        return out

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_work).result(timeout=timeout_s)
    except FuturesTimeout:
        log.warning("option chain timed out for %s", symbol)
    except Exception as exc:  # noqa: BLE001 - yfinance is flaky by design
        log.warning("option chain failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
    return None


def _scan_chains(chains) -> dict:
    """Flag unusual contracts and compute the call/put premium tilt."""
    unusual = []
    call_prem = put_prem = 0.0
    for expiry, calls, puts in chains:
        for otype, frame in (("call", calls), ("put", puts)):
            if frame is None or frame.empty:
                continue
            for _, row in frame.iterrows():
                try:
                    vol = float(row.get("volume") or 0)
                    oi = float(row.get("openInterest") or 0)
                    last = float(row.get("lastPrice") or 0)
                except (TypeError, ValueError):
                    continue
                prem = vol * last * 100.0  # premium changing hands, $
                if otype == "call":
                    call_prem += prem
                else:
                    put_prem += prem
                flagged = (
                    vol >= MIN_CONTRACT_VOLUME and oi > 0 and vol > 2.0 * oi
                ) or vol >= BIG_CONTRACT_VOLUME
                if flagged:
                    unusual.append(
                        {
                            "expiry": str(expiry),
                            "type": otype,
                            "strike": float(row.get("strike") or 0),
                            "volume": int(vol),
                            "openInterest": int(oi),
                            "lastPrice": round(last, 2),
                            "impliedVolatility": (
                                round(float(row.get("impliedVolatility") or 0), 4)
                                if row.get("impliedVolatility") else None
                            ),
                            "premium": round(prem, 2),
                        }
                    )
    unusual.sort(key=lambda c: c["volume"], reverse=True)
    total = call_prem + put_prem
    tilt = (call_prem - put_prem) / total if total > 0 else None
    direction = (
        "bullish" if tilt is not None and tilt > 0.2
        else "bearish" if tilt is not None and tilt < -0.2
        else "neutral" if tilt is not None else None
    )
    return {
        "flagged": bool(unusual),
        "tilt": round(tilt, 3) if tilt is not None else None,
        "tilt_direction": direction,
        "call_premium": round(call_prem, 2),
        "put_premium": round(put_prem, 2),
        "unusual_contracts": unusual[:_TOP_CONTRACTS],
        "n_unusual": len(unusual),
    }


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def scan_unusual_activity(
    symbol: str,
    daily_bars: pd.DataFrame | None = None,
    intraday_bars: pd.DataFrame | None = None,
    chain_fetcher=None,
    asof: str | None = None,
) -> dict:
    """Scan *symbol* for unusual volume and unusual options flow.

    Never raises on data problems — degrades to flagged=False with notes.
    """
    from datetime import datetime

    symbol = symbol.upper()
    asof = asof or datetime.now().isoformat()
    block: dict = {
        "symbol": symbol,
        "asof": asof,
        "flagged": False,
        "summary": "",
        "volume": {
            "daily_z": None,
            "daily_relvol": None,
            "intraday_relvol": None,
            "premarket_relvol": None,
            "flagged": False,
            "read": None,
        },
        "options": {
            "flagged": False,
            "tilt": None,
            "tilt_direction": None,
            "unusual_contracts": [],
            "n_unusual": 0,
            "note": "",
        },
    }

    # ---- volume ----
    try:
        if daily_bars is not None and not daily_bars.empty:
            stats = _daily_volume_stats(daily_bars)
            block["volume"]["daily_z"] = stats["daily_z"]
            block["volume"]["daily_relvol"] = stats["daily_relvol"]
            block["volume"]["read"] = _accumulation_read(daily_bars)

        if intraday_bars is not None and not intraday_bars.empty:
            from data.extended import (
                AFTERHOURS_WINDOW,
                PREMARKET_WINDOW,
                session_relative_volume,
            )

            # intraday relvol: use the regular-session window up to "now"
            block["volume"]["intraday_relvol"] = session_relative_volume(
                intraday_bars, (9, 30, 16, 0)
            )
            block["volume"]["premarket_relvol"] = session_relative_volume(
                intraday_bars, PREMARKET_WINDOW
            )
        v = block["volume"]
        v["flagged"] = bool(
            (v["daily_z"] is not None and v["daily_z"] >= VOLUME_Z_FLAG)
            or (v["intraday_relvol"] is not None
                and v["intraday_relvol"] >= RELVOL_FLAG)
            or (v["premarket_relvol"] is not None
                and v["premarket_relvol"] >= RELVOL_FLAG)
        )
    except Exception as exc:  # noqa: BLE001 - never break the scan
        log.warning("unusual volume scan failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)

    # ---- options ----
    try:
        chains = chain_fetcher(symbol) if chain_fetcher else _fetch_chains_yfinance(symbol)
        if chains:
            block["options"] = _scan_chains(chains)
        else:
            block["options"]["note"] = "option chains unavailable"
    except Exception as exc:  # noqa: BLE001 - never break the scan
        log.warning("unusual options scan failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
        block["options"]["note"] = f"error: {type(exc).__name__}"

    # ---- roll up ----
    parts = []
    v, o = block["volume"], block["options"]
    if v["flagged"]:
        bits = []
        if v["daily_z"] is not None and v["daily_z"] >= VOLUME_Z_FLAG:
            bits.append(f"daily volume {v['daily_z']:.1f}σ")
        if v["intraday_relvol"] is not None and v["intraday_relvol"] >= RELVOL_FLAG:
            bits.append(f"intraday {v['intraday_relvol']:.1f}x avg")
        if v["premarket_relvol"] is not None and v["premarket_relvol"] >= RELVOL_FLAG:
            bits.append(f"pre-market {v['premarket_relvol']:.1f}x avg")
        read = f" ({v['read']})" if v["read"] else ""
        parts.append("unusual volume: " + ", ".join(bits) + read)
    if o["flagged"]:
        parts.append(
            "unusual options: {0} contract(s), {1} tilt ({2:+.0%})".format(
                o["n_unusual"],
                o["tilt_direction"],
                o["tilt"] or 0.0,
            )
        )
    block["flagged"] = bool(parts)
    block["summary"] = "; ".join(parts)
    return block
