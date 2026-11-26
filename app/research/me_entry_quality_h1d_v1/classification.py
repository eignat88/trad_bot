"""Issue #314 / H1-D — real event classification
(``H1D_REAL_EVENT_CLASSIFICATION_V1``).

Pure and deterministic orchestration over the FROZEN H1-C contracts —
``build_source_identity()``, ``provide_htf_candles()`` and ``run_h1_event()``
from ``app.research.me_entry_quality_h1c_v1`` are reused WITHOUT any
modification and WITHOUT weakening any H1-C contract (Issue #308):

* classification ASOF = ``source_event.signal_time`` (never ``captured_at``);
* a failed source NEVER becomes a market mode — everything is an explicit
  reason code;
* lineage (``source_event_key``, ``source_identity``, ``lineage_hash``,
  ``activation_freeze_ts``) is preserved verbatim.

What H1-D adds on top (all diagnostics, no semantics change):

* per-timeframe causal-ASOF coverage from :mod:`coverage`
  (``COMPLETE_COVERAGE`` / ``INCOMPLETE_COVERAGE`` / ``INVALID_SOURCE`` at
  microsecond precision, exact missing buckets);
* a combined eligibility that requires BOTH the H1-C eligible outcome AND
  60/60 canonical coverage on BOTH timeframes — stricter fail-closed, never
  weaker;
* the full per-TF regime / BOS / combined state / eligibility report with
  diagnostics demanded by Issue #314.

No network, no database, no clock, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.research.me_entry_quality_h1_v1.htf_candles import (
    TIMEFRAME_1D,
    TIMEFRAME_4H,
)
from app.research.me_entry_quality_h1c_v1.event_adapter import (
    SourceEventRecord,
    classification_asof_ms,
    classification_asof_us,
)
from app.research.me_entry_quality_h1c_v1.h1_result import H1Result
from app.research.me_entry_quality_h1c_v1.htf_provider import HtfSourceIdentity
from app.research.me_entry_quality_h1c_v1.pipeline import (
    H1EventOutcome,
    run_h1_event,
)
from app.research.me_entry_quality_h1d_v1.coverage import (
    COVERAGE_COMPLETE,
    COVERAGE_INVALID,
    CoverageResult,
    evaluate_coverage,
)

H1D_CLASSIFICATION_VERSION = "H1D_REAL_EVENT_CLASSIFICATION_V1"

REASON_ELIGIBLE = "ELIGIBLE"
REASON_INVALID_SOURCE = "INVALID_SOURCE"
REASON_INCOMPLETE_COVERAGE = "INCOMPLETE_COVERAGE"


@dataclass(frozen=True)
class H1DClassification:
    """Complete H1-D classification outcome for ONE #305 source event."""

    classification_version: str
    record: SourceEventRecord
    asof_us: int
    asof_ms: int
    outcome: H1EventOutcome
    result: H1Result | None
    coverage_4h: CoverageResult
    coverage_1d: CoverageResult
    eligible: bool
    eligibility_reason: str
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification_version": self.classification_version,
            "lineage": self.record.lineage_payload(),
            "symbol": self.record.symbol,
            "asof_us": self.asof_us,
            "asof_ms": self.asof_ms,
            "eligible": self.eligible,
            "eligibility_reason": self.eligibility_reason,
            "h1_event_outcome": {
                "ok": self.outcome.ok,
                "reason": self.outcome.reason,
                "detail": self.outcome.detail,
            },
            "result": None if self.result is None else self.result.to_dict(),
            "coverage_4h": self.coverage_4h.to_dict(),
            "coverage_1d": self.coverage_1d.to_dict(),
            "diagnostics": dict(self.diagnostics),
        }


def classify_event(
    record: SourceEventRecord,
    rows_4h: Sequence[Mapping[str, Any]],
    rows_1d: Sequence[Mapping[str, Any]],
    *,
    source_4h: HtfSourceIdentity,
    source_1d: HtfSourceIdentity,
    swing_lookback: int = 3,
    canonical_lookback: int = 60,
) -> H1DClassification:
    """Classify ONE #305 source event offline from two raw HTF payloads.

    Reuses the frozen H1-C pipeline verbatim; adds causal-ASOF coverage
    (µs precision) and the combined fail-closed eligibility.  Every
    failure is an explicit ``eligibility_reason`` — never a market mode.
    """
    if not isinstance(record, SourceEventRecord):
        raise ValueError("record must be a SourceEventRecord")
    if not isinstance(source_4h, HtfSourceIdentity):
        raise ValueError("source_4h must be an HtfSourceIdentity")
    if not isinstance(source_1d, HtfSourceIdentity):
        raise ValueError("source_1d must be an HtfSourceIdentity")

    asof_us = classification_asof_us(record)
    asof_ms = classification_asof_ms(record)

    coverage_4h = evaluate_coverage(
        rows_4h, timeframe=TIMEFRAME_4H, asof_us=asof_us,
        canonical_lookback=canonical_lookback)
    coverage_1d = evaluate_coverage(
        rows_1d, timeframe=TIMEFRAME_1D, asof_us=asof_us,
        canonical_lookback=canonical_lookback)

    outcome: H1EventOutcome = run_h1_event(
        record, rows_4h, rows_1d,
        source_4h=source_4h, source_1d=source_1d,
        swing_lookback=swing_lookback,
        canonical_lookback=canonical_lookback,
    )
    result = outcome.result

    reasons: list[str] = []
    for label, coverage in (("4H", coverage_4h), ("1D", coverage_1d)):
        if coverage.status == COVERAGE_INVALID:
            reasons.append(
                f"{REASON_INVALID_SOURCE}:{label}:{coverage.reason}")
        elif coverage.status != COVERAGE_COMPLETE:
            reasons.append(
                f"{REASON_INCOMPLETE_COVERAGE}:{label}:"
                f"{coverage.present_buckets}/{coverage.expected_buckets}")
    if result is None or not result.combined_eligible:
        reasons.append(f"H1C:{outcome.reason}:{outcome.detail}")
    elif not result.eligible:
        reasons.append(f"H1C:{result.eligibility_reason}")

    eligible = bool(
        outcome.ok
        and result is not None
        and result.eligible
        and coverage_4h.status == COVERAGE_COMPLETE
        and coverage_1d.status == COVERAGE_COMPLETE
    )
    eligibility_reason = REASON_ELIGIBLE if eligible else ";".join(reasons)

    diagnostics: dict[str, Any] = {
        "coverage_4h_status": coverage_4h.status,
        "coverage_1d_status": coverage_1d.status,
        "coverage_4h_missing_open_ms": list(coverage_4h.missing_open_ms),
        "coverage_1d_missing_open_ms": list(coverage_1d.missing_open_ms),
        "coverage_4h_dropped_unclosed_opens": list(
            coverage_4h.dropped_unclosed_opens),
        "coverage_1d_dropped_unclosed_opens": list(
            coverage_1d.dropped_unclosed_opens),
        "h1c_pipeline_reason": outcome.reason,
        "combined_regime": None if result is None else result.combined_regime,
    }

    return H1DClassification(
        classification_version=H1D_CLASSIFICATION_VERSION,
        record=record,
        asof_us=asof_us,
        asof_ms=asof_ms,
        outcome=outcome,
        result=result,
        coverage_4h=coverage_4h,
        coverage_1d=coverage_1d,
        eligible=eligible,
        eligibility_reason=eligibility_reason,
        diagnostics=diagnostics,
    )
