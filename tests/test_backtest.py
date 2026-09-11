"""Backtest determinism test.

Runs the backtest engine twice on a 2-symbol universe over a 3-month
window and asserts identical metrics dicts.

NOTE: hits the network (yfinance) via the backtest engine. Skips (does
not fail) when the engine is missing or the network is unreachable.
"""

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_run_backtest():
    for module in ("backtest.engine", "backtest.backtest", "backtest"):
        try:
            mod = __import__(module, fromlist=["run_backtest"])
            fn = getattr(mod, "run_backtest", None)
            if callable(fn):
                return fn
        except ImportError:
            continue
    pytest.skip("backtest engine (run_backtest) not implemented yet")


def _looks_like_connection_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "connection", "urlopen", "max retries", "name resolution",
        "failed to establish", "timeout", "timed out", "network",
        "nodename nor servname", "temporary failure",
    )
    return any(m in text for m in markers)


def test_backtest_deterministic():
    run_backtest = _load_run_backtest()
    import yaml

    with open(REPO_ROOT / "config.yaml", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    kwargs = dict(
        universe=["AAPL", "MSFT"],
        start="2026-06-01",
        end="2026-09-01",
        initial_cash=100000,
        config=config,
    )
    try:
        m1 = run_backtest(**kwargs)
        m2 = run_backtest(**kwargs)
    except Exception as exc:  # noqa: BLE001 — network errors -> skip
        if _looks_like_connection_error(exc):
            pytest.skip(f"network unavailable for backtest: {exc}")
        raise
    assert isinstance(m1, dict) and isinstance(m2, dict)
    assert m1["metrics"] == m2["metrics"]
    assert m1["trades"] == m2["trades"]
