"""Portfolio-level guards — correlation cap, portfolio heat, regime sizing.

These sit ABOVE the per-trade RiskManager gates: they decide whether a new
paper position is allowed at all, and at what size, given the whole book.

* Correlation cap: reject a new position whose 60-day daily-return
  correlation exceeds `correlation_cap` (default 0.7) with any open position.
  Two names moving in lockstep are one bet, not two.
* Portfolio heat: sum of position risk-$ across open positions must stay
  under `portfolio_heat_cap_pct` (default 6%) of equity. New cards are
  scaled down to fit whatever headroom remains.
* Regime-adaptive sizing: Kalshi macro risk >= `regime_halve_score`
  (default 55) halves all sizes; >= 70 the council veto already fires
  (multiplier 0.0 here for completeness). A VIX proxy >= 25 also halves.

Paper-trading only. Pure functions — no I/O, no credentials.
"""

import logging as _logging
import math as _math

LOG = _logging.getLogger(__name__)

_DEFAULT_CORR_CAP = 0.7
_DEFAULT_HEAT_CAP_PCT = 6.0
_DEFAULT_REGIME_HALVE_SCORE = 55.0
_VETO_SCORE = 70.0
_VIX_HALVE = 25.0


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation; None if undefined (constant series / too short)."""
    n = min(len(xs), len(ys))
    if n < 10:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / _math.sqrt(sxx * syy)


def daily_returns(closes: list[float]) -> list[float]:
    """Simple daily returns from a close series."""
    return [
        (closes[i] / closes[i - 1] - 1.0)
        for i in range(1, len(closes))
        if closes[i - 1]
    ]


def correlation_check(
    symbol: str,
    open_symbols: list[str],
    returns_fn,
    threshold: float = _DEFAULT_CORR_CAP,
    lookback_days: int = 60,
) -> tuple[bool, float | None, str | None]:
    """Reject a new position if too correlated with any open one.

    returns_fn(symbol) -> list of daily returns (most recent last).
    Returns (rejected, max_correlation, offending_symbol).
    Missing data never rejects — it abstains (logged).
    """
    symbol = symbol.upper()
    worst_corr: float | None = None
    worst_sym: str | None = None
    try:
        new_rets = returns_fn(symbol)
    except Exception as exc:  # noqa: BLE001 - data gaps must not block trading
        LOG.warning("correlation_check: no returns for %s (%s) — abstaining",
                    symbol, exc)
        return False, None, None
    if not new_rets:
        return False, None, None
    new_rets = new_rets[-lookback_days:]
    for other in open_symbols:
        if other.upper() == symbol:
            continue
        try:
            other_rets = returns_fn(other)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("correlation_check: no returns for %s (%s) — skipping",
                        other, exc)
            continue
        corr = _pearson(new_rets, other_rets[-lookback_days:])
        if corr is None:
            continue
        if worst_corr is None or corr > worst_corr:
            worst_corr, worst_sym = corr, other.upper()
    if worst_corr is not None and worst_corr > threshold:
        LOG.warning("correlation_check REJECT %s: %.2f corr with %s (cap %.2f)",
                    symbol, worst_corr, worst_sym, threshold)
        return True, worst_corr, worst_sym
    return False, worst_corr, worst_sym


def portfolio_heat(open_risks: list[float], equity: float) -> float:
    """Current heat = sum of open position risk-$ / equity, as percent."""
    if equity <= 0:
        return 0.0
    return 100.0 * sum(max(0.0, r) for r in open_risks) / equity


def scale_sizes_to_heat(
    new_risks: list[float],
    open_risks: list[float],
    equity: float,
    cap_pct: float = _DEFAULT_HEAT_CAP_PCT,
) -> list[float]:
    """Per-card size multipliers (0..1) so total heat stays under the cap.

    If headroom covers all new risk, every multiplier is 1.0. Otherwise new
    cards share the remaining headroom pro-rata. Zero headroom -> all 0.0
    (caller should drop the cards, not emit dust).
    """
    if equity <= 0 or cap_pct <= 0:
        return [0.0 for _ in new_risks]
    headroom = equity * cap_pct / 100.0 - sum(max(0.0, r) for r in open_risks)
    total_new = sum(max(0.0, r) for r in new_risks)
    if total_new <= 0:
        return [1.0 for _ in new_risks]
    if headroom <= 0:
        LOG.warning("heat cap: no headroom (cap %.1f%%) — new cards blocked",
                    cap_pct)
        return [0.0 for _ in new_risks]
    if headroom >= total_new:
        return [1.0 for _ in new_risks]
    scale = headroom / total_new
    LOG.warning("heat cap: scaling new cards by %.2f to fit %.1f%% cap",
                scale, cap_pct)
    return [scale for _ in new_risks]


def regime_size_multiplier(
    macro_risk_score: float | None = None,
    vix: float | None = None,
    halve_score: float = _DEFAULT_REGIME_HALVE_SCORE,
) -> float:
    """Size multiplier from the macro regime.

    1.0 normal; 0.5 when Kalshi macro risk >= halve_score (default 55) or
    VIX proxy >= 25; 0.0 when score >= 70 (council veto fires there anyway).
    """
    if macro_risk_score is not None and macro_risk_score >= _VETO_SCORE:
        return 0.0
    if macro_risk_score is not None and macro_risk_score >= halve_score:
        return 0.5
    if vix is not None and vix >= _VIX_HALVE:
        return 0.5
    return 1.0
