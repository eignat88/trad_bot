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

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.scanners.context_builder import build_market_context
from app.scanners.orchestrator import ScannerOrchestrator

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "scanner.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("scanner_runner")

SHUTDOWN = False


def _handle_signal(signum, frame):
    global SHUTDOWN
    logger.info("shutdown signal received")
    SHUTDOWN = True


def get_scanner_symbols(client: BybitClient, settings: Settings) -> list[str]:
    """Return the scanner universe selected by the current configuration."""
    universe = settings.scanner_universe
    if universe.mode == "dynamic":
        symbols = client.get_liquid_symbols(
            top_n=universe.top_n,
            min_turnover_24h=universe.min_turnover_24h,
            min_volume_24h=universe.min_volume_24h,
            quote_coin=universe.quote_coin,
        )
        if not symbols:
            raise RuntimeError(
                "Dynamic scanner universe is empty; check liquidity thresholds"
            )
        return symbols

    symbols = list(settings.symbols)
    if not symbols:
        raise RuntimeError("Static scanner universe is empty")
    return symbols


def seconds_until_next_cycle(started_at: float, interval: int, now: float) -> float:
    """Return the remaining delay, keeping cycles anchored to their start."""
    return max(0, interval - (now - started_at))


def run_scan_cycle(
    client: BybitClient,
    orchestrator: ScannerOrchestrator,
    repository: ScannerRepository,
    symbols: list[str],
    settings: Settings | None = None,
) -> tuple[int, int, int]:
    """Returns (total_found, scanned, failed)."""
    settings = settings or load_settings()
    total_found = 0
    scanned = 0
    failed = 0

    if not symbols:
        return 0, 0, 0

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
                    error_type="MARKET_DATA_ERROR",
                    error_message=str(sys.exc_info()[1]),
                )
                continue

            try:
                candidates = orchestrator.scan_all(ctx)

                for c in candidates:
                    repository.save_setup(c)
                    repository.save_event(
                        "SETUP_DETECTED", c.scanner_name, symbol,
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
                    error_type="SCAN_ERROR",
                    error_message=str(sys.exc_info()[1]),
                )

    return total_found, scanned, failed


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    settings = load_settings()
    client = BybitClient(settings)
    symbols = get_scanner_symbols(client, settings)

    repository = ScannerRepository(
        host="localhost", port=5432, database="trad_bot", user="postgres",
    )
    repository.ensure_schema()

    orchestrator = ScannerOrchestrator(repository=repository)

    logger.info(
        "scanner started: symbols=%s scanners=%d interval=%ds",
        symbols, len(orchestrator.scanners), settings.scan_interval,
    )

    cycle = 0
    while not SHUTDOWN:
        try:
            symbols = get_scanner_symbols(client, settings)
            logger.info("scanner universe refreshed: %d symbols", len(symbols))
        except Exception:
            # Keep the last good universe after a transient Bybit failure and
            # retry the refresh at the beginning of the next cycle.
            logger.exception("failed to refresh scanner universe")

        cycle += 1
        start = time.monotonic()

        # Start a new run
        run_id = repository.start_run(symbols_total=len(symbols))

        try:
            total, scanned, failed = run_scan_cycle(
                client, orchestrator, repository, symbols, settings,
            )

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
        except Exception:
            logger.exception("cycle #%d failed", cycle)
            if run_id:
                repository.finish_run(
                    run_id, status="FAILED", error_count=1,
                )

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
