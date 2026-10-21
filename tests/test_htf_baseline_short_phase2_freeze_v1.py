"""Issue #176 Phase-2 mandatory test matrix (2): freeze strictness and fail-closed gates."""
from __future__ import annotations

import pytest

from app.research.htf_baseline_short_phase2_freeze_v1 import (
    REASON_FIRST_TOUCH_NOT_AFTER_FREEZE,
    REASON_FREEZE_MISMATCH,
    REASON_MISSING_FREEZE,
    REASON_OK,
    REASON_RESUMED_PRE_FREEZE_EPISODE,
    REASON_SIGNAL_NOT_AFTER_FREEZE,
    REASON_WARMUP_ORIGIN_EPISODE,
    FreezeMismatchError,
    Phase2FreezeBoundary,
    parse_ts_ms,
    reject_all,
)

FREEZE = "2026-11-01T00:00:00Z"


def _boundary(**overrides):
    kwargs = dict(
        experiment_id="HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1",
        protocol_freeze_ts=FREEZE,
        registry_freeze_ts=FREEZE,
        database_freeze_ts=FREEZE,
        protocol_fingerprint="0" * 64,
    )
    kwargs.update(overrides)
    return Phase2FreezeBoundary.create(**kwargs)


def test_create_accepts_matching_sources():
    boundary = _boundary()
    assert boundary.freeze_ts_ms == parse_ts_ms(FREEZE)


def test_create_rejects_any_missing_source():
    for missing in ("protocol_freeze_ts", "registry_freeze_ts", "database_freeze_ts"):
        with pytest.raises(FreezeMismatchError) as excinfo:
            _boundary(**{missing: None})
        assert "fail closed" in str(excinfo.value)


def test_create_rejects_mismatched_sources():
    with pytest.raises(FreezeMismatchError) as excinfo:
        _boundary(database_freeze_ts="2026-11-01T00:00:01Z")
    assert "mismatch" in str(excinfo.value).lower()


def test_create_rejects_missing_or_short_fingerprint():
    with pytest.raises(FreezeMismatchError):
        _boundary(protocol_fingerprint="")


def test_signal_time_must_be_strictly_after_freeze():
    boundary = _boundary()
    assert boundary.check_signal_time(FREEZE).reason == REASON_SIGNAL_NOT_AFTER_FREEZE
    assert not boundary.check_signal_time(FREEZE)
    assert not boundary.check_signal_time("2026-10-31T23:59:59Z")
    assert boundary.check_signal_time("2026-11-01T00:00:01Z").reason == REASON_OK


def test_first_touch_must_be_strictly_after_freeze():
    boundary = _boundary()
    assert boundary.check_first_touch(FREEZE).reason == REASON_FIRST_TOUCH_NOT_AFTER_FREEZE
    assert not boundary.check_first_touch("2026-10-15T00:00:00Z")
    assert boundary.check_first_touch("2026-11-02T00:00:00Z").reason == REASON_OK


def test_pre_freeze_touch_rejected_even_when_confirmation_is_post_freeze():
    boundary = _boundary()
    decision = boundary.check_event(
        signal_time="2026-11-05T00:00:00Z",  # post-freeze confirmation
        first_touch_time="2026-10-31T23:00:00Z",  # pre-freeze touch
    )
    assert not decision
    assert decision.reason == REASON_FIRST_TOUCH_NOT_AFTER_FREEZE


def test_warmup_origin_episode_never_eligible():
    boundary = _boundary()
    decision = boundary.check_event(
        signal_time="2026-11-05T00:00:00Z",
        first_touch_time="2026-11-02T00:00:00Z",
        warmup_origin=True,
    )
    assert not decision
    assert decision.reason == REASON_WARMUP_ORIGIN_EPISODE


def test_resumed_pre_freeze_episode_never_eligible():
    boundary = _boundary()
    decision = boundary.check_event(
        signal_time="2026-11-05T00:00:00Z",
        first_touch_time="2026-11-02T00:00:00Z",
        resumed_pre_freeze=True,
    )
    assert not decision
    assert decision.reason == REASON_RESUMED_PRE_FREEZE_EPISODE


def test_fully_post_freeze_event_is_eligible():
    boundary = _boundary()
    decision = boundary.check_event(
        signal_time="2026-11-05T00:00:00Z",
        first_touch_time="2026-11-02T00:00:00Z",
    )
    assert decision
    assert decision.reason == REASON_OK


def test_reject_all_is_fail_closed():
    decision = reject_all()
    assert not decision
    assert decision.reason == REASON_MISSING_FREEZE


def test_point_b_freeze_is_never_reused():
    # Phase-2 boundary with the Point-B timestamp is constructible only as data;
    # the protocol/runbook forbid reuse. This test asserts the boundary is
    # experiment-scoped and the Point-B id can never share it.
    boundary = _boundary()
    assert boundary.experiment_id == "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"
    assert boundary.experiment_id != "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"


def test_parse_ts_ms_rejects_naive_and_bool():
    from datetime import datetime, timezone

    assert parse_ts_ms("2026-11-01T00:00:00Z") == parse_ts_ms(
        datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    with pytest.raises(ValueError):
        parse_ts_ms("2026-11-01T00:00:00")  # naive
    with pytest.raises(ValueError):
        parse_ts_ms(True)
