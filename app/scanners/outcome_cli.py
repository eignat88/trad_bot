"""Backfill measured outcomes for saved scanner setups."""
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
    # Fetch extra bars so filtering after signal_candle_open_time still leaves a
    # full evaluation horizon for recent setups. Bybit caps kline limit at 1000.
    limit = min(1000, max(200, max_bars + 50))
    candles = client.get_klines(setup.symbol, interval, limit)
    return [c for c in candles if c.timestamp > setup.signal_candle_open_time][:max_bars]


def process_pending_outcomes(
    repository: ScannerRepository,
    client: BybitClient,
    *,
    limit: int = 100,
    min_age_minutes: int = 240,
    max_bars: int = 48,
    fee_slippage_r: float = 0.0,
    dry_run: bool = False,
) -> tuple[int, int]:
    setups = repository.get_setups_without_outcomes(
        limit=limit,
        min_age_minutes=min_age_minutes,
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
                "failed to evaluate outcome: setup_id=%s symbol=%s scanner=%s",
                setup.setup_id,
                setup.symbol,
                setup.scanner_name,
            )
    return evaluated, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill scanner signal outcomes")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--min-age-minutes", type=int, default=240)
    parser.add_argument("--max-bars", type=int, default=48)
    parser.add_argument("--fee-slippage-r", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = load_settings()
    repository = ScannerRepository(backend="postgres")
    repository.ensure_schema()
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
        )
    finally:
        repository.close()
    print(f"outcomes evaluated={evaluated} failed={failed} dry_run={args.dry_run}")


if __name__ == "__main__":
    main()
