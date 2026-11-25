"""Issue #312 — H2 prospective runtime: result store tests (FR-05).

Covers the persistence invariants of the research-only store:

* idempotent ingest with a preserved first-seen snapshot;
* UNIQUE (event, variant, policy) terminal outcomes — an identical rewrite
  is a no-op, a differing rewrite is an explicit ``IMMUTABLE_RESULT_CONFLICT``
  and never replaces the stored outcome (criterion 2 / 15 semantics);
* terminal states are never recreated or re-decided;
* explicit integrity-conflict ledger;
* watermark checkpointing (restart safety, criterion 13 support);
* schema isolation: the store writes ONLY inside
  ``research_me_entry_quality_h2_prospective.*`` (criterion 18).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.research.me_entry_quality_h2_prospective_v1 import store as store_mod
from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    INGEST_ACCEPTED,
    STATE_CONFIRMED_DELAYED,
    STATE_PENDING_CONFIRMATION,
    immutable_result_hash,
)
from app.research.me_entry_quality_h2_prospective_v1.lifecycle import (
    LifecycleDecision,
)
from app.research.me_entry_quality_h2_prospective_v1.source_reader import (
    delivery_from_row,
)
from app.research.me_entry_quality_h2_prospective_v1.store import (
    InMemoryProspectiveStore,
)
from app.research.me_upstream_source_v1.contracts import lineage_hash

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
BASE_MS = 1_767_225_600_000


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


def decision(state=STATE_CONFIRMED_DELAYED, terminal=True):
    return LifecycleDecision(new_state=state, terminal=terminal, reason="OK")


class TestIngestIdempotency:
    def test_first_seen_snapshot_is_immutable(self):
        store = InMemoryProspectiveStore()
        row = make_source_row()
        store.ingest_event(delivery_from_row(row))
        first = store.get_source_row("evt-1")
        store.ingest_event(delivery_from_row(make_source_row()))  # redelivery
        assert store.get_source_row("evt-1") == first
        assert store.ingest_event_count() == 1


class TestResultImmutability:
    def test_result_written_once_then_idempotent_noop(self):
        store = InMemoryProspectiveStore()
        payload = result_dict()
        first = store.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=payload,
            decision=decision(),
            evaluation_asof_ms=BASE_MS + 400_000,
            window_elapsed=True,
        )
        assert first.result_written and first.state == STATE_CONFIRMED_DELAYED
        second = store.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=result_dict(),
            decision=decision(),
            evaluation_asof_ms=BASE_MS + 500_000,
            window_elapsed=True,
        )
        assert not second.result_written and not second.conflict
        assert len(store.results) == 1

    def test_differing_rewrite_is_explicit_conflict_never_overwrite(self):
        store = InMemoryProspectiveStore()
        payload = result_dict()
        store.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=payload,
            decision=decision(),
            evaluation_asof_ms=BASE_MS + 400_000,
            window_elapsed=True,
        )
        tampered = result_dict(h2_rsi_at_confirmation=99.0)
        outcome = store.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=tampered,
            decision=decision(),
            evaluation_asof_ms=BASE_MS + 500_000,
            window_elapsed=True,
        )
        assert outcome.conflict and not outcome.result_written
        stored = store.results[("evt-1", "H2_RSI_TURN_1", "ff" * 32)]
        assert stored["result_payload"]["h2_rsi_at_confirmation"] == 75.3334
        assert store.conflicts[0]["conflict_kind"] == "IMMUTABLE_RESULT_CONFLICT"
        assert store.conflicts[0]["delivered_hash"] == immutable_result_hash(tampered)

    def test_terminal_state_is_never_re_decided(self):
        store = InMemoryProspectiveStore()
        payload = result_dict()
        store.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=payload,
            decision=decision(),
            evaluation_asof_ms=BASE_MS + 400_000,
            window_elapsed=True,
        )
        # a later evaluation attempt (e.g. after a redelivery) is a no-op
        outcome = store.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=result_dict(status="EXPIRED"),
            decision=decision(state="EXPIRED"),
            evaluation_asof_ms=BASE_MS + 900_000,
            window_elapsed=True,
        )
        assert outcome.state == STATE_CONFIRMED_DELAYED
        assert not outcome.result_written
        assert len(store.results) == 1


class TestVariantState:
    def test_ensure_variant_state_is_idempotent(self):
        store = InMemoryProspectiveStore()
        for _ in range(3):
            state = store.ensure_variant_state(
                source_event_key="evt-1",
                variant_id="H2_RSI_TURN_1",
                policy_fingerprint="ff" * 32,
                policy={"variant_id": "H2_RSI_TURN_1"},
                symbol="BTCUSDT",
                signal_time_us=BASE_MS * 1000,
            )
            assert state == STATE_PENDING_CONFIRMATION
        assert len(store.list_variant_states()) == 1

    def test_pending_states_are_listed_terminal_states_are_not(self):
        store = InMemoryProspectiveStore()
        store.ensure_variant_state(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            policy={},
            symbol="BTCUSDT",
            signal_time_us=1,
        )
        store.ensure_variant_state(
            source_event_key="evt-2",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            policy={},
            symbol="BTCUSDT",
            signal_time_us=2,
        )
        store.apply_evaluation(
            source_event_key="evt-2",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=result_dict(source_event_key="evt-2"),
            decision=decision(),
            evaluation_asof_ms=3,
            window_elapsed=True,
        )
        pending = store.list_variant_states(pending_only=True)
        assert [s.source_event_key for s in pending] == ["evt-1"]


class TestWatermark:
    def test_watermark_roundtrip(self):
        store = InMemoryProspectiveStore()
        assert store.get_watermark() == (0, "")
        store.set_watermark(BASE_MS * 1000, "evt-9")
        assert store.get_watermark() == (BASE_MS * 1000, "evt-9")


class TestHealthAndConflicts:
    def test_health_rows_are_recorded(self):
        store = InMemoryProspectiveStore()
        store.record_health("RUNNING", {"a": 1}, {"b": 2})
        assert store.health_rows[-1]["status"] == "RUNNING"
        assert store.health_rows[-1]["counters"] == {"a": 1}

    def test_lineage_conflict_ledger(self):
        store = InMemoryProspectiveStore()
        store.record_conflict(
            conflict_kind="EVENT_KEY_LINEAGE_CONFLICT",
            source_event_key="evt-1",
            stored_hash="aa",
            delivered_hash="bb",
        )
        assert store.conflicts[0]["stored_hash"] == "aa"


class TestSchemaIsolation:
    def test_store_writes_only_its_own_schema(self):
        # criterion 18: no statement may target anything outside the
        # dedicated research schema
        own = "research_me_entry_quality_h2_prospective."
        for name, value in vars(store_mod).items():
            if name.endswith("_TABLE"):
                assert value.startswith(own), f"{name} -> {value}"

    def test_no_write_statement_targets_the_305_schema(self):
        import inspect

        source = inspect.getsource(store_mod)
        for line in source.splitlines():
            stripped = line.strip().upper()
            if stripped.startswith("INSERT") or stripped.startswith("UPDATE") \
                    or stripped.startswith("DELETE"):
                assert "RESEARCH_ME_UPSTREAM_COVERAGE" not in stripped


class _RecordingCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        # driver-level contract: every %s placeholder must be bound
        placeholders = sql.count("%s")
        assert placeholders == len(params), (
            f"placeholder mismatch ({placeholders} vs {len(params)}): "
            f"{sql[:80]!r}"
        )
        head = sql.strip().split(None, 1)[0].upper()
        if head in ("INSERT", "UPDATE", "DELETE"):
            # writes may only touch the dedicated research schema
            assert "research_me_entry_quality_h2_prospective." in sql, sql[:120]
            assert "research_me_upstream_coverage." not in sql
        self._conn.statements.append((head, sql.strip()))

    def fetchone(self):
        return self._conn.fetchone_rows.pop(0) if self._conn.fetchone_rows else None

    def fetchall(self):
        return self._conn.fetchall_rows.pop(0) if self._conn.fetchall_rows else []


class _RecordingConnection:
    def __init__(self):
        self.statements = []
        self.fetchone_rows = []
        self.fetchall_rows = []
        self.commits = 0

    def cursor(self):
        return _RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        return None

    def close(self):
        return None


class TestPgSqlContract:
    """PG-store SQL contract without a server: bound parameters, schema
    isolation and statement shape for every public method."""

    def _store(self):
        conn = _RecordingConnection()
        return store_mod.PgProspectiveStore(conn), conn

    def test_ingest_insert_binds_all_parameters(self):
        pg, conn = self._store()
        conn.fetchone_rows.append(None)  # no existing row
        outcome = pg.ingest_event(delivery_from_row(make_source_row()))
        assert outcome.status == INGEST_ACCEPTED
        heads = [h for h, _ in conn.statements]
        assert heads == ["SELECT", "INSERT"]

    def test_ingest_duplicate_path_updates_redelivery_count(self):
        pg, conn = self._store()
        row = make_source_row()
        delivery = delivery_from_row(row)
        conn.fetchone_rows.append((
            INGEST_ACCEPTED, "OK", delivery.source_identity,
            delivery.lineage_hash, delivery.signal_time_us,
            delivery.immutable_hash,
        ))
        outcome = pg.ingest_event(delivery)
        assert outcome.duplicate
        heads = [h for h, _ in conn.statements]
        assert heads == ["SELECT", "UPDATE"]

    def test_watermark_roundtrip_statements(self):
        pg, conn = self._store()
        conn.fetchone_rows.append((123, "evt-1"))
        assert pg.get_watermark() == (123, "evt-1")
        pg.set_watermark(456, "evt-2")
        heads = [h for h, _ in conn.statements]
        assert heads == ["SELECT", "INSERT"]

    def test_apply_evaluation_statements_are_parameterized(self):
        pg, conn = self._store()
        # ensure_variant_state: INSERT ... RETURNING -> inserted
        conn.fetchone_rows.append(("PENDING_CONFIRMATION",))
        # terminal result lookup -> none yet
        conn.fetchone_rows.append(None)
        payload = result_dict()
        outcome = pg.apply_evaluation(
            source_event_key="evt-1",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            result_dict=payload,
            decision=decision(),
            evaluation_asof_ms=BASE_MS + 400_000,
            window_elapsed=True,
        )
        assert outcome.result_written
        heads = [h for h, _ in conn.statements]
        assert "INSERT" in heads and "UPDATE" in heads

    def test_list_variant_states_and_health(self):
        pg, conn = self._store()
        conn.fetchall_rows.append([])
        assert pg.list_variant_states() == []
        pg.record_health("RUNNING", {"a": 1})
        pg.record_conflict(
            conflict_kind="EVENT_KEY_LINEAGE_CONFLICT",
            source_event_key="evt-1",
        )
        heads = [h for h, _ in conn.statements]
        assert heads == ["SELECT", "INSERT", "INSERT"]
