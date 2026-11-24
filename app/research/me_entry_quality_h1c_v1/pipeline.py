"""Issue #308 — H1-C pipeline: source rows + #305 event → H1 result.

Orchestrates the three H1-C stages for ONE event (all pure):

1. :func:`app.research.me_entry_quality_h1c_v1.htf_provider.provide_htf_candles`
   — read-only validation of raw 4H / 1D rows at the event's ``signal_time``;
2. :func:`app.research.me_entry_quality_h1_v1.classifier.classify_timeframe`
   — the Issue #297 causal classifier, used UNCHANGED;
3. :func:`app.research.me_entry_quality_h1c_v1.h1_result.build_h1_result`
   — the per-event result contract with explicit eligibility and full
   lineage.

Failure semantics: a failed source NEVER produces a market mode.  The
outcome is still emitted with ``result=None`` (or, when only ONE timeframe
failed, with an ``INVALID_SOURCE`` state block for that timeframe) and an
explicit reason code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.research.me_entry_quality_h1_v1.classifier import (
    BOS_NOT_EVALUATED,
    CANONICAL_LOOKBACK_CANDLES,
    COVERAGE_INVALID_SOURCE,
    REGIME_INVALID_SOURCE,
    SWING_LOOKBACK_DEFAULT,
    TimeframeState,
    classify_timeframe,
)
from app.research.me_entry_quality_h1_v1.htf_candles import (
    TIMEFRAME_1D,
    TIMEFRAME_4H,
)
from app.research.me_entry_quality_h1c_v1.event_adapter import (
    SourceEventRecord,
    classification_asof_ms,
)
from app.research.me_entry_quality_h1c_v1.h1_result import (
    H1Result,
    HtfStateSummary,
    build_h1_result,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    HtfCandleSet,
    HtfSourceIdentity,
    provide_htf_candles,
)

H1C_PIPELINE_VERSION = "H1C_PIPELINE_1.0.0"

REASON_OK = "OK"
REASON_INVALID_SOURCE = "INVALID_SOURCE"


@dataclass(frozen=True)
class H1EventOutcome:
    """Deterministic pipeline outcome for ONE adapted source event."""

    record: SourceEventRecord
    result: H1Result | None
    ok: bool
    reason: str
    detail: str
    candle_set_4h: HtfCandleSet | None = None
    candle_set_1d: HtfCandleSet | None = None
    pipeline_version: str = H1C_PIPELINE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_version": self.pipeline_version,
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "record": self.record.to_dict(),
            "result": None if self.result is None else self.result.to_dict(),
        }


def _invalid_summary(
    candle_set: HtfCandleSet, *, asof_ms: int
) -> HtfStateSummary:
    return HtfStateSummary(
        timeframe=candle_set.timeframe,
        regime=REGIME_INVALID_SOURCE,
        bos=BOS_NOT_EVALUATED,
        coverage_status=COVERAGE_INVALID_SOURCE,
        reason=candle_set.reason,
        detail=candle_set.detail,
        evaluation_asof_ms=asof_ms,
        source=candle_set.source_identity.to_dict(),
    )


def run_h1_event(
    record: SourceEventRecord,
    rows_4h: Sequence[Mapping[str, Any]],
    rows_1d: Sequence[Mapping[str, Any]],
    *,
    source_4h: HtfSourceIdentity,
    source_1d: HtfSourceIdentity,
    swing_lookback: int = SWING_LOOKBACK_DEFAULT,
    canonical_lookback: int = CANONICAL_LOOKBACK_CANDLES,
    min_closed_candles: int | None = None,
) -> H1EventOutcome:
    """Classify 4H / 1D structure for ONE event at its ``signal_time``.

    ``min_closed_candles`` defaults to the Issue #297 frozen-candidate
    ``2 * swing_lookback + 1``.
    """
    if not isinstance(record, SourceEventRecord):
        raise ValueError("record must be a SourceEventRecord")
    asof_ms = classification_asof_ms(record)
    if min_closed_candles is None:
        min_closed_candles = 2 * int(swing_lookback) + 1

    set_4h = provide_htf_candles(
        rows_4h,
        timeframe=TIMEFRAME_4H,
        evaluation_asof_ms=asof_ms,
        source_identity=source_4h,
        canonical_lookback=canonical_lookback,
        min_closed_candles=min_closed_candles,
    )
    set_1d = provide_htf_candles(
        rows_1d,
        timeframe=TIMEFRAME_1D,
        evaluation_asof_ms=asof_ms,
        source_identity=source_1d,
        canonical_lookback=canonical_lookback,
        min_closed_candles=min_closed_candles,
    )

    def _summarize(candle_set: HtfCandleSet) -> HtfStateSummary:
        if not candle_set.ok:
            return _invalid_summary(candle_set, asof_ms=asof_ms)
        state: TimeframeState = classify_timeframe(
            candle_set.candles,
            timeframe=candle_set.timeframe,
            evaluation_asof_ms=asof_ms,
            swing_lookback=swing_lookback,
            canonical_lookback=canonical_lookback,
        )
        return HtfStateSummary.from_timeframe_state(
            state, source=candle_set.source_identity.to_dict()
        )

    summary_4h = _summarize(set_4h)
    summary_1d = _summarize(set_1d)

    result = build_h1_result(
        record,
        summary_4h=summary_4h,
        summary_1d=summary_1d,
        swing_lookback=swing_lookback,
        canonical_lookback=canonical_lookback,
    )

    if result.combined_eligible:
        return H1EventOutcome(
            record=record,
            result=result,
            ok=True,
            reason=REASON_OK,
            detail="",
            candle_set_4h=set_4h,
            candle_set_1d=set_1d,
        )
    return H1EventOutcome(
        record=record,
        result=result,
        ok=False,
        reason=REASON_INVALID_SOURCE,
        detail=result.eligibility_reason,
        candle_set_4h=set_4h,
        candle_set_1d=set_1d,
    )
