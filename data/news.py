"""Headline news for a symbol: Alpaca news API with Google News RSS fallback.

Never raises on network failure — logs and returns [] instead.
No secrets stored; Alpaca credentials come from the environment.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

_ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
_GNEWS_RSS_URL = "https://news.google.com/rss/search"


def get_news(symbol: str, limit: int = 20, since_days: int = 7) -> list:
    """Return up to *limit* recent news items as dicts with keys:
    headline, summary, url, published_at, source. Returns [] on any failure.
    """
    items = _alpaca_news(symbol, limit, since_days)
    if items is None:
        items = _gnews_rss(symbol, limit, since_days)
    return items or []


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _alpaca_news(symbol: str, limit: int, since_days: int):
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    if not (key and secret):
        return None
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        resp = requests.get(
            _ALPACA_NEWS_URL,
            params={
                "symbols": symbol.upper(),
                "limit": limit,
                "sort": "desc",
                "start": since,
            },
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("news", [])
        return [
            {
                "headline": n.get("headline", ""),
                "summary": n.get("summary", ""),
                "url": n.get("url", ""),
                "published_at": n.get("created_at", ""),
                "source": n.get("source", "alpaca"),
            }
            for n in data[:limit]
        ]
    except Exception as exc:
        log.warning("Alpaca news failed for %s (%s); trying Google News RSS", symbol, exc)
        return None


def _gnews_rss(symbol: str, limit: int, since_days: int):
    try:
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime

        resp = requests.get(
            _GNEWS_RSS_URL,
            params={"q": f"{symbol} stock"},
            headers={"User-Agent": "Mozilla/5.0 (paper-trading research)"},
            timeout=10,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in list(root.iter("item"))[:limit]:
            pub = (item.findtext("pubDate") or "").strip()
            try:
                pub = parsedate_to_datetime(pub).isoformat()
            except Exception:
                pass
            items.append(
                {
                    "headline": (item.findtext("title") or "").strip(),
                    "summary": (item.findtext("description") or "").strip(),
                    "url": (item.findtext("link") or "").strip(),
                    "published_at": pub,
                    "source": (item.findtext("source") or "google-news").strip(),
                }
            )
        return items
    except Exception as exc:
        log.warning("Google News RSS failed for %s: %s", symbol, exc)
        return []
