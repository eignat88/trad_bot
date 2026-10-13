"""Closed-candle V3 research detector; never sends setups to trading."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.scanners.models import MarketContext, SetupCandidate
from app.scanners.momentum_exhaustion_reverse_long_v2 import (
    MomentumExhaustionReverseLongV2Scanner,
)

from .closed_context import build_closed_v3_context
from .temporal import verify_trigger_candle, verify_source_timeframes


@dataclass(frozen=True)
class V3Detection:
    candidate: SetupCandidate | None
    clean_context: MarketContext | None
    status: str
    reason: str


def detect_v3_closed(
    ctx: MarketContext,
    scanner: MomentumExhaustionReverseLongV2Scanner | None = None,
) -> V3Detection:
    """Re-evaluate V2 rules on an independent, closed-candle snapshot."""
    try:
        clean_ctx = build_closed_v3_context(ctx)
        source_ok, reason = verify_source_timeframes(
            clean_ctx, clean_ctx.evaluated_at
        )
        if not source_ok:
            return V3Detection(None, clean_ctx, "REJECTED", reason)

        detector = scanner or MomentumExhaustionReverseLongV2Scanner()
        from app.scanners.funnel_diagnostics import FunnelCollector
        detector._research_funnel = FunnelCollector(
            "ME_REVERSE_LONG_V3_RESEARCH_ONLY"
        )
        results = detector.scan(clean_ctx)

        if not results:
            return V3Detection(None, clean_ctx, "NO_SIGNAL", "NO_SETUP")

        candidates = [
            c for c in results
            if c.scanner_name == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
            and c.direction == "LONG"
        ]
        if len(candidates) != 1:
            return V3Detection(None, clean_ctx, "REJECTED", "INVALID_SOURCE_COUNT")

        candidate = candidates[0]
        temporal = verify_trigger_candle(
            clean_ctx.candles_5m,
            candidate.detected_at,
        )
        if temporal.status != "TEMPORAL_VALIDATED":
            return V3Detection(None, clean_ctx, "REJECTED", temporal.reason)

        return V3Detection(candidate, clean_ctx, "SIGNAL", "CLOSED_SOURCE_VALIDATED")

    except (ValueError, TypeError, IndexError) as exc:
        return V3Detection(None, None, "REJECTED", str(exc))
