"""Issue #308 / H1-C.4 — per-event H1 result contract (research-only).

Assembles, for ONE adapted source event, the complete H1 outcome:

* 4H regime / BOS / coverage (per-timeframe :class:`HtfStateSummary`);
* 1D regime / BOS / coverage;
* one of the nine ``<4H>/<1D>`` HTF combinations (preserved verbatim,
  never averaged) — or a verbatim ``INVALID_SOURCE`` combination when a
  source is unusable;
* explicit eligibility (``eligible`` + ``eligibility_reason``);
* the research protocol version;
* full source lineage: ``source_event_key``, ``source_identity``,
  ``signal_time``, ``activation_freeze_ts``, ``lineage_hash`` preserved
  VERBATIM from the #305 source event.

Causality guard: every state summary must carry ``evaluation_asof_ms ==
record.signal_time_ms`` — a result classified at ``captured_at`` (or any
other instant) is rejected with ``ValueError``.

Pure and deterministic: no database, no network, no clock, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.research.me_entry_quality_h1_v1.classifier import (
    COMBINED_REGIME_SEPARATOR,
    COVERAGE_VALID,
    REGIME_INVALID_SOURCE,
    TimeframeState,
)
from app.research.me_entry_quality_h1c_v1.event_adapter import (
    SourceEventRecord,
    classification_asof_ms,
    classification_asof_us,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    COVERAGE_COMPLETE,
    COVERAGE_NONE,
)

H1C_PROTOCOL_VERSION = "H1C_1.1.0"

REASON_ELIGIBLE = "ELIGIBLE"
REASON_INVALID_4H = "HTF_4H_INVALID_SOURCE"
REASON_INVALID_1D = "HTF_1D_INVALID_SOURCE"
REASON_INVALID_BOTH = "HTF_BOTH_INVALID_SOURCE"
REASON_INCOMPLETE_4H = "HTF_4H_INCOMPLETE_COVERAGE"
REASON_INCOMPLETE_1D = "HTF_1D_INCOMPLETE_COVERAGE"
REASON_INCOMPLETE_BOTH = "HTF_BOTH_INCOMPLETE_COVERAGE"


@dataclass(frozen=True)
class HtfStateSummary:
    """Per-timeframe state block of the H1 result.

    ``window_coverage`` is the provider's canonical-window coverage
    (``COMPLETE`` / ``PARTIAL`` / ``NONE``) — a diagnostic classification
    over a partial window is kept, but eligibility requires ``COMPLETE``.
    """

    timeframe: str
    regime: str
    bos: str
    coverage_status: str
    reason: str
    detail: str
    evaluation_asof_ms: int
    window_coverage: str = COVERAGE_NONE
    source: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "regime": self.regime,
            "bos": self.bos,
            "coverage_status": self.coverage_status,
            "reason": self.reason,
            "detail": self.detail,
            "evaluation_asof_ms": self.evaluation_asof_ms,
            "window_coverage": self.window_coverage,
            "source": None if self.source is None else dict(self.source),
        }

    @classmethod
    def from_timeframe_state(
        cls,
        state: TimeframeState,
        *,
        window_coverage: str = COVERAGE_COMPLETE,
        source: dict[str, Any] | None = None,
    ) -> "HtfStateSummary":
        return cls(
            timeframe=state.timeframe,
            regime=state.regime,
            bos=state.bos,
            coverage_status=state.coverage_status,
            reason=state.reason,
            detail=state.detail,
            evaluation_asof_ms=state.evaluation_asof_ms,
            window_coverage=window_coverage,
            source=source,
        )


@dataclass(frozen=True)
class H1Result:
    """Immutable H1 outcome for ONE source event."""

    protocol_version: str
    evaluation_asof_ms: int
    source_event_key: str
    source_identity: str
    symbol: str
    activation_freeze_ts_ms: int
    lineage_hash: str
    htf_4h: HtfStateSummary
    htf_1d: HtfStateSummary
    combined_regime: str
    combined_eligible: bool
    eligible: bool
    eligibility_reason: str
    swing_lookback: int
    canonical_lookback: int
    signal_time_us: int = 0

    def lineage_payload(self) -> dict[str, Any]:
        """Exactly the preserved source lineage fields (verbatim)."""
        return {
            "source_event_key": self.source_event_key,
            "source_identity": self.source_identity,
            "signal_time_ms": self.evaluation_asof_ms,
            "signal_time_us": self.signal_time_us,
            "activation_freeze_ts_ms": self.activation_freeze_ts_ms,
            "lineage_hash": self.lineage_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "evaluation_asof_ms": self.evaluation_asof_ms,
            "lineage": self.lineage_payload(),
            "symbol": self.symbol,
            "htf_4h": self.htf_4h.to_dict(),
            "htf_1d": self.htf_1d.to_dict(),
            "combined_regime": self.combined_regime,
            "combined_eligible": self.combined_eligible,
            "eligible": self.eligible,
            "eligibility_reason": self.eligibility_reason,
            "swing_lookback": self.swing_lookback,
            "canonical_lookback": self.canonical_lookback,
        }


def build_h1_result(
    record: SourceEventRecord,
    *,
    summary_4h: HtfStateSummary,
    summary_1d: HtfStateSummary,
    protocol_version: str = H1C_PROTOCOL_VERSION,
    swing_lookback: int = 3,
    canonical_lookback: int = 60,
) -> H1Result:
    """Assemble the H1 result for one event from the two HTF states.

    Eligibility (review remediation): ``eligible`` requires BOTH timeframes
    classified (``combined_eligible``) AND ``COMPLETE`` canonical-window
    coverage for BOTH.  A classification over a partial window (e.g. 17 or
    59 of 60 candles) is kept as a DIAGNOSTIC result but never eligible.

    Raises ``ValueError`` when a summary was not classified at the event's
    ``signal_time`` (e.g. at ``captured_at``) — the causal anchor is not
    negotiable.
    """
    asof_ms = classification_asof_ms(record)
    for summary in (summary_4h, summary_1d):
        if not isinstance(summary, HtfStateSummary):
            raise ValueError("summaries must be HtfStateSummary")
        if summary.evaluation_asof_ms != asof_ms:
            raise ValueError(
                "summary.evaluation_asof_ms must equal record.signal_time_ms "
                f"({summary.evaluation_asof_ms} != {asof_ms}) — classify at "
                "signal_time, never at captured_at"
            )

    combined_regime = (
        f"{summary_4h.regime}{COMBINED_REGIME_SEPARATOR}{summary_1d.regime}"
    )
    valid_4h = summary_4h.regime != REGIME_INVALID_SOURCE
    valid_1d = summary_1d.regime != REGIME_INVALID_SOURCE
    combined_eligible = valid_4h and valid_1d

    complete_4h = summary_4h.window_coverage == COVERAGE_COMPLETE
    complete_1d = summary_1d.window_coverage == COVERAGE_COMPLETE

    if not valid_4h and not valid_1d:
        eligibility_reason = REASON_INVALID_BOTH
    elif not valid_4h:
        eligibility_reason = f"{REASON_INVALID_4H}:{summary_4h.reason}"
    elif not valid_1d:
        eligibility_reason = f"{REASON_INVALID_1D}:{summary_1d.reason}"
    elif not complete_4h and not complete_1d:
        eligibility_reason = REASON_INCOMPLETE_BOTH
    elif not complete_4h:
        eligibility_reason = REASON_INCOMPLETE_4H
    elif not complete_1d:
        eligibility_reason = REASON_INCOMPLETE_1D
    else:
        eligibility_reason = REASON_ELIGIBLE

    coverage_ok = (
        summary_4h.coverage_status == COVERAGE_VALID
        and summary_1d.coverage_status == COVERAGE_VALID
    )

    return H1Result(
        protocol_version=str(protocol_version),
        evaluation_asof_ms=asof_ms,
        source_event_key=record.source_event_key,
        source_identity=record.source_identity,
        symbol=record.symbol,
        activation_freeze_ts_ms=record.activation_freeze_ts_ms,
        lineage_hash=record.lineage_hash,
        htf_4h=summary_4h,
        htf_1d=summary_1d,
        combined_regime=combined_regime,
        combined_eligible=combined_eligible,
        eligible=bool(
            combined_eligible and coverage_ok and complete_4h and complete_1d
        ),
        eligibility_reason=eligibility_reason,
        swing_lookback=int(swing_lookback),
        canonical_lookback=int(canonical_lookback),
        signal_time_us=classification_asof_us(record),
    )
