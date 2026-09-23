"""MFE/MAE evaluation module for shadow signals — multi-horizon incremental.

Horizons are evaluated independently as they mature:
  15m, 30m, 60m, 120m, 240m, EOD

A horizon is "mature" when now >= signal_time + horizon_minutes.
EOD is mature when the next UTC day has started.

No lookahead: when evaluating a horizon, only candles within
[signal_time, signal_time + horizon] are used.
"""
from __future__ import annotations

import argparse
import logging
import signal as _signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.shadow.repository import ShadowSignalRepository

logger = logging.getLogger(__name__)

# Horizon definitions: (name, minutes)
HORIZONS = [
    ("15m", 15),
    ("30m", 30),
    ("60m", 60),
    ("120m", 120),
    ("240m", 240),
]


@dataclass
class HorizonResult:
    mfe: float | None
    mae: float | None
    evaluated_at: datetime | None


def _is_horizon_mature(signal_time: datetime, horizon_minutes: int, now: datetime) -> bool:
    """Check if a horizon has fully matured."""
    return now >= signal_time + timedelta(minutes=horizon_minutes)


def _is_eod_mature(signal_time: datetime, now: datetime) -> bool:
    """Check if EOD has been reached (next UTC day started).

    EOD = end of signal's UTC day (23:59:59).
    """
    signal_day_end = signal_time.replace(hour=23, minute=59, second=59, microsecond=0)
    return now > signal_day_end


def _calculate_mfe_mae_for_window(
    candles: list[Candle],
    entry_price: float,
    max_minutes: int | None,  # None = until end of day
    signal_time: datetime,
) -> tuple[float | None, float | None]:
    """Calculate MFE/MAE using only candles within the allowed window.

    For SHORT signals:
    - MFE = max price drop (positive = good)
    - MAE = max price rise (positive = bad)
    """
    if not candles:
        return None, None

    # Filter candles: only use those after signal_time and within horizon
    cutoff_ts = None
    if max_minutes is not None:
        cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    signal_ts = int(signal_time.timestamp() * 1000)
    filtered = []
    for c in candles:
        if c.timestamp <= signal_ts:
            continue
        if cutoff_ts and c.timestamp > cutoff_ts:
            continue
        filtered.append(c)

    if not filtered:
        return None, None

    max_favorable = 0.0
    max_adverse = 0.0

    for candle in filtered:
        # For SHORT: favorable = price goes down, adverse = price goes up
        favorable = (entry_price - candle.low) / entry_price * 100
        adverse = (candle.high - entry_price) / entry_price * 100
        max_favorable = max(max_favorable, favorable)
        max_adverse = max(max_adverse, adverse)

    return max_favorable, max_adverse


def _check_target_achievement(
    candles: list[Candle],
    signal_price: float,
) -> dict[str, bool]:
    """Check if price reached specific targets.

    For SHORT signals:
    - Reached -0.5% = price dropped 0.5% from entry
    - Reached -1.0% = price dropped 1.0% from entry
    - etc.

    Also checks if adverse excursion hit before target.
    """
    results = {
        "reached_minus_0_5": False,
        "reached_minus_1_0": False,
        "reached_minus_1_5": False,
        "reached_minus_2_0": False,
        "hit_plus_0_5_before_target": False,
        "hit_plus_1_0_before_target": False,
        "hit_plus_1_5_before_target": False,
    }

    # Target prices (for SHORT: price needs to drop)
    targets = {
        0.5: "reached_minus_0_5",
        1.0: "reached_minus_1_0",
        1.5: "reached_minus_1_5",
        2.0: "reached_minus_2_0",
    }

    # Adverse excursion thresholds
    adverse_thresholds = {
        0.5: "hit_plus_0_5_before_target",
        1.0: "hit_plus_1_0_before_target",
        1.5: "hit_plus_1_5_before_target",
    }

    # Track which targets have been reached
    reached_targets: dict[float, bool] = {t: False for t in targets}
    hit_adverse: dict[float, bool] = {t: False for t in adverse_thresholds}

    for candle in candles:
        # Check if price dropped to target
        for target_pct, key in targets.items():
            if not reached_targets[target_pct]:
                target_price = signal_price * (1 - target_pct / 100)
                if candle.low <= target_price:
                    reached_targets[target_pct] = True
                    results[key] = True

        # Check if price rose to adverse threshold
        for adverse_pct, key in adverse_thresholds.items():
            if not hit_adverse[adverse_pct]:
                adverse_price = signal_price * (1 + adverse_pct / 100)
                if candle.high >= adverse_price:
                    hit_adverse[adverse_pct] = True
                    # Check if this happened before any target was reached
                    if not any(reached_targets.values()):
                        results[key] = True

    return results


class ShadowSignalEvaluator:
    """Evaluates post-signal price movement (MFE/MAE) for shadow signals.

    Multi-horizon incremental evaluation:
    - Only evaluates mature horizons (now >= signal_time + horizon_minutes)
    - EOD is evaluated when the next UTC day starts
    - No lookahead: only candles within [signal_time, signal_time + horizon]
    - Groups signals by symbol for efficient candle fetching
    """

    def __init__(
        self,
        client: BybitClient,
        repo: ShadowSignalRepository,
    ) -> None:
        self.client = client
        self.repo = repo
        self._running = False

    def run_evaluation_cycle(self) -> dict[str, Any]:
        """Run one complete evaluation cycle with multi-horizon incremental updates."""
        now = datetime.now(timezone.utc)
        start_time = now

        # Get eligible signals
        eligible = self.repo.get_eligible_signals(limit=5000)

        # Group by symbol for efficient candle fetching
        by_symbol: dict[str, list[dict]] = {}
        for sig in eligible:
            by_symbol.setdefault(sig["symbol"], []).append(sig)

        stats = {
            "start_time": start_time.isoformat(),
            "signals_checked": len(eligible),
            "symbols_processed": 0,
            "horizons_updated": {"15m": 0, "30m": 0, "60m": 0, "120m": 0, "240m": 0, "eod": 0},
            "outcomes_created": 0,
            "outcomes_updated": 0,
            "finalized": 0,
            "errors": 0,
        }

        for symbol, signals in by_symbol.items():
            stats["symbols_processed"] += 1

            # Find the oldest signal that needs candles
            oldest_signal_time = min(s["signal_time"] for s in signals)

            # Fetch candles from just before oldest signal to now
            candles = self._get_candles_for_symbol(symbol, oldest_signal_time, now)
            if not candles:
                stats["errors"] += len(signals)
                continue

            for sig in signals:
                try:
                    self._evaluate_signal_incremental(
                        sig, candles, now, stats,
                    )
                except Exception:
                    stats["errors"] += 1
                    logger.exception("Error evaluating signal %d", sig["signal_id"])

        end_time = datetime.now(timezone.utc)
        stats["end_time"] = end_time.isoformat()

        logger.info(
            "Shadow evaluation cycle: checked=%d symbols=%d created=%d updated=%d finalized=%d errors=%d",
            stats["signals_checked"], stats["symbols_processed"],
            stats["outcomes_created"], stats["outcomes_updated"],
            stats["finalized"], stats["errors"],
        )
        logger.info("Horizons: %s", stats["horizons_updated"])

        return stats

    def _evaluate_signal_incremental(
        self,
        sig: dict,
        all_candles: list[Candle],
        now: datetime,
        stats: dict,
    ) -> None:
        """Evaluate a single signal, updating only mature horizons."""
        signal_time = sig["signal_time"]
        signal_price = sig["signal_price"]
        signal_id = sig["signal_id"]
        symbol = sig["symbol"]

        # Filter candles for this signal (after signal_time)
        signal_ts = int(signal_time.timestamp() * 1000)
        post_candles = [c for c in all_candles if c.timestamp > signal_ts]

        # Determine which horizons are mature and need evaluation
        updates: dict[str, Any] = {}
        is_new = sig["outcome_id"] is None

        for horizon_name, horizon_minutes in HORIZONS:
            # Check if already evaluated
            evaluated_field = f"evaluated_{horizon_name}_at"
            if sig.get(evaluated_field) is not None:
                continue  # Already evaluated

            # Check maturity
            if not _is_horizon_mature(signal_time, horizon_minutes, now):
                continue  # Not mature yet

            # Calculate MFE/MAE for this horizon
            mfe, mae = _calculate_mfe_mae_for_window(
                post_candles, signal_price, horizon_minutes, signal_time,
            )
            updates[f"mfe_{horizon_name}"] = mfe
            updates[f"mae_{horizon_name}"] = mae
            updates[evaluated_field] = now
            stats["horizons_updated"][horizon_name] += 1

        # Check EOD
        if sig.get("evaluated_eod_at") is None and _is_eod_mature(signal_time, now):
            mfe_eod, mae_eod = _calculate_mfe_mae_for_window(
                post_candles, signal_price, None, signal_time,
            )
            updates["mfe_eod"] = mfe_eod
            updates["mae_eod"] = mae_eod
            updates["evaluated_eod_at"] = now
            stats["horizons_updated"]["eod"] += 1

        # Check if all horizons are done → finalize
        all_done = all(sig.get(f"evaluated_{h}_at") is not None for h, _ in HORIZONS)
        all_done = all_done and sig.get("evaluated_eod_at") is not None

        if all_done and not sig.get("is_final"):
            # Calculate target flags using the FULL observation window
            target_flags = _check_target_achievement(post_candles, signal_price)
            updates.update(target_flags)
            updates["is_final"] = True
            stats["finalized"] += 1

        if not updates:
            return

        # Save
        created_or_updated = self.repo.save_outcome_partial(
            signal_id=signal_id, symbol=symbol, **updates,
        )
        if created_or_updated:
            if is_new:
                stats["outcomes_created"] += 1
            else:
                stats["outcomes_updated"] += 1

    def _get_candles_for_symbol(
        self,
        symbol: str,
        from_time: datetime,
        to_time: datetime,
    ) -> list[Candle]:
        """Fetch 5m candles from exchange for the required time range."""
        try:
            # Include a small buffer before from_time
            start_ms = int((from_time - timedelta(minutes=5)).timestamp() * 1000)
            end_ms = int(to_time.timestamp() * 1000)

            # Calculate how many candles we need
            diff_ms = end_ms - start_ms
            num_candles = min(diff_ms // 300_000 + 10, 1000)  # 5m = 300s = 300_000ms

            candles = self.client.get_klines(symbol, "5", num_candles)

            # Filter to our range
            signal_candles = [
                c for c in candles
                if start_ms <= c.timestamp <= end_ms
            ]

            return signal_candles
        except Exception:
            logger.exception("Failed to fetch candles for %s", symbol)
            return []

    def start(self, interval_seconds: int = 300) -> None:
        """Start continuous shadow signal evaluation."""
        self._running = True

        def signal_handler(signum, frame):
            logger.info("Received signal %d, stopping shadow evaluator...", signum)
            self._running = False

        _signal.signal(_signal.SIGINT, signal_handler)
        _signal.signal(_signal.SIGTERM, signal_handler)

        logger.info(
            "Starting shadow evaluator (interval=%ds)",
            interval_seconds,
        )

        while self._running:
            try:
                summary = self.run_evaluation_cycle()
                logger.info(
                    "Evaluation cycle complete: checked=%d finalized=%d",
                    summary["signals_checked"], summary["finalized"],
                )
            except Exception:
                logger.exception("Shadow evaluation cycle failed")

            if self._running:
                time.sleep(interval_seconds)

        logger.info("Shadow evaluator stopped")


def main() -> None:
    """CLI entrypoint for shadow evaluator."""
    parser = argparse.ArgumentParser(
        description="Shadow evaluator: calculate MFE/MAE for ATR Wick Rejection Short signals",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single evaluation cycle and exit",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=300,
        help="Interval between evaluation cycles in seconds (default: 300)",
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
        logger.error("PostgreSQL connection required for shadow evaluator")
        sys.exit(1)

    # Create shadow repository
    shadow_repo = ShadowSignalRepository(db_repo._conn)

    # Create Bybit client
    client = BybitClient(settings)

    # Create evaluator
    evaluator = ShadowSignalEvaluator(client, shadow_repo)

    if args.once:
        # Single cycle
        logger.info("Running single shadow evaluation cycle...")
        summary = evaluator.run_evaluation_cycle()
        print("\n" + "=" * 80)
        print("SHADOW EVALUATION CYCLE COMPLETE")
        print("=" * 80)
        print(f"Signals checked: {summary['signals_checked']}")
        print(f"Outcomes created: {summary['outcomes_created']}")
        print(f"Outcomes updated: {summary['outcomes_updated']}")
        print(f"Finalized: {summary['finalized']}")
        print(f"Errors: {summary['errors']}")
        print(f"Horizons: {summary['horizons_updated']}")
        print("=" * 80)
    else:
        # Continuous mode
        evaluator.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()
