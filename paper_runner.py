"""Paper Trading Runner — continuously monitors setups and manages paper trades.

Runs alongside scanner_runner.py. On each cycle:
1. Fetches current prices for all symbols with active setups + open positions.
2. Checks exits for open positions (TP/SL/trailing/expire).
3. Scans for new entries (READY_TO_TRADE setups in entry zone).
4. Saves account snapshots.
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
from app.scanners.expectancy_filter import ExpectancyFilter, filter_candidates, load_expectancy

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "paper_trading.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("paper_runner")

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
    cursor = repo._conn.cursor()
    cursor.execute(
        """
        SELECT s.setup_id, i.symbol, s.scanner_name, s.direction, s.score,
               s.entry_zone_low, s.entry_zone_high, s.invalidation_price,
               s.target_1, s.target_2, s.market_regime, s.detected_at,
               s.reference_price
        FROM dds.scanner_setup s
        JOIN dds.instrument i ON i.instrument_id = s.instrument_id
        WHERE s.status = 'READY_TO_TRADE'
          AND s.detected_at > now() - interval '2 hours'
        ORDER BY s.score DESC
        """
    )
    rows = cursor.fetchall()
    return [
        {
            "setup_id": r[0], "symbol": r[1], "scanner_name": r[2],
            "direction": r[3], "score": float(r[4]),
            "entry_zone_low": float(r[5]) if r[5] else 0.0,
            "entry_zone_high": float(r[6]) if r[6] else 0.0,
            "invalidation_price": float(r[7]) if r[7] else 0.0,
            "target_1": float(r[8]) if r[8] else None,
            "target_2": float(r[9]) if r[9] else None,
            "market_regime": r[10],
            "detected_at": r[11],
            "reference_price": float(r[12]) if r[12] else 0.0,
        }
        for r in rows
    ]


def run_cycle(
    engine: PaperTradingEngine,
    client: BybitClient,
    repo: ScannerRepository,
    expectancy_filter: ExpectancyFilter | None,
    settings: Settings,
) -> dict[str, Any]:
    """Run one paper trading cycle: exits → entries → snapshot.

    Does NOT re-scan via Bybit API. Reads READY_TO_TRADE setups from DB
    and only fetches current prices for entry-zone / exit checks.
    """
    stats = {"exits": 0, "entries": 0, "skipped_no_setup": 0, "expectancy_rejected": 0, "emergency_stop": 0}

    # --- 1. CHECK EXITS (monitor existing open positions) ---
    open_symbols = list(engine.open_trades.keys())
    if open_symbols:
        prices = _get_prices(client, open_symbols)
        funding_rates = _get_funding_rates(client, open_symbols)
        closed = engine.check_exits(prices, funding_rates)
        stats["exits"] = len(closed)

    # --- 2. CHECK ENTRIES (read from DB, fetch prices only) ---
    if _emergency_stop_requested(settings):
        stats["emergency_stop"] = 1
        logger.critical("paper emergency stop is active: new entries are disabled")
        ready_setups = []
    else:
        try:
            ready_setups = _load_ready_setups(repo)
        except Exception:
            logger.exception("failed to load READY_TO_TRADE setups")
            ready_setups = []

    candidates = []
    all_prices: dict[str, float] = {}
    if not ready_setups:
        stats["skipped_no_setup"] = 1
    else:
        # Deduplicate symbols and fetch current entry prices.
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

        opened = engine.check_entries(candidates, all_prices)
        stats["entries"] = len(opened)

    # --- 3. EXPIRE OLD SETUPS ---
    try:
        expired = repo.expire_stale_setups(max_age_minutes=120)
        if expired:
            logger.info("expired %d stale setups", expired)
    except Exception:
        logger.exception("failed to expire setups")

    # --- 4. ACCOUNT SNAPSHOT ---
    try:
        stats_rows = repo.get_paper_trade_stats()
        total_trades = sum(s.get("total_trades", 0) or 0 for s in stats_rows)
        winning = sum(s.get("wins", 0) or 0 for s in stats_rows)
        losing = sum(s.get("losses", 0) or 0 for s in stats_rows)
        total_pnl = sum(s.get("total_pnl_usdt", 0) or 0 for s in stats_rows)

        repo.save_paper_account_snapshot(
            balance=engine.balance,
            equity=engine.balance,
            open_positions=len(engine.open_trades),
            total_trades=total_trades,
            winning_trades=winning,
            losing_trades=losing,
            total_pnl=total_pnl,
            max_drawdown=engine._max_drawdown,
        )
    except Exception:
        logger.exception("failed to save account snapshot")

    return stats


def main() -> None:
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
    repo.ensure_schema()

    # Load expectancy filter
    expectancy_filter = None
    if settings.expectancy_filter_enabled:
        expectancy_filter = load_expectancy(repo)
        logger.info(
            "expectancy filter loaded: %d records",
            len(expectancy_filter.records),
        )

    engine = PaperTradingEngine(settings, repo)
    logger.info(
        "paper runner started: balance=$%.2f positions=%d",
        engine.balance, len(engine.open_trades),
    )

    cycle = 0
    interval = settings.paper_scan_interval  # Default 300s (5 minutes)

    while not SHUTDOWN:
        cycle += 1
        start = time.monotonic()

        # Keepalive ping to prevent idle connection timeout
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
            stats = run_cycle(engine, client, repo, expectancy_filter, settings)
            logger.info(
                "cycle #%d: entries=%d exits=%d open=%d balance=$%.2f",
                cycle, stats["entries"], stats["exits"],
                len(engine.open_trades), engine.balance,
            )
        except Exception:
            logger.exception("cycle #%d failed", cycle)
            # Attempt reconnect after DB connection drop
            if repo.reconnect():
                logger.info("paper runner reconnected to PostgreSQL")
                if settings.expectancy_filter_enabled:
                    expectancy_filter = load_expectancy(repo)

        # Sleep in small intervals to respond to shutdown
        delay = max(0, interval - (time.monotonic() - start))
        for _ in range(int(delay)):
            if SHUTDOWN:
                break
            time.sleep(1)

    # Save final snapshot
    try:
        snap = engine.snapshot()
        logger.info("paper runner shutting down: %s", snap)
    except Exception:
        pass

    repo.close()
    logger.info("paper runner stopped")


if __name__ == "__main__":
    main()
