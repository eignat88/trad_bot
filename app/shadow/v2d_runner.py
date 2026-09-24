"""V2_D prospective OOS runner.

Scans raw ATR Wick SHORT candidates, applies V2_D StochRSI filter,
and persists PASS candidates to dds.v2d_signal.

Architecture:
  - Worker threads: lightweight 5m market fetch + raw detection (parallel)
  - Main thread: V2_D filter + sequential DB persistence

Only signals from deploy time forward are recorded (prospective).
No backfill of historical V1 observations.
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
from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner, WickRejectionSignal
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels
from app.shadow.atr_wick_v2d_filter import apply_v2d_filter, EXPERIMENT_ID
from app.shadow.v2d_repository import V2DRepository, V2DSaveStatus

logger = logging.getLogger(__name__)

# Shadow runner uses fewer workers to stay under rate limits
SHADOW_MAX_WORKERS = 6
SHADOW_KLINE_LIMIT = 200

# Rate limit retry settings
MAX_RETRIES = 3
BACKOFF_BASE = 0.5

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

    signal_time = timestamp of the last CLOSED 5m candle.
    """
    candles_5m = client.get_klines(symbol, "5", SHADOW_KLINE_LIMIT)
    if not candles_5m or len(candles_5m) < 50:
        return None

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    five_min_ms = 300_000
    closed_candles = [
        c for c in candles_5m
        if (now_ms - c.timestamp) >= five_min_ms
    ]

    if len(closed_candles) < 50:
        return None

    last_closed = closed_candles[-1]
    signal_time = datetime.fromtimestamp(last_closed.timestamp / 1000, tz=timezone.utc)

    closes = [c.close for c in closed_candles]
    volumes = [c.volume for c in closed_candles]

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
        evaluated_at=signal_time,
    )


class V2DRunner:
    """Runner for V2_D prospective OOS signal collection.

    Architecture:
    - Worker threads: 5m market fetch + raw detection (parallel, NO DB)
    - Main thread: V2_D filter + sequential DB persistence
    """

    def __init__(
        self,
        settings: Settings,
        client: BybitClient,
        v2d_repo: V2DRepository,
    ) -> None:
        self.settings = settings
        self.client = client
        self.v2d_repo = v2d_repo
        self.scanner = AtrWickRejectionShortScanner()
        self._running = False

    def _scan_symbol(self, symbol: str) -> tuple[WickRejectionSignal | None, bool]:
        """Scan one symbol: fetch 5m data, detect RAW candidate.

        Returns:
            (candidate, rate_limited) tuple.
        """
        ctx = _build_shadow_context(self.client, symbol)
        if ctx is None:
            return None, False

        raw = self.scanner.detect_raw_candidate(ctx)
        if raw is None:
            return None, False

        return raw, False

    def _scan_symbol_with_retry(self, symbol: str) -> tuple[WickRejectionSignal | None, bool, bool]:
        """Scan with bounded retry for rate limit errors."""
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

    def _persist_candidate(self, raw: WickRejectionSignal) -> str:
        """Apply V2_D filter and persist if PASS. Main thread only."""
        # Apply V2_D filter
        filter_result = apply_v2d_filter(
            stoch_rsi=raw.stoch_rsi,
            direction="SHORT",
        )

        if not filter_result.passed:
            return "filtered_out"

        # Persist PASS candidate
        result = self.v2d_repo.save_signal(
            symbol=raw.symbol,
            signal_time=raw.signal_time,
            signal_price=raw.signal_price,
            open=raw.open,
            high=raw.high,
            low=raw.low,
            close=raw.close,
            volume=raw.volume,
            atr=raw.atr,
            atr_pct=raw.atr_pct,
            wick_size=raw.wick_size,
            wick_atr=raw.wick_atr,
            upper_wick_pct=raw.upper_wick_pct,
            close_location=raw.close_location,
            rsi=raw.rsi,
            stoch_rsi=raw.stoch_rsi,
            bb_upper=raw.bb_upper,
            bb_mid=raw.bb_mid,
            bb_lower=raw.bb_lower,
            bb_width=raw.bb_width,
            distance_to_upper_bb=raw.distance_to_upper_bb,
            ema_fast=raw.ema_fast,
            ema_medium=raw.ema_medium,
            ema_slow=raw.ema_slow,
            ema_slope=raw.ema_slope,
            volume_ratio=raw.volume_ratio,
            filter_pass=True,
            filter_reason=filter_result.reason,
            signal_version=raw.signal_version,
        )

        if result.status == V2DSaveStatus.INSERTED:
            logger.info(
                "V2_D PASS candidate: symbol=%s signal_time=%s stoch_rsi=%.4f "
                "experiment_id=%s",
                raw.symbol, raw.signal_time, raw.stoch_rsi or 0.0,
                EXPERIMENT_ID,
            )
            return "inserted"
        elif result.status == V2DSaveStatus.DUPLICATE:
            return "duplicate"
        else:
            return "db_error"

    def run_cycle(self) -> dict[str, Any]:
        """Run one complete scan+filter+persist cycle.

        Phase 1 (parallel workers): 5m market fetch + detection, NO DB
        Phase 2 (main thread): V2_D filter + sequential DB persistence
        """
        start_time = datetime.now(timezone.utc)
        symbols = self._get_universe_symbols()

        # ── Phase 1: parallel market fetch + detection ────────
        candidates: list[WickRejectionSignal] = []
        raw_count = 0
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
                        candidates.append(candidate)
                except Exception:
                    logger.exception("V2D scan failed for %s", symbol)
                    scan_errors += 1

        # ── Phase 2: V2_D filter + sequential DB persistence ──
        inserted = 0
        filtered_out = 0
        duplicates = 0
        db_errors = 0

        for raw in candidates:
            status = self._persist_candidate(raw)
            if status == "inserted":
                inserted += 1
            elif status == "filtered_out":
                filtered_out += 1
            elif status == "duplicate":
                duplicates += 1
            else:
                db_errors += 1

        end_time = datetime.now(timezone.utc)

        summary = {
            "experiment": EXPERIMENT_ID,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "symbols_scanned": len(symbols),
            "raw_candidates": raw_count,
            "filter_passed": inserted + duplicates,
            "filter_passed_inserted": inserted,
            "filter_filtered_out": filtered_out,
            "inserted": inserted,
            "duplicates": duplicates,
            "scan_errors": scan_errors,
            "db_errors": db_errors,
            "rate_limit_retries": rate_limit_retries,
        }

        logger.info(
            "V2D scan: symbols=%d raw=%d filter_pass=%d inserted=%d "
            "filtered=%d dupes=%d scan_err=%d db_err=%d",
            len(symbols), raw_count, inserted + duplicates, inserted,
            filtered_out, duplicates, scan_errors, db_errors,
        )

        return summary

    def _get_universe_symbols(self) -> list[str]:
        """Get list of symbols to scan from universe configuration."""
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
                        "V2D Dynamic universe: %d symbols",
                        len(symbols),
                    )
                    return symbols
                logger.warning("V2D Dynamic universe empty, falling back to static")
            except Exception:
                logger.exception("V2D Failed to fetch dynamic universe")

        if self.settings.symbols:
            return list(self.settings.symbols)

        return [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
            "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT",
            "UNIUSDT", "ATOMUSDT", "NEARUSDT", "FTMUSDT", "ALGOUSDT",
            "HBARUSDT", "VETUSDT", "ICPUSDT", "FILUSDT", "AAVEUSDT",
        ]

    def start(self, interval_seconds: int = 300) -> None:
        """Start continuous V2_D signal collection."""
        self._running = True

        def signal_handler(signum, frame):
            logger.info("V2D Received signal %d, stopping...", signum)
            self._running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        logger.info(
            "Starting V2D scanner runner (experiment=%s, interval=%ds)",
            EXPERIMENT_ID, interval_seconds,
        )

        while self._running:
            try:
                summary = self.run_cycle()
            except Exception:
                logger.exception("V2D scan cycle failed")

            if self._running:
                time.sleep(interval_seconds)

        logger.info("V2D scanner runner stopped")


def main() -> None:
    """CLI entrypoint for V2D runner."""
    parser = argparse.ArgumentParser(
        description=f"V2D runner: prospective OOS signal collection ({EXPERIMENT_ID})",
    )
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    settings = load_settings(args.config)

    db_repo = ScannerRepository(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        backend="postgres",
    )

    if not db_repo._use_pg:
        logger.error("PostgreSQL connection required for V2D runner")
        sys.exit(1)

    v2d_repo = V2DRepository(db_repo._conn)
    client = BybitClient(settings)
    runner = V2DRunner(settings, client, v2d_repo)

    if args.once:
        logger.info("Running single V2D scan cycle...")
        summary = runner.run_cycle()
        print("\n" + "=" * 80)
        print(f"V2D SCAN CYCLE COMPLETE — {EXPERIMENT_ID}")
        print("=" * 80)
        print(f"Symbols scanned:   {summary['symbols_scanned']}")
        print(f"Raw candidates:    {summary['raw_candidates']}")
        print(f"Filter passed:     {summary['filter_passed']}")
        print(f"Inserted:          {summary['inserted']}")
        print(f"Filtered out:      {summary['filter_filtered_out']}")
        print(f"Duplicates:        {summary['duplicates']}")
        print(f"Scan errors:       {summary['scan_errors']}")
        print(f"DB errors:         {summary['db_errors']}")
        print("=" * 80)
    else:
        runner.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()
