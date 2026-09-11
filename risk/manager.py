"""Risk management for the autonomous paper-trading system.

Paper-trading context only. No real orders, no credentials, headless-safe.

`RiskManager` is the single choke point for pre-trade checks and position
sizing:

* kill switch: a sentinel file on disk halts all new trades immediately.
* daily loss halt: stop opening new positions once day PnL breaches
  daily_loss_halt_pct of equity.
* position cap: max_positions concurrent open positions.
* buying-power check: notional must fit account buying power.
* PDT guard: at most max_day_trades distinct day-trade dates in the trailing
  5 calendar days.
* position sizing: fixed fractional risk per trade scaled by conviction
  (0-100). risk_dollars = equity * (max_risk_per_trade_pct/100) *
  (0.5 + 0.5*conviction/100); shares = floor(risk_dollars / (atr*atr_mult)).

Day trades are persisted to state_dir/risk_state.json ({"day_trades": [...]})
so the PDT count survives restarts. All decisions are logged via stdlib
logging.
"""

import datetime as _dt
import json as _json
import logging as _logging
import os as _os

LOG = _logging.getLogger(__name__)

_STATE_FILENAME = "risk_state.json"
_PDT_WINDOW_DAYS = 5


class RiskManager:
    """Pre-trade risk gate and position sizer.

    config: dict with keys
        risk: {
            max_risk_per_trade_pct: float = 1.0,
            max_positions: int = 8,
            daily_loss_halt_pct: float = 3.0,
            kill_switch_file: str (path to sentinel file),
            max_day_trades: int = 3,
        }
        strategy1: { atr_trailing_mult: float = 2.5 }

    state_dir: directory holding risk_state.json, created if missing.
    """

    def __init__(self, config: dict, state_dir: str):
        risk = (config or {}).get("risk", {})
        self._max_risk_per_trade_pct = float(risk.get("max_risk_per_trade_pct", 1.0))
        self._max_positions = int(risk.get("max_positions", 8))
        self._daily_loss_halt_pct = float(risk.get("daily_loss_halt_pct", 3.0))
        self._kill_switch_file = str(risk.get("kill_switch_file", ""))
        self._max_day_trades = int(risk.get("max_day_trades", 3))
        self._atr_trailing_mult = float(
            (config or {}).get("strategy1", {}).get("atr_trailing_mult", 2.5)
        )

        self._state_dir = state_dir
        _os.makedirs(state_dir, exist_ok=True)
        self._state_path = _os.path.join(state_dir, _STATE_FILENAME)
        self._state = self._load_state()
        LOG.info(
            "RiskManager init: risk_pct=%s max_positions=%d halt_pct=%s "
            "max_day_trades=%d atr_mult=%s kill_switch=%s",
            self._max_risk_per_trade_pct,
            self._max_positions,
            self._daily_loss_halt_pct,
            self._max_day_trades,
            self._atr_trailing_mult,
            self._kill_switch_file or "(none configured)",
        )

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                state = _json.load(fh)
            if isinstance(state, dict) and isinstance(
                state.get("day_trades"), list
            ):
                return state
        except (OSError, ValueError) as exc:
            LOG.warning("could not load risk state %s: %s", self._state_path, exc)
        return {"day_trades": []}

    def _save_state(self) -> None:
        try:
            with open(self._state_path, "w", encoding="utf-8") as fh:
                _json.dump(self._state, fh, indent=2)
        except OSError as exc:
            LOG.error("could not persist risk state %s: %s", self._state_path, exc)

    # ------------------------------------------------------------------
    # gates
    # ------------------------------------------------------------------
    def kill_switch_active(self) -> bool:
        """True if the kill-switch sentinel file exists."""
        active = bool(self._kill_switch_file) and _os.path.exists(
            self._kill_switch_file
        )
        if active:
            LOG.warning("kill switch ACTIVE: %s", self._kill_switch_file)
        return active

    def can_open_new(self, n_open: int) -> bool:
        """True if another position fits under max_positions."""
        return n_open < self._max_positions

    def daily_loss_halted(self, day_pnl: float, equity: float) -> bool:
        """True if day PnL has breached the daily loss halt level."""
        halted = day_pnl <= -(self._daily_loss_halt_pct / 100) * equity
        if halted:
            LOG.warning(
                "daily loss halt: day_pnl=%.2f equity=%.2f threshold_pct=%.2f",
                day_pnl,
                equity,
                self._daily_loss_halt_pct,
            )
        return halted

    def _day_trade_count_trailing_5d(self, today: _dt.date | None = None) -> int:
        """Distinct day-trade dates in the trailing 5 calendar days (excl. today)."""
        today = today or _dt.date.today()
        window_start = today - _dt.timedelta(days=_PDT_WINDOW_DAYS)
        distinct: set[str] = set()
        for entry in self._state.get("day_trades", []):
            try:
                d = _dt.date.fromisoformat(str(entry))
            except ValueError:
                continue
            if window_start <= d < today:
                distinct.add(str(entry))
        return len(distinct)

    def position_size_shares(
        self, equity: float, conviction: float, price: float, atr: float
    ) -> int:
        """Shares to buy given equity, conviction (0-100), price and ATR."""
        stop_distance = atr * self._atr_trailing_mult
        if stop_distance <= 0 or price <= 0 or equity <= 0:
            LOG.debug(
                "position_size_shares -> 0 (equity=%s price=%s atr=%s)",
                equity,
                price,
                atr,
            )
            return 0
        risk_dollars = (
            equity
            * (self._max_risk_per_trade_pct / 100)
            * (0.5 + 0.5 * max(0.0, min(100.0, conviction)) / 100)
        )
        shares = int(risk_dollars / stop_distance)
        LOG.debug(
            "position_size_shares: equity=%.2f conviction=%.1f risk_dollars=%.2f "
            "stop_distance=%.2f -> %d shares",
            equity,
            conviction,
            risk_dollars,
            stop_distance,
            shares,
        )
        return shares

    def check_pre_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        account: dict,
        n_open: int,
        day_pnl: float,
    ) -> tuple[bool, str]:
        """Run pre-trade gates in order.

        Order: kill switch -> daily loss halt -> max positions ->
        buying power -> PDT guard. Returns (True, "ok") or (False, reason).
        """
        equity = float((account or {}).get("equity", 0.0))
        buying_power = float((account or {}).get("buying_power", 0.0))

        if self.kill_switch_active():
            reason = "kill switch active"
            LOG.warning("pre-trade BLOCK %s %s: %s", side, symbol, reason)
            return False, reason

        if self.daily_loss_halted(day_pnl, equity):
            reason = "daily loss halt breached"
            LOG.warning("pre-trade BLOCK %s %s: %s", side, symbol, reason)
            return False, reason

        if not self.can_open_new(n_open):
            reason = f"max positions reached ({self._max_positions})"
            LOG.warning("pre-trade BLOCK %s %s: %s", side, symbol, reason)
            return False, reason

        notional = qty * price
        if notional > buying_power:
            reason = (
                f"insufficient buying power: need {notional:.2f}, have "
                f"{buying_power:.2f}"
            )
            LOG.warning("pre-trade BLOCK %s %s: %s", side, symbol, reason)
            return False, reason

        if self._day_trade_count_trailing_5d() >= self._max_day_trades:
            reason = (
                f"PDT guard: {self._max_day_trades} day trades already in "
                f"trailing {_PDT_WINDOW_DAYS}d window"
            )
            LOG.warning("pre-trade BLOCK %s %s: %s", side, symbol, reason)
            return False, reason

        LOG.info(
            "pre-trade PASS %s %s qty=%s price=%s", side, symbol, qty, price
        )
        return True, "ok"

    def record_day_trade(self, date_str: str) -> None:
        """Append an ISO day-trade date (e.g. '2026-09-10') and persist."""
        try:
            _dt.date.fromisoformat(date_str)
        except ValueError:
            LOG.error("record_day_trade: invalid ISO date %r", date_str)
            raise
        day_trades = self._state.setdefault("day_trades", [])
        if date_str not in day_trades:
            day_trades.append(date_str)
            self._save_state()
            LOG.info("recorded day trade on %s", date_str)
        else:
            LOG.debug("day trade on %s already recorded", date_str)
