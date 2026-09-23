"""Shadow scanner runner for real-time signal collection."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner, WickRejectionSignal
from app.scanners.context_builder import build_market_context
from app.shadow.repository import SaveSignalStatus, ShadowSignalRepository

logger = logging.getLogger(__name__)


class ShadowScannerRunner:
    """Runner for real-time shadow signal collection.

    Architecture:
    - Worker threads: market fetch + detection (parallel, no DB)
    - Main thread: sequential DB persistence via single connection

    This avoids pg8000 thread-safety issues with shared connections.
    """

    def __init__(
        self,
        settings: Settings,
        client: BybitClient,
        repo: ShadowSignalRepository,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repo = repo
        self.scanner = AtrWickRejectionShortScanner()
        self._running = False

    def _scan_symbol(self, symbol: str) -> WickRejectionSignal | None:
        """Scan one symbol: fetch market data, detect RAW candidate.

        NO DB operations — pure computation + API call.
        Returns WickRejectionSignal if a raw candidate exists, None otherwise.
        """
        ctx = build_market_context(self.client, symbol, self.settings)

        raw = self.scanner.detect_raw_candidate(ctx)
        if raw is None:
            return None

        # Set strict_pass via centralised predicate
        strict = self.scanner.passes_strict_filters(raw)
        return WickRejectionSignal(
            symbol=raw.symbol,
            signal_time=raw.signal_time,
            signal_price=raw.signal_price,
            open=raw.open, high=raw.high, low=raw.low,
            close=raw.close, volume=raw.volume,
            atr=raw.atr, atr_pct=raw.atr_pct,
            wick_size=raw.wick_size, wick_atr=raw.wick_atr,
            upper_wick_pct=raw.upper_wick_pct,
            close_location=raw.close_location,
            rsi=raw.rsi, stoch_rsi=raw.stoch_rsi,
            bb_upper=raw.bb_upper, bb_mid=raw.bb_mid,
            bb_lower=raw.bb_lower, bb_width=raw.bb_width,
            distance_to_upper_bb=raw.distance_to_upper_bb,
            ema_fast=raw.ema_fast, ema_medium=raw.ema_medium,
            ema_slow=raw.ema_slow, ema_slope=raw.ema_slope,
            volume_ratio=raw.volume_ratio,
            strict_pass=strict,
            signal_version=raw.signal_version,
        )

    def _persist_candidate(self, signal: WickRejectionSignal) -> str:
        """Persist a single candidate via repo. Main thread only.

        Returns: "inserted" | "duplicate" | "db_error"
        """
        result = self.repo.save_signal(signal)
        if result.status == SaveSignalStatus.INSERTED:
            return "inserted"
        elif result.status == SaveSignalStatus.DUPLICATE:
            return "duplicate"
        else:
            return "db_error"

    def run_cycle(self) -> dict[str, Any]:
        """Run one complete scan+persist cycle.

        Phase 1 (parallel): fetch market data + detect candidates
        Phase 2 (sequential): persist candidates to DB

        Returns summary with separate scan_error / db_error counts.
        """
        start_time = datetime.now(timezone.utc)

        symbols = self._get_universe_symbols()

        # ── Phase 1: parallel market fetch + detection ────────
        candidates: list[WickRejectionSignal] = []
        raw_count = 0
        strict_count = 0
        scan_errors = 0

        with ThreadPoolExecutor(max_workers=self.settings.scanner_workers) as executor:
            future_to_symbol = {
                executor.submit(self._scan_symbol, symbol): symbol
                for symbol in symbols
            }
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    candidate = future.result()
                    if candidate is not None:
                        raw_count += 1
                        if candidate.strict_pass:
                            strict_count += 1
                        candidates.append(candidate)
                except Exception:
                    logger.exception("Shadow scan failed for %s", symbol)
                    scan_errors += 1

        # ── Phase 2: sequential DB persistence ────────────────
        inserted = 0
        duplicates = 0
        db_errors = 0

        for signal in candidates:
            status = self._persist_candidate(signal)
            if status == "inserted":
                inserted += 1
            elif status == "duplicate":
                duplicates += 1
            else:
                db_errors += 1

        end_time = datetime.now(timezone.utc)

        summary = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "symbols_scanned": len(symbols),
            "raw_candidates": raw_count,
            "strict_pass": strict_count,
            "inserted": inserted,
            "duplicates": duplicates,
            "scan_errors": scan_errors,
            "db_errors": db_errors,
        }

        logger.info(
            "Shadow scan cycle: symbols=%d raw=%d strict=%d "
            "inserted=%d duplicates=%d scan_errors=%d db_errors=%d",
            len(symbols), raw_count, strict_count,
            inserted, duplicates, scan_errors, db_errors,
        )

        return summary

    def _get_universe_symbols(self) -> list[str]:
        """Get list of symbols to scan from universe configuration.

        Uses the same logic as the main scanner:
        - dynamic mode: fetches top N liquid symbols from Bybit
        - static mode: uses configured symbols list
        """
        universe = self.settings.scanner_universe
        if universe.mode == "dynamic":
            try:
                symbols = self.client.get_liquid_symbols(
                    top_n=universe.top_n,
                    min_turnover_24h=universe.min_turnover_24h,
                    min_volume_24h=universe.min_volume_24h,
                    quote_coin=universe.quote_coin,
                )
                if symbols:
                    logger.info(
                        "Dynamic universe: %d symbols (top_n=%d, min_turnover=%.0f)",
                        len(symbols), universe.top_n, universe.min_turnover_24h,
                    )
                    return symbols
                logger.warning("Dynamic universe empty, falling back to static")
            except Exception:
                logger.exception("Failed to fetch dynamic universe, falling back to static")

        # Static mode or fallback
        if self.settings.symbols:
            return list(self.settings.symbols)

        # Default universe
        return [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
            "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT",
            "UNIUSDT", "ATOMUSDT", "NEARUSDT", "FTMUSDT", "ALGOUSDT",
            "HBARUSDT", "VETUSDT", "ICPUSDT", "FILUSDT", "AAVEUSDT",
        ]

    def start(self, interval_seconds: int = 300) -> None:
        """Start continuous shadow signal collection.

        Args:
            interval_seconds: Interval between scan cycles (default: 300 = 5 minutes)
        """
        self._running = True

        def signal_handler(signum, frame):
            logger.info("Received signal %d, stopping shadow runner...", signum)
            self._running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        logger.info(
            "Starting shadow scanner runner (interval=%ds)",
            interval_seconds,
        )

        while self._running:
            try:
                summary = self.run_cycle()
                logger.info(
                    "Cycle complete: %d signals found",
                    summary["total_signals"],
                )
            except Exception:
                logger.exception("Shadow scan cycle failed")

            if self._running:
                time.sleep(interval_seconds)

        logger.info("Shadow scanner runner stopped")


def main() -> None:
    """CLI entrypoint for shadow runner."""
    parser = argparse.ArgumentParser(
        description="Shadow runner: real-time ATR Wick Rejection Short signal collection",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan cycle and exit",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=300,
        help="Interval between scan cycles in seconds (default: 300)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load settings
    settings = load_settings(args.config)

    # Create database connection
    db_repo = ScannerRepository(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        backend="postgres",
    )

    if not db_repo._use_pg:
        logger.error("PostgreSQL connection required for shadow runner")
        sys.exit(1)

    # Create shadow repository
    shadow_repo = ShadowSignalRepository(db_repo._conn)

    # Create Bybit client
    client = BybitClient(settings)

    # Create runner
    runner = ShadowScannerRunner(settings, client, shadow_repo)

    if args.once:
        # Single cycle
        logger.info("Running single shadow scan cycle...")
        summary = runner.run_cycle()
        print("\n" + "=" * 80)
        print("SHADOW SCAN CYCLE COMPLETE")
        print("=" * 80)
        print(f"Symbols scanned:   {summary['symbols_scanned']}")
        print(f"Raw candidates:    {summary['raw_candidates']}")
        print(f"Strict pass:       {summary['strict_pass']}")
        print(f"Inserted:          {summary['inserted']}")
        print(f"Duplicates:        {summary['duplicates']}")
        print(f"Scan errors:       {summary['scan_errors']}")
        print(f"DB errors:         {summary['db_errors']}")
        print("=" * 80)
    else:
        # Continuous mode
        runner.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()