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
from app.shadow.repository import ShadowSignalRepository

logger = logging.getLogger(__name__)


class ShadowScannerRunner:
    """Runner for real-time shadow signal collection.

    This runner operates independently of the main scanner and paper trading.
    It collects signals for experimental analysis without affecting trading.
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

    def scan_symbol(self, symbol: str) -> dict[str, int]:
        """Scan a single symbol for RAW shadow candidates.

        Returns {"raw": N, "strict": M, "inserted": X, "duplicates": D, "errors": E}.
        """
        result = {"raw": 0, "strict": 0, "inserted": 0, "duplicates": 0, "errors": 0}

        try:
            ctx = build_market_context(self.client, symbol, self.settings)

            # RAW candidate: upper_wick > 0 only, no strict filters
            raw = self.scanner.detect_raw_candidate(ctx)
            if raw is None:
                return result

            result["raw"] = 1

            # Determine strict_pass for this candidate
            strict = self.scanner.passes_strict_filters(raw)
            signal_to_save = WickRejectionSignal(
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

            if strict:
                result["strict"] = 1

            saved_id = self.repo.save_signal(signal_to_save)
            if saved_id is not None:
                result["inserted"] = 1
            else:
                # Could be duplicate (ON CONFLICT DO NOTHING) or DB error
                # Check if signal exists to distinguish
                if self.repo.signal_exists(symbol, raw.signal_time):
                    result["duplicates"] = 1
                else:
                    result["errors"] = 1

        except Exception:
            logger.exception("Shadow scan failed for %s", symbol)
            result["errors"] = 1

        return result

    def scan_universe(self, symbols: list[str]) -> dict[str, int]:
        """Scan all symbols in the universe.

        Returns aggregated counts: raw, strict, inserted, duplicates, errors.
        """
        totals = {"raw": 0, "strict": 0, "inserted": 0, "duplicates": 0, "errors": 0}

        with ThreadPoolExecutor(max_workers=self.settings.scanner_workers) as executor:
            future_to_symbol = {
                executor.submit(self.scan_symbol, symbol): symbol
                for symbol in symbols
            }
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    result = future.result()
                    for key in totals:
                        totals[key] += result[key]
                except Exception:
                    logger.exception("Shadow scan failed for %s", symbol)
                    totals["errors"] += 1

        return totals

    def run_cycle(self) -> dict[str, Any]:
        """Run one complete scan cycle.

        Returns summary with raw/strict/inserted/duplicates/errors.
        """
        start_time = datetime.now(timezone.utc)

        # Get universe of symbols
        symbols = self._get_universe_symbols()

        # Scan all symbols — returns aggregated counts
        counts = self.scan_universe(symbols)

        end_time = datetime.now(timezone.utc)

        summary = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "symbols_scanned": len(symbols),
            "raw_candidates": counts["raw"],
            "strict_pass": counts["strict"],
            "inserted": counts["inserted"],
            "duplicates": counts["duplicates"],
            "errors": counts["errors"],
        }

        logger.info(
            "Shadow scan cycle: symbols=%d raw=%d strict=%d inserted=%d "
            "duplicates=%d errors=%d",
            len(symbols), counts["raw"], counts["strict"],
            counts["inserted"], counts["duplicates"], counts["errors"],
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
        print(f"Errors:            {summary['errors']}")
        print("=" * 80)
    else:
        # Continuous mode
        runner.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()