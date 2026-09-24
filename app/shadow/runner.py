"""Shadow scanner runner for real-time signal collection.

Architecture:
- Worker threads: lightweight 5m market fetch + detection (parallel, NO DB)
- Main thread: sequential DB persistence via single connection

Only fetches 5m candles (1 API call per symbol instead of 5).
signal_time = timestamp of the last CLOSED 5m candle, not wall-clock.
"""
from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient, BybitError
from app.exchange.api_telemetry import ApiTelemetry
from app.models import Candle
from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner, WickRejectionSignal
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels
from app.shadow.repository import SaveSignalStatus, ShadowSignalRepository

logger = logging.getLogger(__name__)

# Shadow scanner only needs 5m candles
SHADOW_KLINE_LIMIT = 200

# Rate limit retry settings
MAX_RETRIES = 3
BACKOFF_BASE = 0.5  # seconds

# Shadow runner uses fewer workers to stay under rate limits
SHADOW_MAX_WORKERS = 6

# Bybit rate limit error phrases
_RATE_LIMIT_PHRASES = ("Too many visits", "Exceeded the API Rate Limit")


def _is_rate_limit_error(exc: Exception) -> bool:
    """Check if an exception is a Bybit rate limit error."""
    msg = str(exc)
    return any(phrase in msg for phrase in _RATE_LIMIT_PHRASES)


def _build_shadow_context(
    client: BybitClient,
    symbol: str,
) -> MarketContext | None:
    """Build a lightweight MarketContext using only 5m candles.

    Unlike build_market_context() which fetches 5 timeframes (5 calls),
    this fetches only 5m candles (1 call per symbol).

    signal_time = timestamp of the last CLOSED 5m candle.

    Returns None if fetch fails or insufficient data.
    """
    candles_5m = client.get_klines(symbol, "5", SHADOW_KLINE_LIMIT)
    if not candles_5m or len(candles_5m) < 50:
        return None

    # Filter out the forming (unclosed) candle if present.
    # Bybit may return the current candle which is still open.
    # A closed candle has its timestamp < now().
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    five_min_ms = 300_000
    # The last candle is "closed" if its timestamp is at least one
    # full 5m interval behind current time.
    # More precisely: a candle is closed if (now - candle.timestamp) >= 5m
    closed_candles = [
        c for c in candles_5m
        if (now_ms - c.timestamp) >= five_min_ms
    ]

    if len(closed_candles) < 50:
        return None

    # signal_time = timestamp of the last closed candle
    last_closed = closed_candles[-1]
    signal_time = datetime.fromtimestamp(last_closed.timestamp / 1000, tz=timezone.utc)

    # Build minimal indicator snapshot (computed from 5m data)
    closes = [c.close for c in closed_candles]
    volumes = [c.volume for c in closed_candles]

    # Basic indicators from 5m data
    from app.indicators import (
        atr_wilder, bollinger_bands, ema, ema_slope, rsi_wilder,
    )
    from app.scanners.context_builder import _classify_market_regime

    try:
        atr_val = atr_wilder(closed_candles, 14) if len(closed_candles) > 14 else 0
    except ValueError:
        atr_val = 0
    try:
        rsi_val = rsi_wilder(closes, 14) if len(closes) > 14 else 50.0
    except ValueError:
        rsi_val = 50.0
    ema20_val = ema(closes, 20) if len(closes) >= 20 else closes[-1]
    ema50_val = ema(closes, 50) if len(closes) >= 50 else closes[-1]
    ema200_val = ema(closes, 200) if len(closes) >= 200 else closes[-1]
    vol_sma = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0
    bb_upper, bb_mid, bb_lower = (0.0, 0.0, 0.0)
    if len(closes) >= 20:
        bb_upper, bb_mid, bb_lower = bollinger_bands(closes, 20)
    bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0
    adx_val = 0.0
    ema50_slope_val = ema_slope(closes, 50, lookback=5) if len(closes) >= 55 else 0.0

    indicators = IndicatorSnapshot(
        atr=atr_val, rsi=rsi_val, ema20=ema20_val, ema50=ema50_val,
        ema200=ema200_val, bb_upper=bb_upper, bb_lower=bb_lower,
        bb_width=bb_width, volume_sma=vol_sma,
        adx=adx_val, ema50_slope=ema50_slope_val,
    )

    return MarketContext(
        symbol=symbol,
        candles_5m=tuple(closed_candles),
        candles_15m=(),
        candles_1h=(),
        candles_4h=(),
        indicators=indicators,
        market_regime=_classify_market_regime(indicators, closed_candles[-1].close),
        levels=MarketLevels(),
        evaluated_at=signal_time,  # <-- candle timestamp, NOT datetime.now()
    )


class ShadowScannerRunner:
    """Runner for real-time shadow signal collection.

    Architecture:
    - Worker threads: lightweight 5m market fetch + detection (parallel, NO DB)
    - Main thread: sequential DB persistence via single connection

    Only 1 API call per symbol (5m klines) instead of 5.
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

    def _scan_symbol(self, symbol: str) -> tuple[WickRejectionSignal | None, bool]:
        """Scan one symbol: fetch 5m data, detect RAW candidate.

        NO DB operations — pure computation + API call.

        Returns:
            (candidate, rate_limited) tuple.
            candidate is None if no raw candidate found.
            rate_limited is True if the attempt hit a rate limit.
        """
        ctx = _build_shadow_context(self.client, symbol)
        if ctx is None:
            return None, False

        raw = self.scanner.detect_raw_candidate(ctx)
        if raw is None:
            return None, False

        strict = self.scanner.passes_strict_filters(raw)
        candidate = WickRejectionSignal(
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
        return candidate, False

    def _scan_symbol_with_retry(self, symbol: str) -> tuple[WickRejectionSignal | None, bool, bool]:
        """Scan with bounded retry for rate limit errors.

        Returns:
            (candidate, rate_limited, retry_used)
        """
        last_exc: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                candidate, _ = self._scan_symbol(symbol)
                return candidate, False, attempt > 0
            except Exception as exc:
                last_exc = exc
                if _is_rate_limit_error(exc) and attempt < MAX_RETRIES - 1:
                    backoff = BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.3)
                    logger.warning(
                        "Rate limit for %s (attempt %d/%d), retrying in %.1fs",
                        symbol, attempt + 1, MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                else:
                    break

        raise last_exc  # type: ignore[misc]

    def _persist_candidate(self, signal: WickRejectionSignal) -> str:
        """Persist a single candidate via repo. Main thread only."""
        result = self.repo.save_signal(signal)
        if result.status == SaveSignalStatus.INSERTED:
            return "inserted"
        elif result.status == SaveSignalStatus.DUPLICATE:
            return "duplicate"
        else:
            return "db_error"

    def run_cycle(self) -> dict[str, Any]:
        """Run one complete scan+persist cycle.

        Phase 1 (parallel workers): 5m market fetch + detection, NO DB
        Phase 2 (main thread): sequential DB persistence
        """
        start_time = datetime.now(timezone.utc)
        symbols = self._get_universe_symbols()

        # Begin API telemetry cycle for shadow runner
        telemetry = ApiTelemetry.get_instance()
        telemetry.begin_cycle(symbols=symbols)

        # ── Phase 1: parallel market fetch + detection ────────
        candidates: list[WickRejectionSignal] = []
        raw_count = 0
        strict_count = 0
        scan_errors = 0
        rate_limit_retries = 0

        with ThreadPoolExecutor(max_workers=SHADOW_MAX_WORKERS) as executor:
            future_to_symbol = {
                executor.submit(self._scan_symbol_with_retry, symbol): symbol
                for symbol in symbols
            }
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    candidate, rate_limited, retry_used = future.result()
                    if retry_used:
                        rate_limit_retries += 1
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
            "rate_limit_retries": rate_limit_retries,
        }

        logger.info(
            "Shadow scan: symbols=%d raw=%d strict=%d "
            "inserted=%d dupes=%d scan_err=%d db_err=%d rate_limit_retries=%d",
            len(symbols), raw_count, strict_count,
            inserted, duplicates, scan_errors, db_errors, rate_limit_retries,
        )

        # End API telemetry cycle for shadow runner
        shadow_summary = telemetry.end_cycle()
        if shadow_summary.total_calls > 0:
            logger.info(
                "shadow_cycle api_telemetry: total_calls=%d success=%d "
                "rate_limited=%d peak_rps=%.1f",
                shadow_summary.total_calls, shadow_summary.success,
                shadow_summary.rate_limited, shadow_summary.peak_rps,
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
                # run_cycle() already logs the full summary; no duplicate needed.
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
        print(f"Rate limit retries:{summary['rate_limit_retries']}")
        print("=" * 80)
    else:
        # Continuous mode
        runner.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()