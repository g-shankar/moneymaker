"""Daily trading pipeline CLI.

PAPER TRADING ONLY. No live orders exist anywhere in this codebase.

Modes:
  --scan-only [--date YYYY-MM-DD]
      Pull 1y of bars per universe symbol, build dossiers, fetch the Kalshi
      macro gauge once, rank candidates, and write
      reports/dossiers/<date>.json.

  --execute DECISIONS_JSON [--broker simulated|alpaca] [--date YYYY-MM-DD]
      Consume a council decisions file (council/decisions_schema.json),
      submit bracket orders through the configured broker with RiskManager
      gates, run the ATR trailing-stop / time-stop maintenance pass over
      open positions, and write the EOD markdown report.

  --full [--broker ...] [--date ...]
      Scan, then resolve the council step: if council.execution == "panel",
      run the LLM panel in-process (council.panel, imported lazily) and
      execute its decisions; otherwise write a PENDING_COUNCIL stub
      decisions file and print the runbook instructions for the council
      step. Exits 0 either way (cron-safe).

Secrets come from environment variables only and are never logged.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.common import REPO_ROOT, configured_path, load_config, resolve, today_et

log = logging.getLogger(__name__)

_SCAN_LOOKBACK_DAYS = 365
_ATR_REFRESH_DAYS = 60
_TRAILING_LOOKBACK_DAYS = 30
_TRAILING_HIGH_WINDOW = 20
_LAST_EQUITY_FILE = "last_equity.json"
_TRAILING_STATE_FILE = "trailing.json"


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------
def setup_logging() -> None:
    """Stdlib logging to stderr plus a repo-local file (gitignored)."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(resolve("pipeline.log"), encoding="utf-8"))
    except OSError as exc:  # read-only FS etc. — stderr is enough
        print(f"warning: cannot open pipeline.log ({exc}); stderr only", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


# --------------------------------------------------------------------------
# state helpers
# --------------------------------------------------------------------------
def _read_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------
def cmd_scan(cfg: dict, date_str: str) -> Path:
    """Run the universe scan and write the dossiers file. Returns its path."""
    from data.dossier import build_dossier
    from data.kalshi import get_macro_risk
    from data.market import MarketData
    from strategies.stock_picker import rank_candidates

    asof = datetime.now().astimezone().isoformat()
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = (day - timedelta(days=_SCAN_LOOKBACK_DAYS)).isoformat()
    end = day.isoformat()

    market = MarketData(feed=cfg["broker"].get("data_feed", "iex"))
    # Stock universe + ETF sleeve, deduplicated, order-preserving. ETFs are
    # just symbols — the full pipeline (technicals, quant, extended-hours,
    # unusual activity, catalysts, council, advisor) handles them
    # identically, with ETF-specific adjustments in dossier.py.
    universe: list[str] = list(cfg["universe"])
    for sym in cfg.get("etf_universe") or []:
        if sym not in universe:
            universe.append(sym)
    log.info("scan %s: %d symbols (%d stocks + %d ETFs), bars %s..%s",
             date_str, len(universe), len(cfg["universe"]),
             len(cfg.get("etf_universe") or []), start, end)

    spy_bars = market.get_bars("SPY", start, end)
    if spy_bars.empty:
        log.warning("no SPY bars; beta_vs_spy will be null")

    macro_risk = get_macro_risk()
    log.info("macro risk score=%.1f (%s)", macro_risk.get("score"), macro_risk.get("reasoning"))

    dossiers = []
    etf_symbols = set(cfg.get("etf_universe") or [])
    for symbol in universe:
        try:
            dossier = build_dossier(
                symbol, market, spy_bars=spy_bars, asof=asof, kalshi=macro_risk,
                etf_symbols=etf_symbols,
            )
            dossiers.append(dossier)
            log.info("dossier %s: price=%s", symbol, dossier.get("price"))
        except Exception as exc:
            log.error("dossier failed for %s: %s: %s", symbol, type(exc).__name__, exc)

    s1 = cfg["strategy1"]
    ranked = rank_candidates(
        dossiers,
        top_n=int(s1.get("top_n", 5)),
        min_score=float(s1.get("min_composite_score", 60)),
    )
    log.info("scan %s: %d/%d candidates passed the gate", date_str, len(ranked), len(dossiers))
    for r in ranked:
        log.info("  ranked %s score=%.2f", r["symbol"], r["score"])

    payload = {"date": date_str, "macro_risk": macro_risk, "ranked": ranked}
    dossiers_dir = configured_path(cfg, "dossiers_dir")
    out_path = dossiers_dir / f"{date_str}.json"
    _write_json(out_path, payload)
    log.info("wrote %s", out_path)
    return out_path


# --------------------------------------------------------------------------
# broker / risk plumbing
# --------------------------------------------------------------------------
def _init_broker(cfg: dict, broker_name: str | None):
    """Instantiate the paper broker. Simulated needs no keys; alpaca reads
    ALPACA_API_KEY / ALPACA_API_SECRET from the environment."""
    name = (broker_name or cfg["broker"].get("mode", "simulated")).lower()
    if name == "simulated":
        from broker.simulated import SimulatedBroker

        cash = float(cfg["broker"].get("starting_cash", 100000))
        log.info("broker: simulated (starting_cash=%.2f)", cash)
        return SimulatedBroker(cash=cash)
    if name == "alpaca":
        from broker.alpaca_adapter import AlpacaPaperBroker

        log.info("broker: alpaca paper")
        return AlpacaPaperBroker()
    raise ValueError(f"unknown broker {name!r}: expected 'simulated' or 'alpaca'")


def _day_pnl(cfg: dict, date_str: str, equity: float) -> float:
    """Day PnL from the prior state snapshot (0 when unavailable).

    state/last_equity.json holds {"date", "equity"} from the previous run.
    Same-date reruns compare against the earlier snapshot; a new day resets
    the baseline to 0.
    """
    state_dir = configured_path(cfg, "state_dir")
    snap = _read_json(state_dir / _LAST_EQUITY_FILE)
    if snap and snap.get("date") == date_str and isinstance(snap.get("equity"), (int, float)):
        return float(equity) - float(snap["equity"])
    return 0.0


def _refresh_atr_price(cfg: dict, symbol: str):
    """Re-fetch recent bars for a symbol -> (price, atr_14) or (None, None)."""
    from data import indicators
    from data.market import MarketData

    day = datetime.strptime(today_et(), "%Y-%m-%d").date()
    market = MarketData(feed=cfg["broker"].get("data_feed", "iex"))
    df = market.get_bars(
        symbol,
        (day - timedelta(days=_ATR_REFRESH_DAYS)).isoformat(),
        day.isoformat(),
    )
    if df.empty or len(df) < 15:
        log.warning("insufficient bars for %s (%d rows)", symbol, len(df))
        return None, None
    price = float(df["close"].iloc[-1])
    atr = indicators.atr(df)
    atr_val = float(atr.iloc[-1]) if len(atr) else float("nan")
    if not (atr_val == atr_val) or atr_val <= 0:  # NaN guard
        log.warning("no usable ATR for %s", symbol)
        return None, None
    return price, atr_val


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------
def _load_decisions(path: Path) -> dict | None:
    """Load a decisions file. Returns None for a PENDING_COUNCIL stub."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a JSON object")
    if data.get("status") == "PENDING_COUNCIL":
        log.info("decisions file %s is PENDING_COUNCIL — nothing to execute", path)
        return None
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError(f"{path} has no 'decisions' list")
    return data


def cmd_execute(cfg: dict, decisions_path: str, broker_name: str | None, date_str: str) -> None:
    """Execute BUY decisions, maintain stops, and write the EOD report."""
    from data.kalshi import get_macro_risk
    from reports.generator import generate_daily_report, write_report
    from risk.manager import RiskManager
    from strategies.stock_picker import initial_stops, update_trailing_stop

    data = _load_decisions(Path(decisions_path))
    if data is None:
        return
    decisions: list[dict] = data["decisions"]
    log.info("execute %s: %d decision(s) from %s", date_str, len(decisions), decisions_path)

    broker = _init_broker(cfg, broker_name)
    state_dir = configured_path(cfg, "state_dir")
    risk = RiskManager(cfg, str(state_dir))
    s1 = cfg["strategy1"]

    account = broker.get_account()
    equity = float(account.get("equity", 0.0))
    day_pnl = _day_pnl(cfg, date_str, equity)
    positions = broker.get_positions()
    n_open = len(positions)
    open_symbols = {p["symbol"] for p in positions}
    log.info(
        "account equity=%.2f cash=%.2f day_pnl=%.2f open=%d",
        equity, account.get("cash"), day_pnl, n_open,
    )

    trailing_path = state_dir / _TRAILING_STATE_FILE
    trailing = _read_json(trailing_path) or {}

    # --- new entries -----------------------------------------------------
    for d in decisions:
        if not isinstance(d, dict):
            log.warning("skipping malformed decision entry: %r", d)
            continue
        symbol = d.get("symbol")
        if d.get("decision") != "BUY":
            log.info("PASS %s — no action", symbol)
            continue
        if not symbol:
            log.warning("skipping BUY decision without symbol")
            continue
        if symbol in open_symbols:
            log.info("skip %s: already open", symbol)
            continue

        conviction = float(d.get("conviction", cfg["council"].get("min_conviction", 60)))
        price, atr = _refresh_atr_price(cfg, symbol)
        if price is None:
            continue

        qty = risk.position_size_shares(equity, conviction, price, atr)
        if qty <= 0:
            log.info("skip %s: position size 0", symbol)
            continue

        ok, reason = risk.check_pre_trade(symbol, "buy", qty, price, account, n_open, day_pnl)
        if not ok:
            log.warning("pre-trade BLOCK %s: %s", symbol, reason)
            continue

        atr_mult = float(d.get("trailing_atr_mult") or s1.get("atr_trailing_mult", 2.5))
        r_mult = float(d.get("take_profit_r") or s1.get("take_profit_r", 2.0))
        stops = initial_stops(price, atr, atr_mult, r_mult)
        broker.submit_bracket(symbol, qty, "buy", stops["stop"], stops["take_profit"], price)
        trailing[symbol] = {
            "stop": stops["stop"],
            "take_profit": stops["take_profit"],
            "atr_mult": atr_mult,
            "entry_price": price,
            "entry_date": date_str,
        }
        n_open += 1
        open_symbols.add(symbol)
        account = broker.get_account()
        equity = float(account.get("equity", 0.0))
        log.info(
            "BUY %s qty=%d entry=%.2f stop=%.2f tp=%.2f conviction=%.0f",
            symbol, qty, price, stops["stop"], stops["take_profit"], conviction,
        )

    _write_json(trailing_path, trailing)

    # --- trailing-stop / time-stop maintenance pass ----------------------
    positions = broker.get_positions()
    for pos in positions:
        symbol = pos["symbol"]
        entry = trailing.get(symbol, {})
        atr_mult = float(entry.get("atr_mult", s1.get("atr_trailing_mult", 2.5)))
        current_stop = entry.get("stop")
        if current_stop is None:
            # No tracked stop (e.g. position predates trailing state): seed it
            # from the entry price so the ratchet has a starting point.
            current_stop = float(pos["avg_entry_price"]) - atr_mult * 0.0 - 1e-9
            log.info("no tracked stop for %s; seeding ratchet from entry", symbol)

        price, atr = _refresh_atr_price(cfg, symbol)
        if price is None:
            continue
        highest_high = _highest_high(cfg, symbol)
        if highest_high is None:
            continue

        new_stop = update_trailing_stop(float(current_stop), highest_high, atr, atr_mult)
        if new_stop > float(current_stop) + 1e-9:
            _apply_stop(broker, symbol, new_stop)
            trailing.setdefault(symbol, {})["stop"] = new_stop
            trailing[symbol].setdefault("entry_date", date_str)
            log.info("trailing stop %s: %.2f -> %.2f", symbol, current_stop, new_stop)
        else:
            log.info("trailing stop %s unchanged at %.2f", symbol, current_stop)

        # time stop
        max_days = int(s1.get("time_stop_days", 10))
        entry_date = trailing.get(symbol, {}).get("entry_date")
        if entry_date:
            held = (datetime.strptime(date_str, "%Y-%m-%d").date()
                    - datetime.strptime(entry_date, "%Y-%m-%d").date()).days
            if held >= max_days:
                log.info("time stop %s: held %d days >= %d — closing", symbol, held, max_days)
                broker.close_position(symbol)
                trailing.pop(symbol, None)

    _write_json(trailing_path, trailing)

    # --- EOD report ------------------------------------------------------
    account = broker.get_account()
    positions = broker.get_positions()
    equity = float(account.get("equity", 0.0))
    day_pnl = _day_pnl(cfg, date_str, equity)
    risk_status = {
        "kill_switch_active": risk.kill_switch_active(),
        "daily_loss_halted": risk.daily_loss_halted(day_pnl, equity),
        "n_open": len(positions),
        "max_positions": int(cfg["risk"].get("max_positions", 8)),
        "day_pnl": round(day_pnl, 2),
    }
    macro_risk = get_macro_risk()
    markdown = generate_daily_report(
        date=date_str,
        account=account,
        positions=positions,
        decisions=data,
        risk_status=risk_status,
        macro_risk=macro_risk,
    )
    daily_dir = configured_path(cfg, "daily_dir")
    report_path = write_report(date_str, markdown, daily_dir)
    log.info("wrote EOD report %s", report_path)

    _write_json(state_dir / _LAST_EQUITY_FILE, {"date": date_str, "equity": equity})


def _highest_high(cfg: dict, symbol: str) -> float | None:
    """Highest high over the trailing window (approx 'since entry')."""
    from data.market import MarketData

    day = datetime.strptime(today_et(), "%Y-%m-%d").date()
    market = MarketData(feed=cfg["broker"].get("data_feed", "iex"))
    df = market.get_bars(
        symbol,
        (day - timedelta(days=_TRAILING_LOOKBACK_DAYS)).isoformat(),
        day.isoformat(),
    )
    if df.empty:
        return None
    return float(df["high"].tail(_TRAILING_HIGH_WINDOW).max())


def _apply_stop(broker, symbol: str, new_stop: float) -> None:
    """Apply a ratcheted stop.

    SimulatedBroker: updated in-memory (via its public update_stop hook
    when present, else its internal position record — same-repo adapter).
    AlpacaPaperBroker: live order-replace is a v2 item, so we log the
    recommended action instead of touching real orders.
    """
    updater = getattr(broker, "update_stop", None)
    if callable(updater):
        updater(symbol, new_stop)
        log.info("stop updated in-memory for %s -> %.2f", symbol, new_stop)
        return
    internal = getattr(broker, "_positions", None)
    if isinstance(internal, dict) and symbol in internal:
        internal[symbol]["stop"] = float(new_stop)
        log.info("stop updated in-memory for %s -> %.2f (sim)", symbol, new_stop)
        return
    log.warning(
        "RECOMMENDED stop update for %s -> %.2f "
        "(live order-replace is a v2 item — not applied)",
        symbol, new_stop,
    )


# --------------------------------------------------------------------------
# full: scan -> council -> execute
# --------------------------------------------------------------------------
_COUNCIL_STUB_NOTE = (
    "Council step pending. Run the council per council/runbook.md, then re-run "
    "with --execute."
)


def _council_instructions(cfg: dict, date_str: str, dossiers_path: Path, decisions_path: Path) -> str:
    return (
        "COUNCIL STEP REQUIRED (council.execution=%r)\n"
        "The scan is done; the council turns candidates into BUY/PASS decisions.\n"
        "1. Read %s  (ranked candidates; each entry carries its inline dossier)\n"
        "2. Run the council per council/runbook.md: the 5 analyst roles in parallel,\n"
        "   then devil's advocate, then the chair. Every decision must include\n"
        "   conviction, disagreement_notes, independent_lineages_considered, and\n"
        "   reasoning, conforming to council/decisions_schema.json.\n"
        "3. Write %s\n"
        "4. Advisor mode (selected):  python -m pipeline.daily --advisor %s "
        "--dossiers %s\n"
        "   (legacy autonomous: --execute %s — NOT the selected mode)\n"
        % (
            cfg["council"].get("execution"),
            dossiers_path,
            decisions_path,
            decisions_path,
            dossiers_path,
            decisions_path,
        )
    )


def _write_stub_decisions(cfg: dict, date_str: str) -> Path:
    decisions_dir = configured_path(cfg, "decisions_dir")
    out_path = decisions_dir / f"{date_str}.json"
    _write_json(out_path, {"date": date_str, "status": "PENDING_COUNCIL", "note": _COUNCIL_STUB_NOTE})
    log.info("wrote council stub %s", out_path)
    return out_path


def _run_panel_council(cfg: dict, date_str: str, ranked: list[dict]) -> dict | None:
    """Try the in-process LLM panel. Returns the decisions dict, or None on
    any failure (caller falls back to the stub path — cron-safe)."""
    try:
        from council.panel import CouncilPanel, run_council
    except Exception as exc:
        log.warning("council.panel unavailable (%s: %s); using stub path",
                    type(exc).__name__, exc)
        return None
    try:
        panel = CouncilPanel(cfg["council"].get("panel") or {})
        dossiers = [r["dossier"] for r in ranked if isinstance(r, dict) and r.get("dossier")]
        result = run_council(
            panel, dossiers, prompts_dir=str(resolve("council", "prompts")), date=date_str
        )
        if not isinstance(result, dict) or "decisions" not in result:
            raise ValueError("run_council returned an unexpected payload")
        return result
    except Exception as exc:
        log.error("panel council failed (%s: %s); using stub path", type(exc).__name__, exc)
        return None


def cmd_full(cfg: dict, broker_name: str | None, date_str: str) -> None:
    dossiers_path = cmd_scan(cfg, date_str)
    with open(dossiers_path, "r", encoding="utf-8") as fh:
        ranked = json.load(fh).get("ranked", [])

    decisions_dir = configured_path(cfg, "decisions_dir")
    decisions_path = decisions_dir / f"{date_str}.json"

    if cfg["council"].get("execution") == "panel":
        result = _run_panel_council(cfg, date_str, ranked)
        if result is not None:
            _write_json(decisions_path, result)
            log.info("wrote panel decisions %s", decisions_path)
            cmd_execute(cfg, str(decisions_path), broker_name, date_str)
            return
        # panel failed -> stub path below

    decisions_path = _write_stub_decisions(cfg, date_str)
    instructions = _council_instructions(cfg, date_str, dossiers_path, decisions_path)
    print(instructions)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pipeline.daily",
        description="council-trader daily pipeline (paper trading only).",
    )
    ap.add_argument("--date", default=None, help="session date YYYY-MM-DD (default: today, ET)")
    ap.add_argument("--config", default=None, help="path to config.yaml (default: repo root)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan-only", action="store_true", help="scan universe and write dossiers file")
    group.add_argument("--execute", metavar="DECISIONS_JSON", help="execute a council decisions file")
    group.add_argument("--full", action="store_true", help="scan, resolve council step, execute")
    group.add_argument("--advisor", metavar="DECISIONS_JSON",
                       help="advisor mode: build manual-execution trade cards from a "
                            "council decisions JSON file. Never touches broker/; nothing is executed. "
                            "Pair with --dossiers for extended-hours entry prices.")
    group.add_argument("--full-advisor", action="store_true",
                       help="scan only (same as --scan-only); then run the council step via the "
                            "scheduled agent and feed its decisions file to --advisor.")
    ap.add_argument("--dossiers", metavar="DOSSIERS_JSON", default=None,
                    help="dossiers JSON file (from --scan-only) used by --advisor for pricing.")
    ap.add_argument("--scan-session", default="morning",
                    choices=["morning", "midday", "late"],
                    help="which scan session this advisor run belongs to "
                         "(journal attribution; default: morning)")
    ap.add_argument("--broker", choices=["simulated", "alpaca"], default=None,
                    help="broker override (default: config broker.mode)")
    return ap


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        date_str = today_et(args.date)
        if args.scan_only:
            cmd_scan(cfg, date_str)
        elif args.execute:
            cmd_execute(cfg, args.execute, args.broker, date_str)
        elif args.full:
            cmd_full(cfg, args.broker, date_str)
        elif args.advisor:
            from pipeline.advisor import cmd_advisor

            cmd_advisor(cfg, args.advisor, args.dossiers, date_str,
                        scan_session=args.scan_session)
        elif args.full_advisor:
            cmd_scan(cfg, date_str)
            print("ADVISOR NEXT STEP: run the council step (scheduled agent) on the "
                  "dossiers file above, then: "
                  "python -m pipeline.daily --advisor <decisions.json> "
                  "--dossiers <dossiers.json>")
        return 0
    except Exception:
        log.exception("pipeline failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
