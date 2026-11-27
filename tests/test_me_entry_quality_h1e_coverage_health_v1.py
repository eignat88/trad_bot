"""Issue #324 / H1-E.5 — coverage health: five-dimension reconciliation,
grace lag vs persistent gap, conflicts, no blanket PASS.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

from app.research.me_entry_quality_h1e_v1.coverage_health import (
    ALL_DIMENSIONS,
    DIM_CLASSIFICATION_ELIGIBILITY,
    DIM_HTF_DATA_COVERAGE,
    DIM_OBSERVER_DELIVERY_COVERAGE,
    DIM_OOS_STATISTICAL_READINESS,
    DIM_SOURCE_CAPTURE_COVERAGE,
    STATUS_CONFLICT,
    STATUS_GAP,
    STATUS_HOLD,
    STATUS_PASS,
    STATUS_UNKNOWN,
    LedgerEventView,
    SourceEventView,
    reconcile,
)

NOW = 2_000_000_000_000_000  # µs
GRACE = 60_000_000  # 60 s in µs


def _src(key: str, age_us: int = 0) -> SourceEventView:
    return SourceEventView(
        source_event_key=key, signal_time_us=NOW - age_us - 1_000_000,
        captured_at_us=NOW - age_us)


def _ledger(key: str, *, status="CLASSIFIED", eligible=True, htf_complete=True,
            age_us: int = 0) -> LedgerEventView:
    return LedgerEventView(
        source_event_key=key, status=status, eligible=eligible,
        htf_complete=htf_complete, captured_at_us=NOW - age_us)


def test_exactly_five_dimensions_with_stable_names():
    report = reconcile(
        source_events=(), ledger_events=(), now_us=NOW, grace_us=GRACE)
    assert tuple(d.dimension for d in report.dimensions) == ALL_DIMENSIONS
    assert ALL_DIMENSIONS == (
        DIM_SOURCE_CAPTURE_COVERAGE,
        DIM_OBSERVER_DELIVERY_COVERAGE,
        DIM_HTF_DATA_COVERAGE,
        DIM_CLASSIFICATION_ELIGIBILITY,
        DIM_OOS_STATISTICAL_READINESS,
    )


def _dimension(report, name):
    return next(d for d in report.dimensions if d.dimension == name)


def test_unmatched_event_beyond_grace_is_gap():
    report = reconcile(
        source_events=[_src("k1", age_us=GRACE + 1)],
        ledger_events=[],
        now_us=NOW, grace_us=GRACE)
    dim = _dimension(report, DIM_OBSERVER_DELIVERY_COVERAGE)
    assert dim.status == STATUS_GAP
    assert dim.evidence["unmatched_keys"] == ["k1"]


def test_young_unmatched_event_is_grace_lag_hold():
    report = reconcile(
        source_events=[_src("k1", age_us=GRACE - 1)],
        ledger_events=[],
        now_us=NOW, grace_us=GRACE)
    dim = _dimension(report, DIM_OBSERVER_DELIVERY_COVERAGE)
    assert dim.status == STATUS_HOLD
    assert dim.evidence["lagging_keys"] == ["k1"]
    assert "COVERAGE_LAG" in dim.detail


def test_identity_conflicts_are_conflict_status():
    report = reconcile(
        source_events=[_src("k1")],
        ledger_events=[_ledger("k1")],
        conflicts=[{"source_event_key": "k2"}],
        now_us=NOW, grace_us=GRACE)
    assert _dimension(report, DIM_OBSERVER_DELIVERY_COVERAGE).status \
        == STATUS_CONFLICT
    assert report.verdict == STATUS_CONFLICT


def test_no_blanket_pass_on_single_event_success():
    report = reconcile(
        source_events=[_src("k1")],
        ledger_events=[_ledger("k1")],
        now_us=NOW, grace_us=GRACE)
    # the delivery dimension may pass on full accounting...
    assert _dimension(report, DIM_OBSERVER_DELIVERY_COVERAGE).status == STATUS_PASS
    # ...but the OVERALL verdict is never a blanket PASS in Phase A
    assert report.verdict != STATUS_PASS


def test_empty_scope_never_passes():
    report = reconcile(
        source_events=[], ledger_events=[], now_us=NOW, grace_us=GRACE)
    assert _dimension(report, DIM_OBSERVER_DELIVERY_COVERAGE).status == STATUS_HOLD
    assert _dimension(report, DIM_HTF_DATA_COVERAGE).status == STATUS_HOLD
    assert _dimension(report, DIM_CLASSIFICATION_ELIGIBILITY).status == STATUS_HOLD
    assert report.verdict != STATUS_PASS


def test_htf_incomplete_coverage_reports_gap_beyond_grace():
    report = reconcile(
        source_events=[_src("k1", age_us=GRACE + 5)],
        ledger_events=[_ledger("k1", htf_complete=False, age_us=GRACE + 5)],
        now_us=NOW, grace_us=GRACE)
    dim = _dimension(report, DIM_HTF_DATA_COVERAGE)
    assert dim.status == STATUS_GAP
    assert dim.evidence["incomplete_htf_count"] == 1


def test_deferred_classifications_hold_the_funnel():
    report = reconcile(
        source_events=[_src("k1")],
        ledger_events=[_ledger("k1", status="DEFERRED", eligible=False)],
        now_us=NOW, grace_us=GRACE)
    assert _dimension(report, DIM_HTF_DATA_COVERAGE).status == STATUS_HOLD
    assert _dimension(report, DIM_CLASSIFICATION_ELIGIBILITY).status == STATUS_HOLD


def test_source_capture_dimension_is_unknown_without_evidence():
    report = reconcile(
        source_events=[], ledger_events=[], now_us=NOW, grace_us=GRACE)
    dim = _dimension(report, DIM_SOURCE_CAPTURE_COVERAGE)
    assert dim.status == STATUS_UNKNOWN  # UNKNOWN...
    assert report.verdict in (STATUS_HOLD, STATUS_GAP, STATUS_CONFLICT)  # ...never PASS


def test_source_capture_gap_evidence_is_reported():
    report = reconcile(
        source_events=[], ledger_events=[], now_us=NOW, grace_us=GRACE,
        source_capture_evidence={"capture_complete": False})
    assert _dimension(report, DIM_SOURCE_CAPTURE_COVERAGE).status == STATUS_GAP


def test_oos_statistical_readiness_holds_before_accumulation():
    report = reconcile(
        source_events=[_src("k1")], ledger_events=[_ledger("k1")],
        now_us=NOW, grace_us=GRACE)
    assert _dimension(report, DIM_OOS_STATISTICAL_READINESS).status == STATUS_UNKNOWN

    report2 = reconcile(
        source_events=[_src("k1")], ledger_events=[_ledger("k1")],
        now_us=NOW, grace_us=GRACE,
        preregistration={
            "sample_thresholds": {"min_events_per_cohort": 100},
            "accumulated_counts": {},
        })
    assert _dimension(report2, DIM_OOS_STATISTICAL_READINESS).status == STATUS_HOLD
