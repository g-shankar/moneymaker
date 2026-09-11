"""Tests for workstreams 1–5: advisor mode, extended hours, unusual activity,
catalysts, and the ETF sleeve.

All network-touching fetchers are injected/mocked — no live feeds.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# fixtures: synthetic market data
# ---------------------------------------------------------------------------
def _daily_bars(n=40, base_vol=1_000_000, spike_last=False) -> pd.DataFrame:
    idx = pd.date_range("2026-07-01", periods=n, freq="B")
    # small deterministic wiggle so the baseline std is nonzero
    vols = [base_vol + (i % 7) * 1_000 for i in range(n)]
    if spike_last:
        vols[-1] = base_vol * 4
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": [100.0 + i * 0.2 for i in range(n)],
            "volume": vols,
        },
        index=idx,
    )


def _et_datetime(day: str, hh: int, mm: int) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hh:02d}:{mm:02d}", tz="America/New_York")


def _intraday_bars() -> pd.DataFrame:
    """Two days of 15-min bars in ET: day 1 normal, day 2 with a premarket
    volume spike and a +2.5% premarket move."""
    rows = []
    for day, pm_vol, pm_px in (("2026-09-09", 1_000, 100.0),
                               ("2026-09-10", 8_000, 102.5)):
        # premarket 04:00-09:30 (22 bars)
        for i in range(22):
            ts = _et_datetime(day, 4, 0) + pd.Timedelta(minutes=15 * i)
            rows.append((ts, pm_px, pm_px + 0.1, pm_px - 0.1, pm_px, pm_vol))
        # regular 09:30-16:00 (26 bars)
        for i in range(26):
            ts = _et_datetime(day, 9, 30) + pd.Timedelta(minutes=15 * i)
            rows.append((ts, 100.0, 100.5, 99.5, 100.0, 2_000))
    df = pd.DataFrame(
        rows, columns=["ts", "open", "high", "low", "close", "volume"]
    ).set_index("ts")
    return df


class _FakeMarket:
    def __init__(self, bars: pd.DataFrame):
        self._bars = bars

    def get_bars(self, symbol, start, end):
        return self._bars


def _news_item(headline, hours_ago=2, source="reuters"):
    return {
        "headline": headline,
        "summary": "",
        "url": "http://x",
        "published_at": (
            datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        ).isoformat(),
        "source": source,
    }


# ---------------------------------------------------------------------------
# Workstream 2 — extended hours
# ---------------------------------------------------------------------------
class TestExtendedHours:
    def test_premarket_move_and_volume(self):
        from data.extended import get_extended_hours

        blk = get_extended_hours(
            "TST", regular_close=100.0, regular_close_date="2026-09-09",
            intraday_bars=_intraday_bars(), live_print={"price": None},
        )
        assert blk["available"] is True
        pm = blk["premarket"]
        assert pm["change_pct"] == pytest.approx(2.5)
        assert pm["rel_volume"] == pytest.approx(8.0)
        assert pm["flagged"] is True  # |2.5%| >= 2% move flag
        assert blk["premarket_change_pct"] == pytest.approx(2.5)
        assert blk["overnight_gap_risk"] == "moderate"  # >= 2%, < 3%
        assert blk["reference_price"] == pytest.approx(102.5)
        assert blk["reference_source"] == "premarket"
        assert any("premarket" in f for f in blk["flags"])

    def test_live_print_wins_over_premarket(self):
        from data.extended import get_extended_hours

        blk = get_extended_hours(
            "TST", regular_close=100.0, regular_close_date="2026-09-09",
            intraday_bars=_intraday_bars(),
            live_print={"price": 103.0, "source": "test"},
        )
        assert blk["reference_price"] == pytest.approx(103.0)
        assert blk["reference_source"] == "test"

    def test_gap_risk_high_flag(self):
        from data.extended import get_extended_hours

        bars = _intraday_bars().copy()
        # push the latest premarket session up 4%
        latest_day = bars.index.date.max()
        day2 = bars.index.date == latest_day
        pm_mask = (bars.index.time >= pd.Timestamp("04:00").time()) & (
            bars.index.time < pd.Timestamp("09:30").time())
        bars.loc[day2 & pm_mask, "close"] = 104.0
        blk = get_extended_hours(
            "TST", regular_close=100.0, regular_close_date="2026-09-09",
            intraday_bars=bars, live_print={"price": None},
        )
        assert blk["premarket"]["change_pct"] == pytest.approx(4.0)
        assert blk["overnight_gap_risk"] == "high"

    def test_degraded_on_empty_bars(self):
        from data.extended import get_extended_hours

        blk = get_extended_hours("TST", regular_close=100.0,
                                 regular_close_date="2026-09-09",
                                 intraday_bars=pd.DataFrame(),
                                 live_print={})
        assert blk["available"] is False


# ---------------------------------------------------------------------------
# Workstream 3 — unusual activity
# ---------------------------------------------------------------------------
class TestUnusualActivity:
    def test_daily_volume_zscore_flags(self):
        from data.unusual import scan_unusual_activity

        blk = scan_unusual_activity(
            "TST", daily_bars=_daily_bars(n=40, spike_last=True),
            chain_fetcher=lambda s: None,
        )
        assert blk["volume"]["daily_z"] is not None
        assert blk["volume"]["daily_z"] > 2.5
        assert blk["volume"]["flagged"] is True
        assert blk["flagged"] is True
        assert "unusual volume" in blk["summary"]

    def test_quiet_volume_not_flagged(self):
        from data.unusual import scan_unusual_activity

        blk = scan_unusual_activity(
            "TST", daily_bars=_daily_bars(n=40),
            chain_fetcher=lambda s: None,
        )
        assert blk["volume"]["flagged"] is False

    def test_accumulation_read(self):
        from data.unusual import _accumulation_read

        df = _daily_bars(n=5).copy()
        df.iloc[-1, df.columns.get_loc("close")] = 101.8  # top of range, green
        assert _accumulation_read(df) == "accumulation"
        df.iloc[-1, df.columns.get_loc("close")] = 99.2  # bottom, red
        df.iloc[-1, df.columns.get_loc("open")] = 100.0
        assert _accumulation_read(df) == "distribution"

    def test_unusual_options_contract_and_tilt(self):
        from data.unusual import scan_unusual_activity

        calls = pd.DataFrame(
            [{
                "strike": 105.0, "volume": 5000, "openInterest": 100,
                "lastPrice": 2.50, "impliedVolatility": 0.4,
            }]
        )
        puts = pd.DataFrame(
            [{
                "strike": 95.0, "volume": 50, "openInterest": 2000,
                "lastPrice": 1.00, "impliedVolatility": 0.35,
            }]
        )

        def fetcher(sym):
            assert sym == "TST"
            return [("2026-09-18", calls, puts)]

        blk = scan_unusual_activity("TST", chain_fetcher=fetcher)
        o = blk["options"]
        assert o["flagged"] is True
        assert o["tilt_direction"] == "bullish"
        assert o["unusual_contracts"][0]["strike"] == 105.0
        assert o["unusual_contracts"][0]["type"] == "call"
        assert blk["flagged"] is True

    def test_options_failure_degrades(self):
        from data.unusual import scan_unusual_activity

        def boom(sym):
            raise RuntimeError("yfinance down")

        blk = scan_unusual_activity("TST", chain_fetcher=boom)
        assert blk["options"]["flagged"] is False


# ---------------------------------------------------------------------------
# Workstream 4 — catalysts
# ---------------------------------------------------------------------------
class TestCatalysts:
    def test_classify_impact(self):
        from data.catalysts import classify_impact

        assert classify_impact("Morgan Stanley upgrades AAPL to Overweight") == "analyst-upgrade"
        assert classify_impact("Goldman downgrades TSLA, cuts price target") == "analyst-downgrade"
        assert classify_impact("Q3 earnings beat, raises guidance") == "earnings"
        assert classify_impact("FDA approves new drug") == "fda-legal"
        assert classify_impact("Fed holds rates steady") == "macro"
        assert classify_impact("Some random headline here") == "general"

    def test_news_velocity_flagged(self):
        from data.catalysts import news_velocity

        items = [_news_item("story %d" % i, hours_ago=i) for i in range(12)]
        # 12 items in last 24h vs 10-day avg of 1.2 → 10x
        items += [_news_item("old %d" % i, hours_ago=24 * (i + 2)) for i in range(3)]
        v = news_velocity(items)
        assert v["flagged"] is True
        assert v["multiple"] > 3.0

    def test_news_velocity_quiet(self):
        from data.catalysts import news_velocity

        items = [_news_item("old", hours_ago=24 * i) for i in range(1, 6)]
        assert news_velocity(items)["flagged"] is False

    def test_scan_catalysts_hot_and_earnings(self):
        from data.catalysts import scan_catalysts

        earn_date = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        items = [
            _news_item("Company beats Q3 earnings, raises guidance"),
            _news_item("Analyst upgrades to Buy"),
        ]
        blk = scan_catalysts(
            "TST", news_items=items,
            earnings_fetcher=lambda s: earn_date,
        )
        assert blk["hot"] is True
        assert blk["earnings"]["within_window"] is True
        assert blk["items"][0]["impact_tag"] in ("earnings", "analyst-upgrade")

    def test_etf_skips_earnings(self):
        from data.catalysts import scan_catalysts

        called = []

        def fetcher(s):
            called.append(s)
            return "2026-09-20"

        blk = scan_catalysts(
            "XLK", news_items=[_news_item("Sector rotation into tech")],
            earnings_fetcher=fetcher, is_etf=True,
        )
        assert called == []  # earnings fetcher never invoked
        assert blk["earnings"].get("skipped", "").startswith("ETF")
        assert blk["earnings"]["within_window"] is False


# ---------------------------------------------------------------------------
# Workstream 5 — ETF sleeve + dossier integration
# ---------------------------------------------------------------------------
class TestDossierIntegration:
    def _dossier(self, monkeypatch, symbol, etf_symbols):
        import data.dossier as dossier_mod

        monkeypatch.setattr(dossier_mod, "_extended_block",
                            lambda *a, **k: {"available": False})
        monkeypatch.setattr(dossier_mod, "_unusual_block",
                            lambda *a, **k: {"flagged": False})
        monkeypatch.setattr(dossier_mod, "get_news", lambda s: [])
        monkeypatch.setattr(dossier_mod, "get_sentiment",
                            lambda s: {"score": 50})
        monkeypatch.setattr(dossier_mod, "get_macro_risk",
                            lambda: {"score": 60})

        import data.catalysts as cat_mod
        seen = {}

        def fake_scan(sym, **kwargs):
            seen["symbol"] = sym
            seen.update(kwargs)
            return {"hot": False, "earnings": {}}

        monkeypatch.setattr(cat_mod, "scan_catalysts", fake_scan)

        from data.dossier import build_dossier

        d = build_dossier(
            symbol, _FakeMarket(_daily_bars(n=300)),
            include_extended=False, include_unusual=False,
            include_catalysts=True, etf_symbols=etf_symbols,
        )
        return d, seen

    def test_etf_dossier_blocks_and_earnings_skip(self, monkeypatch):
        d, seen = self._dossier(monkeypatch, "XLK", {"XLK"})
        assert d["is_etf"] is True
        assert "extended_hours" in d and "unusual_activity" in d
        assert "catalysts" in d
        assert seen["symbol"] == "XLK"
        assert seen.get("is_etf") is True  # earnings check skipped via is_etf

    def test_stock_dossier_runs_earnings_check(self, monkeypatch):
        d, seen = self._dossier(monkeypatch, "AAPL", {"XLK"})
        assert d["is_etf"] is False
        assert seen.get("is_etf") is False  # earnings check runs for stocks


class TestSocialNeutral:
    def test_missing_social_is_neutral_not_negative(self):
        from strategies.stock_picker import _social_news

        assert _social_news({"social": {}}) == 50
        assert _social_news({}) == 50
        assert _social_news({"social": {"score": 0.0}}) == 50


class TestMissingTradeGuard:
    def _dossier(self, symbol, score_bits, hot=False, veto=False):
        d = {
            "symbol": symbol,
            "price": 100.0,
            "indicators": {"rsi_14": 55, "atr_14": 2.0},
            "quant": {"momentum_score": 50, "mr_zscore": 0.5,
                      "volatility_regime": {"regime": "normal"},
                      "beta_vs_spy": 1.0},
            "social": {"score": 0.0},
            "news": [],
            "kalshi": {"score": 85 if veto else 40},
            "catalysts": {"hot": hot},
        }
        d.update(score_bits)
        return d

    def test_hot_catalyst_boosts_below_gate(self):
        from strategies.stock_picker import rank_candidates

        strong = self._dossier("AAA", {})
        weak_hot = self._dossier("BBB", {}, hot=True)
        out = rank_candidates([strong, weak_hot], top_n=5, min_score=999)
        symbols = [r["symbol"] for r in out]
        assert "BBB" in symbols
        assert out[symbols.index("BBB")].get("catalyst_boost") is True

    def test_veto_never_boosted(self):
        from strategies.stock_picker import rank_candidates

        vetoed_hot = self._dossier("CCC", {}, hot=True, veto=True)
        out = rank_candidates([vetoed_hot], top_n=5, min_score=0)
        assert out == []


# ---------------------------------------------------------------------------
# Workstream 1 — advisor cards
# ---------------------------------------------------------------------------
_ADVISOR_CFG = {
    "advisor": {
        "assumed_equity": 100000,
        "max_cards_per_day": 5,
        "min_conviction": 60,
        "session_validity": "next session only",
        "entry_zone_pct": 0.3,
    },
    "strategy1": {"time_stop_days": 10, "atr_trailing_mult": 2.5,
                  "take_profit_r": 2.0},
    "risk": {
        "max_risk_per_trade_pct": 1.0,
        "max_positions": 8,
        "daily_loss_halt_pct": 3.0,
        "kill_switch_file": "/nonexistent/KILL",
        "max_day_trades": 3,
    },
}


def _decision(**kw):
    d = {
        "symbol": "TST",
        "decision": "BUY",
        "conviction": 72,
        "position_size_pct": 0.5,
        "stop_loss": 97.0,
        "trailing_atr_mult": 2.0,
        "take_profit_r": 2.0,
        "reasoning": "Trend is intact. Volume confirms. Risk is contained.",
        "disagreement_notes": "Devil's advocate notes extension risk.",
    }
    d.update(kw)
    return d


def _dossier_for(symbol="TST", price=100.0, atr=2.0, unusual=None,
                 catalysts=None, ref_price=101.0, ref_src="premarket"):
    return {
        "symbol": symbol,
        "price": price,
        "indicators": {"atr_14": atr},
        "extended_hours": {"available": True, "reference_price": ref_price,
                           "reference_source": ref_src},
        "unusual_activity": unusual or {"flagged": False},
        "catalysts": catalysts or {"hot": False, "items": []},
    }


class TestAdvisorCards:
    def test_card_fields_and_sizing_math(self):
        from pipeline.advisor import build_advisor_cards

        cards = build_advisor_cards(
            [_decision()], {"TST": _dossier_for()}, _ADVISOR_CFG, "2026-09-11"
        )
        assert len(cards) == 1
        c = cards[0]
        for field in ("symbol", "side", "order_type", "entry_limit",
                      "entry_zone_low", "entry_zone_high", "stop_loss",
                      "take_profit", "quantity", "conviction",
                      "reasoning_summary", "invalidation", "time_horizon",
                      "validity"):
            assert field in c, field
        # extended-hours reference price used, not the stale close
        assert c["entry_limit"] == pytest.approx(101.0)
        assert c["entry_price_source"] == "premarket"
        # long stop below entry; sizing math consistent
        assert c["stop_loss"] < c["entry_limit"]
        assert c["risk_dollars"] == pytest.approx(
            c["quantity"] * (c["entry_limit"] - c["stop_loss"]), abs=0.01
        )
        assert c["take_profit"] == pytest.approx(
            c["entry_limit"] + 2.0 * (c["entry_limit"] - c["stop_loss"])
        )
        assert c["quantity"] > 0
        assert c["conviction"] == 72

    def test_bad_chair_stop_falls_back_to_atr(self):
        from pipeline.advisor import build_advisor_cards

        cards = build_advisor_cards(
            [_decision(stop_loss=150.0)],  # above entry — unusable for a long
            {"TST": _dossier_for(ref_price=100.0)},
            _ADVISOR_CFG, "2026-09-11",
        )
        assert cards[0]["stop_loss"] == pytest.approx(100.0 - 2.0 * 2.0)

    def test_unusual_and_catalyst_lines(self):
        from pipeline.advisor import build_advisor_cards, render_advisor_markdown

        unusual = {"flagged": True, "summary": "unusual volume: daily 4.2σ"}
        catalysts = {"hot": True, "items": [{
            "headline": "Beats Q3 earnings", "source": "reuters",
            "impact_tag": "earnings"}]}
        cards = build_advisor_cards(
            [_decision()],
            {"TST": _dossier_for(unusual=unusual, catalysts=catalysts)},
            _ADVISOR_CFG, "2026-09-11",
        )
        c = cards[0]
        assert c["unusual_activity"] and "4.2" in c["unusual_activity"]
        assert c["catalyst"] and "earnings" in c["catalyst"]
        md = render_advisor_markdown(cards, "2026-09-11")
        assert "Unusual activity" in md and "Catalyst" in md

    def test_conviction_filter_and_cap(self):
        from pipeline.advisor import build_advisor_cards

        decisions = [_decision(symbol=f"S{i}", conviction=c)
                     for i, c in enumerate([90, 80, 70, 65, 61, 60, 59])]
        dossiers = {f"S{i}": _dossier_for(symbol=f"S{i}") for i in range(7)}
        cards = build_advisor_cards(decisions, dossiers, _ADVISOR_CFG,
                                    "2026-09-11")
        assert len(cards) == 5  # capped at max_cards_per_day
        assert all(c["conviction"] >= 60 for c in cards)
        assert "S6" not in [c["symbol"] for c in cards]  # 59 filtered

    def test_pass_decisions_ignored(self):
        from pipeline.advisor import build_advisor_cards

        cards = build_advisor_cards(
            [_decision(decision="PASS")], {"TST": _dossier_for()},
            _ADVISOR_CFG, "2026-09-11",
        )
        assert cards == []

    def test_advisor_never_touches_broker(self):
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "pipeline", "advisor.py").read_text()
        assert "from broker" not in src
        assert "import broker" not in src

    def test_cmd_advisor_writes_reports(self, tmp_path):
        import json

        from pipeline.advisor import cmd_advisor

        decisions = {"date": "2026-09-11", "decisions": [_decision()]}
        dec_path = tmp_path / "decisions.json"
        dec_path.write_text(json.dumps(decisions))
        dossiers = {"date": "2026-09-11", "ranked": [
            {"symbol": "TST", "score": 80, "dossier": _dossier_for()}]}
        dos_path = tmp_path / "dossiers.json"
        dos_path.write_text(json.dumps(dossiers))

        cfg = dict(_ADVISOR_CFG)
        cfg["paths"] = {"reports_dir": str(tmp_path / "reports")}
        md_path, json_path = cmd_advisor(cfg, str(dec_path), str(dos_path),
                                         "2026-09-11")
        assert md_path.exists() and json_path.exists()
        payload = json.loads(json_path.read_text())
        assert payload["executed"] is False
        assert payload["cards"][0]["symbol"] == "TST"

    def test_kill_switch_blocks_cards(self, tmp_path):
        import json

        from pipeline.advisor import cmd_advisor

        kill = tmp_path / "KILL_SWITCH"
        kill.write_text("halt")
        decisions = {"date": "2026-09-11", "decisions": [_decision()]}
        dec_path = tmp_path / "decisions.json"
        dec_path.write_text(json.dumps(decisions))

        cfg = dict(_ADVISOR_CFG)
        cfg["risk"] = dict(_ADVISOR_CFG["risk"])
        cfg["risk"]["kill_switch_file"] = str(kill)
        cfg["paths"] = {"reports_dir": str(tmp_path / "reports")}
        md_path, json_path = cmd_advisor(cfg, str(dec_path), None, "2026-09-11")
        payload = json.loads(json_path.read_text())
        assert payload["cards"] == []
        assert "kill switch" in payload["note"].lower()
