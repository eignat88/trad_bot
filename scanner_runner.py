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
from app.exchange.bybit_client import BybitClient, BybitTimeoutError
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


def seconds_until_next_cycle(started_at: float, interval: float, now: float) -> float:
    """Return cadence delay, including the elapsed scan time in the interval."""
    return max(0.0, started_at + interval - now)


def run_scan_cycle(
    client: BybitClient,
    orchestrator: ScannerOrchestrator,
    repository: ScannerRepository,
    symbols: list[str],
    settings: Settings | None = None,
) -> int:
    settings = settings or load_settings()
    total_found = 0
    if not symbols:
        return 0
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
            except BybitTimeoutError:
                logger.error("%s: skipped after %d timeouts", symbol, settings.bybit_max_attempts)
                continue
            except Exception:
                logger.exception("market data failed for %s", symbol)
                continue

            # Keep mutable deduplication and repository state on the main thread.
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

    return total_found


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    settings = load_settings()
    client = BybitClient(settings)
    universe = settings.scanner_universe
    if universe.mode == "dynamic":
        symbols = client.get_liquid_symbols(
            top_n=universe.top_n,
            min_turnover_24h=universe.min_turnover_24h,
            min_volume_24h=universe.min_volume_24h,
            quote_coin=universe.quote_coin,
        )
        if not symbols:
            raise RuntimeError("dynamic scanner universe is empty; check liquidity thresholds")
    else:
        symbols = list(settings.symbols)

    repository = ScannerRepository(
        host="localhost", port=5432, database="trad_bot", user="postgres",
    )
    repository.ensure_schema()

    orchestrator = ScannerOrchestrator(repository=repository)

    logger.info(
        "scanner started: symbols=%d scanners=%d workers=%d interval=%ds universe=%s",
        len(symbols), len(orchestrator.scanners), settings.scanner_workers,
        settings.scan_interval, universe.mode,
    )

    cycle = 0
    while not SHUTDOWN:
        cycle += 1
        start = time.monotonic()

        try:
            total = run_scan_cycle(client, orchestrator, repository, symbols, settings)
            elapsed = time.monotonic() - start
            logger.info("cycle #%d done: %d setups in %.1fs", cycle, total, elapsed)
        except Exception:
            logger.exception("cycle #%d failed", cycle)

        # Scan duration counts toward the interval, keeping a start-to-start cadence.
        while not SHUTDOWN:
            remaining = seconds_until_next_cycle(start, settings.scan_interval, time.monotonic())
            if remaining <= 0:
                break
            time.sleep(min(1, remaining))

    repository.close()
    logger.info("scanner stopped")


if __name__ == "__main__":
    main()
