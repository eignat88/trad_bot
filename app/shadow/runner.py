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
from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner, SCANNER_NAME
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

    def scan_symbol(self, symbol: str) -> int:
        """Scan a single symbol for shadow signals.

        Returns number of signals saved.
        """
        try:
            ctx = build_market_context(self.client, symbol, self.settings)
            signals = self.scanner.scan(ctx)

            saved = 0
            for candidate in signals:
                # Convert SetupCandidate back to WickRejectionSignal for storage
                # Extract signal data from candidate features
                features = candidate.features
                signal = self._candidate_to_signal(candidate, features)
                if signal and self.repo.save_signal(signal):
                    saved += 1

            return saved
        except Exception:
            logger.exception("Shadow scan failed for %s", symbol)
            return 0

    def _candidate_to_signal(
        self, candidate: Any, features: dict
    ) -> Any:
        """Convert a SetupCandidate back to WickRejectionSignal for storage."""
        from app.scanners.atr_wick_rejection_short import WickRejectionSignal

        return WickRejectionSignal(
            symbol=candidate.symbol,
            signal_time=candidate.detected_at,
            signal_price=candidate.reference_price,
            open=features.get("open", 0),
            high=features.get("high", 0),
            low=features.get("low", 0),
            close=features.get("close", 0),
            volume=features.get("volume", 0),
            atr=features.get("atr", 0),
            atr_pct=features.get("atr_pct", 0),
            wick_size=features.get("wick_size", 0),
            wick_atr=features.get("wick_atr", 0),
            upper_wick_pct=features.get("upper_wick_pct", 0),
            close_location=features.get("close_location", 0),
            rsi=features.get("rsi", 0),
            stoch_rsi=features.get("stoch_rsi"),
            bb_upper=features.get("bb_upper", 0),
            bb_mid=features.get("bb_mid", 0),
            bb_lower=features.get("bb_lower", 0),
            bb_width=features.get("bb_width", 0),
            distance_to_upper_bb=features.get("distance_to_upper_bb", 0),
            ema_fast=features.get("ema_fast", 0),
            ema_medium=features.get("ema_medium", 0),
            ema_slow=features.get("ema_slow", 0),
            ema_slope=features.get("ema_slope", 0),
            volume_ratio=features.get("volume_ratio", 0),
            signal_version=features.get("signal_version", "1.0.0"),
        )

    def scan_universe(self, symbols: list[str]) -> dict[str, int]:
        """Scan all symbols in the universe.

        Returns dict of {symbol: signals_saved}.
        """
        results: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=self.settings.scanner_workers) as executor:
            future_to_symbol = {
                executor.submit(self.scan_symbol, symbol): symbol
                for symbol in symbols
            }
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    results[symbol] = future.result()
                except Exception:
                    logger.exception("Shadow scan failed for %s", symbol)
                    results[symbol] = 0

        total = sum(results.values())
        logger.info(
            "Shadow scan complete: %d signals across %d symbols",
            total, len(symbols),
        )
        return results

    def run_cycle(self) -> dict[str, Any]:
        """Run one complete scan cycle.

        Returns summary of the scan.
        """
        start_time = datetime.now(timezone.utc)

        # Get universe of symbols
        symbols = self._get_universe_symbols()

        # Scan all symbols
        results = self.scan_universe(symbols)

        # Calculate summary
        total_signals = sum(results.values())
        symbols_with_signals = sum(1 for v in results.values() if v > 0)

        summary = {
            "start_time": start_time.isoformat(),
            "end_time": datetime.now(timezone.utc).isoformat(),
            "symbols_scanned": len(symbols),
            "symbols_with_signals": symbols_with_signals,
            "total_signals": total_signals,
            "results": results,
        }

        logger.info(
            "Shadow scan cycle complete: %d symbols scanned, %d signals found",
            len(symbols), total_signals,
        )

        return summary

    def _get_universe_symbols(self) -> list[str]:
        """Get list of symbols to scan from universe configuration."""
        # Use configured symbols if available
        if self.settings.symbols:
            return list(self.settings.symbols)

        # Otherwise, get top symbols from exchange
        try:
            # For now, use a default list of popular symbols
            default_symbols = [
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
                "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT",
                "UNIUSDT", "ATOMUSDT", "NEARUSDT", "FTMUSDT", "ALGOUSDT",
                "HBARUSDT", "VETUSDT", "ICPUSDT", "FILUSDT", "AAVEUSDT",
            ]
            return default_symbols[:self.settings.scanner_universe.top_n]
        except Exception:
            logger.exception("Failed to get universe symbols")
            return ["BTCUSDT", "ETHUSDT"]

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
    client = BybitClient(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        timeout=settings.bybit_timeout,
    )

    # Create runner
    runner = ShadowScannerRunner(settings, client, shadow_repo)

    if args.once:
        # Single cycle
        logger.info("Running single shadow scan cycle...")
        summary = runner.run_cycle()
        print("\n" + "=" * 80)
        print("SHADOW SCAN CYCLE COMPLETE")
        print("=" * 80)
        print(f"Symbols scanned: {summary['symbols_scanned']}")
        print(f"Symbols with signals: {summary['symbols_with_signals']}")
        print(f"Total signals: {summary['total_signals']}")
        print("=" * 80)
    else:
        # Continuous mode
        runner.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()