"""Catalyst scanning: overnight news sweep, news velocity, earnings calendar,
analyst actions.

The user requirement: "scan news etc too so we know, I want to not miss any
trades." A symbol with a hot catalyst but a flat chart must still get a
dossier and a council look — the missing-trade guard in
``strategies/stock_picker.rank_candidates`` (via ``catalysts.hot``) handles
that; the 8:00 ET scan already builds dossiers for the full universe.

Resilience: per-symbol try/except, thread timeouts on yfinance, skip-and-log.
Never raises on data problems.

Entry point: :func:`scan_catalysts`. Inject ``news_items`` /
``earnings_fetcher`` in tests to avoid network.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

try:
    _ET = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # pragma: no cover
    _ET = None

_EARNINGS_WINDOW_TRADING_DAYS = 5
_VELOCITY_FLAG_MULT = 3.0
_VELOCITY_LOOKBACK_DAYS = 10
_CALENDAR_TIMEOUT_S = 20
_TOP_CATALYSTS = 3

# keyword -> impact tag, checked against headline + summary (lowercased).
# NOTE: no trailing \b — stems like "upgrad" must match "upgrades".
_IMPACT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("earnings", re.compile(r"\b(earnings|eps|revenue|guidance|q[1-4]\s+results|results\s+beat|missed estimates)")),
    ("analyst-upgrade", re.compile(r"\b(upgrad\w*|rais\w+\s+.*price target|initiated\w*\s+(overweight|buy)|outperform)")),
    ("analyst-downgrade", re.compile(r"\b(downgrad\w*|cut\w*\s+.*price target|lower\w+\s+.*target|underweight|sell rating)")),
    ("mna", re.compile(r"\b(merger|acquisition|acquir\w+|takeover|buyout)")),
    ("fda-legal", re.compile(r"\b(fda|approval|lawsuit|settlement|\bdoj\b|\bsec\b|antitrust|injunction)")),
    ("macro", re.compile(r"\b(fed|fomc|powell|cpi|inflation|jobs report|nonfarm|rate\s+(cut|hike))")),
    ("insider", re.compile(r"\b(insider\s+(buy|sell)|13f|\bstake\b)")),
]

_HIGH_IMPACT_TAGS = {"earnings", "analyst-upgrade", "analyst-downgrade", "mna", "fda-legal"}


def classify_impact(headline: str, summary: str = "") -> str:
    """Return the first matching impact tag, else 'general'."""
    text = f"{headline} {summary}".lower()
    for tag, pat in _IMPACT_PATTERNS:
        if pat.search(text):
            return tag
    return "general"


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _prior_close_et() -> datetime:
    """Yesterday 16:00 ET (approx prior regular close), tz-aware."""
    now_et = datetime.now(_ET) if _ET else datetime.now(timezone.utc)
    return now_et.replace(hour=16, minute=0, second=0, microsecond=0) - timedelta(days=1)


def overnight_items(news_items: list[dict]) -> list[dict]:
    """Items published since the prior regular close."""
    cutoff = _prior_close_et()
    out = []
    for n in news_items or []:
        ts = _parse_time(n.get("published_at"))
        if ts is not None and ts >= cutoff:
            out.append(n)
    return out


def news_velocity(news_items: list[dict]) -> dict:
    """Compare last-24h article count vs the 10-day daily average.

    Returns {"last_24h": int, "daily_avg_10d": float|None,
             "multiple": float|None, "flagged": bool}.
    """
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    counts: dict[str, int] = {}
    last_24h = 0
    for n in news_items or []:
        ts = _parse_time(n.get("published_at"))
        if ts is None:
            continue
        if ts >= now - timedelta(days=_VELOCITY_LOOKBACK_DAYS):
            counts[ts.date().isoformat()] = counts.get(ts.date().isoformat(), 0) + 1
        if ts >= day_ago:
            last_24h += 1
    if not counts:
        return {"last_24h": last_24h, "daily_avg_10d": None,
                "multiple": None, "flagged": False}
    avg = sum(counts.values()) / _VELOCITY_LOOKBACK_DAYS
    mult = (last_24h / avg) if avg > 0 else None
    return {
        "last_24h": last_24h,
        "daily_avg_10d": round(avg, 2),
        "multiple": round(mult, 2) if mult is not None else None,
        "flagged": bool(mult is not None and mult >= _VELOCITY_FLAG_MULT),
    }


def _fetch_earnings_yfinance(symbol: str, timeout_s: int = _CALENDAR_TIMEOUT_S):
    """Next earnings date (ISO) via yfinance, or None."""

    def _work():
        import yfinance as yf

        tk = yf.Ticker(symbol)
        # get_earnings_dates(limit=4): DataFrame indexed by date
        try:
            ed = tk.get_earnings_dates(limit=4)
            if ed is not None and not ed.empty:
                future = [d for d in ed.index if d.date() >= datetime.now().date()]
                if future:
                    return min(future).date().isoformat()
        except Exception:
            pass
        # fallback: calendar dict
        try:
            cal = tk.calendar or {}
            for key in ("Earnings Date", "EarningsDate"):
                val = cal.get(key)
                if val:
                    dates = val if isinstance(val, list) else [val]
                    iso = [d.date().isoformat() if hasattr(d, "date") else str(d)
                           for d in dates]
                    if iso:
                        return sorted(iso)[0][:10]
        except Exception:
            pass
        return None

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_work).result(timeout=timeout_s)
    except FuturesTimeout:
        log.warning("earnings calendar timed out for %s", symbol)
    except Exception as exc:  # noqa: BLE001
        log.warning("earnings calendar failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
    return None


def _trading_days_until(date_iso: str | None) -> int | None:
    if not date_iso:
        return None
    try:
        target = datetime.fromisoformat(date_iso[:10]).date()
    except ValueError:
        return None
    today = datetime.now().date()
    # rough trading-day count: weekdays only
    n, d = 0, today
    while d < target and n < 60:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n if target >= today else 0


def scan_catalysts(
    symbol: str,
    news_items: list[dict] | None = None,
    earnings_fetcher=None,
    is_etf: bool = False,
    asof: str | None = None,
) -> dict:
    """Build the catalysts block for *symbol*.

    When *news_items* is None the module fetches via data.news.get_news
    (10-day window, higher limit for velocity math). *is_etf* skips the
    earnings-date check — ETFs carry no single-stock earnings risk; macro
    event risk (Fed/CPI) is still covered by the news/Kalshi layer.
    Never raises.
    """
    from datetime import datetime as _dt

    symbol = symbol.upper()
    asof = asof or _dt.now(timezone.utc).isoformat()
    block: dict = {
        "symbol": symbol,
        "asof": asof,
        "hot": False,
        "items": [],
        "overnight_count": 0,
        "velocity": {"last_24h": 0, "daily_avg_10d": None,
                     "multiple": None, "flagged": False},
        "earnings": {"next_date": None, "trading_days_until": None,
                     "within_window": False},
        "summary": "",
    }
    try:
        if news_items is None:
            from data.news import get_news

            news_items = get_news(symbol, limit=50, since_days=_VELOCITY_LOOKBACK_DAYS)

        overnight = overnight_items(news_items)
        block["overnight_count"] = len(overnight)
        block["velocity"] = news_velocity(news_items)

        # top catalysts: overnight items first, then most recent — tag each
        pool = (overnight or []) + [n for n in (news_items or []) if n not in overnight]
        seen, items = set(), []
        for n in pool:
            head = (n.get("headline") or "").strip()
            if not head or head in seen:
                continue
            seen.add(head)
            items.append(
                {
                    "headline": head,
                    "source": n.get("source", ""),
                    "published_at": n.get("published_at", ""),
                    "url": n.get("url", ""),
                    "impact_tag": classify_impact(head, n.get("summary", "")),
                    "overnight": n in overnight,
                }
            )
            if len(items) >= _TOP_CATALYSTS:
                break
        # high-impact first
        items.sort(
            key=lambda i: (i["impact_tag"] not in _HIGH_IMPACT_TAGS, i["overnight"]),
        )
        block["items"] = items[:_TOP_CATALYSTS]

        # earnings calendar — skipped for ETFs (no single-stock earnings risk)
        if is_etf:
            block["earnings"] = {
                "next_date": None,
                "trading_days_until": None,
                "within_window": False,
                "skipped": "ETF — no single-stock earnings risk",
            }
        else:
            next_earn = (
                earnings_fetcher(symbol) if earnings_fetcher
                else _fetch_earnings_yfinance(symbol)
            )
            if next_earn:
                tdu = _trading_days_until(next_earn)
                block["earnings"] = {
                    "next_date": next_earn,
                    "trading_days_until": tdu,
                    "within_window": bool(
                        tdu is not None and tdu <= _EARNINGS_WINDOW_TRADING_DAYS
                    ),
                }

        hot_reasons = []
        if block["velocity"]["flagged"]:
            hot_reasons.append(
                "news velocity {0}x normal ({1} items/24h)".format(
                    block["velocity"]["multiple"], block["velocity"]["last_24h"]
                )
            )
        for it in block["items"]:
            if it["impact_tag"] in _HIGH_IMPACT_TAGS:
                hot_reasons.append(
                    "{0}: {1}".format(it["impact_tag"], it["headline"][:80])
                )
                break
        block["hot"] = bool(hot_reasons)
        block["summary"] = "; ".join(hot_reasons)
        return block
    except Exception as exc:  # noqa: BLE001 - never break the scan
        log.warning("catalyst scan failed for %s: %s: %s",
                    symbol, type(exc).__name__, exc)
        block["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return block
