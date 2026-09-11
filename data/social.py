"""Social sentiment via the public StockTwits symbol stream.

No API key required. On any failure, logs and returns a neutral dict
(score 0.0). Never raises.
"""

import logging

import requests

log = logging.getLogger(__name__)

_URL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 (paper-trading research)"}


def get_sentiment(symbol: str) -> dict:
    """Return {bullish, bearish, neutral, message_volume, score, source}.

    score in [-1, 1] = (bullish - bearish) / max(1, total). Neutral on
    any failure.
    """
    neutral = {
        "bullish": 0,
        "bearish": 0,
        "neutral": 0,
        "message_volume": 0,
        "score": 0.0,
        "source": "stocktwits",
    }
    try:
        resp = requests.get(_URL.format(sym=symbol.upper()), headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        messages = resp.json().get("messages", []) or []
        bullish = bearish = neutral_n = 0
        for m in messages:
            sent = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            if sent == "Bullish":
                bullish += 1
            elif sent == "Bearish":
                bearish += 1
            else:
                neutral_n += 1
        total = bullish + bearish + neutral_n
        return {
            "bullish": bullish,
            "bearish": bearish,
            "neutral": neutral_n,
            "message_volume": total,
            "score": float((bullish - bearish) / max(1, total)),
            "source": "stocktwits",
        }
    except Exception as exc:
        log.warning("StockTwits sentiment failed for %s: %s", symbol, exc)
        return dict(neutral)
