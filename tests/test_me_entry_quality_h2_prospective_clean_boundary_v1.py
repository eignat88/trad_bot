"""PR #314: clean H2 prospective cohort (offline, no DB/API)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
    BybitClosedCandleProvider, CandleApiBudget,
)
from app.research.me_entry_quality_h2_prospective_v1.clean_boundary import (
    activation_timestamp_us, eligible_for_clean_holdout,
)
from app.research.me_entry_quality_h2_prospective_v1.config import (
    H2ProspectiveConfig, H2ProspectiveConfigError, load_config,
)
from app.research.me_entry_quality_h2_prospective_v1.runtime import H2ProspectiveRuntime
from app.research.me_entry_quality_h2_prospective_v1.store import (
    InMemoryProspectiveStore,
)
from app.research.me_entry_quality_h2_prospective_v1.telemetry import TelemetryCounters
from app.research.me_entry_quality_h2_v1.result import H2Policy, VARIANT_TURN_1
from app.research.me_upstream_source_v1.contracts import lineage_hash


START = datetime(2026, 11, 26, 10, 0, 0, 123456, tzinfo=timezone.utc)
POLICY = H2Policy(variant_id=VARIANT_TURN_1, observation_window=3)


def source_row(key, signal, capture):
    lineage = {
        "source_identity": f"ident-{key}",
        "source_signal_time": signal.isoformat(),
        "source_signal_candle_open_time": (int(signal.timestamp() * 1000) // 300000) * 300000,
        "source_scanner": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        "source_direction": "LONG",
        "source_features": {"rsi_14": 71.25},
        "source_pipeline_fingerprint": "fp-test-1",
    }
    return {
        "source_event_key": key,
        "source_identity": f"ident-{key}",
        "symbol": "BTCUSDT",
        "signal_time": signal,
        "captured_at": capture,
        "activation_freeze_ts": START - timedelta(days=2),
        "signal_candle_open_time": (int(signal.timestamp() * 1000) // 300000) * 300000,
        "entry_timeframe": "5m",
        "source_fingerprint": "fp-test-1",
        "source_reference_price": 1234.5,
        "features": {"rsi_14": 71.25},
        "lineage": lineage,
        "lineage_hash": lineage_hash(lineage),
    }


def new_runtime(store, ts=START, policy=POLICY):
    config = H2ProspectiveConfig(
        enabled=True,
        clean_activation_ts=ts.isoformat(),
        policy_fingerprint_pins=(policy.fingerprint(),),
        candle_budget_confirmed=False,
    )
    return H2ProspectiveRuntime(
        config=config,
        store=store,
        candle_provider=BybitClosedCandleProvider(
            budget=CandleApiBudget(), enabled=False
        ),
        policies=[policy],
        source_connection_factory=None,
    )


def test_exact_utc_microseconds_and_fail_closed_parsing():
    assert activation_timestamp_us("2026-11-26T10:00:00.123456Z") == (
        activation_timestamp_us(START.isoformat())
    )
    assert activation_timestamp_us("2026-11-26T11:00:00.123456+01:00") == (
        activation_timestamp_us(START.isoformat())
    )
    for bad in ("", "2026-11-26T10:00:00", "not a date"):
        with pytest.raises(ValueError):
            activation_timestamp_us(bad)


def test_enabled_config_requires_explicit_clean_start():
    with pytest.raises(H2ProspectiveConfigError, match="CLEAN_ACTIVATION"):
        load_config({
            "H2_PROSPECTIVE_ENABLED": "1",
            "H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES": "1",
        })
    assert not load_config({"H2_PROSPECTIVE_ENABLED": "0"}).enabled


def test_pre_activation_rows_never_enter_ingest_or_outcomes():
    store = InMemoryProspectiveStore()
    runtime = new_runtime(store)
    counters = TelemetryCounters()
    before = source_row(
        "pre", START - timedelta(microseconds=1), START + timedelta(seconds=1)
    )
    equal = source_row("equal", START, START + timedelta(seconds=1))
    late_capture = source_row(
        "captured-before", START + timedelta(seconds=1),
        START - timedelta(microseconds=1),
    )
    assert not eligible_for_clean_holdout(before, activation_timestamp_us(START.isoformat()))
    for row in (before, equal, late_capture):
        runtime._ingest_row(row, counters)
    assert store.ingest_event_count() == 0
    assert not store.list_variant_states(pending_only=True, limit=100)
    assert counters.source_events_pre_freeze == 3

    valid = source_row(
        "post", START + timedelta(microseconds=1), START + timedelta(seconds=2)
    )
    runtime._ingest_row(valid, counters)
    assert store.ingest_event_count() == 1
    assert len(store.list_variant_states(pending_only=True, limit=100)) == 1
    runtime._ingest_row(valid, counters)
    assert store.ingest_event_count() == 1


def test_restart_rejects_boundary_or_policy_replacement():
    store = InMemoryProspectiveStore()
    new_runtime(store)
    new_runtime(store)  # restart unchanged is safe
    with pytest.raises(ValueError, match="IMMUTABLE_MISMATCH"):
        new_runtime(store, ts=START + timedelta(seconds=1))
    with pytest.raises(ValueError, match="IMMUTABLE_MISMATCH"):
        new_runtime(store, policy=H2Policy(
            variant_id=VARIANT_TURN_1, observation_window=6
        ))


def test_existing_watermark_must_not_be_rebranded_clean():
    store = InMemoryProspectiveStore()
    store.set_cursor(activation_timestamp_us(START.isoformat()), "old-key")
    with pytest.raises(ValueError, match="REQUIRES_EMPTY_STORE"):
        new_runtime(store)
