"""LOCAL_STRUCT Parity Audit — Diagnostic tool.

Compares production LOCAL_STRUCT logic against research backtest_engine.py
for each touched FVG setup. Produces a compact per-FVG diagnostic table.

Production parity (as of fix):
  swing_high = max(c.high for c in candles[touch_idx-20:touch_idx])
  confirmed = candles[touch_idx+1].close > swing_high
  (strict touch_idx+1 only, matching research exactly)

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
    """Audit LOCAL_STRUCT for all WAITING_LOCAL_STRUCT / WAITING_TOUCH setups.

    For each touched FVG, produces a compact record with:
      - timestamps (fvg, touch, confirmation)
      - lookback timestamps (are they the same in research and production?)
      - swing_high and its source candle
      - confirmation candle close vs swing_high
      - distance_to_confirmation
      - production state
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

        # Resolve touch candle index
        touch_idx = _find_idx_by_ts(candles, setup.first_touch_at)
        if touch_idx is None:
            records.append({
                "symbol": symbol, "timeframe": timeframe,
                "fvg_created_at": setup.fvg_created_at,
                "touch_at": setup.first_touch_at,
                "bars_to_touch": setup.bars_to_touch,
                "state": setup.state.value,
                "status": "TOUCH_NOT_IN_WINDOW",
                "note": "Touch candle no longer in 200-candle window",
            })
            continue

        # Lookback window
        swing_start = max(0, touch_idx - LOCAL_STRUCT_LOOKBACK)
        lookback_candles = candles[swing_start:touch_idx]

        if touch_idx <= swing_start:
            records.append({
                "symbol": symbol, "timeframe": timeframe,
                "fvg_created_at": setup.fvg_created_at,
                "touch_at": setup.first_touch_at,
                "touch_idx": touch_idx,
                "state": setup.state.value,
                "status": "LOOKBACK_TOO_SHORT",
            })
            continue

        swing_high = max(c.high for c in lookback_candles)

        # Find swing_high source
        swing_high_ts = None
        for c in lookback_candles:
            if c.high == swing_high:
                swing_high_ts = c.timestamp
                break

        # Confirmation candle (touch_idx + 1 only — strict parity)
        check_idx = touch_idx + 1
        confirmation_close = None
        distance_to_confirmation = None
        would_confirm = False

        if check_idx < len(candles):
            check_candle = candles[check_idx]
            confirmation_close = check_candle.close
            would_confirm = confirmation_close > swing_high
            if swing_high > 0:
                distance_to_confirmation = (confirmation_close / swing_high - 1) * 100

        # C2/C3 timestamps for comparison
        c2_ts = setup.c2_timestamp
        c3_ts = setup.fvg_created_at

        # C2/C3 position relative to lookback
        c2_in_lookback = any(c.timestamp == c2_ts for c in lookback_candles)
        c3_in_lookback = any(c.timestamp == c3_ts for c in lookback_candles)

        record = {
            "symbol": symbol,
            "timeframe": timeframe,
            "fvg_created_at": setup.fvg_created_at,
            "touch_at": setup.first_touch_at,
            "bars_to_touch": setup.bars_to_touch,
            "bars_since": _bars_between(setup.fvg_created_at, candles[-1].timestamp, interval_ms) if candles else 0,

            # Index mapping
            "touch_idx": touch_idx,
            "check_idx": check_idx,

            # Lookback
            "lookback_start": swing_start,
            "lookback_len": len(lookback_candles),
            "swing_high": swing_high,
            "swing_high_source_ts": swing_high_ts,
            "c2_timestamp": c2_ts,
            "c3_timestamp": c3_ts,
            "c2_in_lookback": c2_in_lookback,
            "c3_in_lookback": c3_in_lookback,

            # Confirmation
            "confirmation_close": confirmation_close,
            "would_confirm": would_confirm,
            "distance_to_confirmation": round(distance_to_confirmation, 2) if distance_to_confirmation is not None else None,

            # State
            "state": setup.state.value,
        }
        records.append(record)

        # Compact log per touched FVG
        logger.info(
            "LOCAL_STRUCT audit: %s %s | fvg_at=%s touch_at=%s bars_to_touch=%s | "
            "lookback=[%s..%s] swing_high=%.2f (src=%s) | "
            "check_close=%s distance=%s%% confirm=%s | state=%s",
            symbol, timeframe,
            _ts(setup.fvg_created_at), _ts(setup.first_touch_at), setup.bars_to_touch,
            _ts(candles[swing_start].timestamp) if lookback_candles else "?",
            _ts(candles[touch_idx - 1].timestamp) if touch_idx > 0 else "?",
            swing_high,
            _ts(swing_high_ts) if swing_high_ts else "?",
            f"{confirmation_close:.2f}" if confirmation_close is not None else "N/A",
            f"{distance_to_confirmation:+.2f}%" if distance_to_confirmation is not None else "N/A",
            would_confirm,
            setup.state.value,
        )

    return records


def print_audit_summary(records: list[dict[str, Any]]) -> None:
    """Print a compact summary table of audit results."""
    if not records:
        logger.info("No touched FVG setups found to audit")
        return

    logger.info("=== LOCAL_STRUCT Audit Summary (%d setups) ===", len(records))

    distances = [r["distance_to_confirmation"] for r in records
                 if r.get("distance_to_confirmation") is not None]
    confirms = sum(1 for r in records if r.get("would_confirm"))

    for r in records:
        dist = r.get("distance_to_confirmation")
        dist_str = f"{dist:+.2f}%" if dist is not None else "N/A"
        logger.info(
            "  %s %s fvg=%s touch=%s bars=%s swing=%.2f close=%s dist=%s confirm=%s",
            r.get("symbol", "?"),
            r.get("timeframe", "?"),
            _ts(r.get("fvg_created_at", 0)),
            _ts(r.get("touch_at", 0)),
            r.get("bars_to_touch", "?"),
            r.get("swing_high", 0),
            f"{r['confirmation_close']:.2f}" if r.get("confirmation_close") is not None else "N/A",
            dist_str,
            r.get("would_confirm", "?"),
        )

    if distances:
        avg_dist = sum(distances) / len(distances)
        max_dist = max(distances)
        min_dist = min(distances)
        logger.info(
            "  Summary: %d/%d confirmed | distance_to_confirm: "
            "avg=%.2f%% min=%.2f%% max=%.2f%%",
            confirms, len(records), avg_dist, min_dist, max_dist,
        )


def _ts(ms: int | None) -> str:
    """Pretty-print timestamp."""
    if ms is None:
        return "?"
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")
    except (OSError, ValueError):
        return str(ms)
