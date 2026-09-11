"""Strategy 2 slot: a clean interface for a not-yet-defined strategy.

Strategy 1 (stock_picker) is the live council-gated picker. Strategy 2 does
not exist yet — its specification lives below so the user can fill it in and
hand the details to an agent for implementation. Until then the Strategy2
class is intentionally disabled: every method raises NotImplementedError.

To define Strategy 2, answer the questions in STRATEGY2_SPEC and implement:
  1. scan(market, config)  -> iterable of dossiers in the Strategy-1 dossier
     schema (or a documented extension of it).
  2. signal(dossier)       -> dict with score / veto semantics, or a
     re-export of the Strategy-1 composite_score with a different weight set.
  3. exits(position_state) -> dict with stop / take_profit /
     trailing-stop logic honoring any risk overrides.
  4. Council roles needed (who votes, what gates).
"""

STRATEGY2_SPEC: dict = {
    "name": "Strategy 2",
    "status": "TBD — awaiting user specification",
    "purpose": (
        "TBD. Example: mean-reversion on pullbacks in uptrend leaders; "
        "earnings-gap momentum; pairs trading; sector rotation. "
        "Describe what inefficiency this strategy exploits and why it "
        "should persist."
    ),
    "universe": {
        "description": "TBD. Which symbols can this strategy trade?",
        "examples": [
            "US large-cap equities with avg daily dollar volume > $10M",
            "Watchlist from file X",
            "S&P 500 constituents only",
        ],
        "decision": "TBD",
    },
    "signal_definition": {
        "description": "TBD. What observable conditions constitute a signal?",
        "fields": {
            "inputs": "TBD — which indicators/events (RSI, gaps, earnings, news...)",
            "thresholds": "TBD — numeric trigger levels for long/short",
            "direction": "TBD — long-only, short-only, or both",
            "timeframe": "TBD — bars/interval the signal is evaluated on",
        },
    },
    "entry_rules": {
        "description": "TBD. Exactly when does this strategy enter?",
        "fields": {
            "trigger": "TBD — e.g. signal on daily close, intraday break, limit price rule",
            "order_type": "TBD — market / limit / stop-limit",
            "max_entries_per_day": "TBD",
            "cooldown_after_exit": "TBD — e.g. no re-entry within N days",
            "confirmation": "TBD — any council/quant gate required before entry",
        },
    },
    "exit_rules": {
        "description": "TBD. Exactly when does this strategy exit?",
        "fields": {
            "stop_loss": "TBD — e.g. 2x ATR from entry, fixed %",
            "take_profit": "TBD — e.g. 2R, fixed %, trailing only",
            "trailing_stop": "TBD — ratchet rule, or none",
            "time_stop": "TBD — max holding period",
            "signal_reversal": "TBD — exit if the signal flips direction",
        },
    },
    "risk_overrides": {
        "description": (
            "TBD. Anything that deviates from the global RiskManager "
            "defaults in risk/manager.py."
        ),
        "fields": {
            "max_risk_per_trade_pct": "TBD — or null to inherit global",
            "atr_mult": "TBD — or null to inherit global atr_trailing_mult",
            "max_positions": "TBD — dedicated slot cap, or share global pool",
            "allowed_sides": "TBD — ['long'] or ['long', 'short']",
        },
    },
    "council_roles_needed": {
        "description": "TBD. Which council members/roles vote or gate Strategy 2?",
        "examples": [
            "Re-use the Strategy-1 council (technicals / quant / sentiment / macro)",
            "Add a dedicated earnings analyst role",
            "No council — fully systematic, council is advisory only",
        ],
        "decision": "TBD",
    },
    "backtest_requirements": {
        "description": "TBD. Evidence required before this goes live on paper.",
        "fields": {
            "history_window": "TBD — e.g. 3 years of daily bars",
            "acceptance_metrics": "TBD — e.g. Sharpe > 1.0, max drawdown < 15%",
        },
    },
}

DISABLED_MESSAGE = (
    "Strategy 2 is not yet defined — awaiting user specification. "
    "See STRATEGY2_SPEC."
)


class Strategy2:
    """Placeholder for the user's second strategy. Disabled by default."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.enabled = False

    def scan(self, market, config: dict):
        """Scan the market and return candidate dossiers."""
        raise NotImplementedError(DISABLED_MESSAGE)

    def signal(self, dossier: dict):
        """Score a single dossier / decide entry."""
        raise NotImplementedError(DISABLED_MESSAGE)

    def exits(self, position_state: dict):
        """Compute stop / take-profit / trailing state for a live position."""
        raise NotImplementedError(DISABLED_MESSAGE)
