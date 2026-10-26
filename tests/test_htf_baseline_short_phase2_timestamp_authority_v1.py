"""Issue #197 Phase-2 timestamp authority matrix: frozen protocol, freeze
record (timestamp authority V1), three-authority agreement, fail-closed
startup gate and partial-failure scenarios.

SAFETY: pure logic + stubbed persistence only. This module NEVER connects to
PostgreSQL, never touches production state and performs no deployment action.
The PostgreSQL-side scenarios (activation windows, freeze-registry uniqueness,
concurrent activation, interrupted activation, role privileges) live in
``test_htf_baseline_short_phase2_freeze_record_postgres_v1.py`` and run only
against a disposable database of a dedicated local test cluster.
"""
from __future__ import annotations

import copy

import pytest

from app.research.htf_baseline_short_phase2_freeze_v1 import (
    REASON_FIRST_TOUCH_NOT_AFTER_FREEZE,
    REASON_RESUMED_PRE_FREEZE_EPISODE,
    REASON_SIGNAL_NOT_AFTER_FREEZE,
    REASON_WARMUP_ORIGIN_EPISODE,
    Phase2FreezeBoundary,
    parse_ts_ms,
    reject_all,
)
from app.research.htf_baseline_short_phase2_persistence_v1 import (
    ExperimentState,
    Phase2ObservationWriter,
)
from app.research.htf_baseline_short_phase2_protocol_v1 import (
    APPROVED_FREEZE_TS,
    CANDIDATE_PROTOCOL_FINGERPRINT,
    CANDIDATE_PROTOCOL_STATUS,
    EXPERIMENT_ID,
    FROZEN_PROTOCOL_FINGERPRINT,
    OPERATOR_APPROVED,
    PROTOCOL_STATUS,
    PROTOCOL_V1,
    approved_freeze_ts,
    operator_decisions_pending,
    protocol_fingerprint,
    verify_frozen_fingerprint,
)
from app.research.htf_baseline_short_phase2_timestamp_authority_v1 import (
    ACCEPTANCE_FUTURE_WINDOW_S,
    ACCEPTANCE_PAST_WINDOW_S,
    CLAIM_RECEIPT_SCHEMA,
    FREEZE_RECORD_SCHEMA,
    FreezeRecordError,
    build_claim_receipt,
    build_freeze_record,
    canonical_record_json,
    claim_receipt_hash,
    freeze_record_hash,
    load_claim_receipt,
    load_freeze_record,
    validate_claim_receipt,
    validate_freeze_record,
    verify_authority_agreement,
    verify_claim_identity,
)

FP = "a" * 64
FP_OTHER = "b" * 64
TS = "2026-11-02T10:00:00Z"
TS_OTHER = "2026-11-02T10:01:00Z"
POINT_B_FREEZE = "2026-10-06T14:00:00Z"


def make_record(**overrides) -> dict:
    """Record with raw field overrides (self-hash intentionally stale)."""
    record = _base_record(**overrides)
    return record


def make_sealed_record(**overrides) -> dict:
    """Record with overrides applied AND a correct self-hash (semantic cases)."""
    record = _base_record(**overrides)
    payload = {k: v for k, v in record.items() if k != "record_sha256"}
    payload["record_sha256"] = freeze_record_hash(payload)
    return payload


def _base_record(**overrides) -> dict:
    record = build_freeze_record(
        experiment_id=EXPERIMENT_ID,
        freeze_ts=TS,
        protocol_fingerprint=FP,
        claimed_at=TS,
        operator_approval_ref="GO-TEST-001",
        issued_by="tester",
        record_id="00000000-0000-0000-0000-000000000001",
    )
    for key, value in overrides.items():
        if value is _DROP:
            record.pop(key, None)
            continue
        record[key] = value
    return record


class _Drop:
    pass


_DROP = _Drop()


# ============================================================ frozen protocol
def test_frozen_protocol_keeps_all_six_approved_values():
    assert PROTOCOL_STATUS == "FROZEN"
    assert PROTOCOL_V1["status"] == "FROZEN"
    assert PROTOCOL_V1["geometry"]["proximity_ms"] == {"value": 300000, "status": OPERATOR_APPROVED}
    assert PROTOCOL_V1["geometry"]["price_bps"] == {"value": 20, "status": OPERATOR_APPROVED}
    retirement = PROTOCOL_V1["lifecycle"]["level_retirement_policy"]
    assert retirement["rule"] == "LEVEL_ELIGIBILITY_LOSS_IS_NON_TERMINAL"
    assert retirement["status"] == OPERATOR_APPROVED
    assert PROTOCOL_V1["maturity_gates"]["minimum_confirmations"] == {"value": 30, "status": OPERATOR_APPROVED}
    assert PROTOCOL_V1["maturity_gates"]["minimum_symbols"] == {"value": 5, "status": OPERATOR_APPROVED}
    assert PROTOCOL_V1["maturity_gates"]["minimum_active_days"] == {"value": 14, "status": OPERATOR_APPROVED}
    assert operator_decisions_pending() == []


def test_frozen_protocol_keeps_sensitivity_ambiguity_and_scope_contracts():
    assert "0/10/20/30/40 bps" in PROTOCOL_V1["geometry"]["boundaries"]
    assert "AMBIGUOUS_PRIOR_ANCHORS" in PROTOCOL_V1["geometry"]["ambiguity"]
    assert PROTOCOL_V1["parameter_optimization"]["retrospective_pnl_tuning"] == "PROHIBITED"
    assert PROTOCOL_V1["automation"]["phase2_start"] == "PROHIBITED_WITHOUT_OPERATOR_APPROVAL"
    assert PROTOCOL_V1["automation"]["observer_enabled_default"] is False
    assert PROTOCOL_V1["study_type"] == "PROSPECTIVE_OBSERVE_ONLY"
    assert PROTOCOL_V1["event_identity"]["at_most_one_per_episode"] is True


def test_frozen_fingerprint_matches_pin_and_candidate_is_reproducible():
    assert protocol_fingerprint() == FROZEN_PROTOCOL_FINGERPRINT
    assert verify_frozen_fingerprint() is True
    candidate_payload = copy.deepcopy(PROTOCOL_V1)
    candidate_payload["status"] = CANDIDATE_PROTOCOL_STATUS
    assert protocol_fingerprint(candidate_payload) == CANDIDATE_PROTOCOL_FINGERPRINT
    # any mutation of the frozen payload must break the pin
    mutated = copy.deepcopy(PROTOCOL_V1)
    mutated["maturity_gates"]["minimum_symbols"]["value"] = 6
    assert verify_frozen_fingerprint(mutated) is False


def test_frozen_protocol_carries_no_timestamp_and_frozen_alone_authorizes_nothing():
    # FROZEN is a specification statement only: no timestamp may live in Git
    assert APPROVED_FREEZE_TS is None
    assert approved_freeze_ts() is None


# =============================================================== freeze record
def test_record_hash_is_deterministic_and_self_verifying():
    record = make_record()
    assert record["schema"] == FREEZE_RECORD_SCHEMA
    assert freeze_record_hash(record) == freeze_record_hash(dict(record))
    again = build_freeze_record(
        experiment_id=record["experiment_id"],
        freeze_ts=record["freeze_ts"],
        protocol_fingerprint=record["protocol_fingerprint"],
        claimed_at=record["claimed_at"],
        operator_approval_ref=record["operator_approval_ref"],
        issued_by=record["issued_by"],
        record_id=record["record_id"],
    )
    assert again == record  # byte-stable composition
    assert canonical_record_json(record) == canonical_record_json(dict(record))
    validated = validate_freeze_record(record, expected_experiment_id=EXPERIMENT_ID)
    assert validated["freeze_ts"] == TS


@pytest.mark.parametrize(
    "field,value",
    [
        ("freeze_ts", TS_OTHER),
        ("protocol_fingerprint", FP_OTHER),
        ("experiment_id", "HTF_OTHER"),
        ("claimed_at", "2026-11-02T10:05:00Z"),
        ("operator_approval_ref", "GO-EVIL"),
        ("issued_by", "attacker"),
        ("record_id", "11111111-1111-1111-1111-111111111111"),
    ],
)
def test_record_tamper_is_detected_for_every_field(field, value):
    record = make_record(**{field: value})
    with pytest.raises(FreezeRecordError, match="hash mismatch"):
        validate_freeze_record(record)


def test_record_rejects_missing_fields_and_bad_schema():
    for field in ("schema", "experiment_id", "protocol_fingerprint", "freeze_ts",
                  "claimed_at", "record_id", "operator_approval_ref", "issued_by"):
        with pytest.raises(FreezeRecordError, match="missing field"):
            validate_freeze_record(make_record(**{field: _DROP}))
    with pytest.raises(FreezeRecordError, match="schema"):
        _validate_with_rebuilt_hash("schema", "HTF_PHASE2_FREEZE_RECORD_V0")


def _validate_with_rebuilt_hash(field: str, value) -> None:
    """Rebuild the record with one field changed AND a correct self-hash."""
    record = make_record()
    payload = {k: v for k, v in record.items() if k != "record_sha256"}
    payload[field] = value
    payload["record_sha256"] = freeze_record_hash(payload)
    validate_freeze_record(payload)


@pytest.mark.parametrize(
    "bad_ts",
    [
        "2026-11-02T10:00:00.000Z",  # fractional seconds
        "2026-11-02T10:00:00+00:00",  # non-canonical offset spelling
        "2026-11-02T10:00:00",  # naive
        "2026-11-02 10:00:00Z",  # not ISO-T
        1793623200,  # epoch int is not a canonical record timestamp
    ],
)
def test_record_rejects_noncanonical_timestamps(bad_ts):
    record = make_record()
    payload = {k: v for k, v in record.items() if k != "record_sha256"}
    payload["freeze_ts"] = bad_ts
    payload["claimed_at"] = bad_ts
    payload["record_sha256"] = freeze_record_hash(payload)
    with pytest.raises(FreezeRecordError, match="canonical UTC timestamp"):
        validate_freeze_record(payload)


def test_forbidden_legacy_timestamps_are_structurally_rejected():
    record = make_record()
    payload = {k: v for k, v in record.items() if k != "record_sha256"}
    payload["freeze_ts"] = POINT_B_FREEZE
    payload["claimed_at"] = POINT_B_FREEZE
    payload["record_sha256"] = freeze_record_hash(payload)
    with pytest.raises(FreezeRecordError, match="forbidden legacy boundary"):
        validate_freeze_record(
            payload, forbidden_freeze_ts_ms=frozenset({parse_ts_ms(POINT_B_FREEZE)})
        )
    # and the stale/freshness rules reject it even without the explicit list
    with pytest.raises(FreezeRecordError):
        validate_freeze_record(payload, now_ms=parse_ts_ms(TS))


def test_record_rejects_backdated_and_far_future_relative_to_claim():
    record = make_record()
    payload = {k: v for k, v in record.items() if k != "record_sha256"}
    payload["claimed_at"] = "2026-11-02T11:00:00Z"  # claimed 1h after its freeze
    payload["record_sha256"] = freeze_record_hash(payload)
    with pytest.raises(FreezeRecordError, match="backdated"):
        validate_freeze_record(payload)

    payload = {k: v for k, v in record.items() if k != "record_sha256"}
    payload["freeze_ts"] = "2026-11-02T11:00:00Z"  # 1h after its claim
    payload["record_sha256"] = freeze_record_hash(payload)
    with pytest.raises(FreezeRecordError, match="far future"):
        validate_freeze_record(payload)


def test_freshness_window_boundaries_match_the_postgresql_guard():
    freeze_ms = parse_ts_ms(TS)
    past_ms = ACCEPTANCE_PAST_WINDOW_S * 1000
    future_ms = ACCEPTANCE_FUTURE_WINDOW_S * 1000
    record = make_record()
    # acceptance: now - 60s <= freeze <= now + 300s  <=>  freeze-300s <= now <= freeze+60s
    validate_freeze_record(record, now_ms=freeze_ms + past_ms)  # exact stale edge: OK
    validate_freeze_record(record, now_ms=freeze_ms - future_ms)  # exact future edge: OK
    with pytest.raises(FreezeRecordError, match="acceptance window"):
        validate_freeze_record(record, now_ms=freeze_ms + past_ms + 1)
    with pytest.raises(FreezeRecordError, match="acceptance window"):
        validate_freeze_record(record, now_ms=freeze_ms - future_ms - 1)


def test_record_fingerprint_rules():
    record = make_record()
    with pytest.raises(FreezeRecordError, match="does not match the running code"):
        validate_freeze_record(record, expected_protocol_fingerprint=FP_OTHER)
    with pytest.raises(FreezeRecordError, match="not the pinned FROZEN"):
        validate_freeze_record(record, require_pinned_fingerprint=True)
    pinned = make_sealed_record(protocol_fingerprint=FROZEN_PROTOCOL_FINGERPRINT)
    validate_freeze_record(
        pinned,
        expected_protocol_fingerprint=FROZEN_PROTOCOL_FINGERPRINT,
        require_pinned_fingerprint=True,
    )


def test_load_freeze_record_fails_closed(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(FreezeRecordError, match="unreadable"):
        load_freeze_record(missing)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(FreezeRecordError, match="unreadable"):
        load_freeze_record(bad)
    arr = tmp_path / "arr.json"
    arr.write_text("[]", encoding="utf-8")
    with pytest.raises(FreezeRecordError, match="JSON object"):
        load_freeze_record(arr)


# ============================================= claim receipt / record identity
def test_claim_receipt_is_deterministic_pure_function_of_the_record():
    record = make_sealed_record()
    receipt = build_claim_receipt(record)
    assert receipt["schema"] == CLAIM_RECEIPT_SCHEMA
    assert receipt["record_id"] == record["record_id"]
    assert receipt["record_sha256"] == record["record_sha256"]
    assert receipt["freeze_ts"] == record["freeze_ts"]
    assert receipt["receipt_sha256"] == claim_receipt_hash(receipt)
    assert build_claim_receipt(record) == receipt  # deterministic -> idempotent
    validate_claim_receipt(receipt)

    # a same-second twin (identical boundary, different identity) yields a
    # DIFFERENT receipt and can never be sealed over the original one
    twin = make_sealed_record(record_id="00000000-0000-0000-0000-000000000002")
    assert twin["freeze_ts"] == record["freeze_ts"]
    assert twin["protocol_fingerprint"] == record["protocol_fingerprint"]
    assert twin["record_id"] != record["record_id"]
    assert twin["record_sha256"] != record["record_sha256"]
    assert build_claim_receipt(twin) != receipt


def test_claim_identity_binds_record_id_and_record_sha256():
    record = make_sealed_record()
    receipt = build_claim_receipt(record)
    verify_claim_identity(receipt, record)  # exact same record: admitted

    # same freeze_ts + fingerprint + claimed_at, different record identity —
    # the record self-hash is valid but proves nothing about provenance
    twin = make_sealed_record(record_id="00000000-0000-0000-0000-000000000002")
    assert freeze_record_hash(twin) == twin["record_sha256"]  # self-hash intact
    with pytest.raises(FreezeRecordError, match="record_id mismatch"):
        verify_claim_identity(receipt, twin)

    # a re-sealed FAKE with the claimed record_id but different content is
    # caught by record_sha256 (the receipt binds the full identity)
    fake = make_sealed_record(record_id=record["record_id"], issued_by="someone-else")
    with pytest.raises(FreezeRecordError, match="record_sha256 mismatch"):
        verify_claim_identity(receipt, fake)

    # boundary drift is also refused
    drifted = make_sealed_record(freeze_ts=TS_OTHER)
    with pytest.raises(FreezeRecordError):
        verify_claim_identity(receipt, drifted)


def test_claim_receipt_tamper_and_bad_shape_fail_closed():
    record = make_sealed_record()
    receipt = build_claim_receipt(record)

    tampered = dict(receipt, record_id="00000000-0000-0000-0000-000000000002")
    with pytest.raises(FreezeRecordError, match="hash mismatch"):
        validate_claim_receipt(tampered)

    missing = {k: v for k, v in receipt.items() if k != "record_sha256"}
    with pytest.raises(FreezeRecordError, match="missing field"):
        validate_claim_receipt(missing)

    bad_schema = dict(receipt)
    bad_schema["schema"] = "SOMETHING_ELSE"
    bad_schema["receipt_sha256"] = claim_receipt_hash(bad_schema)
    with pytest.raises(FreezeRecordError, match="schema"):
        validate_claim_receipt(bad_schema)


def test_load_claim_receipt_fails_closed_when_identity_cannot_be_established(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(FreezeRecordError, match="unreadable"):
        load_claim_receipt(missing)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(FreezeRecordError, match="unreadable"):
        load_claim_receipt(bad)
    arr = tmp_path / "arr.json"
    arr.write_text("[]", encoding="utf-8")
    with pytest.raises(FreezeRecordError, match="JSON object"):
        load_claim_receipt(arr)


# ======================================================= three-authority agreement
def _activation(**overrides) -> dict:
    data = {
        "experiment_id": EXPERIMENT_ID,
        "freeze_ts": TS,
        "protocol_fingerprint": FP,
    }
    data.update(overrides)
    return data


def test_agreement_passes_when_all_sources_are_identical():
    agreement = verify_authority_agreement(
        record=make_record(),
        database_freeze_ts=TS,
        database_fingerprint=FP,
        activation=_activation(),
        expected_experiment_id=EXPERIMENT_ID,
        expected_protocol_fingerprint=FP,
    )
    assert agreement["freeze_ts_ms"] == parse_ts_ms(TS)
    assert agreement["protocol_fingerprint"] == FP


def test_agreement_rejects_any_timestamp_disagreement():
    cases = {
        "database": {"database_freeze_ts": TS_OTHER},
        "activation": {"activation": _activation(freeze_ts=TS_OTHER)},
        "record": {"record": make_record(freeze_ts=TS_OTHER)},
    }
    for label, overrides in cases.items():
        kwargs = dict(
            record=make_record(),
            database_freeze_ts=TS,
            database_fingerprint=FP,
            activation=_activation(),
            expected_experiment_id=EXPERIMENT_ID,
            expected_protocol_fingerprint=FP,
        )
        kwargs.update(overrides)
        with pytest.raises(FreezeRecordError):
            verify_authority_agreement(**kwargs)


def test_agreement_rejects_any_fingerprint_disagreement():
    cases = {
        "database": {"database_fingerprint": FP_OTHER},
        "activation": {"activation": _activation(protocol_fingerprint=FP_OTHER)},
        "record": {"record": make_record(protocol_fingerprint=FP_OTHER)},
    }
    for label, overrides in cases.items():
        kwargs = dict(
            record=make_record(),
            database_freeze_ts=TS,
            database_fingerprint=FP,
            activation=_activation(),
            expected_experiment_id=EXPERIMENT_ID,
            expected_protocol_fingerprint=FP,
        )
        kwargs.update(overrides)
        with pytest.raises(FreezeRecordError):
            verify_authority_agreement(**kwargs)


def test_agreement_rejects_missing_sources():
    with pytest.raises(FreezeRecordError, match="missing"):
        verify_authority_agreement(
            record=make_record(),
            database_freeze_ts=None,
            database_fingerprint=FP,
            activation=_activation(),
            expected_experiment_id=EXPERIMENT_ID,
            expected_protocol_fingerprint=FP,
        )
    with pytest.raises(FreezeRecordError, match="missing"):
        verify_authority_agreement(
            record=make_record(),
            database_freeze_ts=TS,
            database_fingerprint=None,
            activation=_activation(),
            expected_experiment_id=EXPERIMENT_ID,
            expected_protocol_fingerprint=FP,
        )
    with pytest.raises(FreezeRecordError, match="activation config is missing"):
        verify_authority_agreement(
            record=make_record(),
            database_freeze_ts=TS,
            database_fingerprint=FP,
            activation=None,
            expected_experiment_id=EXPERIMENT_ID,
            expected_protocol_fingerprint=FP,
        )


# ============================================================ runner startup gate
@pytest.fixture()
def db_stub(monkeypatch):
    """Stub the persistence reads behind require_runnable (no database)."""
    import app.research.htf_baseline_short_phase2_persistence_v1 as persistence

    holder = {
        "state": ExperimentState(
            experiment_id=EXPERIMENT_ID,
            status="RUNNING",
            freeze_ts=TS,
            protocol_fingerprint=FP,
            observer_enabled=False,
        )
    }

    def fake_state(conn, experiment_id=EXPERIMENT_ID):
        return holder["state"]

    def fake_sources(conn, experiment_id=EXPERIMENT_ID):
        state = holder["state"]
        return {
            "database": state.freeze_ts,
            "status": state.status,
            "observer_enabled": state.observer_enabled,
            "database_fingerprint": state.protocol_fingerprint,
        }

    monkeypatch.setattr(persistence, "load_experiment_state", fake_state)
    monkeypatch.setattr(persistence, "freeze_sources", fake_sources)
    return holder


def _gate(**kwargs):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        RunnerConfig,
        require_runnable,
    )

    config = kwargs.pop("config", None) or RunnerConfig(protocol_fingerprint=FP)
    defaults = dict(
        activation=_activation(),
        freeze_record=make_record(),
    )
    defaults.update(kwargs)
    return require_runnable(object(), config, **defaults)


def test_gate_admitted_when_all_three_authorities_agree(db_stub):
    boundary = _gate()
    assert boundary.freeze_ts_ms == parse_ts_ms(TS)
    assert boundary.protocol_fingerprint == FP


def test_scenario_frozen_protocol_db_pending_refuses_startup(db_stub):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import NotRunnableError

    db_stub["state"] = ExperimentState(
        experiment_id=EXPERIMENT_ID,
        status="PENDING",
        freeze_ts=None,
        protocol_fingerprint=None,
        observer_enabled=False,
    )
    with pytest.raises(NotRunnableError):
        _gate()


def test_scenario_missing_activation_config_refuses_startup(db_stub):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import NotRunnableError

    with pytest.raises(NotRunnableError, match="activation"):
        _gate(activation=None)


def test_scenario_missing_freeze_record_refuses_startup(db_stub):
    # protocol authority has no timestamp in Git (approved_freeze_ts() is None):
    # without the operator freeze record there is nothing to run
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import NotRunnableError

    assert approved_freeze_ts() is None
    with pytest.raises(NotRunnableError, match="freeze authority is empty"):
        _gate(freeze_record=None, protocol_freeze_ts=None)


def test_scenario_db_activation_without_protocol_authority_refuses(db_stub):
    # DB activated + activation config present, but no protocol authority at all
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import NotRunnableError

    with pytest.raises(NotRunnableError):
        _gate(freeze_record=None, protocol_freeze_ts=None)
    # and a freeze record is never silently replaced by another authority's value
    with pytest.raises(NotRunnableError):
        _gate(freeze_record=None, protocol_freeze_ts=TS_OTHER)


def test_scenario_timestamp_mismatch_in_one_source_refuses(db_stub):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import NotRunnableError

    with pytest.raises(NotRunnableError):  # activation config drifts
        _gate(activation=_activation(freeze_ts=TS_OTHER))
    with pytest.raises(NotRunnableError):  # database drifts
        _gate_with_state_freeze(db_stub, TS_OTHER)
    other_record = build_freeze_record(
        experiment_id=EXPERIMENT_ID,
        freeze_ts=TS_OTHER,
        protocol_fingerprint=FP,
        claimed_at=TS_OTHER,
        operator_approval_ref="GO-TEST-002",
        issued_by="tester",
        record_id="00000000-0000-0000-0000-000000000002",
    )
    with pytest.raises(NotRunnableError):  # freeze record drifts from the rest
        _gate(freeze_record=other_record)
    with pytest.raises(NotRunnableError):  # explicit protocol ts != record
        _gate(protocol_freeze_ts=TS_OTHER)


def _gate_with_state_freeze(db_stub, freeze_ts):
    db_stub["state"] = ExperimentState(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        freeze_ts=freeze_ts,
        protocol_fingerprint=FP,
        observer_enabled=False,
    )
    return _gate()


def test_scenario_fingerprint_mismatch_refuses(db_stub):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import NotRunnableError

    with pytest.raises(NotRunnableError, match="fingerprint"):
        _gate(activation=_activation(protocol_fingerprint=FP_OTHER))
    with pytest.raises(NotRunnableError, match="fingerprint"):
        _gate(freeze_record=make_sealed_record(protocol_fingerprint=FP_OTHER))
    db_stub["state"] = ExperimentState(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        freeze_ts=TS,
        protocol_fingerprint=FP_OTHER,
        observer_enabled=False,
    )
    with pytest.raises(NotRunnableError, match="fingerprint"):
        _gate()


def test_scenario_tampered_freeze_record_refuses(db_stub):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import NotRunnableError

    tampered = make_record()
    tampered["freeze_ts"] = TS_OTHER  # signature no longer verifies
    with pytest.raises(NotRunnableError, match="freeze record"):
        _gate(freeze_record=tampered)


def test_scenario_stale_freeze_record_refuses(db_stub):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import NotRunnableError

    stale = make_record()
    payload = {k: v for k, v in stale.items() if k != "record_sha256"}
    payload["claimed_at"] = "2026-11-03T10:00:00Z"  # claimed a day after its freeze
    payload["record_sha256"] = freeze_record_hash(payload)
    with pytest.raises(NotRunnableError, match="freeze record"):
        _gate(freeze_record=payload)


def test_scenario_pinned_fingerprint_required_on_production_path(db_stub):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import NotRunnableError

    with pytest.raises(NotRunnableError, match="pinned FROZEN"):
        _gate(require_pinned_fingerprint=True)


# ============================================== freeze boundary event semantics
def _boundary(freeze_ts: str = TS) -> Phase2FreezeBoundary:
    return Phase2FreezeBoundary.create(
        experiment_id=EXPERIMENT_ID,
        protocol_freeze_ts=freeze_ts,
        registry_freeze_ts=freeze_ts,
        database_freeze_ts=freeze_ts,
        protocol_fingerprint=FP,
    )


def test_scenario_pre_freeze_first_touch_post_freeze_confirmation_rejected():
    boundary = _boundary()
    decision = boundary.check_event(
        signal_time="2026-11-02T10:30:00Z",
        first_touch_time="2026-11-02T09:00:00Z",  # BEFORE the freeze
    )
    assert not decision and decision.reason == REASON_FIRST_TOUCH_NOT_AFTER_FREEZE


def test_scenario_pre_freeze_signal_rejected():
    boundary = _boundary()
    decision = boundary.check_signal_time("2026-11-02T10:00:00Z")  # exactly AT freeze: strict >
    assert not decision and decision.reason == REASON_SIGNAL_NOT_AFTER_FREEZE
    assert boundary.check_signal_time("2026-11-02T10:00:01Z")


def test_scenario_warmup_origin_and_resumed_pre_freeze_rejected():
    boundary = _boundary()
    ok = boundary.check_event(
        signal_time="2026-11-02T11:00:00Z", first_touch_time="2026-11-02T10:30:00Z"
    )
    assert ok
    warm = boundary.check_event(
        signal_time="2026-11-02T11:00:00Z",
        first_touch_time="2026-11-02T10:30:00Z",
        warmup_origin=True,
    )
    assert not warm and warm.reason == REASON_WARMUP_ORIGIN_EPISODE
    resumed = boundary.check_event(
        signal_time="2026-11-02T11:00:00Z",
        first_touch_time="2026-11-02T10:30:00Z",
        resumed_pre_freeze=True,
    )
    assert not resumed and resumed.reason == REASON_RESUMED_PRE_FREEZE_EPISODE


def test_scenario_missing_boundary_rejects_everything():
    decision = reject_all()
    assert not decision


def test_scenario_writer_refuses_to_bypass_eligibility():
    writer = Phase2ObservationWriter(conn=None)
    with pytest.raises(ValueError, match="freeze boundary"):
        writer.emit([], None)


# ============================================ checkpoint / restart equivalence
class _DummySource:
    pass


def test_scenario_restart_preserves_freeze_identity_and_checkpoint(tmp_path):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        CheckpointBoundaryError,
        Phase2ObserverRunner,
        RunnerConfig,
    )

    config = RunnerConfig(
        checkpoint_path=str(tmp_path / "state.json"), protocol_fingerprint=FP
    )
    boundary = _boundary()
    first = Phase2ObserverRunner(config, boundary=boundary, source=_DummySource())
    first.save_checkpoint()

    resumed = Phase2ObserverRunner(config, boundary=boundary, source=_DummySource())
    assert resumed.load_checkpoint() is True
    assert resumed.boundary.freeze_ts_ms == boundary.freeze_ts_ms
    assert resumed.boundary.protocol_fingerprint == FP

    other_boundary = _boundary(TS_OTHER)
    conflicting = Phase2ObserverRunner(
        config, boundary=other_boundary, source=_DummySource()
    )
    with pytest.raises(CheckpointBoundaryError):
        conflicting.load_checkpoint()


# ============================================================ CLI fail-closed
def test_scenario_cli_registration_gate_requires_all_config_files(tmp_path):
    # the CLI refuses before any DB connection when the activation config is
    # missing/unreadable; the freeze-record requirement itself is enforced by
    # require_runnable (see test_scenario_missing_freeze_record_refuses_startup)
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        EXIT_NOT_RUNNABLE,
        main,
    )

    data_dir = tmp_path / "candles"
    data_dir.mkdir()
    base = ["--use-registration", "--data-dir", str(data_dir)]
    assert main(base) == EXIT_NOT_RUNNABLE  # no --activation-config
    assert main(base + ["--activation-config", str(tmp_path / "a.json")]) == (
        EXIT_NOT_RUNNABLE
    )  # unreadable activation config
    # unreadable activation config AND unreadable freeze record also fail closed
    assert main(
        base
        + [
            "--activation-config", str(tmp_path / "a.json"),
            "--freeze-record", str(tmp_path / "r.json"),
        ]
    ) == EXIT_NOT_RUNNABLE


def test_scenario_cli_without_any_gate_is_not_runnable(tmp_path):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        EXIT_NOT_RUNNABLE,
        main,
    )

    data_dir = tmp_path / "candles"
    data_dir.mkdir()
    assert main(["--data-dir", str(data_dir)]) == EXIT_NOT_RUNNABLE
