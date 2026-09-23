"""Backfill runner for historical shadow signal collection."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.scanners.atr_wick_rejection_short import (
    AtrWickRejectionShortScanner,
    ATR_PERIOD,
    RSI_PERIOD,
    EMA_MEDIUM,
    EMA_SLOW,
)
from app.shadow.repository import ShadowSignalRepository

logger = logging.getLogger(__name__)

# Indicator warmup: need enough candles for all indicators
# ATR(14) + RSI(14) + EMA(200) + ema_slope(50+5) + BB(20) + volume(20)
# Minimum: 200 + 50 = 250 candles; use 260 for safety
INDICATOR_WARMUP = 260


@dataclass
class BackfillStats:
    """Statistics for backfill operation."""
    evaluated: int = 0
    wick_atr_fail: int = 0
    close_location_fail: int = 0
    rsi_fail: int = 0
    bb_fail: int = 0
    ema_slope_fail: int = 0
    volume_fail: int = 0
    signal_pass: int = 0
    signals_inserted: int = 0
    duplicates_skipped: int = 0
    candles_processed: int = 0


class ShadowBackfillRunner:
    """Backfill historical shadow signals from existing 5m candles.

    This runner processes historical candle data to collect signals
    that occurred before the shadow scanner was started.

    Key design:
    - Fetch candles BEFORE start_time for indicator warmup
    - Only detect/record signals within [start_time, end_time]
    - Never use future candles (no lookahead)
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
    ) -> BackfillStats:
        """Backfill shadow signals for a symbol over a time range.

        Args:
            symbol: Trading pair symbol
            start_time: Start of backfill period (signals only after this)
            end_time: End of backfill period (default: now)

        Returns:
            BackfillStats with all diagnostic counters
        """
        if end_time is None:
            end_time = datetime.now(timezone.utc)

        stats = BackfillStats()

        try:
            # Get historical 5m candles WITH pre-roll for indicator warmup
            fetch_start = start_time - __import__('datetime').timedelta(minutes=INDICATOR_WARMUP * 5)
            all_candles = self._get_historical_candles(symbol, fetch_start, end_time)

            if not all_candles:
                logger.warning("No candles found for %s backfill", symbol)
                return stats

            stats.candles_processed = len(all_candles)

            # Find the index where start_time candles begin
            start_ts = int(start_time.timestamp() * 1000)
            signal_start_idx = 0
            for i, candle in enumerate(all_candles):
                if candle.timestamp >= start_ts:
                    signal_start_idx = i
                    break

            logger.info(
                "Backfill %s: %d total candles, signal start at index %d (need %d warmup)",
                symbol, len(all_candles), signal_start_idx, INDICATOR_WARMUP,
            )

            # Process candles: need INDICATOR_WARMUP candles before the current one
            # For each candle at index i, we need candles[i-INDICATOR_WARMUP:i+1]
            min_warmup = INDICATOR_WARMUP

            for i in range(min_warmup, len(all_candles)):
                # Create window with historical context (no lookahead)
                window = all_candles[max(0, i - min_warmup):i + 1]

                # Only evaluate candles within the requested time range
                candle_time = datetime.fromtimestamp(all_candles[i].timestamp / 1000, tz=timezone.utc)
                if candle_time < start_time:
                    continue
                if end_time and candle_time > end_time:
                    break

                stats.evaluated += 1
                ctx = self._create_context(symbol, window, all_candles[i].timestamp)

                # Detect signal with diagnostic counters
                rejection = self._detect_with_diagnostics(ctx)

                if rejection == "PASS":
                    stats.signal_pass += 1

                    # Check if signal already exists
                    signal_time = datetime.fromtimestamp(all_candles[i].timestamp / 1000, tz=timezone.utc)
                    if self.repo.signal_exists(symbol, signal_time):
                        stats.duplicates_skipped += 1
                        continue

                    # Detect signal again for storage
                    signal_data = self.scanner.detect_signal(ctx)
                    if signal_data and self.repo.save_signal(signal_data):
                        stats.signals_inserted += 1
                else:
                    # Count rejection reason
                    if rejection == "WICK_ATR":
                        stats.wick_atr_fail += 1
                    elif rejection == "CLOSE_LOCATION":
                        stats.close_location_fail += 1
                    elif rejection == "RSI":
                        stats.rsi_fail += 1
                    elif rejection == "BB":
                        stats.bb_fail += 1
                    elif rejection == "EMA_SLOPE":
                        stats.ema_slope_fail += 1
                    elif rejection == "VOLUME":
                        stats.volume_fail += 1

            logger.info(
                "Backfill complete for %s: %d evaluated, %d inserted, %d duplicates",
                symbol, stats.evaluated, stats.signals_inserted, stats.duplicates_skipped,
            )
            return stats

        except Exception:
            logger.exception("Backfill failed for %s", symbol)
            return stats

    def _detect_with_diagnostics(self, ctx: Any) -> str:
        """Detect signal and return rejection reason or 'PASS'.

        This method checks each condition sequentially and returns
        the first failed condition for diagnostic purposes.
        """
        from app.indicators import (
            atr_wilder, bollinger_bands, ema, ema_slope,
            rsi_wilder, volume_ratio as calc_volume_ratio,
        )
        from app.scanners.atr_wick_rejection_short import (
            ATR_PERIOD, RSI_PERIOD, EMA_MEDIUM,
            WICK_ATR_THRESHOLD, CLOSE_LOCATION_THRESHOLD,
            RSI_OVERBOUGHT, BB_UPPER_PROXIMITY,
            EMA_SLOPE_THRESHOLD, VOLUME_RATIO_THRESHOLD,
        )

        candles_5m = list(ctx.candles_5m)
        if len(candles_5m) < 50:
            return "INSUFFICIENT_DATA"

        last_candle = candles_5m[-1]
        closes = [c.close for c in candles_5m]

        # Calculate indicators
        try:
            atr = atr_wilder(candles_5m, ATR_PERIOD)
        except ValueError:
            return "INSUFFICIENT_DATA"

        # 1. Wick check
        upper_wick = last_candle.high - max(last_candle.open, last_candle.close)
        wick_atr_ratio = upper_wick / atr if atr > 0 else 0
        if wick_atr_ratio < WICK_ATR_THRESHOLD:
            return "WICK_ATR"

        # 2. Close location check
        candle_range = last_candle.high - last_candle.low
        close_location = (last_candle.close - last_candle.low) / candle_range if candle_range > 0 else 0.5
        if close_location > CLOSE_LOCATION_THRESHOLD:
            return "CLOSE_LOCATION"

        # 3. RSI check
        rsi = rsi_wilder(closes, RSI_PERIOD)
        if rsi < RSI_OVERBOUGHT:
            return "RSI"

        # 4. BB check
        bb_upper, bb_mid, bb_lower = bollinger_bands(closes, 20)
        distance_to_upper_bb = (bb_upper - last_candle.close) / last_candle.close if last_candle.close > 0 else 0
        if distance_to_upper_bb > BB_UPPER_PROXIMITY:
            return "BB"

        # 5. EMA slope check
        ema_slope_val = ema_slope(closes, EMA_MEDIUM, lookback=5) if len(closes) >= EMA_MEDIUM + 5 else 0
        if ema_slope_val > EMA_SLOPE_THRESHOLD:
            return "EMA_SLOPE"

        # 6. Volume check
        volumes = [c.volume for c in candles_5m]
        vol_ratio = calc_volume_ratio(volumes, 20) if len(volumes) > 20 else 1.0
        if vol_ratio < VOLUME_RATIO_THRESHOLD:
            return "VOLUME"

        return "PASS"

    def backfill_universe(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        symbols: list[str] | None = None,
    ) -> dict[str, BackfillStats]:
        """Backfill shadow signals for all symbols in universe.

        Args:
            start_time: Start of backfill period
            end_time: End of backfill period (default: now)
            symbols: Optional list of symbols (default: universe)

        Returns:
            Dict of {symbol: BackfillStats}
        """
        if symbols is None:
            symbols = self._get_universe_symbols()

        results: dict[str, BackfillStats] = {}
        for symbol in symbols:
            results[symbol] = self.backfill_symbol(symbol, start_time, end_time)

        total_evaluated = sum(r.evaluated for r in results.values())
        total_inserted = sum(r.signals_inserted for r in results.values())
        total_duplicates = sum(r.duplicates_skipped for r in results.values())

        logger.info(
            "Universe backfill complete: %d symbols, %d evaluated, %d inserted, %d duplicates",
            len(symbols), total_evaluated, total_inserted, total_duplicates,
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
    total_evaluated = sum(r.evaluated for r in results.values())
    total_inserted = sum(r.signals_inserted for r in results.values())
    total_duplicates = sum(r.duplicates_skipped for r in results.values())

    print("\n" + "=" * 80)
    print("SHADOW BACKFILL COMPLETE")
    print("=" * 80)
    print(f"Symbols processed: {len(results)}")
    print(f"Total evaluated: {total_evaluated}")
    print(f"Total inserted: {total_inserted}")
    print(f"Total duplicates skipped: {total_duplicates}")
    print("=" * 80)

    # Print per-symbol breakdown with rejection counters
    print("\nPER-SYMBOL BREAKDOWN:")
    for symbol, stats in sorted(results.items()):
        print(f"\n{symbol}:")
        print(f"  evaluated: {stats.evaluated}")
        print(f"  wick_atr_fail: {stats.wick_atr_fail}")
        print(f"  close_location_fail: {stats.close_location_fail}")
        print(f"  rsi_fail: {stats.rsi_fail}")
        print(f"  bb_fail: {stats.bb_fail}")
        print(f"  ema_slope_fail: {stats.ema_slope_fail}")
        print(f"  volume_fail: {stats.volume_fail}")
        print(f"  signal_pass: {stats.signal_pass}")
        print(f"  signals_inserted: {stats.signals_inserted}")
        print(f"  duplicates_skipped: {stats.duplicates_skipped}")


if __name__ == "__main__":
    main()