"""RiskManager unit tests (risk/manager.py).

Covers the sizing formula from the build contract, the daily-loss halt,
the kill-switch sentinel, and the max-positions pre-trade gate.
"""

from risk.manager import RiskManager


def make_risk(tmp_path, **risk_overrides):
    risk_cfg = {
        "max_risk_per_trade_pct": 1.0,
        "max_positions": 8,
        "daily_loss_halt_pct": 3.0,
        "kill_switch_file": str(tmp_path / "KILL_SWITCH"),
        "max_day_trades": 3,
    }
    risk_cfg.update(risk_overrides)
    cfg = {"risk": risk_cfg, "strategy1": {"atr_trailing_mult": 2.5}}
    return RiskManager(cfg, str(tmp_path / "state"))


def test_position_size_shares_matches_contract(tmp_path):
    rm = make_risk(tmp_path)
    # risk$ = 100000 * 1% * (0.5 + 0.5*80/100) = 900; stop dist = 2.0*2.5 = 5
    assert rm.position_size_shares(100000, 80, 100, 2.0) == int(1000 * 0.9 / (2.0 * 2.5)) == 180


def test_position_size_zero_on_bad_inputs(tmp_path):
    rm = make_risk(tmp_path)
    assert rm.position_size_shares(100000, 80, 100, 0.0) == 0
    assert rm.position_size_shares(0, 80, 100, 2.0) == 0


def test_daily_loss_halted(tmp_path):
    rm = make_risk(tmp_path)
    assert rm.daily_loss_halted(-3000, 100000) is True
    assert rm.daily_loss_halted(-2999.99, 100000) is False
    assert rm.daily_loss_halted(500, 100000) is False


def test_kill_switch(tmp_path):
    kill_file = tmp_path / "KILL_SWITCH"
    rm = make_risk(tmp_path)
    assert rm.kill_switch_active() is False
    kill_file.write_text("halt", encoding="utf-8")
    assert rm.kill_switch_active() is True
    ok, reason = rm.check_pre_trade(
        "AAPL", "buy", 10, 150.0, {"equity": 100000, "buying_power": 100000}, 0, 0.0
    )
    assert ok is False
    assert "kill switch" in reason


def test_check_pre_trade_rejects_at_max_positions(tmp_path):
    rm = make_risk(tmp_path)
    account = {"equity": 100000, "buying_power": 100000}
    ok, reason = rm.check_pre_trade("AAPL", "buy", 10, 150.0, account, 8, 0.0)
    assert ok is False
    assert "max positions" in reason


def test_check_pre_trade_passes_when_clear(tmp_path):
    rm = make_risk(tmp_path)
    account = {"equity": 100000, "buying_power": 100000}
    ok, reason = rm.check_pre_trade("AAPL", "buy", 10, 150.0, account, 0, 0.0)
    assert (ok, reason) == (True, "ok")


def test_record_day_trade_persists(tmp_path):
    rm = make_risk(tmp_path)
    rm.record_day_trade("2026-09-10")
    rm2 = make_risk(tmp_path)  # reload from disk
    assert "2026-09-10" in rm2._state["day_trades"]
