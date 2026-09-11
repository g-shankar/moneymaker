"""Strategy 1: council-gated stock picker (LONG-ONLY v1).

Consumes a "dossier" dict per symbol::

    {
        "symbol": "AAPL",
        "price": 235.4,
        "indicators": {
            "rsi_14": 58.2,
            "ema_20": 231.0, "ema_50": 224.5, "sma_200": 210.0,
            "macd": 1.2, "macd_hist": 0.35,
            "atr_14": 4.8, "bb_pct": 0.72, "volume_zscore": 1.6,
        },
        "quant": {
            "momentum_score": 72.0,      # 0-100, higher = stronger momentum
            "mr_zscore": -0.8,           # mean-reversion z-score of price vs fair value
            "volatility_regime": {"regime": "normal"},  # "low" | "normal" | "high"
            "beta_vs_spy": 1.05,
        },
        "news": [ {"title": ..., "published": "2026-09-08T..."}, ... ],
        "social": {"score": 0.25, "message_volume": 1840},   # score in [-1, 1]
        "kalshi": {"score": 12},                             # 0-100 macro risk
    }

Missing/None indicator fields degrade gracefully (treated as neutral).

The council gates candidates through four components:

* technicals (weight 0.35): starts at 50.
    - +15 if price > ema_20 > ema_50 (trend alignment)
    - +10 if macd_hist > 0
    - RSI: +10 if 50 <= rsi <= 70 (healthy momentum zone),
      -15 if rsi > 78 or rsi < 25 (overbought washout risk / capitulation)
    - +8 if volume_zscore > 1.0 (conviction behind the move)
    - +7 if 0.55 <= bb_pct <= 0.95 (upper-band ride, not pinned to the top)
    - clipped to [0, 100]
* quant (weight 0.25): 0.6 * momentum_score
    + 0.4 * (100 - min(100, abs(mr_zscore) * 25)); subtract 20 if
    volatility_regime == "high"; clipped to [0, 100].
* social_news (weight 0.25): 50 + 40 * social.score, +10 if >= 2 news
    items published in the last 7 days; clipped to [0, 100].
* kalshi (weight 0.15): 100 - kalshi.score (inverts macro risk).
    VETO: if kalshi.score >= 70 the macro risk gate fires
    (veto=True, veto_reason="macro risk gate") and the candidate is dropped.

score = 0.35*technicals + 0.25*quant + 0.25*social_news + 0.15*kalshi.

This module also provides exit rules: fixed initial stops
(2.5x ATR stop, 2R take-profit), ATR trailing stop ratchet, and a
10-trading-day time stop. LONG-ONLY v1: no shorts, no options, no margin.
"""

import datetime as _dt
import logging as _logging

LOG = _logging.getLogger(__name__)

MACRO_RISK_VETO_THRESHOLD = 70.0

_WEIGHTS = {"technicals": 0.35, "quant": 0.25, "social_news": 0.25, "kalshi": 0.15}


def _clip(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _technicals(dossier: dict) -> float:
    ind = dossier.get("indicators") or {}
    score = 50.0

    price = ind.get("price", dossier.get("price"))
    ema_20 = ind.get("ema_20")
    ema_50 = ind.get("ema_50")
    if (
        price is not None
        and ema_20 is not None
        and ema_50 is not None
        and price > ema_20 > ema_50
    ):
        score += 15

    macd_hist = ind.get("macd_hist")
    if macd_hist is not None and macd_hist > 0:
        score += 10

    rsi = ind.get("rsi_14")
    if rsi is not None:
        if 50 <= rsi <= 70:
            score += 10
        elif rsi > 78 or rsi < 25:
            score -= 15

    vol_z = ind.get("volume_zscore")
    if vol_z is not None and vol_z > 1.0:
        score += 8

    bb = ind.get("bb_pct")
    if bb is not None and 0.55 <= bb <= 0.95:
        score += 7

    return _clip(score)


def _quant(dossier: dict) -> float:
    q = dossier.get("quant") or {}
    momentum = q.get("momentum_score", 50.0)
    mr_z = q.get("mr_zscore", 0.0)
    try:
        mr_component = 100 - min(100, abs(mr_z) * 25)
    except TypeError:
        mr_component = 100
    score = 0.6 * float(momentum) + 0.4 * mr_component
    regime = (q.get("volatility_regime") or {}).get("regime", "normal")
    if regime == "high":
        score -= 20
    return _clip(score)


def _news_count_last_7d(dossier: dict, today: _dt.date | None = None) -> int:
    """Count news items published within the last 7 days (inclusive)."""
    today = today or _dt.date.today()
    cutoff = today - _dt.timedelta(days=7)
    count = 0
    for item in dossier.get("news") or []:
        raw = (item or {}).get("published")
        if not raw:
            continue
        try:
            pub = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            continue
        if pub >= cutoff:
            count += 1
    return count


def _social_news(dossier: dict) -> float:
    s = dossier.get("social") or {}
    try:
        social_score = float(s.get("score", 0.0))
    except (TypeError, ValueError):
        social_score = 0.0
    social_score = max(-1.0, min(1.0, social_score))
    score = 50 + 40 * social_score
    if _news_count_last_7d(dossier) >= 2:
        score += 10
    return _clip(score)


def _kalshi(dossier: dict) -> tuple[float, bool, str | None]:
    k = dossier.get("kalshi") or {}
    try:
        macro = float(k.get("score", 0.0))
    except (TypeError, ValueError):
        macro = 0.0
    veto = macro >= MACRO_RISK_VETO_THRESHOLD
    reason = "macro risk gate" if veto else None
    return _clip(100 - macro), veto, reason


def composite_score(dossier: dict) -> dict:
    """Score a candidate dossier.

    Returns {"score": 0-100 float, "breakdown": {technicals, quant,
    social_news, kalshi}, "veto": bool, "veto_reason": str | None}.
    """
    symbol = dossier.get("symbol")
    technicals = _technicals(dossier)
    quant = _quant(dossier)
    social_news = _social_news(dossier)
    kalshi, veto, veto_reason = _kalshi(dossier)

    score = (
        _WEIGHTS["technicals"] * technicals
        + _WEIGHTS["quant"] * quant
        + _WEIGHTS["social_news"] * social_news
        + _WEIGHTS["kalshi"] * kalshi
    )
    score = round(float(score), 2)

    result = {
        "score": score,
        "breakdown": {
            "technicals": round(technicals, 2),
            "quant": round(quant, 2),
            "social_news": round(social_news, 2),
            "kalshi": round(kalshi, 2),
        },
        "veto": veto,
        "veto_reason": veto_reason,
    }
    LOG.debug(
        "scored %s: %.2f veto=%s%s",
        symbol,
        score,
        veto,
        f" ({veto_reason})" if veto_reason else "",
    )
    return result


def passes_gate(scored: dict, min_score: float) -> bool:
    """True if the candidate is not vetoed and score >= min_score."""
    return not scored.get("veto", False) and scored.get("score", 0.0) >= min_score


def rank_candidates(
    dossiers: list[dict], top_n: int, min_score: float
) -> list[dict]:
    """Score each dossier, drop vetoed and below-min_score, sort desc.

    Missing-trade guard: a dossier whose ``catalysts.hot`` is true (hot
    catalyst — news velocity spike or high-impact item) is ALWAYS included
    for a council look, even when its composite score is below min_score.
    A hot catalyst with a flat chart is exactly the trade the scan must not
    miss. Such entries carry ``catalyst_boost: True`` and are appended
    after the scored top_n (never displacing scored entries).

    Returns entries of {"symbol", "score", "breakdown", "dossier"}.
    """
    ranked: list[dict] = []
    boosted: list[dict] = []
    for dossier in dossiers:
        scored = composite_score(dossier)
        if not passes_gate(scored, min_score):
            catalysts = dossier.get("catalysts") or {}
            # Boost applies to below-gate scores only — a macro veto
            # (Kalshi risk gate) is never overridden by a hot catalyst.
            if not scored.get("veto") and catalysts.get("hot"):
                LOG.info(
                    "catalyst boost %s: hot catalyst, score=%.2f below gate",
                    dossier.get("symbol"),
                    scored["score"],
                )
                boosted.append(
                    {
                        "symbol": dossier.get("symbol"),
                        "score": scored["score"],
                        "breakdown": scored["breakdown"],
                        "dossier": dossier,
                        "catalyst_boost": True,
                    }
                )
                continue
            LOG.info(
                "rejected %s: veto=%s score=%.2f",
                dossier.get("symbol"),
                scored["veto"],
                scored["score"],
            )
            continue
        ranked.append(
            {
                "symbol": dossier.get("symbol"),
                "score": scored["score"],
                "breakdown": scored["breakdown"],
                "dossier": dossier,
            }
        )
    ranked.sort(key=lambda r: r["score"], reverse=True)
    out = ranked[: max(0, top_n)]
    seen = {r["symbol"] for r in out}
    out.extend(b for b in boosted if b["symbol"] not in seen)
    return out


def initial_stops(
    entry_price: float, atr: float, atr_mult: float = 2.5, r_multiple: float = 2.0
) -> dict:
    """Fixed initial exits for a long position.

    stop = entry - atr_mult*atr; take_profit = entry + r_multiple*stop_distance;
    risk_per_share = stop_distance.
    """
    risk_per_share = atr_mult * atr
    return {
        "stop": entry_price - risk_per_share,
        "take_profit": entry_price + r_multiple * risk_per_share,
        "risk_per_share": risk_per_share,
    }


def update_trailing_stop(
    current_stop: float, highest_high: float, atr: float, atr_mult: float = 2.5
) -> float:
    """ATR trailing stop ratchet: only ever moves up."""
    return max(current_stop, highest_high - atr_mult * atr)


def time_stop_reached(
    entry_date: _dt.date, today: _dt.date, max_days: int = 10
) -> bool:
    """True if the position has been open >= max_days calendar days."""
    return (today - entry_date).days >= max_days
