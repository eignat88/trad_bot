"""MFE/MAE evaluation module for shadow signals."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from app.config import load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.shadow.repository import ShadowSignalRepository

logger = logging.getLogger(__name__)


class ShadowSignalEvaluator:
    """Evaluates post-signal price movement (MFE/MAE) for shadow signals.

    For SHORT signals:
    - MFE = how much price dropped in our favor (positive = good)
    - MAE = how much price rose against us (positive = bad)
    """

    def __init__(
        self,
        client: BybitClient,
        repo: ShadowSignalRepository,
    ) -> None:
        self.client = client
        self.repo = repo
        self._running = False

    def evaluate_signal(self, signal_data: dict) -> dict | None:
        """Evaluate MFE/MAE for a single shadow signal.

        Args:
            signal_data: Signal data from database

        Returns:
            Dictionary with MFE/MAE results or None if evaluation fails
        """
        symbol = signal_data["symbol"]
        signal_time = signal_data["signal_time"]
        signal_price = signal_data["signal_price"]

        try:
            # Get candles after signal time
            candles = self._get_post_signal_candles(symbol, signal_time)
            if not candles:
                logger.warning("No post-signal candles for %s at %s", symbol, signal_time)
                return None

            # Calculate MFE/MAE for different horizons
            horizons = {
                "15m": 15,
                "30m": 30,
                "60m": 60,
                "120m": 120,
                "240m": 240,
            }

            results: dict[str, Any] = {}

            for horizon_name, horizon_minutes in horizons.items():
                mfe, mae = self._calculate_mfe_mae(
                    candles, signal_price, horizon_minutes
                )
                results[f"mfe_{horizon_name}"] = mfe
                results[f"mae_{horizon_name}"] = mae

            # Calculate EOD results
            mfe_eod, mae_eod = self._calculate_mfe_mae(
                candles, signal_price, None  # None = until end of day
            )
            results["mfe_eod"] = mfe_eod
            results["mae_eod"] = mae_eod

            # Check target achievement
            target_results = self._check_target_achievement(candles, signal_price)
            results.update(target_results)

            return results

        except Exception:
            logger.exception("Failed to evaluate signal for %s", symbol)
            return None

    def _get_post_signal_candles(
        self,
        symbol: str,
        signal_time: datetime,
    ) -> list[Candle]:
        """Get candles after signal time."""
        try:
            # Get candles from exchange
            # Request enough candles to cover longest horizon (4 hours = 48 candles)
            candles = self.client.get_klines(symbol, "5", 100)

            # Filter to post-signal candles
            signal_ts = int(signal_time.timestamp() * 1000)
            return [c for c in candles if c.timestamp > signal_ts]

        except Exception:
            logger.exception("Failed to get post-signal candles for %s", symbol)
            return []

    def _calculate_mfe_mae(
        self,
        candles: list[Candle],
        entry_price: float,
        horizon_minutes: int | None,
    ) -> tuple[float | None, float | None]:
        """Calculate MFE and MAE for a given horizon.

        For SHORT signals:
        - MFE = max price drop (positive when price goes down)
        - MAE = max price rise (positive when price goes up)

        Returns:
            Tuple of (mfe, mae) as percentages
        """
        if not candles:
            return None, None

        # Filter candles to horizon
        if horizon_minutes is not None:
            max_candles = horizon_minutes // 5  # 5m candles
            candles = candles[:max_candles]

        if not candles:
            return None, None

        # Calculate price movements
        max_favorable = 0.0  # Max drop from entry (positive = good for SHORT)
        max_adverse = 0.0    # Max rise from entry (positive = bad for SHORT)

        for candle in candles:
            # For SHORT: favorable = price goes down, adverse = price goes up
            favorable = (entry_price - candle.low) / entry_price * 100
            adverse = (candle.high - entry_price) / entry_price * 100

            max_favorable = max(max_favorable, favorable)
            max_adverse = max(max_adverse, adverse)

        return max_favorable, max_adverse

    def _check_target_achievement(
        self,
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

    def evaluate_pending_signals(self, min_age_minutes: int = 60) -> int:
        """Evaluate all pending shadow signals.

        Returns number of signals evaluated.
        """
        # Get signals without outcomes
        pending = self.repo.get_signals_without_outcomes(min_age_minutes)

        evaluated = 0
        for signal_data in pending:
            outcome = self.evaluate_signal(signal_data)
            if outcome:
                # Save outcome to database
                if self.repo.save_outcome(
                    signal_id=signal_data["signal_id"],
                    **outcome,
                ):
                    evaluated += 1

        logger.info(
            "Evaluated %d shadow signals (pending: %d)",
            evaluated, len(pending),
        )
        return evaluated

    def run_evaluation_cycle(self) -> dict[str, Any]:
        """Run one complete evaluation cycle.

        Returns summary of the evaluation.
        """
        start_time = datetime.now(timezone.utc)

        # Evaluate pending signals
        evaluated = self.evaluate_pending_signals(min_age_minutes=60)

        end_time = datetime.now(timezone.utc)

        summary = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "signals_evaluated": evaluated,
        }

        logger.info(
            "Shadow evaluation cycle complete: %d signals evaluated",
            evaluated,
        )

        return summary

    def start(self, interval_seconds: int = 300) -> None:
        """Start continuous shadow signal evaluation.

        Args:
            interval_seconds: Interval between evaluation cycles (default: 300 = 5 minutes)
        """
        self._running = True

        def signal_handler(signum, frame):
            logger.info("Received signal %d, stopping shadow evaluator...", signum)
            self._running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        logger.info(
            "Starting shadow evaluator (interval=%ds)",
            interval_seconds,
        )

        while self._running:
            try:
                summary = self.run_evaluation_cycle()
                logger.info(
                    "Evaluation cycle complete: %d signals evaluated",
                    summary["signals_evaluated"],
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
        "--min-age-minutes",
        type=int,
        default=60,
        help="Minimum age of signals to evaluate in minutes (default: 60)",
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
        print(f"Signals evaluated: {summary['signals_evaluated']}")
        print("=" * 80)
    else:
        # Continuous mode
        evaluator.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()