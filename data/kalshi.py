"""Macro risk gauge from Kalshi prediction markets (public API, no key).

HEURISTIC (documented honestly): for open events about macro topics
(FED, CPI, FOMC, ELECTION, GDP, NFP, PRES) we take the first nested
market's yes_bid/yes_ask midpoint as an implied probability p. A market
at 50% (maximum binary uncertainty) is riskier than one priced at 5% or
95%, so each event contributes 4*p*(1-p) — the normalized variance of a
Bernoulli(p), which is 1 at p=0.5 and 0 at the extremes. The score is
100 * the average uncertainty across matched events, plus a hawkish tilt:
events whose title mentions a "hike" (a Fed tightening outcome priced
with non-trivial probability) add up to +10, since rate-hike risk is the
most directly equity-negative macro outcome in this heuristic. The score
is clipped to [0, 100].

This is a rough, model-free gauge — it is not calibrated to realized
volatility and should be treated as a qualitative input to the trading
council, not a risk number.

On any failure: returns {score: 50.0, events: [], reasoning: "Kalshi
unreachable — neutral assumption", asof: <ISO>}.
"""

import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_URL = "https://api.elections.kalshi.com/trade-api/v2/events"
_KEYWORDS = ("FED", "CPI", "FOMC", "ELECTION", "GDP", "NFP", "PRES")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _probability(market: dict) -> float:
    # Kalshi v2 uses dollar-denominated book fields (0-1 scale, often
    # strings like "0.1000"); older responses used cent-denominated
    # yes_bid/yes_ask (0-100).
    for bid_key, ask_key, scale in (
        ("yes_bid_dollars", "yes_ask_dollars", 1.0),
        ("yes_bid", "yes_ask", 100.0),
    ):
        vals = [_num(market.get(bid_key)), _num(market.get(ask_key))]
        vals = [v for v in vals if v is not None]
        if vals:
            return float(max(0.0, min(1.0, sum(vals) / len(vals) / scale)))
    last = _num(market.get("last_price_dollars", market.get("last_price")))
    if last is not None:
        scale = 1.0 if "last_price_dollars" in market else 100.0
        return float(max(0.0, min(1.0, last / scale)))
    return 0.5


def get_macro_risk() -> dict:
    """Return {score, events, reasoning, asof} — score in [0, 100]."""
    asof = datetime.now(timezone.utc).isoformat()
    fallback = {
        "score": 50.0,
        "events": [],
        "reasoning": "Kalshi unreachable — neutral assumption",
        "asof": asof,
    }
    try:
        resp = requests.get(
            _URL,
            params={
                "with_nested_markets": "true",
                "status": "open",
                "min_close_ts": int(time.time()),
            },
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json().get("events", []) or []
    except Exception as exc:
        log.warning("Kalshi events fetch failed: %s", exc)
        return dict(fallback)

    matched = []
    for ev in events:
        ticker = str(ev.get("event_ticker", "")).upper()
        if not any(k in ticker for k in _KEYWORDS):
            continue
        markets = ev.get("markets") or []
        if not markets:
            continue
        p = _probability(markets[0])
        matched.append(
            {
                "ticker": ev.get("event_ticker", ""),
                "title": ev.get("title", ""),
                "probability": round(p, 4),
            }
        )

    if not matched:
        return {
            "score": 50.0,
            "events": [],
            "reasoning": "No macro events matched — neutral assumption",
            "asof": asof,
        }

    uncertainty = sum(4 * e["probability"] * (1 - e["probability"]) for e in matched) / len(
        matched
    )
    hawkish = sum(
        1
        for e in matched
        if "HIKE" in e["title"].upper() and e["probability"] >= 0.25
    )
    score = float(min(100.0, max(0.0, 100 * uncertainty + min(hawkish, 1) * 10)))
    reasoning = (
        f"Kalshi macro gauge: {len(matched)} open macro event(s); "
        f"avg binary uncertainty {uncertainty:.2f}"
        + (f"; {hawkish} Fed-hike-type market(s) priced >=25% (+10 tilt)" if hawkish else "")
    )
    return {"score": score, "events": matched, "reasoning": reasoning, "asof": asof}
