"""Issue #298 — lineage immutability, idempotency, repeatability, fingerprint.

Offline: no DB, no network, no filesystem, no clock.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_entry_quality_h1c_v1.event_adapter import (
    REASON_DUPLICATE_SOURCE_EVENT,
    adapt_source_events,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_OFFLINE_SNAPSHOT,
)
from app.research.me_entry_quality_h2_v1.candle_provider import (
    CANDLE_5M_MS,
    build_source_identity,
)
from app.research.me_entry_quality_h2_v1.pipeline import run_h2_event
from app.research.me_entry_quality_h2_v1.result import (
    H2_POLICY_STATUS,
    H2_RSI_CALC_VERSION,
    H2Policy,
    H2VariantResult,
    STATE_CONFIRMED_DELAYED,
    VARIANT_PEAK_REVERSAL,
    VARIANT_TURN_1,
    VARIANT_TURN_2,
)

BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MS = CANDLE_5M_MS
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

CRAFTED = (
    [100.0 + i for i in range(17)]
    + [118.0, 114.0, 116.5, 113.0, 111.5, 115.0]
)
SIGNAL_US = (BASE_MS + 18 * MS + 60_000) * 1000


def rows_from_closes(closes, start_index=0):
    rows = []
    for i, close in enumerate(closes):
        open_ms = BASE_MS + (start_index + i) * MS
        prev = rows[-1]["close"] if rows else float(close)
        rows.append({
            "open_ms": open_ms,
            "open": prev,
            "high": max(prev, float(close)) + 0.5,
            "low": min(prev, float(close)) - 0.5,
            "close": float(close),
            "volume": 1.0,
        })
    return rows


def event_row(signal_us, *, key="evt-lineage"):
    signal = EPOCH + timedelta(microseconds=signal_us)
    return {
        "source_event_key": key,
        "source_identity": (
            "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|BTCUSDT|LONG|5m|"
            f"{signal_us // 1000 // MS * MS}"
        ),
        "symbol": "BTCUSDT",
        "lineage_hash": "ef" * 32,
        "signal_time": signal,
        "captured_at": signal + timedelta(seconds=20),
        "activation_freeze_ts": EPOCH + timedelta(milliseconds=BASE_MS),
        "source_fingerprint": "fp-lineage-1",
        "source_reference_price": 2222.0,
        "features": {
            "rsi_14": 71.25,
            "rsi_delta_3": 4.5,
            "entry_confirmation_rsi_rising": True,
        },
    }


def run_all(signal_us=SIGNAL_US, asof_ms=BASE_MS + 22 * MS, key="evt-lineage"):
    rows = rows_from_closes(CRAFTED)
    identity = build_source_identity(
        SOURCE_KIND_OFFLINE_SNAPSHOT, "lineage-test", rows
    )
    policies = [
        H2Policy(VARIANT_TURN_1, observation_window=3),
        H2Policy(VARIANT_TURN_2, observation_window=6),
        H2Policy(VARIANT_PEAK_REVERSAL, observation_window=6,
                 peak_rule="CAUSAL_STRICT_LOCAL_MAX_LAG1_CONFIRM_FIRST_DECLINE_V1"),
    ]
    return run_h2_event(
        event_row(signal_us, key=key),
        rows_5m=rows,
        source_identity=identity,
        policies=policies,
        evaluation_asof_ms=asof_ms,
    )


class TestLineageImmutability:
    def test_lineage_payload_is_exactly_the_preserved_fields(self):
        outcome = run_all()
        for result in outcome.results:
            payload = result.lineage_payload()
            assert set(payload) == {
                "source_event_key",
                "source_identity",
                "signal_time_ms",
                "signal_time_us",
                "activation_freeze_ts_ms",
                "lineage_hash",
            }
            assert payload["source_event_key"] == "evt-lineage"
            assert payload["lineage_hash"] == "ef" * 32
            assert payload["signal_time_us"] == SIGNAL_US
            assert payload["activation_freeze_ts_ms"] == BASE_MS

    def test_microsecond_precision_and_ms_floor_policy(self):
        signal_us = (BASE_MS + 19 * MS) * 1000 - 1  # x.999999 second
        outcome = run_all(signal_us=signal_us)
        result = outcome.results[0]
        assert result.signal_time_us == signal_us
        assert result.signal_time_ms == signal_us // 1000
        assert result.signal_time_ms * 1000 != signal_us  # floor, not round

    def test_result_is_frozen(self):
        result = run_all().results[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = "TAMPERED"

    def test_original_price_never_becomes_execution_price(self):
        for result in run_all().results:
            assert result.original_price == 2222.0
            if result.status == STATE_CONFIRMED_DELAYED:
                assert result.entry_candidate_price is None
                assert result.entry_candidate_time_ms > result.signal_time_ms


class TestIdempotencyAndRepeatability:
    def test_rerun_is_bit_identical(self):
        first = run_all().results
        second = run_all().results
        assert [r.to_dict() for r in first] == [r.to_dict() for r in second]

    def test_redelivery_of_same_event_key_is_rejected_by_adapter(self):
        row = event_row(SIGNAL_US)
        batch = adapt_source_events([row, row])  # one batch, two deliveries
        assert len(batch.records) == 1
        assert batch.rejections[0].reason == REASON_DUPLICATE_SOURCE_EVENT
        assert batch.rejections[0].source_event_key == "evt-lineage"

    def test_redelivered_event_key_produces_identical_results(self):
        a = run_all(key="evt-dup")
        b = run_all(key="evt-dup")
        assert [r.to_dict() for r in a.results] == [r.to_dict() for r in b.results]

    def test_distinct_signal_candle_is_a_distinct_event(self):
        rows = rows_from_closes(CRAFTED)
        identity = build_source_identity(
            SOURCE_KIND_OFFLINE_SNAPSHOT, "lineage-test", rows
        )
        policy = H2Policy(VARIANT_TURN_1, observation_window=3)
        outcome = run_h2_event(
            event_row(SIGNAL_US + MS * 1000, key="evt-other"),
            rows_5m=rows,
            source_identity=identity,
            policies=[policy],
            evaluation_asof_ms=BASE_MS + 22 * MS,
        )
        assert outcome.results[0].source_event_key == "evt-other"


class TestPolicyFingerprint:
    def test_fingerprint_is_canonical_sha256(self):
        policy = H2Policy(VARIANT_TURN_2, observation_window=6)
        payload = json.dumps(
            policy.to_dict(), sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert policy.fingerprint() == expected

    def test_fingerprint_changes_with_every_parameter(self):
        base = H2Policy(VARIANT_TURN_1, observation_window=3)
        assert base.fingerprint() != H2Policy(
            VARIANT_TURN_1, observation_window=6
        ).fingerprint()
        assert base.fingerprint() != H2Policy(
            VARIANT_TURN_2, observation_window=3
        ).fingerprint()
        peak = H2Policy(VARIANT_PEAK_REVERSAL, observation_window=3,
                        peak_rule="CAUSAL_STRICT_LOCAL_MAX_LAG1_CONFIRM_FIRST_DECLINE_V1")
        assert peak.fingerprint() != H2Policy(
            VARIANT_PEAK_REVERSAL, observation_window=3
        ).fingerprint()

    def test_fingerprint_includes_calc_version_and_unfrozen_status(self):
        policy = H2Policy(VARIANT_TURN_1, observation_window=3)
        assert policy.to_dict()["rsi_calc_version"] == H2_RSI_CALC_VERSION
        assert policy.to_dict()["policy_status"] == H2_POLICY_STATUS

    def test_results_carry_the_policy_fingerprint(self):
        for result in run_all().results:
            assert result.policy_fingerprint == result.policy.fingerprint()

    def test_result_classifies_variants_separately(self):
        results = run_all().results
        assert [r.variant_id for r in results] == [
            VARIANT_TURN_1,
            VARIANT_TURN_2,
            VARIANT_PEAK_REVERSAL,
        ]
        assert all(isinstance(r, H2VariantResult) for r in results)
