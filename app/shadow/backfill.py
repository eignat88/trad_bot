"""Backfill runner for historical shadow signal collection."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Any

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner
from app.shadow.repository import ShadowSignalRepository

logger = logging.getLogger(__name__)


class ShadowBackfillRunner:
    """Backfill historical shadow signals from existing 5m candles.

    This runner processes historical candle data to collect signals
    that occurred before the shadow scanner was started.
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

    def backfill_symbol(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime | None = None,
    ) -> tuple[int, int, int, int]:
        """Backfill shadow signals for a symbol over a time range.

        Args:
            symbol: Trading pair symbol
            start_time: Start of backfill period
            end_time: End of backfill period (default: now)

        Returns:
            Tuple of (candles_processed, signals_detected, signals_inserted, duplicates_skipped)
        """
        if end_time is None:
            end_time = datetime.now(timezone.utc)

        candles_processed = 0
        signals_detected = 0
        signals_inserted = 0
        duplicates_skipped = 0

        try:
            # Get historical 5m candles
            candles = self._get_historical_candles(symbol, start_time, end_time)
            if not candles:
                logger.warning("No candles found for %s backfill", symbol)
                return 0, 0, 0, 0

            candles_processed = len(candles)

            # Process candles in sequence
            for i in range(50, len(candles)):  # Need 50 candles for indicators
                window = candles[i-49:i+1]
                ctx = self._create_context(symbol, window, candles[i].timestamp)

                # Detect signal
                signal_data = self.scanner.detect_signal(ctx)
                if signal_data:
                    signals_detected += 1

                    # Check if signal already exists
                    if self.repo.signal_exists(symbol, signal_data.signal_time):
                        duplicates_skipped += 1
                        continue

                    # Save to database
                    if self.repo.save_signal(signal_data):
                        signals_inserted += 1

            logger.info(
                "Backfill complete for %s: %d candles, %d detected, %d inserted, %d duplicates",
                symbol, candles_processed, signals_detected, signals_inserted, duplicates_skipped,
            )
            return candles_processed, signals_detected, signals_inserted, duplicates_skipped

        except Exception:
            logger.exception("Backfill failed for %s", symbol)
            return candles_processed, signals_detected, signals_inserted, duplicates_skipped

    def backfill_universe(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        symbols: list[str] | None = None,
    ) -> dict[str, tuple[int, int, int, int]]:
        """Backfill shadow signals for all symbols in universe.

        Args:
            start_time: Start of backfill period
            end_time: End of backfill period (default: now)
            symbols: Optional list of symbols (default: universe)

        Returns:
            Dict of {symbol: (candles, detected, inserted, duplicates)}
        """
        if symbols is None:
            symbols = self._get_universe_symbols()

        results: dict[str, tuple[int, int, int, int]] = {}
        for symbol in symbols:
            results[symbol] = self.backfill_symbol(symbol, start_time, end_time)

        total_candles = sum(r[0] for r in results.values())
        total_detected = sum(r[1] for r in results.values())
        total_inserted = sum(r[2] for r in results.values())
        total_duplicates = sum(r[3] for r in results.values())

        logger.info(
            "Universe backfill complete: %d symbols, %d candles, %d detected, %d inserted, %d duplicates",
            len(symbols), total_candles, total_detected, total_inserted, total_duplicates,
        )
        return results

    def _get_historical_candles(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        """Get historical 5m candles for backfill."""
        try:
            # Calculate number of candles needed
            time_diff = (end_time - start_time).total_seconds()
            num_candles = int(time_diff / 300) + 100  # 5m = 300 seconds

            # Get candles from exchange
            candles = self.client.get_klines(symbol, "5", min(num_candles, 1000))

            # Filter to time range
            start_ts = int(start_time.timestamp() * 1000)
            end_ts = int(end_time.timestamp() * 1000)

            return [
                c for c in candles
                if start_ts <= c.timestamp <= end_ts
            ]

        except Exception:
            logger.exception("Failed to get historical candles for %s", symbol)
            return []

    def _create_context(
        self,
        symbol: str,
        candles: list[Candle],
        timestamp: int,
    ) -> Any:
        """Create a MarketContext from historical candles."""
        from app.scanners.context_builder import _build_indicators, _classify_market_regime
        from app.scanners.models import MarketContext, MarketLevels

        # Build indicators from the candle window
        indicators = _build_indicators(candles)

        # Classify market regime
        market_regime = _classify_market_regime(indicators, candles[-1].close)

        # Create context
        return MarketContext(
            symbol=symbol,
            candles_5m=tuple(candles),
            candles_15m=tuple(candles),  # Use same candles for simplicity
            candles_1h=tuple(candles),
            candles_4h=tuple(candles),
            indicators=indicators,
            market_regime=market_regime,
            levels=MarketLevels(),
            evaluated_at=datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc),
        )

    def _get_universe_symbols(self) -> list[str]:
        """Get list of symbols from universe configuration."""
        if self.settings.symbols:
            return list(self.settings.symbols)

        # Default universe
        return [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
            "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT",
            "UNIUSDT", "ATOMUSDT", "NEARUSDT", "FTMUSDT", "ALGOUSDT",
            "HBARUSDT", "VETUSDT", "ICPUSDT", "FILUSDT", "AAVEUSDT",
        ]


def main() -> None:
    """CLI entrypoint for shadow backfill."""
    parser = argparse.ArgumentParser(
        description="Shadow backfill: collect historical ATR Wick Rejection Short signals",
    )
    parser.add_argument(
        "--start",
        type=str,
        required=True,
        help="Start time (ISO format, e.g. 2026-09-23T00:00:00Z)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End time (ISO format, default: now)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Single symbol to backfill (e.g. HBARUSDT)",
    )
    parser.add_argument(
        "--all-symbols",
        action="store_true",
        help="Backfill all symbols in universe",
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

    # Parse start time
    try:
        start_time = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    except ValueError:
        logger.error("Invalid start time format: %s", args.start)
        sys.exit(1)

    # Parse end time
    end_time = None
    if args.end:
        try:
            end_time = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
        except ValueError:
            logger.error("Invalid end time format: %s", args.end)
            sys.exit(1)

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
        logger.error("PostgreSQL connection required for shadow backfill")
        sys.exit(1)

    # Create shadow repository
    shadow_repo = ShadowSignalRepository(db_repo._conn)

    # Create Bybit client
    client = BybitClient(settings)

    # Create backfill runner
    runner = ShadowBackfillRunner(settings, client, shadow_repo)

    # Determine symbols
    if args.symbol:
        symbols = [args.symbol]
    elif args.all_symbols:
        symbols = None  # Will use universe
    else:
        # Default: use configured symbols
        symbols = list(settings.symbols) if settings.symbols else None

    # Run backfill
    logger.info(
        "Starting shadow backfill: %s to %s, symbols=%s",
        start_time.isoformat(),
        end_time.isoformat() if end_time else "now",
        symbols if symbols else "universe",
    )

    results = runner.backfill_universe(start_time, end_time, symbols)

    # Print summary
    total_candles = sum(r[0] for r in results.values())
    total_detected = sum(r[1] for r in results.values())
    total_inserted = sum(r[2] for r in results.values())
    total_duplicates = sum(r[3] for r in results.values())

    print("\n" + "=" * 80)
    print("SHADOW BACKFILL COMPLETE")
    print("=" * 80)
    print(f"Symbols processed: {len(results)}")
    print(f"Candles processed: {total_candles}")
    print(f"Signals detected: {total_detected}")
    print(f"Signals inserted: {total_inserted}")
    print(f"Duplicates skipped: {total_duplicates}")
    print("=" * 80)

    # Print per-symbol breakdown
    print("\nPER-SYMBOL BREAKDOWN:")
    for symbol, (candles, detected, inserted, duplicates) in sorted(results.items()):
        if detected > 0:
            print(f"  {symbol}: {candles} candles, {detected} detected, {inserted} inserted, {duplicates} duplicates")


if __name__ == "__main__":
    main()