"""Equal-risk paper position sizing.

Every card risks the same dollars, regardless of share price or volatility.
A 100-share fixed size lets a $400 stock dominate the book while a $10 stock
is decoration; this module fixes that.

PAPER-ONLY defaults: $100,000 paper equity, 1% risk per trade ($1,000).
These numbers are arbitrary paper-trading parameters, NOT the user's real
account size. Real-money sizing is never produced by this system.
"""

PAPER_EQUITY = 100_000.0
RISK_PCT = 0.01


def risk_dollars(equity: float = PAPER_EQUITY, risk_pct: float = RISK_PCT) -> float:
    """Dollars risked per trade."""
    return equity * risk_pct


def size_shares(
    entry: float,
    stop: float,
    equity: float = PAPER_EQUITY,
    risk_pct: float = RISK_PCT,
) -> int:
    """Shares such that (entry - stop) * shares ~= risk_dollars.

    BUY-side only (entry > stop required). Always returns >= 1.
    """
    per_share_risk = entry - stop
    if per_share_risk <= 0:
        raise ValueError(f"entry ({entry}) must be above stop ({stop}) for sizing")
    return max(1, int(risk_dollars(equity, risk_pct) // per_share_risk))
