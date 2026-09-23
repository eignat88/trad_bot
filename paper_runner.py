"""Paper Trading Runner — continuously monitors setups and manages paper trades.

Runs alongside scanner_runner.py.  Two independent loops:

  **Fast loop** (position monitor, ~10 s):
    Fetches prices for open positions and checks SL/TP/trailing/breakeven.
    Runs in a dedicated daemon thread so that stop-loss monitoring is never
    delayed by the slower entry cycle.

  **Slow loop** (main thread, ~300 s):
    Reads READY_TO_TRADE setups from the DB, opens new paper positions,
    expires stale setups, and saves account snapshots.

Separating the two loops eliminates the root cause of systematic
STOP_LOSS_GAP events: with a 5-minute check cycle almost every stop hit
gapped through the level.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, load_settings
from app.config.settings import get_dca_source
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.paper.engine import PaperTradingEngine
from app.paper.position_monitor import PositionMonitor
from app.paper.shadow_engine import ShadowPaperEngine
from app.scanners.direction_gate import ScannerDirectionGatePolicy
from app.scanners.expectancy_filter import ExpectancyFilter, filter_candidates, load_expectancy
from app.scanners.orchestrator import ScannerOrchestrator

# ME_R_LONG Early MAE Exit shadow experiment (observe-only)
from app.shadow.me_long_early_exit_shadow import (
    MELongEarlyExitShadowObserver,
    SCANNER_NAME as ME_EARLY_EXIT_SCANNER,
)

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
LOG_DIR = PROJECT_ROOT / "logs"
logger = logging.getLogger("paper_runner")


def setup_logging() -> None:
    """Configure runner logging only when the executable is started."""
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "paper_trading.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

SHUTDOWN = False

# --- ME_R_LONG Early MAE Exit shadow experiment (singleton) ---
_me_early_exit_observer: MELongEarlyExitShadowObserver | None = None


def _get_me_early_exit_observer(repo: ScannerRepository) -> MELongEarlyExitShadowObserver:
    """Lazy-init the ME_R_LONG early exit shadow observer."""
    global _me_early_exit_observer
    if _me_early_exit_observer is None:
        _me_early_exit_observer = MELongEarlyExitShadowObserver(repo)
    return _me_early_exit_observer


def _observe_me_early_exit_on_open(
    repo: ScannerRepository,
    engine: PaperTradingEngine,
    opened_trades: list,
) -> None:
    """Create shadow observations for newly opened ME_R_LONG trades.

    Called after engine.check_entries() to observe each matching trade.
    NEVER blocks the trade — fire-and-forget observation.
    """
    observer = _get_me_early_exit_observer(repo)
    for trade in opened_trades:
        if (trade.scanner_name == ME_EARLY_EXIT_SCANNER
                and trade.direction == "LONG"):
            try:
                observer.on_trade_open(
                    trade_id=trade.trade_id,
                    symbol=trade.symbol,
                    entered_at=trade.entered_at,
                    entry_price=trade.entry_price,
                    stop_price=trade.stop_price,
                )
            except Exception:
                logger.debug(
                    "ME_R_LONG early exit shadow: on_trade_open failed "
                    "for trade_id=%s",
                    trade.trade_id,
                    exc_info=True,
                )


def _evaluate_me_early_exit_variants(
    repo: ScannerRepository,
    engine: PaperTradingEngine,
) -> None:
    """Periodically evaluate ME_R_LONG early exit variants for open trades.

    Queries market.candle data to compute MAE at each variant's evaluation
    horizon and checks if the early-exit rule triggers.
    """
    observer = _get_me_early_exit_observer(repo)
    now = datetime.now(timezone.utc)

    for symbol, trade in engine.open_trades.items():
        if (trade.scanner_name != ME_EARLY_EXIT_SCANNER
                or trade.direction != "LONG"
                or trade.trade_id is None):
            continue

        # Resolve instrument_id
        instrument_id = None
        if repo._use_pg:
            instrument_id = repo.ensure_instrument(trade.symbol)

        if instrument_id is None:
            continue

        try:
            observer.evaluate_variants(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                instrument_id=instrument_id,
                entered_at=trade.entered_at,
                entry_price=trade.entry_price,
                stop_price=trade.stop_price,
                current_time=now,
            )
        except Exception:
            logger.debug(
                "ME_R_LONG early exit shadow: evaluate_variants failed "
                "for trade_id=%s",
                trade.trade_id,
                exc_info=True,
            )


def _notify_me_early_exit_on_close(
    repo: ScannerRepository,
    closed_trades: list,
) -> None:
    """Notify the ME_R_LONG early exit shadow observer when paper trades close.

    Called from the position monitor after trades are closed.
    """
    observer = _get_me_early_exit_observer(repo)
    for trade in closed_trades:
        if (trade.scanner_name == ME_EARLY_EXIT_SCANNER
                and trade.direction == "LONG"
                and trade.trade_id is not None):
            try:
                observer.on_trade_close(
                    trade_id=trade.trade_id,
                    actual_pnl_r=trade.net_pnl / (
                        abs(trade.entry_price - trade.stop_price) * trade.position_size
                        if abs(trade.entry_price - trade.stop_price) > 0
                        and trade.position_size > 0
                        else 1.0
                    ),
                    actual_exit_reason=trade.exit_reason
                        if hasattr(trade, 'exit_reason') else None,
                )
            except Exception:
                logger.debug(
                    "ME_R_LONG early exit shadow: on_trade_close failed "
                    "for trade_id=%s",
                    trade.trade_id,
                    exc_info=True,
                )


def _finalize_me_early_exit_closes(repo: ScannerRepository) -> None:
    """Poll for recently closed ME_R_LONG paper trades missing shadow close.

    This is a resilient fallback that queries the DB for closed paper trades
    whose shadow observations are still in EVALUATED status (not yet CLOSED).
    Called every slow cycle (~300s).
    """
    if not repo._use_pg:
        return

    observer = _get_me_early_exit_observer(repo)

    sql = """
    SELECT DISTINCT pt.trade_id, pt.pnl_r, pt.exit_reason
    FROM dds.paper_trade pt
    JOIN dds.me_r_long_early_exit_observation obs
        ON obs.trade_id = pt.trade_id
        AND obs.experiment_id = %(experiment_id)s
    WHERE pt.scanner_name = %(scanner_name)s
      AND pt.direction = 'LONG'
      AND pt.status = 'CLOSED'
      AND obs.status = 'EVALUATED'
      AND pt.closed_at > now() - interval '1 hour'
    """
    try:
        cursor = repo._execute(sql, {
            "experiment_id": "ME_R_LONG_EARLY_MAE_EXIT_OOS_V1",
            "scanner_name": ME_EARLY_EXIT_SCANNER,
        })
        rows = cursor.fetchall() if cursor else []
        for row in rows:
            trade_id = row[0]
            pnl_r = float(row[1]) if row[1] is not None else 0.0
            exit_reason = row[2]
            observer.on_trade_close(
                trade_id=trade_id,
                actual_pnl_r=pnl_r,
                actual_exit_reason=exit_reason,
            )
    except Exception:
        logger.debug("ME_R_LONG early exit shadow finalize query failed", exc_info=True)


def _handle_signal(signum, frame):
    global SHUTDOWN
    logger.info("shutdown signal received")
    SHUTDOWN = True


def _load_settings() -> Settings:
    settings = load_settings(path=CONFIG_PATH, env_file=ENV_PATH)
    logger.info(
        "paper config: initial_balance=%.0f risk_per_trade=%.3f max_positions=%d",
        settings.initial_balance, settings.risk_per_trade,
        settings.max_open_positions,
    )
    # Log effective DCA configuration with source
    dca = settings.dca
    dca_source = get_dca_source()
    logger.info(
        "DCA config: enabled=%s source=%s level_atr=%.2f "
        "initial_entry_pct=%.2f dca_entry_pct=%.2f exit_mode=%s "
        "stop_loss_atr=%.2f max_dca_count=%d",
        dca.enabled, dca_source, dca.level_atr,
        dca.initial_entry_pct, dca.dca_entry_pct, dca.exit_mode,
        dca.stop_loss_atr, dca.max_dca_count,
    )
    # Log execution policies
    sources = getattr(settings, "_execution_policy_sources", {})
    for scanner_name, directions in settings.execution_policy_configs.items():
        for direction, policy in directions.items():
            source = sources.get((scanner_name, direction), "CONFIG")
            logger.info(
                "Execution policy: scanner=%s direction=%s policy=%s enabled=%s "
                "hold_minutes=%d dca=%s trailing=%s tp=%s expiry=%s "
                "config_source=%s",
                scanner_name, direction, policy.policy, policy.enabled,
                policy.hold_minutes, policy.dca_enabled,
                policy.trailing_enabled, policy.tp_enabled, policy.expiry_enabled,
                source,
            )
    return settings


def _get_prices(client: BybitClient, symbols: list[str]) -> dict[str, float]:
    """Fetch latest close prices for a list of symbols."""
    prices: dict[str, float] = {}
    try:
        for ticker in client.get_tickers("linear"):
            sym = ticker.get("symbol", "")
            if sym in symbols:
                try:
                    prices[sym] = float(ticker["lastPrice"])
                except (KeyError, TypeError, ValueError):
                    pass
    except Exception:
        logger.exception("failed to fetch prices")
    return prices


def _get_funding_rates(client: BybitClient, symbols: list[str]) -> dict[str, float]:
    """Fetch the latest realized funding rate percentage for open positions."""
    rates: dict[str, float] = {}
    for symbol in symbols:
        try:
            rates[symbol] = client.get_funding_rate(symbol)
        except Exception:
            logger.exception("failed to fetch funding rate for %s", symbol)
    return rates


def _emergency_stop_requested(settings: Settings) -> bool:
    """Return True when the operator-created stop file blocks new entries."""
    stop_file = Path(settings.paper_emergency_stop_file)
    if not stop_file.is_absolute():
        stop_file = PROJECT_ROOT / stop_file
    return stop_file.exists()


def _load_ready_setups(repo: ScannerRepository) -> list[dict]:
    """Load READY_TO_TRADE setups directly from DB — no re-scanning."""
    return repo.load_ready_setups()


def run_entry_cycle(
    engine: PaperTradingEngine,
    client: BybitClient,
    repo: ScannerRepository,
    expectancy_filter: ExpectancyFilter | None,
    settings: Settings,
    shadow_engine: ShadowPaperEngine | None = None,
) -> dict[str, Any]:
    """Run one paper-trading entry cycle (runs every ~300 s).

    Does NOT check exits — that is handled by the fast position monitor
    thread.  Reads READY_TO_TRADE setups from DB, checks entry zones,
    and saves account snapshots.

    Each DB block is independently error-protected to prevent cascading
    SQLSTATE 25P02 errors from a single transient failure.
    """
    stats: dict[str, Any] = {
        "entries": 0, "skipped_no_setup": 0,
        "expectancy_rejected": 0, "emergency_stop": 0,
    }

    # --- 1. CHECK ENTRIES (read from DB, fetch prices only) ---
    ready_setups: list[dict] = []
    if _emergency_stop_requested(settings):
        stats["emergency_stop"] = 1
        logger.critical("paper emergency stop is active: new entries are disabled")
    else:
        try:
            ready_setups = _load_ready_setups(repo)
        except Exception:
            logger.exception("failed to load READY_TO_TRADE setups")
            ready_setups = []

    candidates = []
    if not ready_setups:
        stats["skipped_no_setup"] = 1
    else:
        needed_symbols = list({s["symbol"] for s in ready_setups} | set(engine.open_trades.keys()))
        all_prices = _get_prices(client, needed_symbols)

        from app.scanners.models import SetupCandidate
        candidates = [
            SetupCandidate(
                setup_id=s["setup_id"],
                scanner_name=s["scanner_name"],
                symbol=s["symbol"],
                direction=s["direction"],
                score=s["score"],
                entry_zone_low=s["entry_zone_low"],
                entry_zone_high=s["entry_zone_high"],
                invalidation_price=s["invalidation_price"],
                target_1=s["target_1"],
                target_2=s["target_2"],
                market_regime=s["market_regime"],
                reference_price=s["reference_price"],
                entry_timeframe=s["entry_timeframe"],
            )
            for s in ready_setups
        ]

        # Re-check at entry time: an operator gate update is applied on the
        # next paper cycle even to setups that were READY before the update.
        gate_policy = ScannerDirectionGatePolicy.load_for_cycle(
            repo,
            scanner_names=ScannerOrchestrator().scanners.keys(),
            blocked_combinations=settings.blocked_scanner_directions,
            regime_whitelist=settings.scanner_regime_whitelist,
        )
        gated_candidates = []
        for candidate in candidates:
            decision = gate_policy.evaluate(
                candidate.scanner_name, candidate.direction, candidate.market_regime
            )
            if decision.allowed:
                gated_candidates.append(candidate)
            else:
                logger.info(
                    "paper entry direction gate rejected: setup=%s scanner=%s direction=%s reason_code=%s",
                    candidate.setup_id, candidate.scanner_name, candidate.direction, decision.reason_code,
                )
        candidates = gated_candidates

        if expectancy_filter is not None:
            candidates, rejected = filter_candidates(
                candidates,
                expectancy_filter,
                min_avg_r=settings.expectancy_min_avg_r,
                min_samples=settings.expectancy_min_samples,
                blocked_combinations=frozenset(settings.blocked_scanner_directions),
                trading_mode=settings.trading_mode,
            )
            stats["expectancy_rejected"] = rejected

        # Acquire lock so the position monitor thread is not mutating
        # open_trades while we open new positions.
        with engine.trading_lock:
            opened = engine.check_entries(candidates, all_prices)
        stats["entries"] = len(opened)

        # --- ME_R_LONG Early Exit shadow: observe new entries ---
        if opened:
            try:
                _observe_me_early_exit_on_open(repo, engine, opened)
            except Exception:
                logger.debug("ME_R_LONG early exit shadow open hook failed", exc_info=True)

        # --- SHADOW ENGINE: create reverse LONG shadow for ME SHORT entries ---
        if shadow_engine is not None and shadow_engine.is_enabled and opened:
            shadow_entries = 0
            for trade in opened:
                price = all_prices.get(trade.symbol)
                if price is None:
                    continue
                shadow_trade = shadow_engine.check_shadow_entries(trade, price)
                if shadow_trade is not None:
                    shadow_entries += 1
            if shadow_entries > 0:
                stats["shadow_entries"] = shadow_entries
                logger.info(
                    "shadow engine: %d reverse LONG shadow trades created this cycle",
                    shadow_entries,
                )

    # --- 1b. ME_R_LONG Early Exit shadow: evaluate open trades ---
    try:
        _evaluate_me_early_exit_variants(repo, engine)
    except Exception:
        logger.debug("ME_R_LONG early exit shadow evaluation hook failed", exc_info=True)

    # --- 1c. ME_R_LONG Early Exit shadow: close observations for recently closed trades ---
    try:
        _finalize_me_early_exit_closes(repo)
    except Exception:
        logger.debug("ME_R_LONG early exit shadow close hook failed", exc_info=True)

    # --- 2. EXPIRE OLD SETUPS (independently protected) ---
    try:
        expired = repo.expire_stale_setups(max_age_minutes=120)
        if expired:
            logger.info("expired %d stale setups", expired)
    except Exception:
        logger.exception("failed to expire setups")

    # --- 3. ACCOUNT SNAPSHOT (independently protected) ---
    try:
        stats_rows = repo.get_paper_trade_stats()
        total_trades = sum(s.get("total_trades", 0) or 0 for s in stats_rows)
        winning = sum(s.get("wins", 0) or 0 for s in stats_rows)
        losing = sum(s.get("losses", 0) or 0 for s in stats_rows)
        total_pnl = sum(s.get("total_pnl_usdt", 0) or 0 for s in stats_rows)

        with engine.trading_lock:
            repo.save_paper_account_snapshot(
                balance=engine.balance,
                equity=engine.balance,
                open_positions=len(engine.open_trades),
                total_trades=total_trades,
                winning_trades=winning,
                losing_trades=losing,
                total_pnl=total_pnl,
                max_drawdown=engine._max_drawdown,
                cooldown_until=engine._cooldown_until,
            )
    except Exception:
        logger.exception("failed to save account snapshot")

    return stats


def run_cycle(
    engine: PaperTradingEngine,
    client: BybitClient,
    repo: ScannerRepository,
    expectancy_filter: ExpectancyFilter | None,
    settings: Settings,
) -> dict[str, Any]:
    """Run one slow paper cycle (compatibility entry point).

    Position exits are deliberately handled by :class:`PositionMonitor` in the
    fast background loop.  Keeping this public wrapper preserves callers that
    invoke a cycle directly while ensuring they use the same locked entry,
    expiry, and snapshot path as the production runner.
    """
    return run_entry_cycle(engine, client, repo, expectancy_filter, settings)


def main() -> None:
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    settings = _load_settings()
    client = BybitClient(settings)
    repo = ScannerRepository(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        backend="postgres",
    )

    # Verify DB connectivity
    if not repo.ping():
        logger.error("database health check failed")
        repo.close()
        raise SystemExit("database health check failed")

    # Load expectancy filter
    expectancy_filter = None
    if settings.expectancy_filter_enabled:
        expectancy_filter = load_expectancy(repo)
        logger.info(
            "expectancy filter loaded: %d records",
            len(expectancy_filter.records),
        )

    engine = PaperTradingEngine(settings, repo)

    # --- Initialize shadow engine for experimental scanners ---
    shadow_engine = None
    me_reverse_config = settings.experimental_scanners.get("ME_SHORT_REVERSE_LONG_V1")
    if me_reverse_config is not None and me_reverse_config.enabled:
        shadow_engine = ShadowPaperEngine(settings, repo, me_reverse_config)
        logger.info(
            "shadow engine initialized: experiment=%s mode=%s "
            "source_scanner=%s source_direction=%s trade_direction=%s "
            "SL=%.1f%% TP=%.1f%% open_trades=%d",
            "ME_SHORT_REVERSE_LONG_V1", me_reverse_config.mode,
            me_reverse_config.source_scanner, me_reverse_config.source_direction,
            me_reverse_config.trade_direction,
            me_reverse_config.stop_loss_pct, me_reverse_config.take_profit_pct,
            len(shadow_engine.open_trades),
        )
    else:
        logger.info("shadow engine: disabled or not configured")

    # --- Create and start the FAST position monitor (background thread) ---
    monitor_interval = getattr(settings, "position_monitor_interval", 10)
    monitor = PositionMonitor(
        engine=engine,
        price_fetcher=lambda symbols: _get_prices(client, symbols),
        funding_fetcher=lambda symbols: _get_funding_rates(client, symbols),
        interval_seconds=monitor_interval,
        shadow_engine=shadow_engine,
    )
    monitor.start()

    logger.info(
        "paper runner started: balance=$%.2f positions=%d "
        "monitor_interval=%ds paper_cycle=%ds",
        engine.balance, len(engine.open_trades),
        monitor_interval, settings.paper_scan_interval,
    )

    # --- SLOW loop: entry checks + snapshots ---
    cycle = 0
    interval = settings.paper_scan_interval
    last_heartbeat_log = time.monotonic()

    while not SHUTDOWN:
        cycle += 1
        start = time.monotonic()

        # Keepalive ping
        if not repo.ping():
            logger.warning("DB ping failed, attempting reconnect")
            if repo.reconnect():
                logger.info("paper runner reconnected to PostgreSQL")
                if settings.expectancy_filter_enabled:
                    expectancy_filter = load_expectancy(repo)
            else:
                logger.error("reconnect failed, will retry next cycle")
                time.sleep(5)
                continue

        try:
            stats = run_entry_cycle(
                engine, client, repo, expectancy_filter, settings,
                shadow_engine=shadow_engine,
            )
            logger.info(
                "cycle #%d: entries=%d open=%d balance=$%.2f",
                cycle, stats["entries"],
                len(engine.open_trades), engine.balance,
            )
        except Exception:
            logger.exception("cycle #%d failed", cycle)
            if repo.reconnect():
                logger.info("paper runner reconnected to PostgreSQL")
                if settings.expectancy_filter_enabled:
                    expectancy_filter = load_expectancy(repo)

        # --- Independent heartbeat logging every 60 s ---
        now_mono = time.monotonic()
        if now_mono - last_heartbeat_log >= 60:
            hb = monitor.heartbeat
            logger.info(
                "position monitor: status=%s checks=%d closes=%d "
                "stop_gap_24h=%d last_check_age=%.1fs",
                hb["status"], hb["total_checks"], hb["total_closes"],
                hb["stop_gap_24h"],
                hb["last_check_age_sec"] or 0,
            )
            # ME_R_LONG early exit shadow stats
            if _me_early_exit_observer is not None:
                me_stats = _me_early_exit_observer.stats
                logger.info(
                    "ME_R_LONG early exit shadow: observations=%d "
                    "evaluated=%d closed=%d errors=%d",
                    me_stats["observations"], me_stats["evaluated"],
                    me_stats["closed"], me_stats["errors"],
                )
            last_heartbeat_log = now_mono

        # Sleep in small intervals to respond to shutdown
        delay = max(0, interval - (time.monotonic() - start))
        for _ in range(int(delay)):
            if SHUTDOWN:
                break
            time.sleep(1)

    # --- Shutdown ---
    logger.info("stopping position monitor...")
    monitor.stop()

    try:
        with engine.trading_lock:
            snap = engine.snapshot()
        logger.info("paper runner shutting down: %s", snap)
    except Exception:
        pass

    repo.close()
    logger.info("paper runner stopped")


if __name__ == "__main__":
    main()
