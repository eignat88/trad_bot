"""Pure matching regressions for Issue #169. No database required."""
from datetime import datetime, timedelta, timezone

from tools.research.htf_point_b_final_paired_oos_verdict_v1 import (
    BASELINE, POINT_B, EventRecord, match_frozen_events,
)

T = datetime(2026, 10, 7, tzinfo=timezone.utc)


def row(exp, *, event="touch-1", direction="SHORT", symbol="BTCUSDT",
        time=T, obs=1, final=True, r=0.2):
    return EventRecord(exp, event, obs, symbol, direction, time, final, r)


def test_matched_event_delta_and_original_entry_times():
    report = match_frozen_events([
        row(BASELINE, time=T, r=0.4),
        row(POINT_B, time=T + timedelta(minutes=25), r=-0.1),
    ])
    assert len(report.pairs) == 1
    assert round(report.pairs[0].delta_r, 8) == -0.5
    assert report.reasons == {}


def test_unmatched_funnel_is_not_silently_dropped():
    report = match_frozen_events([
        row(BASELINE, event="no-point-b"),
        row(POINT_B, event="no-baseline"),
    ])
    assert not report.pairs
    assert report.reasons == {
        "baseline_only_event": 1,
        "point_b_only_event": 1,
    }


def test_duplicate_event_fails_closed():
    report = match_frozen_events([
        row(BASELINE, obs=1),
        row(BASELINE, obs=2),
        row(POINT_B),
    ])
    assert not report.pairs
    assert report.reasons["duplicate_event_identity"] == 1


def test_cross_direction_does_not_pair():
    report = match_frozen_events([
        row(BASELINE, direction="LONG"),
        row(POINT_B, direction="SHORT"),
    ])
    assert len(report.pairs) == 0
    assert report.reasons["baseline_only_event"] == 1


def test_causality_violation_fails_closed():
    report = match_frozen_events([
        row(BASELINE, time=T + timedelta(minutes=30)),
        row(POINT_B, time=T),
    ])
    assert not report.pairs
    assert report.reasons["baseline_after_point_b"] == 1


def test_unfinalized_pair_excluded():
    report = match_frozen_events([
        row(BASELINE),
        row(POINT_B, final=False),
    ])
    assert not report.pairs
    assert report.reasons["not_both_finalized"] == 1


def test_prefreeze_rejected():
    freeze = datetime(2026, 10, 6, 14, tzinfo=timezone.utc)
    report = match_frozen_events([row(POINT_B, time=freeze)])
    assert report.reasons["prefreeze"] == 1
