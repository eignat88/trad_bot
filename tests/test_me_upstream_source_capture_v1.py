"""Issue #305 — ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1 unit tests.

Covers the activation and capture layers of the isolated, prospective-only,
fail-open upstream source capture:

* runtime feature flag gate (no store access while disabled);
* fail-closed activation (RUNNING + frozen freeze_ts + matching protocol
  fingerprint) with honest reason codes;
* deterministic capture of one ME LONG detector event (idempotent
  ``source_event_key``, best-effort health with delivery latency);
* INVALID_SCANNER / INVALID_EVENT / PRE_FREEZE determinstic rejections with
  the durable ``rejected_event`` ledger (criterion 12: never in the eligible
  cohort);
* re-delivery idempotency (DUPLICATE after restart) and immutable-lineage
  conflict fail-closed (sticky BLOCKED) / fail-open (no exception);
* write-error fail-open (ERROR status, never raises).

Plain pytest: no network, no DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.research.me_upstream_source_v1.activation import (
    REASON_DISABLED,
    REASON_FINGERPRINT_MISMATCH,
    REASON_INVALID_FREEZE,
    REASON_MISSING_FREEZE,
    REASON_NOT_INSTALLED,
    REASON_NOT_RUNNING,
    REASON_PRE_FREEZE,
    evaluate_activation,
    is_post_freeze,
    is_runtime_enabled,
)
from app.research.me_upstream_source_v1.capture import (
    BLOCK_REASON_LINEAGE_CONFLICT,
    CAPTURE_BLOCKED,
    CAPTURE_DISABLED,
    CAPTURE_DUPLICATE,
    CAPTURE_ERROR,
    CAPTURE_INVALID_EVENT,
    CAPTURE_INVALID_SCANNER,
    CAPTURE_INTEGRITY_CONFLICT,
    CAPTURE_NOT_ACTIVE,
    CAPTURE_PRE_FREEZE,
    CAPTURE_STATUSES,
    CAPTURED,
    capture_with_status,
)
from app.research.me_upstream_source_v1.contracts import (
    REASON_EVENT_KEY_LINEAGE_CONFLICT,
    REASON_INCOMPLETE_FEATURES,
    SourceIntegrityError,
    build_source_event,
)
from app.research.me_upstream_source_v1.identity import source_event_key
from app.research.me_upstream_source_v1.protocol import (
    ACTIVATION_FLAG,
    EXPERIMENT_ID,
    ROUTE_PRE_SCORING_SOURCE,
    protocol_fingerprint,
)
from app.research.me_upstream_source_v1.store import (
    SAVE_CAPTURED,
    SAVE_DUPLICATE,
    SAVE_NOT_ACTIVE,
    InMemoryUpstreamSourceStore,
    PgUpstreamSourceStore,
)
from app.scanners.models import SetupCandidate

UTC = timezone.utc

# Frozen activation boundary and a strictly-post-freeze signal time.
FREEZE = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
SIGNAL_TIME = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)  # strictly after freeze
CAPTURED_AT = SIGNAL_TIME + timedelta(seconds=90)

CANDLE_MS = 1_700_000_000_000
FIXED_SETUP_ID = UUID("12345678-1234-5678-1234-567812345678")

ENABLED = {ACTIVATION_FLAG: "1"}
VALID_FEATURES = {
    "rsi_14": 55.0,
    "rsi_delta_3": 1.25,
    "entry_confirmation_rsi_rising": True,
}


# ----------------------------------------------------------------------
# Builders / doubles
# ----------------------------------------------------------------------

def fixed_clock() -> datetime:
    """Deterministic capture clock."""
    return CAPTURED_AT


def make_activation(**overrides):
    """A valid activation record (as stored by the admin script)."""
    row = {
        "experiment_id": EXPERIMENT_ID,
        "status": "RUNNING",
        "protocol_version": "1.0.0",
        "protocol_fingerprint": protocol_fingerprint(),
        "freeze_ts": FREEZE,
    }
    row.update(overrides)
    return row


def make_candidate(**overrides) -> SetupCandidate:
    """A valid ME LONG detector candidate (freshly detected, post-freeze)."""
    fields = {
        "setup_id": FIXED_SETUP_ID,
        "scanner_name": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        "direction": "LONG",
        "symbol": "BTCUSDT",
        "entry_timeframe": "5m",
        "detected_at": SIGNAL_TIME,
        "signal_candle_open_time": CANDLE_MS,
        "features": dict(VALID_FEATURES),
    }
    fields.update(overrides)
    return SetupCandidate(**fields)


def make_store(**activation_overrides) -> InMemoryUpstreamSourceStore:
    return InMemoryUpstreamSourceStore(make_activation(**activation_overrides))


def build_event(candidate: SetupCandidate):
    return build_source_event(
        candidate,
        activation_freeze_ts=FREEZE,
        captured_at=CAPTURED_AT,
    )


class EverythingRaisesStore:
    """Store double whose every method raises — capture must stay fail-open."""

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise RuntimeError(f"boom:{name}")

        return _boom


class SaveEventRaisesStore(InMemoryUpstreamSourceStore):
    def save_event(self, event):
        raise RuntimeError("save_event down")


class LoadActivationRaisesStore(InMemoryUpstreamSourceStore):
    def load_activation(self):
        raise RuntimeError("load_activation down")


class RecordHealthRaisesStore(InMemoryUpstreamSourceStore):
    def record_health(self, *args, **kwargs):
        raise RuntimeError("record_health down")


# ======================================================================
# Criterion 1 — capture disabled: no store access at all
# ======================================================================

def test_c1_disabled_flag_returns_disabled_and_store_untouched():
    store = make_store()
    assert capture_with_status(store, make_candidate(), env={}) == CAPTURE_DISABLED
    assert store.writes == 0
    assert store.events == {}
    assert store.health == []
    assert store.rejected == []
    assert store.states == []


def test_c1_disabled_flag_performs_no_store_access_even_on_explosive_store():
    store = EverythingRaisesStore()
    # Any store access would raise -> ERROR; DISABLED proves zero access.
    assert capture_with_status(store, make_candidate(), env={}) == CAPTURE_DISABLED


def test_c1_is_runtime_enabled_only_literal_one():
    assert is_runtime_enabled({}) is False
    assert is_runtime_enabled({ACTIVATION_FLAG: "1"}) is True
    assert is_runtime_enabled({ACTIVATION_FLAG: "0"}) is False
    assert is_runtime_enabled({ACTIVATION_FLAG: "true"}) is False
    assert is_runtime_enabled({ACTIVATION_FLAG: "True"}) is False
    assert is_runtime_enabled({ACTIVATION_FLAG: ""}) is False


# ======================================================================
# Criterion 2 — no RUNNING activation / no freeze -> NOT_ACTIVE, zero writes
# ======================================================================

def _assert_not_active_no_writes(store, candidate=None):
    status = capture_with_status(
        store,
        candidate if candidate is not None else make_candidate(),
        env=ENABLED,
        clock=fixed_clock,
    )
    assert status == CAPTURE_NOT_ACTIVE
    assert store.writes == 0
    assert store.events == {}
    assert store.health == []
    assert store.rejected == []


def test_c2_activation_row_missing_not_active_zero_writes():
    _assert_not_active_no_writes(InMemoryUpstreamSourceStore(None))


def test_c2_status_ready_not_active_zero_writes():
    _assert_not_active_no_writes(make_store(status="READY"))


def test_c2_running_without_freeze_ts_not_active_zero_writes():
    _assert_not_active_no_writes(make_store(freeze_ts=None))


def test_c2_running_with_foreign_fingerprint_not_active_zero_writes():
    _assert_not_active_no_writes(make_store(protocol_fingerprint="0" * 64))
    activation, reason = evaluate_activation(
        make_activation(protocol_fingerprint="0" * 64), enabled=True
    )
    assert activation is None
    assert reason == REASON_FINGERPRINT_MISMATCH


def test_c2_naive_freeze_ts_string_not_active_zero_writes():
    _assert_not_active_no_writes(make_store(freeze_ts="2026-01-02T00:00:00"))


def test_c2_evaluate_activation_honest_reasons():
    # Disabled flag wins over everything.
    assert evaluate_activation(None, enabled=False) == (None, REASON_DISABLED)

    # Missing / foreign activation row.
    assert evaluate_activation(None, enabled=True) == (None, REASON_NOT_INSTALLED)
    foreign = make_activation(experiment_id="SOMETHING_ELSE")
    assert evaluate_activation(foreign, enabled=True) == (None, REASON_NOT_INSTALLED)

    # Not RUNNING (READY / PAUSED / DISABLED / BLOCKED all refuse).
    for status in ("READY", "PAUSED", "DISABLED", "BLOCKED"):
        assert evaluate_activation(
            make_activation(status=status), enabled=True
        ) == (None, REASON_NOT_RUNNING)

    # RUNNING but no frozen freeze_ts.
    assert evaluate_activation(
        make_activation(freeze_ts=None), enabled=True
    ) == (None, REASON_MISSING_FREEZE)

    # RUNNING + frozen but a foreign protocol fingerprint.
    assert evaluate_activation(
        make_activation(protocol_fingerprint="ff" * 32), enabled=True
    ) == (None, REASON_FINGERPRINT_MISMATCH)

    # Naive freeze_ts (string without tz info) fails closed.
    assert evaluate_activation(
        make_activation(freeze_ts="2026-01-02T00:00:00"), enabled=True
    ) == (None, REASON_INVALID_FREEZE)
    assert evaluate_activation(
        make_activation(freeze_ts=datetime(2026, 1, 2, 0, 0)), enabled=True
    ) == (None, REASON_INVALID_FREEZE)

    # Happy path: everything matches -> ACTIVE with a frozen activation.
    activation, reason = evaluate_activation(make_activation(), enabled=True)
    assert reason == "ACTIVE"
    assert activation is not None
    assert activation.experiment_id == EXPERIMENT_ID
    assert activation.status == "RUNNING"
    assert activation.freeze_ts == FREEZE


# ======================================================================
# Criterion 3 — valid capture: one event row + health with delivery latency
# ======================================================================

def test_c3_valid_candidate_captured_single_row_keyed_by_source_event_key():
    store = make_store()
    candidate = make_candidate()
    assert capture_with_status(
        store, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURED

    key = source_event_key(candidate)
    assert list(store.events.keys()) == [key]
    row = store.events[key]
    assert row["symbol"] == "BTCUSDT"
    assert row["signal_time"] == SIGNAL_TIME
    assert row["captured_at"] == CAPTURED_AT
    assert row["lineage_hash"]
    assert row["comparable_lineage_hash"]
    assert row["lineage"]["source_scanner"] == (
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
    )


def test_c3_health_row_captured_with_delivery_latency():
    store = make_store()
    candidate = make_candidate()
    assert capture_with_status(
        store, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURED

    assert len(store.health) == 1
    health = store.health[0]
    assert health["status"] == CAPTURED
    assert health["events_seen"] == 1
    assert health["events_inserted"] == 1
    assert health["duplicates"] == 0
    assert health["errors"] == 0
    expected_latency = (CAPTURED_AT - SIGNAL_TIME).total_seconds()
    assert expected_latency == 90.0
    assert health["delivery_latency_seconds"] == expected_latency


# ======================================================================
# Criterion 4 — foreign scanner / wrong direction -> INVALID_SCANNER
# ======================================================================

def test_c4_foreign_scanner_invalid_scanner_zero_writes():
    store = make_store()
    candidate = make_candidate(scanner_name="BREAKOUT_RETEST")
    assert capture_with_status(
        store, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURE_INVALID_SCANNER
    assert store.writes == 0
    assert store.events == {}
    assert store.rejected == []


def test_c4_wrong_direction_invalid_scanner_zero_writes():
    store = make_store()
    candidate = make_candidate(direction="SHORT")
    assert capture_with_status(
        store, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURE_INVALID_SCANNER
    assert store.writes == 0
    assert store.events == {}


# ======================================================================
# Criterion 5 — re-delivery / redelivery after restart -> DUPLICATE
# ======================================================================

def test_c5_redelivery_same_candidate_duplicate_single_row():
    store = make_store()
    candidate = make_candidate()
    assert capture_with_status(
        store, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURED
    assert capture_with_status(
        store, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURE_DUPLICATE
    assert len(store.events) == 1

    assert store.health[1]["status"] == CAPTURE_DUPLICATE
    assert store.health[1]["duplicates"] == 1
    assert store.health[1]["events_inserted"] == 0


def test_c5_restart_new_store_same_key_duplicate_semantics():
    candidate = make_candidate()
    first = make_store()
    assert capture_with_status(
        first, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURED

    # Simulated restart: fresh store, same activation, same candidate.
    restarted = make_store()
    assert capture_with_status(
        restarted, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURED
    assert capture_with_status(
        restarted, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURE_DUPLICATE
    assert len(restarted.events) == 1
    # The key is restart-stable (derived only from the signal identity).
    assert set(first.events) == set(restarted.events) == {source_event_key(candidate)}


# ======================================================================
# Criterion 6 — immutable lineage conflict -> INTEGRITY_CONFLICT + sticky BLOCKED
# ======================================================================

def test_c6_lineage_conflict_sticky_blocked_and_no_new_writes():
    store = make_store()
    first = make_candidate()
    assert capture_with_status(
        store, first, env=ENABLED, clock=fixed_clock
    ) == CAPTURED

    # SAME source_event_key (same scanner/symbol/direction/timeframe/candle),
    # DIFFERENT immutable lineage (rsi_14 changed).
    conflicting = make_candidate(features={
        "rsi_14": 60.0,
        "rsi_delta_3": 1.25,
        "entry_confirmation_rsi_rising": True,
    })
    assert capture_with_status(
        store, conflicting, env=ENABLED, clock=fixed_clock
    ) == CAPTURE_INTEGRITY_CONFLICT

    # Still exactly one immutable event row.
    assert len(store.events) == 1

    # Sticky BLOCKED state with the honest reason code.
    blocked = [s for s in store.states if s["state"] == "BLOCKED"]
    assert len(blocked) == 1
    assert blocked[0]["reason_code"] == BLOCK_REASON_LINEAGE_CONFLICT
    assert BLOCK_REASON_LINEAGE_CONFLICT == REASON_EVENT_KEY_LINEAGE_CONFLICT

    # Health observes the conflict.
    assert store.health[-1]["status"] == CAPTURE_INTEGRITY_CONFLICT
    assert store.health[-1]["errors"] == 1

    # Sticky fail-closed: a subsequent capture of anything is BLOCKED and
    # writes NOTHING new.
    writes_after_conflict = store.writes
    assert capture_with_status(
        store, make_candidate(symbol="ETHUSDT"), env=ENABLED, clock=fixed_clock
    ) == CAPTURE_BLOCKED
    assert capture_with_status(
        store, make_candidate(), env=ENABLED, clock=fixed_clock
    ) == CAPTURE_BLOCKED
    assert store.writes == writes_after_conflict
    assert len(store.events) == 1


def test_c6_reference_price_lineage_conflict_still_conflicts():
    store = make_store()
    assert capture_with_status(
        store, make_candidate(), env=ENABLED, clock=fixed_clock
    ) == CAPTURED
    drifted_price = make_candidate(reference_price=99_999.0)
    assert capture_with_status(
        store, drifted_price, env=ENABLED, clock=fixed_clock
    ) == CAPTURE_INTEGRITY_CONFLICT
    assert len(store.events) == 1


def test_c6_store_save_event_raises_source_integrity_error_on_conflict():
    store = make_store()
    event_a = build_event(make_candidate())
    assert store.save_event(event_a) == SAVE_CAPTURED

    event_b = build_event(make_candidate(features={
        "rsi_14": 60.0,
        "rsi_delta_3": 1.25,
        "entry_confirmation_rsi_rising": True,
    }))
    assert event_b.source_event_key == event_a.source_event_key
    assert event_b.lineage_hash != event_a.lineage_hash
    with pytest.raises(SourceIntegrityError) as excinfo:
        store.save_event(event_b)
    assert REASON_EVENT_KEY_LINEAGE_CONFLICT in excinfo.value.args

    # Byte-identical re-delivery is a DUPLICATE, never a conflict.
    assert store.save_event(event_a) == SAVE_DUPLICATE
    assert len(store.events) == 1


# ======================================================================
# Criterion 7 — write errors fail-open (ERROR status, never raises)
# ======================================================================

def test_c7_save_event_error_returns_error_and_never_raises():
    store = SaveEventRaisesStore(make_activation())
    assert capture_with_status(
        store, make_candidate(), env=ENABLED, clock=fixed_clock
    ) == CAPTURE_ERROR


def test_c7_load_activation_error_returns_error_and_never_raises():
    store = LoadActivationRaisesStore(make_activation())
    assert capture_with_status(
        store, make_candidate(), env=ENABLED, clock=fixed_clock
    ) == CAPTURE_ERROR


def test_c7_record_health_error_is_best_effort_still_captured():
    store = RecordHealthRaisesStore(make_activation())
    assert capture_with_status(
        store, make_candidate(), env=ENABLED, clock=fixed_clock
    ) == CAPTURED
    assert len(store.events) == 1
    assert store.health == []  # health write failed silently (best-effort)


def test_c7_none_candidate_is_invalid_event_zero_writes():
    store = make_store()
    assert capture_with_status(store, None, env=ENABLED) == CAPTURE_INVALID_EVENT
    assert store.writes == 0
    assert store.events == {}


# ======================================================================
# Criterion 8 — pre-freeze events refused (strict signal_time > freeze_ts)
# ======================================================================

def test_c8_pre_freeze_event_refused_and_ledgered():
    store = make_store()
    pre_freeze = make_candidate(detected_at=FREEZE - timedelta(seconds=1))
    assert capture_with_status(
        store, pre_freeze, env=ENABLED, clock=fixed_clock
    ) == CAPTURE_PRE_FREEZE

    # NOT in the eligible source_event cohort.
    assert store.events == {}
    # Durable rejection ledger row with the honest reason code.
    assert store.rejected[-1]["reason_code"] == REASON_PRE_FREEZE
    assert REASON_PRE_FREEZE == "PRE_FREEZE"
    assert store.health[-1]["status"] == CAPTURE_PRE_FREEZE


def test_c8_freeze_boundary_is_strict_equal_is_refused_after_is_captured():
    store = make_store()

    # Exactly AT freeze_ts is refused (strict > semantics).
    at_freeze = make_candidate(detected_at=FREEZE)
    assert capture_with_status(
        store, at_freeze, env=ENABLED, clock=fixed_clock
    ) == CAPTURE_PRE_FREEZE
    assert store.events == {}

    # Strictly after freeze_ts captures fine.
    after_freeze = make_candidate(detected_at=FREEZE + timedelta(seconds=1))
    assert capture_with_status(
        store, after_freeze, env=ENABLED, clock=fixed_clock
    ) == CAPTURED
    assert len(store.events) == 1
    assert source_event_key(after_freeze) in store.events


# ======================================================================
# Criterion 9 — incomplete features -> INVALID_EVENT + rejection ledger
# ======================================================================

def test_c9_missing_rsi_delta_3_invalid_event_and_rejected():
    store = make_store()
    candidate = make_candidate(features={
        "rsi_14": 55.0,
        "entry_confirmation_rsi_rising": True,
    })
    assert capture_with_status(
        store, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURE_INVALID_EVENT

    # Never enters the eligible cohort (criterion 12).
    assert store.events == {}
    assert store.rejected[-1]["reason_code"] == REASON_INCOMPLETE_FEATURES
    assert REASON_INCOMPLETE_FEATURES == "INCOMPLETE_FEATURES"
    assert store.health[-1]["status"] == CAPTURE_INVALID_EVENT


# ======================================================================
# Criterion 10 — unknown capture_route -> INVALID_EVENT; default route
# ======================================================================

def test_c10_unknown_route_invalid_event_zero_writes():
    store = make_store()
    assert capture_with_status(
        store,
        make_candidate(),
        capture_route="WHATEVER",
        env=ENABLED,
        clock=fixed_clock,
    ) == CAPTURE_INVALID_EVENT
    assert store.writes == 0
    assert store.events == {}


def test_c10_default_route_is_pre_scoring_source():
    store = make_store()
    candidate = make_candidate()
    assert capture_with_status(
        store, candidate, env=ENABLED, clock=fixed_clock
    ) == CAPTURED
    key = source_event_key(candidate)
    assert store.events[key]["lineage"]["capture_route"] == ROUTE_PRE_SCORING_SOURCE
    assert ROUTE_PRE_SCORING_SOURCE == "PRE_SCORING_SOURCE"


# ======================================================================
# Criterion 11 — capture_with_status NEVER raises (fail-open)
# ======================================================================

def test_c11_store_raising_on_every_method_returns_error():
    store = EverythingRaisesStore()  # load_capture_state raises first
    assert capture_with_status(
        store, make_candidate(), env=ENABLED, clock=fixed_clock
    ) == CAPTURE_ERROR


def test_c11_never_raises_across_the_full_scenario_matrix():
    scenarios = [
        # (store, candidate, env, capture_route)
        (InMemoryUpstreamSourceStore(None), make_candidate(),
         ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(), ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(detected_at=None),
         ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(detected_at=datetime(2026, 1, 2, 3, 4)),
         ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(features={}),
         ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(features={
            "rsi_14": 150.0, "rsi_delta_3": 1.25,
            "entry_confirmation_rsi_rising": True,
        }), ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(features={
            "rsi_14": True, "rsi_delta_3": 1.25,
            "entry_confirmation_rsi_rising": True,
        }), ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(features={
            "rsi_14": 55.0, "rsi_delta_3": 1.25,
            "entry_confirmation_rsi_rising": "yes",
        }), ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(detected_at=FREEZE),
         ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), None, ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(scanner_name="BREAKOUT_RETEST"),
         ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(), ENABLED, "WHATEVER"),
        (EverythingRaisesStore(), make_candidate(),
         ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (SaveEventRaisesStore(make_activation()), make_candidate(),
         ENABLED, ROUTE_PRE_SCORING_SOURCE),
        (make_store(), make_candidate(), {}, ROUTE_PRE_SCORING_SOURCE),
    ]
    for store, candidate, env, route in scenarios:
        status = capture_with_status(
            store,
            candidate,
            capture_route=route,
            env=env,
            clock=fixed_clock,
        )
        assert status in CAPTURE_STATUSES
        assert isinstance(status, str)


# ======================================================================
# Criterion 12 — freeze semantics
# ======================================================================

def test_c12_is_post_freeze_strictly_after_only():
    assert is_post_freeze(FREEZE + timedelta(seconds=1), FREEZE) is True
    assert is_post_freeze(FREEZE + timedelta(days=1), FREEZE) is True
    # Equal timestamps are REFUSED (strict >).
    assert is_post_freeze(FREEZE, FREEZE) is False
    assert is_post_freeze(FREEZE - timedelta(seconds=1), FREEZE) is False
    # Naive timestamps on either side fail closed.
    assert is_post_freeze(datetime(2026, 1, 2, 3, 0), FREEZE) is False
    assert is_post_freeze(SIGNAL_TIME, datetime(2026, 1, 2, 0, 0)) is False


def test_c12_evaluate_activation_disabled_flag():
    assert evaluate_activation(None, enabled=False) == (None, REASON_DISABLED)
    assert REASON_DISABLED == "DISABLED_FLAG"
    # A perfect activation record is still refused while the flag is off.
    assert evaluate_activation(
        make_activation(), enabled=False
    ) == (None, REASON_DISABLED)


# ======================================================================
# InMemoryUpstreamSourceStore invariants
# ======================================================================

def test_store_save_event_without_activation_not_active_and_no_row():
    store = InMemoryUpstreamSourceStore(None)
    event = build_event(make_candidate())
    assert store.save_event(event) == SAVE_NOT_ACTIVE
    assert store.events == {}
    assert store.writes == 0

    # Even with an activation, a non-RUNNING / unfrozen one refuses.
    for overrides in (
        {"status": "READY"},
        {"freeze_ts": None},
        {"protocol_fingerprint": "ff" * 32},
    ):
        guarded = InMemoryUpstreamSourceStore(
            make_activation(**overrides),
            expected_fingerprint=protocol_fingerprint(),
        )
        assert guarded.save_event(event) == SAVE_NOT_ACTIVE
        assert guarded.events == {}
        assert guarded.writes == 0


def test_store_save_event_duplicate_vs_captured():
    store = make_store()
    event = build_event(make_candidate())
    assert store.save_event(event) == SAVE_CAPTURED
    assert store.save_event(event) == SAVE_DUPLICATE
    assert store.save_event(build_event(make_candidate())) == SAVE_DUPLICATE
    assert len(store.events) == 1
    assert store.writes == 1


def test_store_record_state_rejects_invalid_state_with_value_error():
    store = make_store()
    with pytest.raises(ValueError):
        store.record_state("SOMETHING_ELSE", "WHATEVER")
    with pytest.raises(ValueError):
        store.record_state("", "WHATEVER")
    assert store.states == []
    assert store.writes == 0


def test_store_reject_event_appends_rows():
    store = make_store()
    store.reject_event(
        source_event_key="key-1",
        source_identity="id-1",
        reason_code="PRE_FREEZE",
        payload={"symbol": "BTCUSDT"},
    )
    store.reject_event(
        source_event_key="key-2",
        source_identity=None,
        reason_code="INCOMPLETE_FEATURES",
    )
    assert [row["reason_code"] for row in store.rejected] == [
        "PRE_FREEZE",
        "INCOMPLETE_FEATURES",
    ]
    assert store.rejected[0]["payload"] == {"symbol": "BTCUSDT"}
    assert store.rejected[1]["source_identity"] is None
    assert store.writes == 2


# ----------------------------------------------------------------------
# Redelivery variance (criterion 5 vs criterion 6 boundary)
# ----------------------------------------------------------------------

class TestRedeliveryVariance:
    def test_redetection_variance_is_duplicate_not_conflict(self):
        # The SAME signal candle re-detected on a later scan carries a NEW
        # setup_id and a NEW detected_at (re-detection variance).  That must
        # stay an idempotent DUPLICATE — never an INTEGRITY_CONFLICT — and
        # never create a second row (criterion 5).
        store = make_store()
        first = make_candidate()
        assert capture_with_status(
            store, first, env=ENABLED, clock=fixed_clock,
        ) == "CAPTURED"

        redetected = make_candidate(
            setup_id=uuid4(),
            detected_at=SIGNAL_TIME + timedelta(minutes=2),
        )
        assert capture_with_status(
            store, redetected, env=ENABLED, clock=fixed_clock,
        ) == "DUPLICATE"
        assert len(store.events) == 1
        assert store.states == []  # no BLOCKED, no conflict

    def test_immutable_fact_drift_is_still_a_conflict(self):
        # A re-delivery that changes an IMMUTABLE fact (price / features /
        # scanner version) keeps firing the conflict path.
        store = make_store()
        assert capture_with_status(
            store, make_candidate(), env=ENABLED, clock=fixed_clock,
        ) == "CAPTURED"
        drifted = make_candidate(
            setup_id=uuid4(),
            detected_at=SIGNAL_TIME + timedelta(minutes=2),
            reference_price=999.0,
        )
        assert capture_with_status(
            store, drifted, env=ENABLED, clock=fixed_clock,
        ) == "INTEGRITY_CONFLICT"


# ----------------------------------------------------------------------
# PgUpstreamSourceStore SQL contract (runnable without a server)
# ----------------------------------------------------------------------

class _FakeCursor:
    """Scripted DB-API cursor: records calls, serves fetchone() results."""

    def __init__(self, fetchone_script=()):
        self._script = list(fetchone_script)
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, tuple(params or ())))

    def fetchone(self):
        return self._script.pop(0) if self._script else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, fetchone_script=()):
        self.cur = _FakeCursor(fetchone_script)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class TestPgStoreSqlContract:
    def test_save_event_captured_on_returned_key(self):
        conn = _FakeConnection(fetchone_script=[("k" * 64,)])
        store = PgUpstreamSourceStore(conn)
        event = build_event(make_candidate())
        assert store.save_event(event) == "CAPTURED"
        sql = conn.cur.calls[0][0]
        assert "research_me_upstream_coverage.source_event" in sql
        assert "ON CONFLICT (source_event_key) DO NOTHING" in sql
        assert "status = 'RUNNING'" in sql  # activation guard in SQL
        assert "freeze_ts IS NOT NULL" in sql
        assert conn.commits == 1

    def test_save_event_duplicate_on_same_comparable_hash(self):
        event = build_event(make_candidate())
        conn = _FakeConnection(fetchone_script=[None, (event.comparable_lineage_hash,)])
        store = PgUpstreamSourceStore(conn)
        assert store.save_event(event) == "DUPLICATE"
        # The duplicate probe only SELECTs from the own-schema ledger.
        select_sql = conn.cur.calls[1][0]
        assert "research_me_upstream_coverage.source_event" in select_sql
        assert select_sql.strip().startswith("SELECT")

    def test_save_event_conflict_on_comparable_lineage_drift(self):
        event = build_event(make_candidate())
        conn = _FakeConnection(fetchone_script=[None, ("0" * 64,)])
        store = PgUpstreamSourceStore(conn)
        with pytest.raises(SourceIntegrityError) as excinfo:
            store.save_event(event)
        assert str(excinfo.value) == "EVENT_KEY_LINEAGE_CONFLICT"

    def test_save_event_not_active_when_row_vanished(self):
        conn = _FakeConnection(fetchone_script=[None, None])
        store = PgUpstreamSourceStore(conn)
        assert store.save_event(build_event(make_candidate())) == "NOT_ACTIVE"

    def test_write_methods_touch_only_own_schema(self):
        conn = _FakeConnection(fetchone_script=[("k" * 64,)])
        store = PgUpstreamSourceStore(conn)
        event = build_event(make_candidate())
        store.save_event(event)
        store.reject_event(
            source_event_key="r", source_identity=None,
            reason_code="PRE_FREEZE", payload={"x": 1},
        )
        store.record_state("BLOCKED", "EVENT_KEY_LINEAGE_CONFLICT", {})
        store.record_health(status="CAPTURED", events_inserted=1)
        inserts = [s for s, _ in conn.cur.calls]
        for sql in inserts:
            assert "dds." not in sql
            assert "research.research" not in sql
            assert "INSERT INTO research_me_upstream_coverage." in sql or \
                "SELECT" in sql
        assert {"research_me_upstream_coverage.source_event",
                "research_me_upstream_coverage.rejected_event",
                "research_me_upstream_coverage.capture_state_event",
                "research_me_upstream_coverage.capture_health"} <= {
            s.split("INTO ", 1)[1].split(" ", 1)[0].split("(", 1)[0]
            for s in inserts if "INSERT INTO" in s
        }
