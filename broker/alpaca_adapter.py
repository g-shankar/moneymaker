"""Alpaca paper-trading broker adapter.

PAPER TRADING ONLY. The only permitted base URL is PAPER_BASE_URL
(https://paper-api.alpaca.markets). Any attempt to instantiate with a
different URL raises ValueError immediately, so no live-trading code
path can exist here.

API secrets are read from the environment (ALPACA_API_KEY and
ALPACA_API_SECRET) and are never printed, logged, or included in
exceptions or request logs.
"""

from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1, 2, 4)


def _with_retries(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Retry a broker call up to 3 times with exponential backoff.

    Waits 1s, 2s, 4s between attempts and logs each retry via stdlib
    logging. Never logs API keys or full request bodies (callers pass
    only safe values such as symbol/qty/prices to the logger).
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if attempt >= _MAX_ATTEMPTS:
                    log.error("Broker call %s failed after %d attempts", fn.__name__, _MAX_ATTEMPTS)
                    raise
                wait = _BACKOFF_SECONDS[attempt - 1]
                log.warning(
                    "Broker call %s attempt %d/%d failed (%s: %s); retrying in %ds",
                    fn.__name__,
                    attempt,
                    _MAX_ATTEMPTS,
                    type(exc).__name__,
                    str(exc)[:200],
                    wait,
                )
                time.sleep(wait)
        raise RuntimeError("unreachable")  # pragma: no cover

    return wrapper


class AlpacaPaperBroker:
    """Broker adapter for Alpaca paper trading only.

    Raises ValueError at construction if the base URL is anything other
    than PAPER_BASE_URL, and defensively also if the URL contains
    "api.alpaca.markets" without the "paper" prefix (the live-trading
    host). Raises RuntimeError if ALPACA_API_KEY / ALPACA_API_SECRET
    are not set in the environment.

    alpaca-py is imported lazily inside __init__ so this module can be
    imported (e.g. for tests or the SimulatedBroker pipeline) without
    the dependency installed.
    """

    def __init__(self, base_url: str = PAPER_BASE_URL) -> None:
        """Create the paper-trading broker client.

        Args:
            base_url: Must equal PAPER_BASE_URL; anything else raises
                ValueError (live trading is blocked).

        Raises:
            ValueError: If base_url is not the paper-trading URL.
            RuntimeError: If the API key/secret env vars are missing.
        """
        if base_url != PAPER_BASE_URL:
            raise ValueError("LIVE TRADING BLOCKED: only the Alpaca paper URL is permitted")
        if "api.alpaca.markets" in base_url and "paper" not in base_url:
            raise ValueError("LIVE TRADING BLOCKED: only the Alpaca paper URL is permitted")

        api_key = os.environ.get("ALPACA_API_KEY")
        api_secret = os.environ.get("ALPACA_API_SECRET")
        if not api_key or not api_secret:
            # Message deliberately contains no key material.
            raise RuntimeError("ALPACA_API_KEY / ALPACA_API_SECRET not set in environment")

        from alpaca.trading.client import TradingClient

        self._client = TradingClient(api_key=api_key, secret_key=api_secret, paper=True)
        log.info("AlpacaPaperBroker connected to paper endpoint")

    @_with_retries
    def get_account(self) -> dict:
        """Fetch account summary from Alpaca paper.

        Returns:
            dict with equity (float), cash (float), buying_power (float).
        """
        account = self._client.get_account()
        result = {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
        }
        log.info("get_account: equity=%.2f cash=%.2f", result["equity"], result["cash"])
        return result

    @_with_retries
    def get_positions(self) -> list[dict]:
        """Fetch all open positions from Alpaca paper.

        Returns:
            List of dicts, each with keys: symbol, qty (float),
            avg_entry_price, current_price, unrealized_pl,
            market_value, side ("long").
        """
        out = []
        for p in self._client.get_all_positions():
            out.append(
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "avg_entry_price": float(p.avg_entry_price),
                    "current_price": float(p.current_price),
                    "unrealized_pl": float(p.unrealized_pl),
                    "market_value": float(p.market_value),
                    "side": "long",
                }
            )
        log.info("get_positions: %d open", len(out))
        return out

    def submit_bracket(
        self,
        symbol: str,
        qty: float,
        side: str,
        stop_price: float,
        take_profit_price: float,
    ) -> dict:
        """Submit a bracket order (entry + stop-loss + take-profit).

        v1 supports long entries only: side must be "buy", otherwise
        ValueError is raised (validation happens before any retry, so a
        bad side is never retried). The order uses OrderClass.BRACKET
        with time_in_force=Day.

        Args:
            symbol: Ticker symbol.
            qty: Number of shares.
            side: Must be "buy" (v1 does not support shorting).
            stop_price: Stop-loss price.
            take_profit_price: Take-profit price.

        Returns:
            dict with order_ids (list of the created order's id).
        """
        if side != "buy":
            raise ValueError(f"Unsupported side {side!r}: v1 supports 'buy' only (no shorting)")
        return self._submit_bracket(symbol, qty, stop_price, take_profit_price)

    @_with_retries
    def _submit_bracket(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        take_profit_price: float,
    ) -> dict:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            OrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        log.info(
            "submit_bracket: symbol=%s qty=%s side=buy stop=%.2f tp=%.2f",
            symbol,
            qty,
            stop_price,
            take_profit_price,
        )
        request = OrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            order_class=OrderClass.BRACKET,
            time_in_force=TimeInForce.DAY,
            stop_loss=StopLossRequest(stop_price=stop_price),
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
        )
        order = self._client.submit_order(request)
        return {"order_ids": [order.id]}

    @_with_retries
    def close_position(self, symbol: str) -> None:
        """Close (liquidate) the open position in the given symbol.

        Args:
            symbol: Ticker symbol of the position to close.
        """
        log.info("close_position: symbol=%s", symbol)
        self._client.close_position(symbol)

    @_with_retries
    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order by its Alpaca order id.

        Args:
            order_id: The Alpaca order id.
        """
        log.info("cancel_order: order_id=%s", order_id)
        self._client.cancel_order_by_id(order_id)

    @_with_retries
    def list_orders(self, status: str = "open") -> list[dict]:
        """List orders by status (default: open).

        Args:
            status: Alpaca order status filter ("open" or "closed").

        Returns:
            List of dicts with keys: id, symbol, side, qty, type, status.
        """
        from alpaca.trading.enums import QueryOrderStatus

        query_status = QueryOrderStatus.OPEN if status == "open" else QueryOrderStatus.CLOSED
        out = []
        for o in self._client.get_orders(status=query_status):
            out.append(
                {
                    "id": o.id,
                    "symbol": o.symbol,
                    "side": str(o.side.value) if hasattr(o.side, "value") else str(o.side),
                    "qty": float(o.qty) if o.qty else 0.0,
                    "type": str(o.order_type.value) if hasattr(o.order_type, "value") else str(o.order_type),
                    "status": str(o.status.value) if hasattr(o.status, "value") else str(o.status),
                }
            )
        log.info("list_orders: status=%s count=%d", status, len(out))
        return out
