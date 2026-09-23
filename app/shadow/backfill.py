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

# Threshold buckets for wick_atr distribution analysis
WICK_ATR_BUCKETS = [0.20, 0.30, 0.40, 0.50, 0.75, 1.00, 1.25, 1.50]


@dataclass
class BackfillStats:
    """Statistics for backfill operation."""
    evaluated: int = 0
    raw_candidates: int = 0
    strict_pass: int = 0
    signals_inserted: int = 0
    duplicates_skipped: int = 0
    candles_processed: int = 0
    # All wick_atr values from ALL evaluated candles
    all_wick_atr_values: list[float] = field(default_factory=list)
    # Threshold bucket counts
    wick_atr_bucket_counts: dict[float, int] = field(default_factory=dict)
    # TOP candles by wick_atr
    top_candles: list[dict] = field(default_factory=list)


@dataclass
class DiagnosticStats:
    """Diagnostic statistics for a metric."""
    min_val: float = 0.0
    max_val: float = 0.0
    median: float = 0.0
    p25: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    count: int = 0


class ShadowBackfillRunner:
    """Backfill historical shadow signals from existing 5m candles.

    This runner processes historical candle data to collect signals
    that occurred before the shadow scanner was started.

    Key design:
    - Fetch candles BEFORE start_time for indicator warmup
    - Only detect/record signals within [start_time, end_time]
    - Never use future candles (no lookahead)
    - Save RAW candidates (wick rejection only) for diagnostic analysis
    - strict_pass flag indicates if all filters passed
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
            min_warmup = INDICATOR_WARMUP

            # Initialize bucket counters
            stats.wick_atr_bucket_counts = {b: 0 for b in WICK_ATR_BUCKETS}

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

                # Detect raw candidate (upper_wick > 0 only — NO filters)
                raw = self.scanner.detect_raw_candidate(ctx)
                if raw is None:
                    # Still track this candle in TOP-20 even if no upper wick
                    # (upper_wick = 0 means it's not interesting)
                    continue

                stats.raw_candidates += 1

                # Collect wick_atr from ALL raw candidates
                stats.all_wick_atr_values.append(raw.wick_atr)

                # Count threshold buckets
                for bucket in WICK_ATR_BUCKETS:
                    if raw.wick_atr >= bucket:
                        stats.wick_atr_bucket_counts[bucket] += 1

                # Check if strict filters pass
                strict = self.scanner.detect_signal(ctx)
                if strict:
                    stats.strict_pass += 1

                # Save to database (raw candidate)
                signal_time = candle_time
                if self.repo.signal_exists(symbol, signal_time):
                    stats.duplicates_skipped += 1
                    continue

                if self.repo.save_signal(raw):
                    stats.signals_inserted += 1

                # Track TOP candles by wick_atr (keep all, sort later)
                stats.top_candles.append({
                    "timestamp": candle_time.isoformat(),
                    "open": raw.open,
                    "high": raw.high,
                    "low": raw.low,
                    "close": raw.close,
                    "volume": raw.volume,
                    "atr": raw.atr,
                    "upper_wick": raw.wick_size,
                    "wick_atr": raw.wick_atr,
                    "close_location": raw.close_location,
                    "rsi": raw.rsi,
                    "bb_distance": raw.distance_to_upper_bb,
                    "ema_slope": raw.ema_slope,
                    "volume_ratio": raw.volume_ratio,
                    "strict_pass": strict is not None,
                })

            # Sort top candles by wick_atr descending and keep top 20
            stats.top_candles.sort(key=lambda x: x["wick_atr"], reverse=True)
            stats.top_candles = stats.top_candles[:20]

            logger.info(
                "Backfill complete for %s: %d evaluated, %d raw, %d strict, %d inserted",
                symbol, stats.evaluated, stats.raw_candidates, stats.strict_pass, stats.signals_inserted,
            )
            return stats

        except Exception:
            logger.exception("Backfill failed for %s", symbol)
            return stats

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
        total_raw = sum(r.raw_candidates for r in results.values())
        total_strict = sum(r.strict_pass for r in results.values())
        total_inserted = sum(r.signals_inserted for r in results.values())
        total_duplicates = sum(r.duplicates_skipped for r in results.values())

        logger.info(
            "Universe backfill complete: %d symbols, %d evaluated, %d raw, %d strict, %d inserted, %d duplicates",
            len(symbols), total_evaluated, total_raw, total_strict, total_inserted, total_duplicates,
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


def _calculate_diagnostic_stats(values: list[float]) -> DiagnosticStats:
    """Calculate diagnostic statistics for a list of values."""
    if not values:
        return DiagnosticStats()

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    return DiagnosticStats(
        min_val=sorted_vals[0],
        max_val=sorted_vals[-1],
        median=sorted_vals[n // 2],
        p25=sorted_vals[int(n * 0.25)] if n >= 4 else sorted_vals[0],
        p75=sorted_vals[int(n * 0.75)] if n >= 4 else sorted_vals[-1],
        p90=sorted_vals[int(n * 0.9)] if n >= 10 else sorted_vals[-1],
        p95=sorted_vals[int(n * 0.95)] if n >= 20 else sorted_vals[-1],
        p99=sorted_vals[int(n * 0.99)] if n >= 100 else sorted_vals[-1],
        count=n,
    )


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
    total_raw = sum(r.raw_candidates for r in results.values())
    total_strict = sum(r.strict_pass for r in results.values())
    total_inserted = sum(r.signals_inserted for r in results.values())
    total_duplicates = sum(r.duplicates_skipped for r in results.values())

    print("\n" + "=" * 80)
    print("SHADOW BACKFILL COMPLETE")
    print("=" * 80)
    print(f"Symbols processed: {len(results)}")
    print(f"Total evaluated: {total_evaluated}")
    print(f"Total raw candidates: {total_raw}")
    print(f"Total strict pass: {total_strict}")
    print(f"Total inserted: {total_inserted}")
    print(f"Total duplicates skipped: {total_duplicates}")
    print("=" * 80)

    # Print per-symbol breakdown with diagnostics
    print("\nPER-SYMBOL BREAKDOWN:")
    for symbol, stats in sorted(results.items()):
        print(f"\n{symbol}:")
        print(f"  evaluated: {stats.evaluated}")
        print(f"  raw_candidates (upper_wick > 0): {stats.raw_candidates}")
        print(f"  strict_pass: {stats.strict_pass}")
        print(f"  signals_inserted: {stats.signals_inserted}")
        print(f"  duplicates_skipped: {stats.duplicates_skipped}")

        # Print wick_atr distribution statistics (from ALL evaluated candles with upper_wick > 0)
        if stats.all_wick_atr_values:
            wick_stats = _calculate_diagnostic_stats(stats.all_wick_atr_values)
            print(f"\n  Wick ATR Distribution (n={wick_stats.count}):")
            print(f"    min:  {wick_stats.min_val:.4f}")
            print(f"    p25:  {wick_stats.p25:.4f}")
            print(f"    median: {wick_stats.median:.4f}")
            print(f"    p75:  {wick_stats.p75:.4f}")
            print(f"    p90:  {wick_stats.p90:.4f}")
            print(f"    p95:  {wick_stats.p95:.4f}")
            print(f"    p99:  {wick_stats.p99:.4f}")
            print(f"    max:  {wick_stats.max_val:.4f}")

        # Print threshold bucket counts
        if stats.wick_atr_bucket_counts:
            print(f"\n  Threshold Buckets:")
            for bucket, count in sorted(stats.wick_atr_bucket_counts.items()):
                pct = (count / stats.raw_candidates * 100) if stats.raw_candidates > 0 else 0
                print(f"    wick_atr >= {bucket:.2f}: {count} ({pct:.1f}%)")

        # Print TOP candles
        if stats.top_candles:
            print(f"\n  TOP-20 Candles by Wick ATR:")
            for i, candle in enumerate(stats.top_candles[:20], 1):
                print(f"    {i}. {candle['timestamp']}")
                print(f"       OHLC: {candle['open']:.6f} / {candle['high']:.6f} / {candle['low']:.6f} / {candle['close']:.6f}")
                print(f"       ATR: {candle['atr']:.6f}, Wick: {candle['upper_wick']:.6f}, Wick/ATR: {candle['wick_atr']:.4f}")
                print(f"       Close Loc: {candle['close_location']:.4f}, RSI: {candle['rsi']:.2f}")
                print(f"       BB Dist: {candle['bb_distance']:.6f}, EMA Slope: {candle['ema_slope']:.8f}")
                print(f"       Vol Ratio: {candle['volume_ratio']:.4f}, Strict: {candle['strict_pass']}")


if __name__ == "__main__":
    main()