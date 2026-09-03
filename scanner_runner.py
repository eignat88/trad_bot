"""Scanner Runner - continuous background scanner that writes to PostgreSQL.

Runs on system startup via Windows Startup folder.
Scans every 5 minutes, saves results to PostgreSQL, logs to file.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.scanners.context_builder import build_market_context
from app.scanners.direction_gate import ScannerDirectionGatePolicy
from app.scanners.expectancy_filter import ExpectancyFilter, load_expectancy
from app.scanners.orchestrator import ScannerOrchestrator

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
LOG_DIR = PROJECT_ROOT / "logs"
logger = logging.getLogger("scanner_runner")


def setup_logging() -> None:
    """Configure runner logging only when the executable is started."""
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "scanner.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

SHUTDOWN = False


def _load_runner_settings() -> Settings:
    """Load settings from files next to this runner, regardless of cwd."""
    settings = load_settings(path=CONFIG_PATH, env_file=ENV_PATH)
    logger.info(
        "scanner config: universe_mode=%s top_n=%s symbols=%s cwd=%s config=%s env_file=%s",
        settings.scanner_universe.mode,
        settings.scanner_universe.top_n,
        list(settings.symbols),
        Path.cwd(),
        CONFIG_PATH,
        ENV_PATH,
    )
    return settings


def _handle_signal(signum, frame):
    global SHUTDOWN
    logger.info("shutdown signal received")
    SHUTDOWN = True


def get_scanner_symbols(client: BybitClient, settings: Settings) -> list[str]:
    """Return the scanner universe selected by the current configuration."""
    return [str(item["symbol"]) for item in get_scanner_universe(client, settings)]


def get_scanner_universe(
    client: BybitClient, settings: Settings,
) -> list[dict[str, Any]]:
    """Return the ranked universe and the metadata used to select it."""
    universe = settings.scanner_universe
    if universe.mode == "dynamic":
        instruments = client.get_liquid_instruments(
            top_n=universe.top_n,
            min_turnover_24h=universe.min_turnover_24h,
            min_volume_24h=universe.min_volume_24h,
            quote_coin=universe.quote_coin,
        )
        if not instruments:
            raise RuntimeError(
                "Dynamic scanner universe is empty; check liquidity thresholds"
            )
        return instruments

    symbols = list(settings.symbols)
    if not symbols:
        raise RuntimeError("Static scanner universe is empty")
    return [{"symbol": symbol, "rank": rank}
            for rank, symbol in enumerate(symbols, start=1)]


def seconds_until_next_cycle(started_at: float, interval: int, now: float) -> float:
    """Return the remaining delay, keeping cycles anchored to their start."""
    return max(0, interval - (now - started_at))


def run_scan_cycle(
    client: BybitClient,
    orchestrator: ScannerOrchestrator,
    repository: ScannerRepository,
    symbols: list[str],
    run_id: int | None,
    settings: Settings | None = None,
    expectancy_filter: ExpectancyFilter | None = None,
) -> tuple[int, int, int]:
    """Returns (total_found, scanned, failed)."""
    settings = settings or _load_runner_settings()
    total_found = 0
    scanned = 0
    failed = 0
    expectancy_rejected = 0
    run_stats = {
        name: {"symbols_scanned": 0, "candidates_found": 0,
               "setups_saved": 0, "errors_count": 0, "duration_ms": 0.0}
        for name in orchestrator.scanners
    }

    if not symbols:
        return 0, 0, 0

    min_avg_r = settings.expectancy_min_avg_r if settings.expectancy_filter_enabled else 0.0
    # One SELECT per scan cycle; DB errors retain Settings as a fail-safe policy.
    gate_policy = ScannerDirectionGatePolicy.load_for_cycle(
        repository,
        scanner_names=orchestrator.scanners.keys(),
        blocked_combinations=settings.blocked_scanner_directions,
        regime_whitelist=settings.scanner_regime_whitelist,
    )
    workers = min(settings.scanner_workers, len(symbols))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-data") as executor:
        futures = {
            executor.submit(build_market_context, client, symbol, settings): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                ctx = future.result()
                scanned += 1
            except Exception:
                logger.exception("market data failed for %s", symbol)
                failed += 1
                repository.save_error(
                    symbol=symbol, scanner_name="ALL",
                    run_id=run_id,
                    error_type="MARKET_DATA_ERROR",
                    error_message=str(sys.exc_info()[1]),
                )
                continue

            try:
                if settings.expectancy_filter_enabled:
                    candidates, symbol_stats = orchestrator.scan_all_with_stats(
                        ctx,
                        expectancy_filter=expectancy_filter,
                        min_avg_r=min_avg_r,
                        min_samples=settings.expectancy_min_samples,
                        gate_policy=gate_policy,
                        regime_filter=settings.regime_filter_enabled,
                        scanner_regime_whitelist=settings.scanner_regime_whitelist,
                        trading_mode=settings.trading_mode,
                    )
                else:
                    candidates, symbol_stats = orchestrator.scan_all_with_stats(
                        ctx,
                        gate_policy=gate_policy,
                        regime_filter=settings.regime_filter_enabled,
                        scanner_regime_whitelist=settings.scanner_regime_whitelist,
                    )
                for name, values in symbol_stats.items():
                    stat = run_stats[name]
                    stat["symbols_scanned"] += 1
                    for field in ("candidates_found", "setups_saved", "errors_count", "duration_ms"):
                        stat[field] += values[field]

                for c in candidates:
                    repository.save_setup(c, run_id=run_id)
                    repository.save_event(
                        "SETUP_DETECTED", c.scanner_name, symbol,
                        run_id=run_id,
                        timeframe=c.setup_timeframe,
                        direction=c.direction,
                        score=c.score,
                        detected_at=c.detected_at,
                        payload={"entry_zone": [c.entry_zone_low, c.entry_zone_high]},
                    )

                total_found += len(candidates)
                if candidates:
                    best = max(candidates, key=lambda c: c.score)
                    logger.info(
                        "%s: %d setups (best: %s %s score=%.0f)",
                        symbol, len(candidates), best.direction,
                        best.scanner_name, best.score,
                    )
            except Exception:
                logger.exception("scan failed for %s", symbol)
                failed += 1
                repository.save_error(
                    symbol=symbol, scanner_name="SCANNER",
                    run_id=run_id,
                    error_type="SCAN_ERROR",
                    error_message=str(sys.exc_info()[1]),
                )

    for scanner_name, values in run_stats.items():
        repository.save_run_stat(run_id, scanner_name, **values)
    return total_found, scanned, failed


def main() -> None:
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    settings = _load_runner_settings()
    client = BybitClient(settings)
    universe = get_scanner_universe(client, settings)
    symbols = [str(item["symbol"]) for item in universe]

    repository = ScannerRepository(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        backend="postgres",
    )

    # Verify DB connectivity (schema must be applied separately via schema.sql)
    if not repository.ping():
        logger.error("database health check failed")
        repository.close()
        raise SystemExit("database health check failed")

    if not repository.acquire_runner_lock():
        # Try to clear stale idle backends holding the advisory lock
        killed = repository.cleanup_stale_advisory_lock()
        if killed:
            logger.warning("cleaned up %d stale advisory lock holder(s)", killed)
            if not repository.acquire_runner_lock():
                logger.error("scanner runner lock still held after cleanup")
                repository.close()
                raise SystemExit("scanner runner already active")
        else:
            logger.error(
                "scanner runner lock held by active process — terminating"
            )
            repository.close()
            raise SystemExit("scanner runner already active")
    repository.abort_stale_runs()

    orchestrator = ScannerOrchestrator(repository=repository)
    # Load expectancy filter if enabled
    expectancy_filter = None
    if settings.expectancy_filter_enabled:
        expectancy_filter = load_expectancy(repository)
        logger.info(
            "expectancy filter loaded: %d records, min_avg_r=%.4f, min_samples=%d",
            len(expectancy_filter.records), settings.expectancy_min_avg_r,
            settings.expectancy_min_samples,
        )
    else:
        logger.info("expectancy filter disabled")

    logger.info(
        "scanner started: symbols=%s scanners=%d interval=%ds expectancy_filter=%s",
        symbols, len(orchestrator.scanners), settings.scan_interval,
        "ON" if expectancy_filter else "OFF",
    )

    cycle = 0
    while not SHUTDOWN:
        try:
            universe = get_scanner_universe(client, settings)
            symbols = [str(item["symbol"]) for item in universe]
            logger.info("scanner universe refreshed: %d symbols", len(symbols))
        except Exception:
            # Keep the last good universe after a transient Bybit failure and
            # retry the refresh at the beginning of the next cycle.
            logger.exception("failed to refresh scanner universe")

        cycle += 1
        start = time.monotonic()

        # Start a new run (reconnect if DB connection dropped)
        try:
            run_id = repository.start_run(
                symbols_total=len(symbols),
                universe_mode=settings.scanner_universe.mode,
            )
            repository.save_run_universe(run_id, universe)
        except Exception:
            logger.warning("DB error starting run, attempting reconnect")
            if repository.reconnect():
                try:
                    run_id = repository.start_run(
                        symbols_total=len(symbols),
                        universe_mode=settings.scanner_universe.mode,
                    )
                    repository.save_run_universe(run_id, universe)
                except Exception:
                    logger.exception("run start failed after reconnect")
                    run_id = None
            else:
                logger.error("reconnect failed, skipping cycle")
                run_id = None

        try:
            total, scanned, failed = run_scan_cycle(
                client, orchestrator, repository, symbols, run_id, settings,
                expectancy_filter=expectancy_filter,
            )

            repository.expire_setups()

            # Aggregate signals
            active_signals = 0
            try:
                active_signals = repository.aggregate_signals(run_id)
            except Exception:
                logger.exception("signal aggregation failed")

            elapsed = time.monotonic() - start

            # Finish the run
            repository.finish_run(
                run_id,
                symbols_scanned=scanned,
                symbols_failed=failed,
                setups_found=total,
                error_count=failed,
                status="COMPLETED" if failed == 0 else "PARTIAL",
            )

            logger.info(
                "cycle #%d done: %d/%d symbols | %d setups | %d active signals | %.1fs",
                cycle, scanned, len(symbols), total, active_signals, elapsed,
            )

            # Refresh expectancy filter every 10 cycles
            if expectancy_filter is not None and cycle % 10 == 0:
                expectancy_filter = load_expectancy(repository)
                logger.info("expectancy filter refreshed: %d records", len(expectancy_filter.records))
        except Exception:
            logger.exception("cycle #%d failed", cycle)
            if run_id:
                try:
                    repository.finish_run(
                        run_id, status="FAILED", error_count=1,
                    )
                except Exception:
                    logger.warning("could not record failed run status")
                    repository.reconnect()

        # Sleep in small intervals to respond to shutdown
        delay = seconds_until_next_cycle(
            start, settings.scan_interval, time.monotonic(),
        )
        for _ in range(int(delay)):
            if SHUTDOWN:
                break
            time.sleep(1)

    repository.close()
    logger.info("scanner stopped")


if __name__ == "__main__":
    main()
