"""LOCAL_STRUCT Parity Audit — Diagnostic tool.

Compares production LOCAL_STRUCT logic against research backtest_engine.py
for each touched FVG setup. Logs detailed decision data to help diagnose
why confirmed_total == 0 despite touched > 0.

This is a READ-ONLY diagnostic tool. It does not modify scanner state.
"""
from __future__ import annotations

import logging
from typing import Any

from app.scanners.fvg_reaction_long_local_struct_v1 import (
    FVGReactionLongLocalStructV1Scanner,
    FVGSetup,
    FVGState,
    LOCAL_STRUCT_LOOKBACK,
    _find_idx_by_ts,
    _bars_between,
    _INTERVAL_MS,
)

logger = logging.getLogger(__name__)


def audit_local_struct(
    scanner: FVGReactionLongLocalStructV1Scanner,
    symbol: str,
    timeframe: str,
    candles: list,
) -> list[dict[str, Any]]:
    """Audit LOCAL_STRUCT logic for all WAITING_LOCAL_STRUCT setups.

    For each touched FVG, log:
      - touch timestamp and index in current window
      - 20 candles in lookback window
      - swing_high value
      - subsequent candles and their close vs swing_high
      - whether confirmation would fire

    Returns list of audit records for programmatic use.
    """
    interval_ms = _INTERVAL_MS.get(timeframe, 300_000)
    records: list[dict[str, Any]] = []

    for key, setup in scanner._setups.items():
        if setup.symbol != symbol or setup.timeframe != timeframe:
            continue
        if setup.state not in (FVGState.WAITING_LOCAL_STRUCT, FVGState.WAITING_TOUCH):
            continue
        if setup.first_touch_at is None:
            continue

        # Resolve touch candle index in current window
        touch_idx = _find_idx_by_ts(candles, setup.first_touch_at)
        if touch_idx is None:
            records.append({
                "fvg_created_at": setup.fvg_created_at,
                "touch_at": setup.first_touch_at,
                "status": "TOUCH_NOT_IN_WINDOW",
                "note": "Touch candle no longer in 200-candle window",
            })
            continue

        # Compute lookback window
        swing_start = max(0, touch_idx - LOCAL_STRUCT_LOOKBACK)
        lookback_candles = candles[swing_start:touch_idx]

        if touch_idx <= swing_start:
            records.append({
                "fvg_created_at": setup.fvg_created_at,
                "touch_at": setup.first_touch_at,
                "touch_idx": touch_idx,
                "status": "LOOKBACK_TOO_SHORT",
                "note": f"touch_idx={touch_idx} <= swing_start={swing_start}",
            })
            continue

        swing_high = max(c.high for c in lookback_candles)

        # Check each candle after touch
        check_results: list[dict[str, Any]] = []
        for check_idx in range(touch_idx + 1, len(candles)):
            candle = candles[check_idx]
            close = candle.close
            would_confirm = close > swing_high
            check_results.append({
                "idx": check_idx,
                "ts": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": close,
                "swing_high": swing_high,
                "close_vs_swing": round(close - swing_high, 4),
                "would_confirm": would_confirm,
            })
            if would_confirm:
                break  # research only needs first confirming candle

        any_confirms = any(r["would_confirm"] for r in check_results)

        record = {
            "fvg_created_at": setup.fvg_created_at,
            "fvg_low": setup.fvg_low,
            "fvg_high": setup.fvg_high,
            "fvg_atr": setup.fvg_atr,
            "touch_at": setup.first_touch_at,
            "bars_to_touch": setup.bars_to_touch,
            "touch_idx": touch_idx,
            "lookback_start": swing_start,
            "lookback_len": len(lookback_candles),
            "swing_high": swing_high,
            "lookback_highs": [c.high for c in lookback_candles],
            "lookback_timestamps": [c.timestamp for c in lookback_candles],
            "candles_after_touch": check_results,
            "any_confirms": any_confirms,
            "state": setup.state.value,
            "bars_since": _bars_between(setup.fvg_created_at, candles[-1].timestamp, interval_ms),
        }

        # Key diagnostic: why is swing_high so high?
        # Find which candle in lookback set the swing_high
        for i, c in enumerate(lookback_candles):
            if c.high == swing_high:
                record["swing_high_source_idx"] = swing_start + i
                record["swing_high_source_ts"] = c.timestamp
                break

        records.append(record)

        # Log the critical diagnostic
        logger.info(
            "LOCAL_STRUCT audit: symbol=%s tf=%s fvg_at=%.4f touch_at=%.4f "
            "touch_idx=%d swing_high=%.4f lookback=[%d..%d] "
            "candles_after=%d any_confirms=%s",
            symbol, timeframe,
            setup.fvg_created_at / 1000, setup.first_touch_at / 1000,
            touch_idx, swing_high,
            swing_start, touch_idx - 1,
            len(check_results), any_confirms,
        )

        # Log each candle after touch with swing comparison
        for r in check_results:
            logger.info(
                "  candle ts=%.0f close=%.4f swing=%.4f diff=%+.4f confirm=%s",
                r["ts"] / 1000, r["close"], r["swing_high"],
                r["close_vs_swing"], r["would_confirm"],
            )

    return records
