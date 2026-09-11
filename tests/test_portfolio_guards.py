"""Tests for risk/portfolio_guards.py: correlation cap, portfolio heat,
regime-adaptive sizing."""

import pytest

from risk import portfolio_guards as pg


def _lcg_returns(seed: int, n: int = 70) -> list[float]:
    """Deterministic pseudo-random return series (independent per seed)."""
    x = seed % 100000
    out = []
    for _ in range(n):
        x = (1103515245 * x + 12345) % 2**31
        out.append(((x % 2000) - 1000) / 1000 * 0.02)
    return out


def test_correlation_rejects_lockstep_pair():
    # identical series -> correlation 1.0 > 0.7 cap
    series = _lcg_returns(42)
    fn = lambda s: series  # noqa: E731
    rejected, corr, offender = pg.correlation_check(
        "AAA", ["BBB"], fn, threshold=0.7)
    assert rejected is True
    assert corr == pytest.approx(1.0)
    assert offender == "BBB"


def test_correlation_accepts_uncorrelated():
    fn = lambda s: _lcg_returns(abs(hash(s)) % 100000 + 7)  # noqa: E731
    rejected, corr, offender = pg.correlation_check(
        "AAA", ["BBB", "CCC"], fn, threshold=0.7)
    assert rejected is False
    assert corr is not None and abs(corr) < 0.7


def test_correlation_abstains_on_missing_data():
    def fn(s):
        raise ValueError("no data")
    rejected, corr, offender = pg.correlation_check("AAA", ["BBB"], fn)
    assert rejected is False and corr is None and offender is None


def test_correlation_ignores_self():
    series = _lcg_returns(42)
    fn = lambda s: series  # noqa: E731
    rejected, _, _ = pg.correlation_check("AAA", ["AAA"], fn, threshold=0.7)
    assert rejected is False


def test_portfolio_heat_math():
    assert pg.portfolio_heat([500, 1000], 100000) == pytest.approx(1.5)
    assert pg.portfolio_heat([], 100000) == 0.0
    assert pg.portfolio_heat([500], 0) == 0.0


def test_heat_scaling_full_headroom():
    scales = pg.scale_sizes_to_heat([500, 500], [1000], 100000, cap_pct=6.0)
    assert scales == [1.0, 1.0]


def test_heat_scaling_pro_rata():
    # open risk 5000 (5%), cap 6% -> headroom 1000; new risk 2000 -> scale 0.5
    scales = pg.scale_sizes_to_heat([1200, 800], [5000], 100000, cap_pct=6.0)
    assert scales == pytest.approx([0.5, 0.5])


def test_heat_scaling_zero_headroom_blocks():
    scales = pg.scale_sizes_to_heat([500], [6000], 100000, cap_pct=6.0)
    assert scales == [0.0]


def test_regime_multiplier_thresholds():
    assert pg.regime_size_multiplier(macro_risk_score=30) == 1.0
    assert pg.regime_size_multiplier(macro_risk_score=54.9) == 1.0
    assert pg.regime_size_multiplier(macro_risk_score=55) == 0.5
    assert pg.regime_size_multiplier(macro_risk_score=69.9) == 0.5
    assert pg.regime_size_multiplier(macro_risk_score=70) == 0.0
    assert pg.regime_size_multiplier(macro_risk_score=90) == 0.0


def test_regime_multiplier_vix_proxy():
    assert pg.regime_size_multiplier(vix=18) == 1.0
    assert pg.regime_size_multiplier(vix=25) == 0.5
    assert pg.regime_size_multiplier(vix=40) == 0.5


def test_regime_multiplier_no_signal_is_neutral():
    assert pg.regime_size_multiplier() == 1.0


def test_daily_returns_math():
    rets = pg.daily_returns([100.0, 110.0, 99.0])
    assert rets == pytest.approx([0.10, -0.10])


# ---------------------------------------------------------------------------
# advisor wiring: build_advisor_cards(..., portfolio_context=...)
# ---------------------------------------------------------------------------

def _adv_cfg(**risk_over):
    risk = {"max_risk_per_trade_pct": 1.0, "max_positions": 8,
            "correlation_cap": 0.7, "portfolio_heat_cap_pct": 6.0,
            "regime_halve_score": 55}
    risk.update(risk_over)
    return {
        "advisor": {"assumed_equity": 100000, "max_cards_per_day": 5,
                    "min_conviction": 60, "session_validity": "next session only",
                    "entry_zone_pct": 0.3},
        "strategy1": {"time_stop_days": 10, "atr_trailing_mult": 2.5,
                      "take_profit_r": 2.0},
        "risk": risk,
    }


def _adv_decision(symbol="TST"):
    return {
        "symbol": symbol, "decision": "BUY", "conviction": 72,
        "position_size_pct": 0.5, "stop_loss": 97.0,
        "trailing_atr_mult": 2.0, "take_profit_r": 2.0,
        "reasoning": "Trend intact.", "disagreement_notes": "Devil notes risk.",
    }


def _adv_dossier(symbol="TST", ref_price=101.0):
    return {
        "symbol": symbol, "price": 100.0,
        "indicators": {"atr_14": 2.0},
        "extended_hours": {"available": True, "reference_price": ref_price,
                           "reference_source": "premarket"},
        "unusual_activity": {"flagged": False},
        "catalysts": {"hot": False, "items": []},
    }


def _adv_cards(symbols, ctx):
    from pipeline.advisor import build_advisor_cards

    dossiers = {s: _adv_dossier(s) for s in symbols}
    return build_advisor_cards([_adv_decision(s) for s in symbols], dossiers,
                               _adv_cfg(), "2026-09-11",
                               portfolio_context=ctx)


def test_advisor_no_context_keeps_legacy_sizing():
    from pipeline.advisor import build_advisor_cards

    cards = build_advisor_cards([_adv_decision()], {"TST": _adv_dossier()},
                                _adv_cfg(), "2026-09-11")
    # entry 101, stop 97 -> risk 860 -> qty 215; no guards engaged
    assert cards[0]["quantity"] == 215
    assert cards.guard_notes == []


def test_advisor_regime_halves_size():
    cards = _adv_cards(["TST"], {"open_positions": [],
                                 "macro_risk_score": 60.0,
                                 "returns_fn": None})
    assert cards[0]["quantity"] == 107  # int(215 * 0.5)
    assert any("macro risk" in n for n in cards.guard_notes)


def test_advisor_regime_veto_blocks_all():
    cards = _adv_cards(["TST"], {"open_positions": [],
                                 "macro_risk_score": 75.0,
                                 "returns_fn": None})
    assert cards == []
    assert any("veto" in n for n in cards.guard_notes)


def test_advisor_correlation_rejects():
    series = _lcg_returns(42)
    ctx = {"open_positions": [{"symbol": "BBB", "risk_dollars": 500.0}],
           "macro_risk_score": 30.0,
           "returns_fn": lambda s: series}
    cards = _adv_cards(["TST"], ctx)
    assert cards == []
    assert any("correlation" in n for n in cards.guard_notes)


def test_advisor_correlation_passes_when_uncorrelated():
    ctx = {"open_positions": [{"symbol": "BBB", "risk_dollars": 500.0}],
           "macro_risk_score": 30.0,
           "returns_fn": lambda s: _lcg_returns(abs(hash(s)) % 100000 + 7)}
    cards = _adv_cards(["TST"], ctx)
    assert len(cards) == 1


def test_advisor_heat_scales_new_cards():
    # open risk $5500 of 6%*$100k=$6000 -> $500 headroom; new risk $860
    ctx = {"open_positions": [{"symbol": "OLD", "risk_dollars": 5500.0}],
           "macro_risk_score": 30.0, "returns_fn": None}
    cards = _adv_cards(["TST"], ctx)
    assert len(cards) == 1
    assert cards[0]["quantity"] == int(215 * 500 / 860)  # 125
    assert cards[0]["risk_dollars"] == pytest.approx(125 * 4.0)
    assert any("heat cap" in n for n in cards.guard_notes)


def test_advisor_heat_exhausted_blocks():
    ctx = {"open_positions": [{"symbol": "OLD", "risk_dollars": 6000.0}],
           "macro_risk_score": 30.0, "returns_fn": None}
    cards = _adv_cards(["TST"], ctx)
    assert cards == []
    assert any("heat cap" in n for n in cards.guard_notes)
