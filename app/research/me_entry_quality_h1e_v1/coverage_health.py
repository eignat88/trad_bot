"""Issue #324 / H1-E.5 — ``H1E_COVERAGE_HEALTH_V1``.

Pure reconciliation / coverage-health contract: no I/O, no clock (``now``
is always caller-supplied).  Reconciles the #305 source cohort against the
H1-E ledger across FIVE strictly separated dimensions (spec):

* ``SOURCE_CAPTURE_COVERAGE``      — completeness of the #305 capture
  itself (blind intervals, UNVERIFIED windows, capture_health evidence);
* ``OBSERVER_DELIVERY_COVERAGE``   — every eligible source event is
  delivered/processed by the observer (lag vs persistent gap);
* ``HTF_DATA_COVERAGE``            — 60/60 closed-candle coverage of the
  4H/1D payloads actually used;
* ``CLASSIFICATION_ELIGIBILITY``   — accepted / deferred / rejected /
  non-eligible strata accounting;
* ``OOS_STATISTICAL_READINESS``    — pre-registered sample-size/diversity
  thresholds (never PASS before accumulation).

Statuses per dimension: ``PASS`` / ``HOLD`` / ``GAP`` / ``CONFLICT``.
Overall verdict precedence: ``CONFLICT`` > ``GAP`` > ``HOLD`` > ``PASS``.

Hard rules:

* grace lag: an undelivered event younger than the grace window is
  ``HOLD`` (``COVERAGE_LAG``, eventual consistency); older than the grace
  window it is ``GAP`` (``COVERAGE_GAP``);
* any identity conflict in scope → ``CONFLICT``;
* NO blanket PASS on single-event success: ``OBSERVER_DELIVERY_COVERAGE``
  cannot be ``PASS`` with zero observed events, and ``PASS`` requires full
  accounting;
* unsupported evidence stays ``UNKNOWN`` and resolves to ``HOLD`` — never
  a silent PASS.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

H1E_COVERAGE_HEALTH_VERSION = "H1E_COVERAGE_HEALTH_V1"

DIM_SOURCE_CAPTURE_COVERAGE = "SOURCE_CAPTURE_COVERAGE"
DIM_OBSERVER_DELIVERY_COVERAGE = "OBSERVER_DELIVERY_COVERAGE"
DIM_HTF_DATA_COVERAGE = "HTF_DATA_COVERAGE"
DIM_CLASSIFICATION_ELIGIBILITY = "CLASSIFICATION_ELIGIBILITY"
DIM_OOS_STATISTICAL_READINESS = "OOS_STATISTICAL_READINESS"

ALL_DIMENSIONS: tuple[str, ...] = (
    DIM_SOURCE_CAPTURE_COVERAGE,
    DIM_OBSERVER_DELIVERY_COVERAGE,
    DIM_HTF_DATA_COVERAGE,
    DIM_CLASSIFICATION_ELIGIBILITY,
    DIM_OOS_STATISTICAL_READINESS,
)

STATUS_PASS = "PASS"
STATUS_HOLD = "HOLD"
STATUS_GAP = "GAP"
STATUS_CONFLICT = "CONFLICT"
STATUS_UNKNOWN = "UNKNOWN"

STATUS_ORDER: dict[str, int] = {
    STATUS_PASS: 0,
    STATUS_UNKNOWN: 1,
    STATUS_HOLD: 1,
    STATUS_GAP: 2,
    STATUS_CONFLICT: 3,
}


@dataclass(frozen=True)
class DimensionReport:
    dimension: str
    status: str
    evidence: Mapping[str, Any]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "status": self.status,
            "evidence": dict(self.evidence),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HealthReport:
    version: str
    dimensions: tuple[DimensionReport, ...]
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "verdict": self.verdict,
            "dimensions": [d.to_dict() for d in self.dimensions],
        }


def _worst(statuses: Sequence[str]) -> str:
    """Worst dimension status; ``UNKNOWN`` resolves to ``HOLD`` in a verdict."""
    worst = STATUS_PASS
    for status in statuses:
        normalized = STATUS_HOLD if status == STATUS_UNKNOWN else status
        if STATUS_ORDER.get(normalized, 1) > STATUS_ORDER[worst]:
            worst = normalized
    return worst


@dataclass(frozen=True)
class SourceEventView:
    """Minimal reconciliation view of ONE eligible #305 source event."""

    source_event_key: str
    signal_time_us: int
    captured_at_us: int


@dataclass(frozen=True)
class LedgerEventView:
    """Minimal reconciliation view of ONE H1-E ledger record."""

    source_event_key: str
    status: str  # CLASSIFIED | DEFERRED | REJECTED | CONFLICT
    eligible: bool
    htf_complete: bool
    captured_at_us: int


def reconcile(
    *,
    source_events: Sequence[SourceEventView],
    ledger_events: Sequence[LedgerEventView],
    now_us: int,
    grace_us: int,
    conflicts: Sequence[Mapping[str, Any]] = (),
    blind_intervals: Sequence[Mapping[str, Any]] = (),
    source_capture_evidence: Mapping[str, Any] | None = None,
    preregistration: Mapping[str, Any] | None = None,
) -> HealthReport:
    """Reconcile the source cohort against the H1-E ledger (pure).

    ``now_us`` / ``grace_us`` are explicit integers — this function never
    reads a clock.  ``source_capture_evidence`` carries #305-side evidence
    (``capture_health``/``coverage_audit`` findings); ``None`` = unsupported
    → ``UNKNOWN``→``HOLD``.
    """
    now = int(now_us)
    grace = int(grace_us)
    ledger_by_key: dict[str, list[LedgerEventView]] = {}
    for view in ledger_events:
        ledger_by_key.setdefault(view.source_event_key, []).append(view)

    dimensions: list[DimensionReport] = []

    # ── 1. SOURCE_CAPTURE_COVERAGE ──────────────────────────────────────
    if source_capture_evidence is None:
        dimensions.append(DimensionReport(
            DIM_SOURCE_CAPTURE_COVERAGE, STATUS_UNKNOWN,
            {"source_capture_evidence": None},
            "unsupported — #305 capture evidence not supplied "
            "(VPS-only detail; UNKNOWN, never PASS)",
        ))
    else:
        capture_ok = source_capture_evidence.get("capture_complete")
        if capture_ok is True:
            status = STATUS_PASS
            detail = "#305 capture evidence complete for the window"
        elif capture_ok is False:
            status = STATUS_GAP
            detail = "#305 capture gaps reported by capture evidence"
        else:
            status = STATUS_UNKNOWN
            detail = "unsupported capture evidence value"
        dimensions.append(DimensionReport(
            DIM_SOURCE_CAPTURE_COVERAGE, status,
            {
                "source_capture_evidence": dict(source_capture_evidence),
                "blind_intervals": [dict(b) for b in blind_intervals],
            },
            detail,
        ))

    # ── 2. OBSERVER_DELIVERY_COVERAGE ───────────────────────────────────
    delivered_keys = set(ledger_by_key)
    unmatched: list[str] = []
    lagging: list[str] = []
    for view in source_events:
        if view.source_event_key in delivered_keys:
            continue
        if now - int(view.captured_at_us) <= grace:
            lagging.append(view.source_event_key)
        else:
            unmatched.append(view.source_event_key)
    if conflicts:
        delivery_status = STATUS_CONFLICT
        delivery_detail = (
            f"{len(conflicts)} identity conflict(s) in scope — fail-closed")
    elif unmatched:
        delivery_status = STATUS_GAP
        delivery_detail = (
            f"{len(unmatched)} source event(s) unprocessed beyond the grace "
            "window (COVERAGE_GAP)")
    elif lagging:
        delivery_status = STATUS_HOLD
        delivery_detail = (
            f"{len(lagging)} young event(s) inside the grace window "
            "(COVERAGE_LAG — eventual consistency)")
    elif not source_events:
        delivery_status = STATUS_HOLD
        delivery_detail = (
            "no source events in scope — a single success (or none) never "
            "proves coverage")
    else:
        delivery_status = STATUS_PASS
        delivery_detail = "every eligible source event has a ledger record"
    dimensions.append(DimensionReport(
        DIM_OBSERVER_DELIVERY_COVERAGE, delivery_status,
        {
            "source_event_count": len(source_events),
            "delivered_count": len(delivered_keys & {
                v.source_event_key for v in source_events}),
            "lagging_keys": lagging,
            "unmatched_keys": unmatched,
            "conflict_count": len(conflicts),
        },
        delivery_detail,
    ))

    # ── 3. HTF_DATA_COVERAGE ────────────────────────────────────────────
    classified = [
        view for view in ledger_events if view.status == "CLASSIFIED"
    ]
    incomplete = [
        view for view in classified if not view.htf_complete
    ]
    deferred = [view for view in ledger_events if view.status == "DEFERRED"]
    if conflicts:
        htf_status = STATUS_CONFLICT
        htf_detail = "identity conflicts poison the HTF evidence chain"
    elif incomplete:
        age_ok = all(
            now - int(view.captured_at_us) <= grace for view in incomplete)
        htf_status = STATUS_HOLD if age_ok else STATUS_GAP
        htf_detail = (
            f"{len(incomplete)} classified record(s) without 60/60 HTF "
            "coverage")
    elif deferred:
        htf_status = STATUS_HOLD
        htf_detail = f"{len(deferred)} deferred classification(s) pending"
    elif not classified:
        htf_status = STATUS_HOLD
        htf_detail = "no classified records yet — coverage unproven"
    else:
        htf_status = STATUS_PASS
        htf_detail = "all classified records carry 60/60 HTF coverage"
    dimensions.append(DimensionReport(
        DIM_HTF_DATA_COVERAGE, htf_status,
        {
            "classified_count": len(classified),
            "incomplete_htf_count": len(incomplete),
            "deferred_count": len(deferred),
        },
        htf_detail,
    ))

    # ── 4. CLASSIFICATION_ELIGIBILITY ───────────────────────────────────
    rejected = [view for view in ledger_events if view.status == "REJECTED"]
    conflicted = [view for view in ledger_events if view.status == "CONFLICT"]
    eligible = [view for view in ledger_events if view.eligible]
    if conflicted or conflicts:
        elig_status = STATUS_CONFLICT
        elig_detail = "conflicting identity payloads are isolated, not counted"
    elif deferred:
        elig_status = STATUS_HOLD
        elig_detail = "deferred classifications pending — funnel incomplete"
    elif not ledger_events:
        elig_status = STATUS_HOLD
        elig_detail = "empty ledger — no eligibility accounting possible"
    else:
        elig_status = STATUS_PASS
        elig_detail = (
            f"funnel accounted: {len(eligible)} eligible / "
            f"{len(classified) - len(eligible)} non-eligible / "
            f"{len(rejected)} rejected")
    dimensions.append(DimensionReport(
        DIM_CLASSIFICATION_ELIGIBILITY, elig_status,
        {
            "eligible_count": len(eligible),
            "classified_count": len(classified),
            "rejected_count": len(rejected),
            "deferred_count": len(deferred),
            "conflict_count": len(conflicted),
        },
        elig_detail,
    ))

    # ── 5. OOS_STATISTICAL_READINESS ────────────────────────────────────
    if preregistration is None:
        oos_status = STATUS_UNKNOWN
        oos_detail = "no pre-registered thresholds supplied"
        oos_evidence: Mapping[str, Any] = {"preregistration": None}
    else:
        thresholds = preregistration.get("sample_thresholds", {})
        min_events = int(thresholds.get("min_events_per_cohort", 0) or 0)
        cohort_counts = preregistration.get("accumulated_counts", {}) or {}
        if not cohort_counts:
            oos_status = STATUS_HOLD
            oos_detail = (
                "accumulation not started — OOS_STATISTICAL_READINESS is "
                "never PASS before the pre-registered thresholds are met")
            oos_evidence = {"thresholds": dict(thresholds)}
        elif any(int(v) < min_events for v in cohort_counts.values()):
            oos_status = STATUS_HOLD
            oos_detail = "below pre-registered per-cohort minimums"
            oos_evidence = {
                "thresholds": dict(thresholds),
                "accumulated_counts": dict(cohort_counts),
            }
        else:
            oos_status = STATUS_PASS
            oos_detail = "pre-registered thresholds met (evaluation still HOLD)"
            oos_evidence = {
                "thresholds": dict(thresholds),
                "accumulated_counts": dict(cohort_counts),
            }
    dimensions.append(DimensionReport(
        DIM_OOS_STATISTICAL_READINESS, oos_status, oos_evidence, oos_detail))

    verdict = _worst([dimension.status for dimension in dimensions])
    return HealthReport(
        version=H1E_COVERAGE_HEALTH_VERSION,
        dimensions=tuple(dimensions),
        verdict=verdict,
    )
