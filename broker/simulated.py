"""Simulated broker for zero-key testing of the full trading pipeline.

Implements the same method surface and dict shapes as
:class:`broker.alpaca_adapter.AlpacaPaperBroker` so strategies and the
pipeline can be exercised end-to-end without API credentials.

Simplifications (documented):
- submit_bracket fills IMMEDIATELY at the given entry_price (a market
  fill at the requested price, no slippage model).
- step() evaluates stop/take-profit triggers against the CLOSING
  prices passed in (no intraday high/low simulation).
- cancel_order / list_orders are no-ops returning [] because orders
  fill instantly and are never left open.
- No margin: buying_power always equals cash.
"""

from __future__ import annotations

import itertools
import logging

log = logging.getLogger(__name__)


class SimulatedBroker:
    """In-memory paper broker for testing with no API keys."""

    def __init__(self, cash: float = 100000.0) -> None:
        """Create the simulated broker.

        Args:
            cash: Starting cash balance.
        """
        self._cash = float(cash)
        # symbol -> {qty, avg_entry, stop, take_profit, entry_date}
        self._positions: dict[str, dict] = {}
        self.realized_pl = 0.0
        self._prices: dict[str, float] = {}
        self._order_seq = itertools.count(1)

    def submit_bracket(
        self,
        symbol: str,
        qty: float,
        side: str,
        stop_price: float,
        take_profit_price: float,
        entry_price: float,
    ) -> dict:
        """Open a long position that fills immediately at entry_price.

        Simplification: this is a market fill at the requested price --
        there is no slippage model and no pending order lifecycle.

        Args:
            symbol: Ticker symbol.
            qty: Number of shares.
            side: Must be "buy" (v1 does not support shorting).
            stop_price: Stop-loss price.
            take_profit_price: Take-profit price.
            entry_price: Fill price for the simulated entry.

        Returns:
            dict with order_ids ([f"sim-{n}"]).
        """
        if side != "buy":
            raise ValueError(f"Unsupported side {side!r}: v1 supports 'buy' only (no shorting)")
        qty = float(qty)
        entry_price = float(entry_price)
        self._cash -= qty * entry_price
        if symbol in self._positions:
            pos = self._positions[symbol]
            total_qty = pos["qty"] + qty
            pos["avg_entry"] = (pos["avg_entry"] * pos["qty"] + entry_price * qty) / total_qty
            pos["qty"] = total_qty
            pos["stop"] = float(stop_price)
            pos["take_profit"] = float(take_profit_price)
        else:
            self._positions[symbol] = {
                "qty": qty,
                "avg_entry": entry_price,
                "stop": float(stop_price),
                "take_profit": float(take_profit_price),
                "entry_date": None,
            }
        self._prices[symbol] = entry_price
        order_id = f"sim-{next(self._order_seq)}"
        log.info(
            "submit_bracket: symbol=%s qty=%s side=%s entry=%.2f stop=%.2f tp=%.2f order=%s",
            symbol,
            qty,
            side,
            entry_price,
            stop_price,
            take_profit_price,
            order_id,
        )
        return {"order_ids": [order_id]}

    def step(self, prices: dict[str, float]) -> None:
        """Advance one bar: update prices and evaluate bracket triggers.

        Simplification: stop/take-profit triggers are checked against
        the closing prices passed in -- there is no intraday high/low
        simulation. A position whose price touches the stop (<=) is
        closed at the stop price; otherwise one touching the
        take-profit (>=) is closed at the take-profit price.

        Args:
            prices: Mapping of symbol -> closing price for this bar.
        """
        self._prices.update({s: float(p) for s, p in prices.items()})
        for symbol in list(self._positions):
            price = self._prices.get(symbol)
            if price is None:
                continue
            pos = self._positions[symbol]
            if price <= pos["stop"]:
                self._close_at(symbol, pos["stop"], reason="stop")
            elif price >= pos["take_profit"]:
                self._close_at(symbol, pos["take_profit"], reason="take_profit")

    def _close_at(self, symbol: str, price: float, reason: str = "manual") -> None:
        pos = self._positions.pop(symbol)
        proceeds = pos["qty"] * price
        cost = pos["qty"] * pos["avg_entry"]
        pl = proceeds - cost
        self._cash += proceeds
        self.realized_pl += pl
        log.info(
            "close: symbol=%s qty=%s price=%.2f pl=%.2f reason=%s",
            symbol,
            pos["qty"],
            price,
            pl,
            reason,
        )

    def close_position(self, symbol: str, price: float | None = None) -> None:
        """Close the position in the given symbol.

        Args:
            symbol: Ticker symbol.
            price: Fill price; defaults to the last known price for the
                symbol (raises KeyError if the symbol is unknown).
        """
        if symbol not in self._positions:
            log.warning("close_position: no position in %s", symbol)
            return
        fill = float(price) if price is not None else self._prices[symbol]
        self._close_at(symbol, fill, reason="manual")

    def get_account(self) -> dict:
        """Return account summary.

        Returns:
            dict with equity, cash, buying_power (= cash, no margin in
            the sim), realized_pl, unrealized_pl.
        """
        unrealized = 0.0
        market_value = 0.0
        for symbol, pos in self._positions.items():
            price = self._prices.get(symbol, pos["avg_entry"])
            market_value += pos["qty"] * price
            unrealized += pos["qty"] * (price - pos["avg_entry"])
        return {
            "equity": self._cash + market_value,
            "cash": self._cash,
            "buying_power": self._cash,
            "realized_pl": self.realized_pl,
            "unrealized_pl": unrealized,
        }

    def get_positions(self) -> list[dict]:
        """Return open positions in the same shape as AlpacaPaperBroker.

        Returns:
            List of dicts, each with EXACTLY the keys: symbol,
            qty (float), avg_entry_price, current_price, unrealized_pl,
            market_value, side ("long").
        """
        out = []
        for symbol, pos in self._positions.items():
            current = self._prices.get(symbol, pos["avg_entry"])
            out.append(
                {
                    "symbol": symbol,
                    "qty": float(pos["qty"]),
                    "avg_entry_price": float(pos["avg_entry"]),
                    "current_price": float(current),
                    "unrealized_pl": float(pos["qty"] * (current - pos["avg_entry"])),
                    "market_value": float(pos["qty"] * current),
                    "side": "long",
                }
            )
        return out

    def cancel_order(self, order_id: str) -> None:
        """No-op: the sim fills instantly, so no orders remain open."""
        log.info("cancel_order (sim no-op): order_id=%s", order_id)

    def list_orders(self, status: str = "open") -> list[dict]:
        """No-op: the sim fills instantly, so this always returns []."""
        log.info("list_orders (sim no-op): status=%s", status)
        return []
