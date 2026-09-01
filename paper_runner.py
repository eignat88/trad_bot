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
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.paper.engine import PaperTradingEngine
from app.paper.position_monitor import PositionMonitor
from app.scanners.expectancy_filter import ExpectancyFilter, filter_candidates, load_expectancy

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

    # --- Create and start the FAST position monitor (background thread) ---
    monitor_interval = getattr(settings, "position_monitor_interval", 10)
    monitor = PositionMonitor(
        engine=engine,
        price_fetcher=lambda symbols: _get_prices(client, symbols),
        funding_fetcher=lambda symbols: _get_funding_rates(client, symbols),
        interval_seconds=monitor_interval,
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

    def _shutdown_check() -> bool:
        return SHUTDOWN

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
