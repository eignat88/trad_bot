"""Evaluate forward outcomes for saved scanner setups.

Read-only worker that:
  - Connects to existing PostgreSQL schema (created by deployment migrations)
  - Uses per-timeframe maturity horizons (5m=240min, 15m=720min)
  - Is idempotent (ON CONFLICT DO UPDATE at DB level)
  - Never modifies scanner logic or frozen parameters
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import asdict

from app.config import load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.scanners.models import SetupCandidate
from app.scanners.outcome import SignalOutcome, evaluate_setup_outcome

logger = logging.getLogger(__name__)

TIMEFRAME_TO_BYBIT_INTERVAL = {
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
}

# Maturity horizons per timeframe (minutes).
# Setup must be at least this old before outcome evaluation.
# 5m  × 48 bars = 240 minutes
# 15m × 48 bars = 720 minutes
TIMEFRAME_MATURITY_MINUTES: dict[str, int] = {
    "5m": 240,
    "15m": 720,
    "1h": 2880,   # 48h
    "4h": 11520,  # 8 days
}


def bybit_interval_for_timeframe(timeframe: str) -> str:
    try:
        return TIMEFRAME_TO_BYBIT_INTERVAL[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported entry timeframe for outcome evaluation: {timeframe}") from exc


def fetch_outcome_candles(
    client: BybitClient,
    setup: SetupCandidate,
    *,
    max_bars: int,
) -> list[Candle]:
    interval = bybit_interval_for_timeframe(setup.entry_timeframe)
    limit = min(1000, max(200, max_bars + 50))
    candles = client.get_klines(setup.symbol, interval, limit)
    return [c for c in candles if c.timestamp > setup.signal_candle_open_time][:max_bars]


def _maturity_for_timeframe(timeframe: str) -> int:
    """Return maturity in minutes for a given timeframe."""
    return TIMEFRAME_MATURITY_MINUTES.get(timeframe, 240)


def process_pending_outcomes(
    repository: ScannerRepository,
    client: BybitClient,
    *,
    limit: int = 100,
    min_age_minutes: int | None = None,
    max_bars: int = 48,
    fee_slippage_r: float = 0.0,
    dry_run: bool = False,
    scanner_filter: str | None = None,
) -> tuple[int, int]:
    """Evaluate pending outcomes.

    If min_age_minutes is None, uses per-timeframe maturity.
    If min_age_minutes is provided, overrides per-timeframe logic (legacy mode).
    """
    # Determine maturity strategy
    use_per_tf = min_age_minutes is None

    if use_per_tf:
        # Group by timeframe and process separately with correct maturity
        evaluated = 0
        failed = 0
        for tf, maturity in TIMEFRAME_MATURITY_MINUTES.items():
            ev, fl = _process_for_timeframe(
                repository, client, tf, maturity,
                limit=limit, max_bars=max_bars,
                fee_slippage_r=fee_slippage_r, dry_run=dry_run,
                scanner_filter=scanner_filter,
            )
            evaluated += ev
            failed += fl
        return evaluated, failed
    else:
        # Legacy mode: single maturity for all
        return _process_for_timeframe(
            repository, client, None, min_age_minutes,
            limit=limit, max_bars=max_bars,
            fee_slippage_r=fee_slippage_r, dry_run=dry_run,
            scanner_filter=scanner_filter,
        )


def _process_for_timeframe(
    repository: ScannerRepository,
    client: BybitClient,
    timeframe: str | None,
    min_age_minutes: int,
    *,
    limit: int,
    max_bars: int,
    fee_slippage_r: float,
    dry_run: bool,
    scanner_filter: str | None,
) -> tuple[int, int]:
    """Process pending outcomes for a specific timeframe maturity."""
    # Filters applied in SQL — before LIMIT — so other scanners don't starve FVG
    setups = repository.get_setups_without_outcomes(
        limit=limit,
        min_age_minutes=min_age_minutes,
        scanner_name=scanner_filter,
        entry_timeframe=timeframe,
    )

    evaluated = 0
    failed = 0
    for setup in setups:
        try:
            candles = fetch_outcome_candles(client, setup, max_bars=max_bars)
            outcome = evaluate_setup_outcome(
                setup,
                candles,
                max_bars=max_bars,
                fee_slippage_r=fee_slippage_r,
            )
            if dry_run:
                logger.info("dry-run outcome: %s", asdict(outcome))
            else:
                repository.save_signal_outcome(outcome)
            evaluated += 1
        except Exception:
            failed += 1
            logger.exception(
                "failed to evaluate outcome: setup_id=%s symbol=%s scanner=%s tf=%s",
                setup.setup_id,
                setup.symbol,
                setup.scanner_name,
                setup.entry_timeframe,
            )
    return evaluated, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill scanner signal outcomes")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--min-age-minutes", type=int, default=None,
                        help="Override per-TF maturity (legacy mode)")
    parser.add_argument("--max-bars", type=int, default=48)
    parser.add_argument("--fee-slippage-r", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scanner", type=str, default=None,
                        help="Filter by scanner name (e.g. FVG_REACTION_LONG_LOCAL_STRUCT_V1)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = load_settings()
    repository = ScannerRepository(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        backend="postgres",
    )
    # Schema must already exist from deployment migrations — no ensure_schema().
    client = BybitClient(settings)
    try:
        evaluated, failed = process_pending_outcomes(
            repository,
            client,
            limit=args.limit,
            min_age_minutes=args.min_age_minutes,
            max_bars=args.max_bars,
            fee_slippage_r=args.fee_slippage_r,
            dry_run=args.dry_run,
            scanner_filter=args.scanner,
        )
    finally:
        repository.close()
    print(f"outcomes evaluated={evaluated} failed={failed} dry_run={args.dry_run}")


if __name__ == "__main__":
    main()
