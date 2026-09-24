"""V2_D prospective OOS evaluator — multi-horizon incremental.

Horizons evaluated: 15m, 30m, 60m, 120m, 240m (no EOD for V2_D).

Identical logic to the V1 shadow evaluator, but reads from
dds.v2d_signal / dds.v2d_outcome tables.
"""
from __future__ import annotations

import argparse
import logging
import signal as _signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.shadow.v2d_repository import V2DRepository

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "ATR_WICK_FILTER_OOS_V2_D"

# Horizon definitions: (name, minutes)
HORIZONS: list[tuple[str, int]] = [
    ("15m", 15),
    ("30m", 30),
    ("60m", 60),
    ("120m", 120),
    ("240m", 240),
]


def _is_horizon_mature(signal_time: datetime, horizon_minutes: int, now: datetime) -> bool:
    """Return True when the full horizon window has elapsed."""
    return now >= signal_time + timedelta(minutes=horizon_minutes)


def _calculate_mfe_mae_for_window(
    candles: list[Candle],
    entry_price: float,
    max_minutes: int,
    signal_time: datetime,
) -> tuple[float | None, float | None]:
    """MFE/MAE for SHORT using only candles in [signal_time, signal_time + max_minutes)."""
    if not candles:
        return None, None

    signal_ts = int(signal_time.timestamp() * 1000)
    cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    max_favorable = 0.0
    max_adverse = 0.0
    found = False

    for c in candles:
        if c.timestamp <= signal_ts:
            continue
        if c.timestamp >= cutoff_ts:
            continue
        found = True
        fav = (entry_price - c.low) / entry_price * 100
        adv = (c.high - entry_price) / entry_price * 100
        max_favorable = max(max_favorable, fav)
        max_adverse = max(max_adverse, adv)

    if not found:
        return None, None
    return max_favorable, max_adverse


def _check_target_achievement(
    candles: list[Candle],
    signal_price: float,
) -> dict[str, bool]:
    """Target flags for V2_D using all available candles."""
    results: dict[str, bool] = {
        "reached_minus_0_5": False, "reached_minus_1_0": False,
        "reached_minus_1_5": False, "reached_minus_2_0": False,
        "hit_plus_0_5_before_target": False,
        "hit_plus_1_0_before_target": False,
        "hit_plus_1_5_before_target": False,
    }

    targets = {0.5: "reached_minus_0_5", 1.0: "reached_minus_1_0",
               1.5: "reached_minus_1_5", 2.0: "reached_minus_2_0"}
    adverse = {0.5: "hit_plus_0_5_before_target",
               1.0: "hit_plus_1_0_before_target",
               1.5: "hit_plus_1_5_before_target"}

    hit_targets: dict[float, bool] = {t: False for t in targets}
    hit_adverse: dict[float, bool] = {a: False for a in adverse}

    for c in candles:
        for pct, key in targets.items():
            if not hit_targets[pct] and c.low <= signal_price * (1 - pct / 100):
                hit_targets[pct] = True
                results[key] = True
        for pct, key in adverse.items():
            if not hit_adverse[pct] and c.high >= signal_price * (1 + pct / 100):
                hit_adverse[pct] = True
                if not any(hit_targets.values()):
                    results[key] = True

    return results


class V2DEvaluator:
    """Multi-horizon incremental evaluator for V2_D signals."""

    def __init__(self, client: BybitClient, repo: V2DRepository) -> None:
        self.client = client
        self.repo = repo
        self._running = False

    def _get_candles_for_symbol(
        self, symbol: str, from_time: datetime, to_time: datetime,
    ) -> list[Candle]:
        """Fetch 5m candles for *symbol* covering [from_time, to_time]."""
        try:
            start_ms = int((from_time - timedelta(minutes=5)).timestamp() * 1000)
            end_ms = int(to_time.timestamp() * 1000)
            needed = min((end_ms - start_ms) // 300_000 + 10, 1000)
            candles = self.client.get_klines(symbol, "5", needed)
            return [c for c in candles if start_ms <= c.timestamp <= end_ms]
        except Exception:
            logger.exception("V2D Failed to fetch candles for %s", symbol)
            return []

    def _evaluate_signal_incremental(
        self,
        sig: dict,
        all_candles: list[Candle],
        now: datetime,
        stats: dict,
    ) -> None:
        """Evaluate a single V2_D signal, updating only mature horizons."""
        signal_time: datetime = sig["signal_time"]
        signal_price: float = sig["signal_price"]
        signal_id: int = sig["signal_id"]
        symbol: str = sig["symbol"]
        is_new = sig["outcome_id"] is None

        signal_ts = int(signal_time.timestamp() * 1000)
        post_candles = [c for c in all_candles if c.timestamp > signal_ts]

        updates: dict[str, Any] = {}

        # time-horizons
        for label, minutes in HORIZONS:
            eval_field = f"evaluated_{label}_at"
            if sig.get(eval_field) is not None:
                continue
            if not _is_horizon_mature(signal_time, minutes, now):
                continue

            mfe, mae = _calculate_mfe_mae_for_window(
                post_candles, signal_price, minutes, signal_time,
            )
            updates[f"mfe_{label}"] = mfe
            updates[f"mae_{label}"] = mae
            updates[eval_field] = now

        # finalization — effective state = sig OR updates
        all_done = all(
            sig.get(f"evaluated_{h}_at") is not None or f"evaluated_{h}_at" in updates
            for h, _ in HORIZONS
        )

        if all_done and not sig.get("is_final"):
            target_flags = _check_target_achievement(post_candles, signal_price)
            updates.update(target_flags)
            updates["is_final"] = True

        if not updates:
            return

        saved = self.repo.save_outcome_partial(signal_id=signal_id, symbol=symbol, **updates)

        if not saved:
            stats["errors"] += 1
            return

        for label, _ in HORIZONS:
            eval_field = f"evaluated_{label}_at"
            if eval_field in updates and sig.get(eval_field) is None:
                stats["horizons_updated"][label] += 1

        if is_new:
            stats["outcomes_created"] += 1
        else:
            stats["outcomes_updated"] += 1

        if updates.get("is_final"):
            stats["finalized"] += 1

    def run_evaluation_cycle(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        start = now

        eligible = self.repo.get_eligible_signals(limit=5000)

        by_symbol: dict[str, list[dict]] = {}
        for s in eligible:
            by_symbol.setdefault(s["symbol"], []).append(s)

        stats: dict[str, Any] = {
            "experiment": EXPERIMENT_ID,
            "start_time": start.isoformat(),
            "signals_checked": len(eligible),
            "symbols_processed": 0,
            "horizons_updated": {h: 0 for h, _ in HORIZONS},
            "outcomes_created": 0,
            "outcomes_updated": 0,
            "finalized": 0,
            "errors": 0,
        }

        for symbol, signals in by_symbol.items():
            stats["symbols_processed"] += 1
            oldest = min(s["signal_time"] for s in signals)
            candles = self._get_candles_for_symbol(symbol, oldest, now)
            if not candles:
                stats["errors"] += len(signals)
                continue
            for s in signals:
                try:
                    self._evaluate_signal_incremental(s, candles, now, stats)
                except Exception:
                    stats["errors"] += 1
                    logger.exception("V2D Error evaluating signal %d", s["signal_id"])

        stats["end_time"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "V2D evaluation: checked=%d symbols=%d created=%d updated=%d "
            "finalized=%d errors=%d",
            stats["signals_checked"], stats["symbols_processed"],
            stats["outcomes_created"], stats["outcomes_updated"],
            stats["finalized"], stats["errors"],
        )
        return stats

    def start(self, interval_seconds: int = 300) -> None:
        self._running = True

        def _stop(signum: int, frame: Any) -> None:
            logger.info("V2D Received signal %d, stopping evaluator…", signum)
            self._running = False

        _signal.signal(_signal.SIGINT, _stop)
        _signal.signal(_signal.SIGTERM, _stop)
        logger.info("Starting V2D evaluator (experiment=%s, interval=%ds)", EXPERIMENT_ID, interval_seconds)

        while self._running:
            try:
                self.run_evaluation_cycle()
            except Exception:
                logger.exception("V2D Evaluation cycle failed")
            if self._running:
                time.sleep(interval_seconds)
        logger.info("V2D evaluator stopped")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"V2D evaluator — incremental MFE/MAE by horizon ({EXPERIMENT_ID})",
    )
    parser.add_argument("--once", action="store_true", help="Single cycle then exit")
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
        host=settings.db_host, port=settings.db_port,
        database=settings.db_name, user=settings.db_user,
        password=settings.db_password, backend="postgres",
    )
    if not db_repo._use_pg:
        logger.error("PostgreSQL required")
        sys.exit(1)

    v2d_repo = V2DRepository(db_repo._conn)
    client = BybitClient(settings)
    evaluator = V2DEvaluator(client, v2d_repo)

    if args.once:
        summary = evaluator.run_evaluation_cycle()
        print("\n" + "=" * 80)
        print(f"V2D EVALUATION CYCLE COMPLETE — {EXPERIMENT_ID}")
        print("=" * 80)
        print(f"Signals checked:    {summary['signals_checked']}")
        print(f"Symbols processed:  {summary['symbols_processed']}")
        print(f"Horizons updated:   {summary['horizons_updated']}")
        print(f"Outcomes created:   {summary['outcomes_created']}")
        print(f"Outcomes updated:   {summary['outcomes_updated']}")
        print(f"Finalized:          {summary['finalized']}")
        print(f"Errors:             {summary['errors']}")
        print("=" * 80)
    else:
        evaluator.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()
