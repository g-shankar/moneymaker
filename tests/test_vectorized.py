"""Tests for backtest/vectorized.py (clean-room vectorized backtester).

All tests use synthetic data — no network. The engine cross-check
(builds a stub market) also runs offline.
"""

import numpy as np
import pandas as pd
import pytest

from backtest.vectorized import load_panel, rank_to_weights, simulate, stats


# ---------------------------------------------------------------------------
def test_hand_computed_equity_zero_costs():
    """3-symbol panel, two rebalances, zero costs — exact drift math."""
    closes = np.array(
        [
            [100.0, 50.0, 200.0],
            [102.0, 50.0, 190.0],
            [101.0, 55.0, 190.0],
            [103.0, 55.0, 180.0],
            [105.0, 60.0, 180.0],
        ]
    )
    volumes = np.ones_like(closes)
    weights = np.array([[0.5, 0.5, 0.0], [0.0, 0.6, 0.4]])
    res = simulate(
        closes, volumes, weights, [0, 2],
        initial_equity=100_000.0, commission_bps=0.0, slippage_bps=0.0,
    )
    eq = res["equity"]
    # t=0: buy 500 A @100, 1000 B @50 -> equity 100000
    # t=1: 500*102 + 1000*50 = 101000
    # t=2 rebalance: V = 500*101 + 1000*55 = 105500
    #   sell A (-50500), buy B +8300 -> 1000+8300/55 = 12660/11 sh,
    #   buy C 42200/190 = 4220/19 sh, cash 0
    b_sh, c_sh = 12660 / 11, 4220 / 19
    expected = np.array(
        [
            100_000.0,
            101_000.0,
            105_500.0,
            b_sh * 55.0 + c_sh * 180.0,
            b_sh * 60.0 + c_sh * 180.0,
        ]
    )
    np.testing.assert_allclose(eq, expected, rtol=1e-12)
    assert res["total_costs"] == pytest.approx(0.0)
    assert res["n_rebalances"] == 2


def test_cost_model_exact():
    """Single-name buy-and-hold; costs = traded notional * bps, funded cleanly."""
    closes = np.array([[100.0], [110.0]])
    volumes = np.ones_like(closes)
    res = simulate(
        closes, volumes, np.array([[1.0]]), [0],
        initial_equity=100_000.0, commission_bps=10.0, slippage_bps=5.0,
    )
    rate = 15.0 / 10_000.0
    raw_cost = 100_000.0 * rate
    scale = (100_000.0 - raw_cost) / 100_000.0
    shares = 100_000.0 * scale / 100.0
    cost = 100_000.0 * scale * rate
    cash = 100_000.0 - 100_000.0 * scale - cost
    assert res["total_costs"] == pytest.approx(cost)
    assert cash >= 0.0  # fixed-point funding keeps cash non-negative
    np.testing.assert_allclose(
        res["equity"], [cash + shares * 100.0, cash + shares * 110.0], rtol=1e-12
    )
    # turnover: full notional traded once
    assert res["turnover"][0] == pytest.approx(100_000.0 * scale / 100_000.0)


def test_missing_data_mask():
    """NaN at a rebalance date -> symbol skipped, no crash, cash residual."""
    closes = np.array(
        [
            [100.0, 50.0],
            [101.0, np.nan],  # B untradeable on the rebalance date
            [102.0, 60.0],
        ]
    )
    volumes = np.ones_like(closes)
    res = simulate(
        closes, volumes, np.array([[0.5, 0.5]]), [1],
        initial_equity=100_000.0, commission_bps=0.0, slippage_bps=0.0,
    )
    a_sh = 50_000.0 / 101.0
    np.testing.assert_allclose(
        res["equity"],
        [100_000.0, 100_000.0, a_sh * 102.0 + 50_000.0],
        rtol=1e-12,
    )


def test_missing_data_held_position_carried():
    """Position opened, then price goes missing -> carried at last print."""
    closes = np.array([[100.0], [np.nan], [110.0]])
    volumes = np.ones_like(closes)
    res = simulate(
        closes, volumes, np.array([[1.0]]), [0],
        initial_equity=10_000.0, commission_bps=0.0, slippage_bps=0.0,
    )
    np.testing.assert_allclose(res["equity"], [10_000.0, 10_000.0, 11_000.0], rtol=1e-12)


def test_rank_to_weights_equal_and_rank():
    ranks = np.array([[3.0, 1.0, 2.0, np.nan]])
    w_eq = rank_to_weights(ranks, top_n=2, weighting="equal")
    np.testing.assert_allclose(w_eq[0], [0.5, 0.0, 0.5, 0.0])
    w_rank = rank_to_weights(ranks, top_n=2, weighting="rank")
    np.testing.assert_allclose(w_rank[0], [2 / 3, 0.0, 1 / 3, 0.0])
    # top_n larger than eligible pool -> split among available
    w_all = rank_to_weights(ranks, top_n=9, weighting="equal")
    np.testing.assert_allclose(w_all[0], [1 / 3, 1 / 3, 1 / 3, 0.0])
    # nobody eligible -> zeros
    w_none = rank_to_weights(np.full((1, 3), np.nan), top_n=2)
    np.testing.assert_allclose(w_none[0], [0.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        rank_to_weights(ranks, top_n=0)
    with pytest.raises(ValueError):
        rank_to_weights(ranks, top_n=2, weighting="bogus")


def test_load_panel_schema_and_mask(tmp_path):
    """DuckDB round-trip: alignment, duplicates, missing-data mask."""
    import duckdb

    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE daily_bars (symbol VARCHAR, date DATE, open DOUBLE, "
        "high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE)"
    )
    rows = [
        ("A", "2024-01-02", 1, 1, 1, 10.0, 100),
        ("A", "2024-01-03", 1, 1, 1, 11.0, 100),
        ("B", "2024-01-02", 1, 1, 1, 20.0, 200),
        # B missing on 2024-01-03 -> mask False
        ("A", "2024-01-02", 1, 1, 1, 10.5, 100),  # dup: last wins
    ]
    con.executemany("INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.close()

    panel = load_panel(db, symbols=["A", "B"])
    assert panel["symbols"] == ["A", "B"]
    assert panel["close"].shape == (2, 2)
    assert panel["close"][0, 0] == pytest.approx(10.5)  # dup resolved
    assert panel["valid"].tolist() == [[True, True], [True, False]]
    assert panel["volume"][1, 1] == pytest.approx(0.0)


def test_stats_hand_computed():
    eq = np.array([100.0, 110.0, 105.0, 115.0])
    s = stats(eq)
    assert s["total_return"] == pytest.approx(0.15)
    assert s["win_rate"] == pytest.approx(2 / 3)
    # peak at 110 (idx1), trough at 105 (idx2)
    assert s["max_drawdown"] == pytest.approx(105 / 110 - 1)
    rets = np.array([0.10, 105 / 110 - 1, 115 / 105 - 1])
    pf = rets[rets > 0].sum() / -rets[rets < 0].sum()
    assert s["profit_factor"] == pytest.approx(pf)
    assert s["sharpe"] > 0 and np.isfinite(s["sharpe"])
    # drawdown dates wired through
    dates = pd.bdate_range("2024-01-01", periods=4)
    s2 = stats(pd.Series(eq, index=dates))
    assert s2["max_dd_peak"] == dates[1].to_numpy()
    assert s2["max_dd_trough"] == dates[2].to_numpy()


def _stub_market(days: pd.DatetimeIndex, px: np.ndarray):
    class StubMarket:
        def get_bars(self, symbol, start, end):
            if symbol == "^VIX":
                return pd.DataFrame(
                    {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0,
                     "volume": 0.0},
                    index=days,
                )
            return pd.DataFrame(
                {
                    "open": px,
                    "high": px * 1.001,
                    "low": px * 0.999,
                    "close": px,
                    "volume": 1_000_000.0,
                },
                index=days,
            )

    return StubMarket()


def test_cross_check_engine_buy_and_hold_spy():
    """Buy-and-hold through the event engine vs the vectorized engine.

    Synthetic SPY-like bars (flat intraday so the engine's next-open fill
    equals the vectorized close fill). Whole-share rounding in the event
    engine is the only expected difference: < 1%.
    """
    try:
        from backtest.engine import run_backtest
    except ImportError:
        pytest.skip("backtest.engine unavailable")

    rng = np.random.default_rng(7)
    n = 400
    days = pd.bdate_range("2023-01-02", periods=n)
    rets = rng.normal(0.0004, 0.012, n)
    px = 100.0 * np.exp(np.cumsum(rets))  # ~$100 scale: <1-share error is tiny
    market = _stub_market(days, px)

    config = {
        "strategy1": {
            "min_composite_score": -1e9,  # everything passes the score gate
            "atr_trailing_mult": 1e9,  # stop effectively disabled
            "take_profit_r": 1e18,  # take-profit effectively disabled
            "time_stop_days": 10**9,  # time stop effectively disabled
        },
        "risk": {"max_positions": 1},
    }
    out = run_backtest(
        ["SPY"], days[0].strftime("%Y-%m-%d"), days[-1].strftime("%Y-%m-%d"),
        100_000.0, config, market=market,
    )
    engine_eq = out["equity_curve"]["equity"].to_numpy(dtype=float)

    # Engine signals on the first day with >= 230 bars of history (index
    # 229) and executes at the next open (index 230).
    exec_idx = 230
    assert len(out["trades"]) == 1, "expected exactly one round-trip trade"
    assert out["trades"][0]["exit_reason"] == "end_of_backtest"

    closes = px.reshape(-1, 1)
    res = simulate(
        closes, np.ones_like(closes), np.array([[1.0]]), [exec_idx],
        initial_equity=100_000.0, commission_bps=0.0, slippage_bps=0.0,
    )
    vec_eq = res["equity"]
    assert len(engine_eq) == len(vec_eq) == n
    max_rel = float(np.max(np.abs(engine_eq - vec_eq) / vec_eq))
    assert max_rel < 0.01, f"engines disagree by {max_rel:.4%}"
