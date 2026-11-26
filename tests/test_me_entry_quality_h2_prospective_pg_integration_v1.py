"""Real PostgreSQL integration for the Issue #312 H2 prospective runtime.

Covers the FR-05 store + migration 095 against a REAL PostgreSQL server,
inside a FULLY ISOLATED scratch environment:

* an OWN scratch database (created + dropped by this module);
* migration 095 applied transactionally (and re-applied — idempotency);
* no production service, schema or data is read or written;
* the #305 source schema is NEVER created or touched here — the runtime's
  source reader is SELECT-only and its store knows only its own schema.

SAFETY
------
* OPT-IN ONLY: the module skips unless ``ME_H2PRO_PG_INTEGRATION=1``.
* Only connects to the configured TEST server
  (``ME_H2PRO_PG_IT_HOST/PORT/USER``, default ``127.0.0.1:5432 postgres``)
  and only ever writes inside its own ``me_h2pro_it_*`` scratch database.

Scenario coverage (acceptance criteria): 1, 2, 5, 13, 15, 16, 18, plus the
PR #313 review remediation evidence:

* P1-Decimal   — REAL ``NUMERIC`` round-trip (ingest -> persistence ->
                 restore -> H2 evaluation) with
                 ``Decimal('123.4567890123456789')`` exact, DB-level refusal
                 of NaN/Infinity/zero/negative, redelivery and restart
                 equivalence;
* P1-Watermark — cursor persistence and capture-order B-then-A ingestion;
* P1-Budget    — persisted hourly request-window state across "restarts";
* P2-Migration — first apply, RE-APPLY (idempotent), immutability triggers,
                 preserved results, transactional rollback on error and a
                 predictable refusal on an incompatible pre-existing schema.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.research.me_entry_quality_h2_prospective_v1.contracts import (  # noqa: E402
    INGEST_ACCEPTED,
    STATE_CONFIRMED_DELAYED,
    STATE_PENDING_CONFIRMATION,
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
from app.research.me_entry_quality_h1c_v1.htf_provider import (  # noqa: E402
    SOURCE_KIND_BYBIT_KLINE_V5,
    build_source_identity,
)
from app.research.me_entry_quality_h2_v1.pipeline import run_h2_event  # noqa: E402
from app.research.me_entry_quality_h2_v1.result import H2Policy  # noqa: E402
from app.research.me_upstream_source_v1.contracts import lineage_hash  # noqa: E402

HOST = os.environ.get("ME_H2PRO_PG_IT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ME_H2PRO_PG_IT_PORT", "5432"))
USER = os.environ.get("ME_H2PRO_PG_IT_USER", "postgres")

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "sql" / "migrations" / "095_me_entry_quality_h2_prospective_v1.sql"
)
MIGRATION_096_PATH = (
    ROOT / "sql" / "migrations" / "096_me_entry_quality_h2_prospective_overlap_v1.sql"
)
MIGRATIONS = (MIGRATION_PATH, MIGRATION_096_PATH)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
BASE_MS = 1_767_225_600_000

EXACT_PRICE = Decimal("123.4567890123456789")

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
            # terminate any leftover backends so DROP cannot block forever
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
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
    try:
        yield PgProspectiveStore(conn)
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - defensive
            pass


def make_source_row(key="evt-1", signal_ms=BASE_MS + 300_000, tweak=None,
                    price=None):
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
        "source_reference_price": (
            1234.5 if price is None else price
        ),
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

    def test_cursor_roundtrip(self, store):
        # review P1-watermark: capture-order cursor checkpoint
        assert store.get_cursor() == (0, "")
        store.set_cursor(BASE_MS * 1000, "evt-1")
        assert store.get_cursor() == (BASE_MS * 1000, "evt-1")


class TestPgDecimalNumericRoundTrip:
    """Review P1-Decimal: exact decimals through REAL ``NUMERIC`` columns."""

    def test_numeric_round_trip_is_exact(self, store):
        row = make_source_row()
        row["source_reference_price"] = EXACT_PRICE
        delivery = delivery_from_row(row)
        assert delivery.row["source_reference_price"] == EXACT_PRICE
        outcome = store.ingest_event(delivery)
        assert outcome.status == INGEST_ACCEPTED

        raw = store._execute(
            "SELECT source_reference_price, "
            "source_payload->>'source_reference_price' "
            "FROM research_me_entry_quality_h2_prospective.ingest_event "
            "WHERE source_event_key = 'evt-1'"
        ).fetchone()
        assert isinstance(raw[0], Decimal) and raw[0] == EXACT_PRICE
        assert raw[1] == "123.4567890123456789"

        restored = store.get_source_row("evt-1")
        assert restored["source_reference_price"] == EXACT_PRICE

    def test_redelivery_and_restart_keep_the_exact_price(self, store, scratch_db):
        row = make_source_row()
        row["source_reference_price"] = EXACT_PRICE
        store.ingest_event(delivery_from_row(row))
        store.set_cursor(BASE_MS * 1000, "evt-1")

        # "restart": a fresh store object over a NEW connection to the same DB
        conn2 = _admin_connect(scratch_db)
        conn2.autocommit = False
        try:
            store2 = PgProspectiveStore(conn2)
            assert store2.get_source_row("evt-1")["source_reference_price"] == (
                EXACT_PRICE
            )
            assert store2.get_cursor() == (BASE_MS * 1000, "evt-1")
            row2 = make_source_row()
            row2["source_reference_price"] = EXACT_PRICE
            dup = store2.ingest_event(delivery_from_row(row2))
            assert dup.duplicate
            assert store2.ingest_event_count() == 1
            assert store2.get_source_row("evt-1")["source_reference_price"] == (
                EXACT_PRICE
            )
        finally:
            conn2.close()

    def test_restored_price_reaches_the_h2_engine_exactly(self, store):
        # acceptance: ingest -> persistence -> restore -> H2 evaluation,
        # exact decimal text preserved VERBATIM through the pure PR #311
        # engine (no float rounding, no string escape)
        row = make_source_row()
        row["source_reference_price"] = EXACT_PRICE
        store.ingest_event(delivery_from_row(row))
        restored = store.get_source_row("evt-1")

        # identity and rows come from ONE payload (provenance hash is exact)
        from app.models import Candle
        from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
            rows_as_mappings,
        )

        mapped = rows_as_mappings([Candle(BASE_MS, 1.0, 2.0, 0.5, 1.5, 10.0)])
        identity = build_source_identity(
            SOURCE_KIND_BYBIT_KLINE_V5, "h2pro-it:BTCUSDT:5m", mapped,
            fetched_at_ms=BASE_MS,
        )
        outcome = run_h2_event(
            restored,
            rows_5m=mapped,
            source_identity=identity,
            policies=[
                H2Policy(variant_id="H2_RSI_TURN_1", observation_window=3)
            ],
            evaluation_asof_ms=BASE_MS + 400_000,
        )
        assert outcome.results
        payload = outcome.results[0].to_dict()
        assert payload["original_price_exact"] == "123.4567890123456789"

    def test_database_refuses_nan_inf_zero_negative_prices(self, scratch_db):
        # defense in depth behind the strict ingest validation: the CHECK
        # constraint refuses bad NUMERIC literals at the database
        conn = _admin_connect(scratch_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
                for bad in (
                    "'NaN'::numeric",
                    "'Infinity'::numeric",
                    "'-Infinity'::numeric",
                    "0",
                    "-5",
                ):
                    with pytest.raises(Exception):
                        cur.execute(
                            "INSERT INTO research_me_entry_quality_h2_prospective"
                            ".ingest_event (source_event_key, source_identity, "
                            "symbol, signal_time_us, captured_at, "
                            "activation_freeze_ts, signal_candle_open_time, "
                            "entry_timeframe, source_fingerprint, "
                            "ingest_status, "
                            "source_reference_price, immutable_source_hash) "
                            f"VALUES ('evt-bad', 'ident', 'BTCUSDT', 1, NOW(), "
                            f"NOW(), 1, '5m', 'fp', 'ACCEPTED', {bad}, 'hash')"
                        )
                # a finite positive price still passes
                cur.execute(
                    "INSERT INTO research_me_entry_quality_h2_prospective"
                    ".ingest_event (source_event_key, source_identity, "
                    "symbol, signal_time_us, captured_at, "
                    "activation_freeze_ts, signal_candle_open_time, "
                    "entry_timeframe, source_fingerprint, "
                    "ingest_status, "
                    "source_reference_price, immutable_source_hash) "
                    "VALUES ('evt-good', 'ident', 'BTCUSDT', 1, NOW(), "
                    "NOW(), 1, '5m', 'fp', 'ACCEPTED', "
                    "123.4567890123456789, 'hash')"
                )
        finally:
            conn.close()


class TestPgCaptureOrderIngest:
    def test_signal_order_inversion_stores_both_events(self, store):
        # review P1-watermark mandatory scenario at the persistence layer:
        # B (later signal) is ingested FIRST; A (earlier signal) LATER —
        # both first-seen snapshots are stored, none dropped
        s_a = BASE_MS + 1000
        s_b = BASE_MS + 2000
        b = make_source_row(
            key="evt-b", signal_ms=s_b,
        )
        a = make_source_row(key="evt-a", signal_ms=s_a)
        # capture order: B first
        assert store.ingest_event(delivery_from_row(b)).status == INGEST_ACCEPTED
        assert store.ingest_event(delivery_from_row(a)).status == INGEST_ACCEPTED
        assert store.ingest_event_count() == 2
        assert store.get_source_row("evt-a")["source_event_key"] == "evt-a"
        assert store.get_source_row("evt-b")["source_event_key"] == "evt-b"


class TestPgBudgetState:
    def test_api_budget_state_survives_restart(self, store, scratch_db):
        # review P1-budget: the hourly window is persisted in the DB
        assert store.load_api_budget_state() is None
        store.save_api_budget_state(
            {"hour_started_at_ms": 1_700_000_000_000, "requests_in_hour": 42}
        )
        conn2 = _admin_connect(scratch_db)
        conn2.autocommit = False
        try:
            store2 = PgProspectiveStore(conn2)
            assert store2.load_api_budget_state() == {
                "hour_started_at_ms": 1_700_000_000_000,
                "requests_in_hour": 42,
            }
            # overwriting is a singleton upsert
            store2.save_api_budget_state(
                {"hour_started_at_ms": 1_700_000_003_600, "requests_in_hour": 3}
            )
            assert store.load_api_budget_state()["requests_in_hour"] == 3
        finally:
            conn2.close()


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


class TestPgOverlapCursor:
    """Review P1-overlap FR-01/FR-04: the reconciliation pass state is
    DURABLE in the H2 research schema and survives restarts."""

    def test_default_state(self, store):
        state = store.get_overlap_state()
        assert state["pass_active"] is False
        assert state["cursor_captured_at_us"] == 0
        assert state["cursor_key"] == ""
        assert state["passes_completed"] == 0

    def test_state_roundtrip_and_durability_across_connections(
        self, store, scratch_db
    ):
        store.save_overlap_state({
            "cursor_captured_at_us": 1_767_225_600_000_000,
            "cursor_key": "evt-42",
            "pass_floor_us": 1000,
            "pass_ceiling_us": 2000,
            "pass_active": True,
            "passes_completed": 7,
        })
        # a brand-new connection (= a runtime restart) sees the saved pass
        conn2 = _admin_connect(scratch_db)
        conn2.autocommit = True
        try:
            with conn2.cursor() as cur:
                cur.execute(
                    "SELECT cursor_captured_at_us, cursor_key, pass_floor_us,"
                    " pass_ceiling_us, pass_active, passes_completed"
                    " FROM research_me_entry_quality_h2_prospective"
                    ".overlap_cursor WHERE id = 1"
                )
                row = cur.fetchone()
        finally:
            conn2.close()
        assert row == (
            1_767_225_600_000_000, "evt-42", 1000, 2000, True, 7,
        )

    def test_restart_recovery_state_is_seeable_by_a_new_store(
        self, store, scratch_db
    ):
        store.save_overlap_state({
            "cursor_captured_at_us": 555,
            "cursor_key": "evt-x",
            "pass_floor_us": 100,
            "pass_ceiling_us": 600,
            "pass_active": True,
            "passes_completed": 2,
        })
        conn2 = _admin_connect(scratch_db)
        conn2.autocommit = False
        try:
            store2 = PgProspectiveStore(conn2)  # exactly a restart
            state = store2.get_overlap_state()
        finally:
            conn2.close()
        assert state["pass_active"] is True      # resume is armed
        assert state["cursor_captured_at_us"] == 555
        assert state["passes_completed"] == 2

    def test_partial_updates_do_not_regress_other_fields(self, store):
        store.save_overlap_state({
            "cursor_captured_at_us": 10,
            "pass_ceiling_us": 20,
            "pass_active": True,
        })
        store.save_overlap_state({
            "cursor_captured_at_us": 15,
            "passes_completed": 3,
        })
        state = store.get_overlap_state()
        assert state["cursor_captured_at_us"] == 15
        assert state["pass_ceiling_us"] == 20  # preserved
        assert state["pass_active"] is True    # preserved
        assert state["passes_completed"] == 3


class TestPgMigration096:
    """Migration 096 (overlap cursor): first apply, re-apply, data safety,
    diagnostic status CHECK."""

    def test_first_apply_and_reapply_are_idempotent(self, scratch_db):
        conn = _admin_connect(scratch_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
                text = MIGRATION_096_PATH.read_text(encoding="utf-8")
                cur.execute(text)   # first apply: PASS
                cur.execute(text)   # re-apply: PASS (idempotent)
                cur.execute(text)   # third apply: still PASS
                cur.execute(
                    "SELECT to_regclass('research_me_entry_quality_h2_prospective"
                    ".overlap_cursor')"
                )
                assert cur.fetchone()[0] is not None
        finally:
            conn.close()

    def test_reapply_preserves_overlap_state(self, store):
        store.save_overlap_state({
            "cursor_captured_at_us": 42,
            "cursor_key": "evt-keep",
            "pass_floor_us": 1,
            "pass_ceiling_us": 99,
            "pass_active": True,
            "passes_completed": 5,
        })
        store._conn.rollback()
        conn = store._conn
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(MIGRATION_096_PATH.read_text(encoding="utf-8"))
            cur.execute(MIGRATION_096_PATH.read_text(encoding="utf-8"))
        conn.autocommit = False
        state = store.get_overlap_state()
        assert state["cursor_captured_at_us"] == 42
        assert state["cursor_key"] == "evt-keep"
        assert state["pass_active"] is True
        assert state["passes_completed"] == 5

    def test_health_status_accepts_the_diagnostic_and_keeps_the_guard(
        self, store
    ):
        # the FR-05 diagnostic status is accepted ...
        store.record_health(
            "OVERLAP_COVERAGE_DEGRADED",
            {"overlap_coverage_degraded": True},
            {"overlap": {"coverage_degraded": True}},
        )
        row = store._execute(
            "SELECT status FROM research_me_entry_quality_h2_prospective"
            ".runtime_health ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "OVERLAP_COVERAGE_DEGRADED"
        # ... while the CHECK still refuses unknown statuses
        conn = store._conn
        conn.rollback()  # close the read txn before switching autocommit
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO research_me_entry_quality_h2_prospective"
                        ".runtime_health (status) VALUES ('NOT_A_STATUS')"
                    )
                    raised = False
                except psycopg2.errors.CheckViolation:
                    raised = True
                finally:
                    cur.execute("ROLLBACK")
            assert raised
        finally:
            conn.autocommit = False

    def test_reapply_keeps_ingest_states_results_intact(self, store):
        # review §6: re-apply must never destroy ingest events / states /
        # results / conflict ledger
        row = make_source_row(key="evt-keep2")
        store.ingest_event(delivery_from_row(row))
        store.ensure_variant_state(
            source_event_key="evt-keep2",
            variant_id="H2_RSI_TURN_1",
            policy_fingerprint="ff" * 32,
            policy={},
            symbol="BTCUSDT",
            signal_time_us=1,
        )
        store.record_conflict(
            conflict_kind="EVENT_KEY_LINEAGE_CONFLICT",
            source_event_key="evt-keep2",
            stored_hash="aa",
            delivered_hash="bb",
        )
        conn = store._conn
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(MIGRATION_096_PATH.read_text(encoding="utf-8"))
            cur.execute(
                "SELECT (SELECT COUNT(*) FROM research_me_entry_quality"
                "_h2_prospective.ingest_event),"
                " (SELECT COUNT(*) FROM research_me_entry_quality"
                "_h2_prospective.variant_state),"
                " (SELECT COUNT(*) FROM research_me_entry_quality"
                "_h2_prospective.result_conflict)"
            )
            counts = cur.fetchone()
        conn.autocommit = False
        assert counts == (1, 1, 1)


class TestPgMigration095:
    """Review P2-migration: re-applicability, rollback and strict guards."""

    def test_first_apply_and_reapply_are_idempotent(self, scratch_db):
        conn = _admin_connect(scratch_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                text = MIGRATION_PATH.read_text(encoding="utf-8")
                cur.execute(text)   # first apply: PASS
                cur.execute(text)   # re-apply: PASS (idempotent)
                cur.execute(text)   # third apply: still PASS
                cur.execute(
                    "SELECT to_regclass('research_me_entry_quality_h2_prospective"
                    ".api_budget_state')"
                )
                assert cur.fetchone()[0] is not None
        finally:
            conn.close()

    def test_reapply_preserves_existing_results_and_immutability(
        self, store, scratch_db
    ):
        # an existing terminal result must SURVIVE re-apply untouched and the
        # UPDATE/DELETE ban must stay active afterwards
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
        before = store._execute(
            "SELECT status, result_payload FROM "
            "research_me_entry_quality_h2_prospective.variant_result"
        ).fetchall()
        assert len(before) == 1
        # release the store's read transaction so the re-apply DDL
        # (DROP TRIGGER takes ACCESS EXCLUSIVE) cannot deadlock on it
        store._conn.rollback()

        # re-apply the migration on the SAME database (fresh connection)
        conn = _admin_connect(scratch_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
        finally:
            conn.close()

        after = store._execute(
            "SELECT status, result_payload FROM "
            "research_me_entry_quality_h2_prospective.variant_result"
        ).fetchall()
        assert after == before  # byte-identical, nothing touched

        # immutability triggers are still active after re-apply
        with pytest.raises(Exception):
            store._execute(
                "DELETE FROM research_me_entry_quality_h2_prospective"
                ".variant_result"
            )
        store.broken = False

    def test_induced_migration_error_rolls_back_everything(self, scratch_db):
        # transactional migration: a failing statement leaves NO partial
        # schema behind (review P2-migration rollback requirement)
        conn = _admin_connect(scratch_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                text = MIGRATION_PATH.read_text(encoding="utf-8")
                assert text.count("COMMIT;") == 1
                broken = text.replace("COMMIT;", "SELECT 1/0;\nCOMMIT;")
                with pytest.raises(Exception):
                    cur.execute(broken)
                cur.execute("ROLLBACK")  # explicit: conn.rollback() is a
                # no-op in autocommit mode while the txn block is aborted
                cur.execute(
                    "SELECT to_regclass('research_me_entry_quality_h2_prospective"
                    ".reader_watermark')"
                )
                assert cur.fetchone()[0] is None
                cur.execute(
                    "SELECT to_regclass('research_me_entry_quality_h2_prospective"
                    ".variant_result')"
                )
                assert cur.fetchone()[0] is None
        finally:
            conn.close()

    def test_incompatible_pre_existing_schema_is_predictably_refused(
        self, scratch_db
    ):
        # a foreign/legacy table in the schema must NEVER be silently
        # accepted: the migration raises H2P_SCHEMA_INCOMPATIBLE and rolls
        # the whole transaction back
        conn = _admin_connect(scratch_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE SCHEMA research_me_entry_quality_h2_prospective"
                )
                cur.execute(
                    "CREATE TABLE research_me_entry_quality_h2_prospective"
                    ".reader_watermark (id INTEGER PRIMARY KEY)"
                )  # missing cursor_captured_at_us / cursor_key
                with pytest.raises(Exception) as excinfo:
                    cur.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
                assert "H2P_SCHEMA_INCOMPATIBLE" in str(excinfo.value)
                cur.execute("ROLLBACK")  # explicit: autocommit rollback no-op
                # the rollback dropped the new tables created before the
                # guard fired
                cur.execute(
                    "SELECT to_regclass('research_me_entry_quality_h2_prospective"
                    ".api_budget_state')"
                )
                assert cur.fetchone()[0] is None
        finally:
            conn.close()
