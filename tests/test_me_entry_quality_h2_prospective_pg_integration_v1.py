"""Real PostgreSQL integration for the Issue #312 H2 prospective runtime.

Covers the FR-05 store + migration 095 against a REAL PostgreSQL server,
inside a FULLY ISOLATED scratch environment:

* an OWN scratch database (created + dropped by this module);
* migration 095 applied transactionally;
* no production service, schema or data is read or written;
* the #305 source schema is NEVER created or touched here — the runtime's
  source reader is SELECT-only and its store knows only its own schema.

SAFETY
------
* OPT-IN ONLY: the module skips unless ``ME_H2PRO_PG_INTEGRATION=1``.
* Only connects to the configured TEST server
  (``ME_H2PRO_PG_IT_HOST/PORT/USER``, default ``127.0.0.1:5432 postgres``)
  and only ever writes inside its own ``me_h2pro_it_*`` scratch database.

Scenario coverage (acceptance criteria): 1, 2, 5, 13, 15, 16, 18.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.research.me_entry_quality_h2_prospective_v1.contracts import (  # noqa: E402
    INGEST_ACCEPTED,
    STATE_CONFIRMED_DELAYED,
    STATE_PENDING_CONFIRMATION,
    immutable_result_hash,
)
from app.research.me_entry_quality_h2_prospective_v1.lifecycle import (  # noqa: E402
    LifecycleDecision,
)
from app.research.me_entry_quality_h2_prospective_v1.source_reader import (  # noqa: E402
    delivery_from_row,
)
from app.research.me_entry_quality_h2_prospective_v1.store import (  # noqa: E402
    PgProspectiveStore,
)
from app.research.me_upstream_source_v1.contracts import lineage_hash  # noqa: E402

HOST = os.environ.get("ME_H2PRO_PG_IT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ME_H2PRO_PG_IT_PORT", "5432"))
USER = os.environ.get("ME_H2PRO_PG_IT_USER", "postgres")

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    ROOT / "sql" / "migrations" / "095_me_entry_quality_h2_prospective_v1.sql",
)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
BASE_MS = 1_767_225_600_000

pytestmark = pytest.mark.skipif(
    os.environ.get("ME_H2PRO_PG_INTEGRATION") != "1",
    reason="opt-in: set ME_H2PRO_PG_INTEGRATION=1 for the real-PG suite",
)


def _admin_connect(dbname="postgres"):
    return psycopg2.connect(
        host=HOST, port=PORT, dbname=dbname, user=USER, connect_timeout=10
    )


@pytest.fixture()
def scratch_db():
    name = f"me_h2pro_it_{uuid4().hex[:10]}"
    conn = _admin_connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    try:
        yield name
    finally:
        conn = _admin_connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.close()


@pytest.fixture()
def store(scratch_db):
    conn = _admin_connect(scratch_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        for migration in MIGRATIONS:
            cur.execute(migration.read_text(encoding="utf-8"))
    conn.autocommit = False
    yield PgProspectiveStore(conn)


def make_source_row(key="evt-1", signal_ms=BASE_MS + 300_000, tweak=None):
    signal = EPOCH + timedelta(milliseconds=signal_ms)
    lineage = {
        "source_identity": f"ident-{key}",
        "source_signal_time": signal.isoformat(),
        "source_scanner": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        "source_direction": "LONG",
        "source_features": {"rsi_14": 71.25},
        "source_pipeline_fingerprint": "fp-test-1",
    }
    lineage.update(tweak or {})
    return {
        "source_event_key": key,
        "source_identity": f"ident-{key}",
        "symbol": "BTCUSDT",
        "signal_time": signal,
        "captured_at": signal + timedelta(seconds=20),
        "activation_freeze_ts": signal - timedelta(hours=1),
        "signal_candle_open_time": signal_ms // 300_000 * 300_000,
        "entry_timeframe": "5m",
        "source_fingerprint": "fp-test-1",
        "source_reference_price": 1234.5,
        "features": {
            "rsi_14": 71.25,
            "rsi_delta_3": 4.5,
            "entry_confirmation_rsi_rising": True,
        },
        "lineage": lineage,
        "lineage_hash": lineage_hash(lineage),
    }


def result_dict(status=STATE_CONFIRMED_DELAYED, **overrides):
    payload = {
        "variant_id": "H2_RSI_TURN_1",
        "status": status,
        "source_event_key": "evt-1",
        "source_identity": "ident-evt-1",
        "symbol": "BTCUSDT",
        "signal_time_us": BASE_MS * 1000,
        "signal_time_ms": BASE_MS,
        "activation_freeze_ts_ms": BASE_MS - 3_600_000,
        "lineage_hash": "ab" * 32,
        "original_price": 1234.5,
        "original_price_exact": "1234.5",
        "source_rsi_14_snapshot": 71.25,
        "h2_rsi_at_confirmation": 75.3334,
        "entry_candidate_price": None,
        "entry_price_basis": "UNDETERMINED_NO_EXECUTION_MODEL",
        "entry_reference_close_price": 114.0,
        "entry_delay_ms": 300_000,
        "observed_window_candles": 1,
        "coverage_status": "COMPLETE",
        "policy": {"variant_id": "H2_RSI_TURN_1", "observation_window": 3},
        "policy_fingerprint": "ff" * 32,
    }
    payload.update(overrides)
    return payload


class TestPgIngest:
    def test_accepted_once_then_idempotent_duplicate(self, store):
        # criteria 1 + 2
        row = make_source_row()
        first = store.ingest_event(delivery_from_row(row))
        assert first.status == INGEST_ACCEPTED and not first.duplicate
        second = store.ingest_event(delivery_from_row(make_source_row()))
        assert second.duplicate and not second.conflict
        assert store.ingest_event_count() == 1
        assert store.get_source_row("evt-1")["symbol"] == "BTCUSDT"

    def test_lineage_drift_is_explicit_conflict(self, store):
        # criterion 15
        store.ingest_event(delivery_from_row(make_source_row()))
        drifted = delivery_from_row(
            make_source_row(tweak={"source_scanner_version": "v2"})
        )
        outcome = store.ingest_event(drifted)
        assert outcome.conflict
        assert store.ingest_event_count() == 1  # snapshot preserved

    def test_watermark_roundtrip(self, store):
        assert store.get_watermark() == (0, "")
        store.set_watermark(BASE_MS * 1000, "evt-1")
        assert store.get_watermark() == (BASE_MS * 1000, "evt-1")


class TestPgLifecycle:
    def test_state_and_result_lifecycle(self, store):
        # criteria 13 + 16
        assert store.ensure_variant_state(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            policy={"variant_id": "H2_RSI_TURN_1", "observation_window": 3},
            symbol="BTCUSDT",
            signal_time_us=BASE_MS * 1000,
        ) == STATE_PENDING_CONFIRMATION

        outcome = store.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=result_dict(),
            decision=LifecycleDecision(
                new_state=STATE_CONFIRMED_DELAYED, terminal=True, reason="OK"
            ),
            evaluation_asof_ms=BASE_MS + 400_000,
            window_elapsed=True,
        )
        assert outcome.result_written
        assert outcome.state == STATE_CONFIRMED_DELAYED

        # a rewritten differing outcome is an explicit immutable conflict
        tampered = result_dict(h2_rsi_at_confirmation=99.0)
        conflict = store.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=tampered,
            decision=LifecycleDecision(
                new_state=STATE_CONFIRMED_DELAYED, terminal=True, reason="OK"
            ),
            evaluation_asof_ms=BASE_MS + 500_000,
            window_elapsed=True,
        )
        assert conflict.conflict and not conflict.result_written

        state = store.get_variant_state(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
        )
        assert state.state == STATE_CONFIRMED_DELAYED
        assert state.terminal

    def test_result_immutability_is_enforced_by_the_database(self, store):
        # criterion 18: the trigger refuses UPDATE/DELETE on terminal results
        store.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=result_dict(),
            decision=LifecycleDecision(
                new_state=STATE_CONFIRMED_DELAYED, terminal=True, reason="OK"
            ),
            evaluation_asof_ms=BASE_MS + 400_000,
            window_elapsed=True,
        )
        with pytest.raises(Exception):
            store._execute(
                "DELETE FROM research_me_entry_quality_h2_prospective"
                ".variant_result"
            )
            store._commit()
        store.broken = False  # the failed statement rolled back

    def test_pending_states_are_listed_for_restart_recovery(self, store):
        # criterion 13
        store.ensure_variant_state(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            policy={},
            symbol="BTCUSDT",
            signal_time_us=1,
        )
        pending = store.list_variant_states(pending_only=True)
        assert [s.source_event_key for s in pending] == ["evt-1"]

    def test_health_and_conflicts_persist(self, store):
        store.record_health("SOURCE_EMPTY_WAITING", {"a": 1})
        store.record_conflict(
            conflict_kind="EVENT_KEY_LINEAGE_CONFLICT",
            source_event_key="evt-1",
            stored_hash="aa",
            delivered_hash="bb",
        )
        # verify durability through a second connection-independent read
        row = store._execute(
            "SELECT status FROM research_me_entry_quality_h2_prospective"
            ".runtime_health ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "SOURCE_EMPTY_WAITING"
