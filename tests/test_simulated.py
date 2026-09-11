"""SimulatedBroker unit tests (broker/simulated.py).

Bracket semantics: a position filled at entry_price closes at the stop
price when the close touches the stop, or at the take-profit price when
the close touches the take-profit.
"""

from broker.simulated import SimulatedBroker


def test_stop_loss_path():
    broker = SimulatedBroker(100000)
    broker.submit_bracket("AAPL", 10, "buy", 140, 160, 150)
    broker.step({"AAPL": 139})  # below stop 140 -> stopped out at 140
    assert broker.get_positions() == []
    assert broker.get_account()["realized_pl"] == 10 * (140 - 150) == -100


def test_take_profit_path():
    broker = SimulatedBroker(100000)
    broker.submit_bracket("AAPL", 10, "buy", 140, 160, 150)
    broker.step({"AAPL": 161})  # above take-profit 160 -> closed at 160
    assert broker.get_positions() == []
    assert broker.get_account()["realized_pl"] == 10 * (160 - 150) == 100


def test_no_trigger_holds_position():
    broker = SimulatedBroker(100000)
    broker.submit_bracket("AAPL", 10, "buy", 140, 160, 150)
    broker.step({"AAPL": 150})
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"
    assert broker.get_account()["realized_pl"] == 0
