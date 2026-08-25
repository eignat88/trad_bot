"""Scanner Runner - continuous background scanner that writes to PostgreSQL.

Runs on system startup via Windows Startup folder.
Scans every 5 minutes, saves results to PostgreSQL, logs to file.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import load_settings
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

SCAN_INTERVAL = 300  # 5 minutes
SHUTDOWN = False


def _handle_signal(signum, frame):
    global SHUTDOWN
    logger.info("shutdown signal received")
    SHUTDOWN = True


def run_scan_cycle(
    client: BybitClient,
    orchestrator: ScannerOrchestrator,
    repository: ScannerRepository,
    symbols: list[str],
) -> int:
    total_found = 0
    for symbol in symbols:
        try:
            ctx = build_market_context(client, symbol, load_settings())
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
    symbols = list(settings.symbols)

    repository = ScannerRepository(
        host="localhost", port=5432, database="trad_bot", user="postgres",
    )
    repository.ensure_schema()

    orchestrator = ScannerOrchestrator(repository=repository)

    logger.info(
        "scanner started: symbols=%s scanners=%d interval=%ds",
        symbols, len(orchestrator.scanners), SCAN_INTERVAL,
    )

    cycle = 0
    while not SHUTDOWN:
        cycle += 1
        start = time.time()

        try:
            total = run_scan_cycle(client, orchestrator, repository, symbols)
            elapsed = time.time() - start
            logger.info("cycle #%d done: %d setups in %.1fs", cycle, total, elapsed)
        except Exception:
            logger.exception("cycle #%d failed", cycle)

        # Sleep in small intervals to respond to shutdown quickly
        for _ in range(SCAN_INTERVAL):
            if SHUTDOWN:
                break
            time.sleep(1)

    repository.close()
    logger.info("scanner stopped")


if __name__ == "__main__":
    main()
