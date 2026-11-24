"""Real PostgreSQL integration for the Issue #305 upstream source capture.

Covers the acceptance-criteria table of Issue #305 against a REAL
PostgreSQL server, inside a FULLY ISOLATED scratch environment:

* an OWN scratch database (created + dropped by this module);
* a minimal stand-in of ``dds.me_long_v2_inverse_short_v3_event`` in the
  scratch database — production tables are NEVER touched;
* migration 094 applied transactionally;
* no production service, schema or data is read or written.

SAFETY
------
* OPT-IN ONLY: the module skips unless ``ME_UPS305_PG_INTEGRATION=1``.
* Only connects to the configured TEST server
  (``ME_UPS305_PG_IT_HOST/PORT/USER``, default ``127.0.0.1:5432 postgres``)
  and only ever writes inside its own ``me_ups305_it_*`` scratch database.

Scenario coverage (acceptance criteria): 1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
11, 12.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.research.me_upstream_source_v1.activation import (  # noqa: E402
    is_runtime_enabled,
)
from app.research.me_upstream_source_v1.capture import (  # noqa: E402
    CAPTURE_BLOCKED,
    CAPTURE_DUPLICATE,
    CAPTURE_ERROR,
    CAPTURE_INTEGRITY_CONFLICT,
    CAPTURE_INVALID_EVENT,
    CAPTURE_NOT_ACTIVE,
    CAPTURE_PRE_FREEZE,
    CAPTURED,
    capture_with_status,
)
from app.research.me_upstream_source_v1.contracts import (  # noqa: E402
    build_source_event,
)
from app.research.me_upstream_source_v1.coverage import (  # noqa: E402
    COVERAGE_GAP,
    COVERAGE_LAG,
    COVERAGE_PASS,
    INTEGRITY_CONFLICT,
    SOURCE_COVERAGE_UNKNOWN,
    reconcile_coverage,
    save_coverage_audit,
)
from app.research.me_upstream_source_v1.identity import (  # noqa: E402
    normalized_identity,
)
from app.research.me_upstream_source_v1.protocol import (  # noqa: E402
    ACTIVATION_FLAG,
    EXPERIMENT_ID,
    protocol_fingerprint,
)
from app.research.me_upstream_source_v1.store import (  # noqa: E402
    PgUpstreamSourceStore,
)
from app.scanners.models import SetupCandidate  # noqa: E402

HOST = os.environ.get("ME_UPS305_PG_IT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ME_UPS305_PG_IT_PORT", "5432"))
USER = os.environ.get("ME_UPS305_PG_IT_USER", "postgres")

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    ROOT / "sql" / "migrations" / "094_me_upstream_source_capture_v1.sql",
)

SCRATCH_PREFIX = "me_ups305_it_"

NOW = datetime.now(timezone.utc)
FREEZE = NOW - timedelta(hours=2)
SIGNAL_TIME = NOW - timedelta(minutes=5)
CANDLE_MS = int(SIGNAL_TIME.timestamp() // 300) * 300_000

ENABLED = {ACTIVATION_FLAG: "1"}

# Minimal stand-in of the V3 producer ledger (scratch database only).
SOURCE_TABLE_SQL = """
CREATE SCHEMA IF NOT EXISTS dds;
CREATE TABLE dds.me_long_v2_inverse_short_v3_event (
    event_key text PRIMARY KEY,
    symbol text NOT NULL,
    signal_time timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    captured_at timestamptz NOT NULL DEFAULT NOW(),
    source_scanner text NOT NULL,
    source_scanner_version text NOT NULL DEFAULT '',
    source_direction text NOT NULL,
    source_setup_id text NOT NULL DEFAULT '',
    capture_route text NOT NULL,
    entry_timeframe text NOT NULL DEFAULT '5m',
    signal_candle_open_time bigint NOT NULL,
    source_reference_price numeric,
    lineage jsonb NOT NULL DEFAULT '{}'::jsonb
)
"""


def _server_ok() -> bool:
    if os.environ.get("ME_UPS305_PG_INTEGRATION") != "1":
        return False
    try:
        conn = psycopg2.connect(
            host=HOST, port=PORT, database="postgres", user=USER,
            connect_timeout=5,
        )
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_ok(),
    reason="ME_UPS305_PG_INTEGRATION != 1 or no test PostgreSQL at "
           f"{HOST}:{PORT}; Issue #305 PG integration skipped",
)


# ─── scratch database lifecycle ──────────────────────────────────────────


def _admin_connect(dbname: str = "postgres"):
    return psycopg2.connect(
        host=HOST, port=PORT, database=dbname, user=USER,
        connect_timeout=5,
    )


@pytest.fixture(scope="module")
def scratch_db():
    name = SCRATCH_PREFIX + uuid4().hex[:10]
    admin = _admin_connect()
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    admin.close()

    yield name

    admin = _admin_connect()
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture()
def conn(scratch_db):
    """Fresh, fully migrated scratch schema for every test."""

    connection = _admin_connect(scratch_db)
    connection.autocommit = False

    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS research_me_upstream_coverage CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS dds CASCADE")
        cur.execute(SOURCE_TABLE_SQL)

        for migration in MIGRATIONS:
            body = migration.read_text(encoding="utf-8")
            body = body.split("BEGIN;", 1)[1].rsplit("COMMIT;", 1)[0]
            cur.execute(body)

    connection.commit()
    yield connection
    connection.rollback()
    connection.close()


def activate(conn, *, status: str = "RUNNING", freeze_ts=FREEZE) -> None:
    """Administrative activation row (the admin contour writes it)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO research_me_upstream_coverage.activation
                (experiment_id, status, freeze_ts, protocol_version,
                 protocol_fingerprint)
            VALUES (%s, %s, %s, '1.0.0', %s)
            """,
            (EXPERIMENT_ID, status, freeze_ts, protocol_fingerprint()),
        )
    conn.commit()


def make_candidate(**overrides) -> SetupCandidate:
    fields = {
        "setup_id": uuid4(),
        "scanner_name": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        "scanner_version": "1.0.0",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_timeframe": "5m",
        "detected_at": SIGNAL_TIME,
        "signal_candle_open_time": CANDLE_MS,
        "reference_price": 100.0,
        "invalidation_price": 97.5,
        "target_1": 103.0,
        "features": {
            "rsi_14": 55.0,
            "rsi_delta_3": 1.25,
            "entry_confirmation_rsi_rising": True,
        },
    }
    fields.update(overrides)
    return SetupCandidate(**fields)


def insert_v3(conn, candidate: SetupCandidate, *, event_key: str | None = None) -> None:
    """Insert the V3-side record of the SAME source event (same facts)."""
    event = build_source_event(
        candidate, activation_freeze_ts=FREEZE, captured_at=SIGNAL_TIME,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dds.me_long_v2_inverse_short_v3_event (
                event_key, symbol, signal_time, created_at, captured_at,
                source_scanner, source_scanner_version, source_direction,
                source_setup_id, capture_route, entry_timeframe,
                signal_candle_open_time, source_reference_price, lineage
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_key or ("v3-" + event.source_event_key[:40]),
                candidate.symbol,
                event.signal_time,
                event.signal_time + timedelta(seconds=1),
                event.signal_time + timedelta(milliseconds=200),
                event.source_scanner,
                event.source_scanner_version,
                event.source_direction,
                event.source_setup_id,
                "PRE_FILTER_SOURCE",
                event.entry_timeframe,
                event.signal_candle_open_time,
                event.source_reference_price,
                json.dumps(event.lineage),
            ),
        )
    conn.commit()


def event_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM research_me_upstream_coverage.source_event"
        )
        return int(cur.fetchone()[0])


def rejected_rows(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_event_key, reason_code "
            "FROM research_me_upstream_coverage.rejected_event "
            "ORDER BY id"
        )
        return list(cur.fetchall())


def state_rows(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, reason_code "
            "FROM research_me_upstream_coverage.capture_state_event "
            "ORDER BY id"
        )
        return list(cur.fetchall())


def health_rows(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, events_inserted, duplicates, errors, "
            "delivery_latency_seconds "
            "FROM research_me_upstream_coverage.capture_health ORDER BY id"
        )
        return list(cur.fetchall())


# ─── criterion 1: capture disabled -> no writes ─────────────────────────


class TestDisabled:
    def test_flag_off_writes_nothing(self, conn):
        store = PgUpstreamSourceStore(conn)
        status = capture_with_status(store, make_candidate(), env={})
        assert status == "DISABLED"
        assert event_count(conn) == 0
        assert health_rows(conn) == []
        assert is_runtime_enabled({}) is False


# ─── criterion 2: no RUNNING activation / freeze -> no capture ──────────


class TestActivationGate:
    def test_no_activation_row(self, conn):
        store = PgUpstreamSourceStore(conn)
        assert capture_with_status(
            store, make_candidate(), env=ENABLED,
        ) == CAPTURE_NOT_ACTIVE
        assert event_count(conn) == 0

    def test_not_running_status(self, conn):
        activate(conn, status="READY")
        store = PgUpstreamSourceStore(conn)
        assert capture_with_status(
            store, make_candidate(), env=ENABLED,
        ) == CAPTURE_NOT_ACTIVE
        assert event_count(conn) == 0

    def test_foreign_fingerprint(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_me_upstream_coverage.activation
                    (experiment_id, status, freeze_ts, protocol_version,
                     protocol_fingerprint)
                VALUES (%s, 'RUNNING', %s, '1.0.0', %s)
                """,
                (EXPERIMENT_ID, FREEZE, "f" * 64),
            )
        conn.commit()
        store = PgUpstreamSourceStore(conn)
        assert capture_with_status(
            store, make_candidate(), env=ENABLED,
        ) == CAPTURE_NOT_ACTIVE
        assert event_count(conn) == 0


# ─── criteria 3/5: capture + idempotent redelivery ──────────────────────


class TestCaptureAndIdempotency:
    def test_new_me_long_candidate_lands_in_upstream_ledger(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        candidate = make_candidate()
        status = capture_with_status(store, candidate, env=ENABLED)
        assert status == CAPTURED
        assert event_count(conn) == 1

        event = build_source_event(
            candidate, activation_freeze_ts=FREEZE, captured_at=SIGNAL_TIME,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_event_key, source_identity, features, lineage "
                "FROM research_me_upstream_coverage.source_event"
            )
            key, identity, features, lineage = cur.fetchone()
        assert key == event.source_event_key
        assert identity == normalized_identity(candidate)
        # Features preserved verbatim (jsonb round-trip).
        assert features["rsi_14"] == 55.0
        assert features["entry_confirmation_rsi_rising"] is True
        assert lineage["source_signal_candle_open_time"] == CANDLE_MS

        health = health_rows(conn)
        assert health[0][0] == "CAPTURED"
        assert health[0][1] == 1  # events_inserted
        assert health[0][4] is not None  # delivery latency

    def test_redelivery_creates_no_duplicate_row(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        candidate = make_candidate()
        assert capture_with_status(store, candidate, env=ENABLED) == CAPTURED
        # Verbatim redelivery (e.g. after a restart).
        assert capture_with_status(store, candidate, env=ENABLED) == CAPTURE_DUPLICATE
        # Re-detection variance: NEW setup_id + detected_at, same signal.
        assert capture_with_status(
            store,
            make_candidate(
                setup_id=uuid4(),
                detected_at=SIGNAL_TIME + timedelta(minutes=1),
            ),
            env=ENABLED,
        ) == CAPTURE_DUPLICATE
        assert event_count(conn) == 1

    def test_foreign_scanner_ignored(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        assert capture_with_status(
            store, make_candidate(scanner_name="BREAKOUT_RETEST"), env=ENABLED,
        ) == "INVALID_SCANNER"
        assert capture_with_status(
            store, make_candidate(direction="SHORT"), env=ENABLED,
        ) == "INVALID_SCANNER"
        assert event_count(conn) == 0


# ─── criterion 6: immutable lineage conflict ────────────────────────────


class TestIntegrityConflict:
    def test_lineage_conflict_is_recorded_and_blocks(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        candidate = make_candidate()
        assert capture_with_status(store, candidate, env=ENABLED) == CAPTURED

        drifted = make_candidate(
            setup_id=uuid4(),
            reference_price=999.0,  # immutable fact drift, SAME identity key
        )
        assert capture_with_status(store, drifted, env=ENABLED) == (
            CAPTURE_INTEGRITY_CONFLICT
        )
        assert state_rows(conn) == [
            ("BLOCKED", "EVENT_KEY_LINEAGE_CONFLICT"),
        ]
        # Sticky fail-closed: nothing is admitted before manual recovery.
        assert capture_with_status(store, make_candidate(), env=ENABLED) == (
            CAPTURE_BLOCKED
        )
        assert event_count(conn) == 1

    def test_database_rejects_event_mutation(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        assert capture_with_status(
            store, make_candidate(), env=ENABLED,
        ) == CAPTURED
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.Error):
                cur.execute(
                    "UPDATE research_me_upstream_coverage.source_event "
                    "SET symbol = 'X'"
                )
        conn.rollback()


# ─── criterion 7: write error fail-open ─────────────────────────────────


class TestWriteErrorFailOpen:
    def test_closed_connection_yields_error_status_without_exception(self, conn):
        activate(conn)
        broken = _admin_connect(scratch_db_for(conn))
        store = PgUpstreamSourceStore(broken)
        broken.close()
        assert capture_with_status(store, make_candidate(), env=ENABLED) == (
            CAPTURE_ERROR
        )  # fail-open: never raises into the scanner cycle


def scratch_db_for(conn) -> str:
    return conn.info.dbname


# ─── criterion 8: pre-freeze never enters the cohort ────────────────────


class TestProspectiveOnly:
    def test_pre_freeze_event_is_rejected(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        status = capture_with_status(
            store,
            make_candidate(detected_at=FREEZE - timedelta(minutes=5)),
            env=ENABLED,
        )
        assert status == CAPTURE_PRE_FREEZE
        assert event_count(conn) == 0
        assert any(r[1] == "PRE_FREEZE" for r in rejected_rows(conn))

    def test_database_check_blocks_backfill(self, conn):
        activate(conn)
        event = build_source_event(
            make_candidate(),
            activation_freeze_ts=FREEZE,
            captured_at=FREEZE - timedelta(minutes=1),
        )
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.Error):
                cur.execute(
                    """
                    INSERT INTO research_me_upstream_coverage.source_event (
                        source_event_key, experiment_id, source_identity,
                        symbol, signal_time, captured_at,
                        activation_freeze_ts, source_setup_id, source_scanner,
                        source_scanner_version, source_direction,
                        entry_timeframe, signal_candle_open_time,
                        capture_route, lineage, lineage_hash,
                        comparable_lineage_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event.source_event_key, EXPERIMENT_ID,
                        event.source_identity, event.symbol,
                        FREEZE - timedelta(hours=1),  # pre-freeze signal
                        FREEZE - timedelta(minutes=1),
                        FREEZE, event.source_setup_id, event.source_scanner,
                        event.source_scanner_version, event.source_direction,
                        event.entry_timeframe, event.signal_candle_open_time,
                        event.capture_route,
                        json.dumps(event.lineage), event.lineage_hash,
                        event.comparable_lineage_hash,
                    ),
                )
        conn.rollback()

    def test_freeze_ts_is_immutable(self, conn):
        activate(conn)
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.Error):
                cur.execute(
                    "UPDATE research_me_upstream_coverage.activation "
                    "SET freeze_ts = %s",
                    (FREEZE - timedelta(days=1),),
                )
        conn.rollback()

    def test_invalid_event_never_enters_cohort(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        status = capture_with_status(
            store,
            make_candidate(features={"rsi_14": 55.0}),  # incomplete features
            env=ENABLED,
        )
        assert status == CAPTURE_INVALID_EVENT
        assert event_count(conn) == 0
        assert any("INCOMPLETE_FEATURES" in str(r) for r in rejected_rows(conn))


# ─── criteria 9/10/11: coverage reconciliation ──────────────────────────


class TestCoverageReconciliation:
    def test_matched_event_is_coverage_pass(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        candidate = make_candidate()
        assert capture_with_status(store, candidate, env=ENABLED) == CAPTURED
        insert_v3(conn, candidate)

        result = reconcile_coverage(conn)
        assert result["matched"] == 1
        assert result["status"] == COVERAGE_PASS
        assert result["lineage_mismatches"] == 0

    def test_upstream_only_is_lag_then_gap(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        assert capture_with_status(store, make_candidate(), env=ENABLED) == CAPTURED
        insert_v3(conn, make_candidate(symbol="ETHUSDT", setup_id=uuid4()))

        fresh = reconcile_coverage(conn)
        assert fresh["upstream_only_lag"] == 1
        assert fresh["status"] == COVERAGE_LAG

        # After the grace window the same unmatched event is a CONFIRMED gap
        # (read-only: time travel via as_of, never by mutating rows).
        stale = reconcile_coverage(conn, as_of=NOW + timedelta(hours=2))
        assert stale["upstream_only_gap"] == 1
        assert stale["status"] == COVERAGE_GAP

    def test_v3_only_event_is_diagnosed(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        assert capture_with_status(store, make_candidate(), env=ENABLED) == CAPTURED
        insert_v3(conn, make_candidate(symbol="ETHUSDT", setup_id=uuid4()))
        result = reconcile_coverage(conn, as_of=NOW + timedelta(hours=2))
        assert result["v3_only_gap"] == 1
        assert result["status"] == COVERAGE_GAP

    def test_unknown_activity_period_is_source_coverage_unknown(self, conn):
        activate(conn)
        result = reconcile_coverage(conn)
        assert result["status"] == SOURCE_COVERAGE_UNKNOWN
        assert result["reason"] == "NO_UPSTREAM_ACTIVITY"

    def test_missing_activation_is_source_coverage_unknown(self, conn):
        result = reconcile_coverage(conn)
        assert result["status"] == SOURCE_COVERAGE_UNKNOWN
        assert result["reason"] == "NOT_ACTIVATED"

    def test_audit_result_can_be_persisted_explicitly(self, conn):
        activate(conn)
        result = reconcile_coverage(conn)
        save_coverage_audit(conn, result)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, reason FROM "
                "research_me_upstream_coverage.coverage_audit"
            )
            rows = list(cur.fetchall())
        assert rows == [(result["status"], result["reason"])]

    def test_reconciliation_is_read_only(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        assert capture_with_status(store, make_candidate(), env=ENABLED) == CAPTURED
        conn.commit()
        reconcile_coverage(conn)
        # Nothing about the captured row changed (append-only + read-only).
        assert event_count(conn) == 1


# ─── criterion 12: lineage-conflict ledger status ───────────────────────


class TestConflictVerdict:
    def test_blocked_state_makes_coverage_conflict(self, conn):
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        candidate = make_candidate()
        assert capture_with_status(store, candidate, env=ENABLED) == CAPTURED
        insert_v3(conn, candidate)
        store.record_state("BLOCKED", "EVENT_KEY_LINEAGE_CONFLICT", {})

        result = reconcile_coverage(conn)
        assert result["status"] == INTEGRITY_CONFLICT
        assert result["reason"] == "CAPTURE_STATE_BLOCKED"


# ─── regression: activity-window basis (VPS 20/22 remediation) ──────────


class TestActivityWindowBasis:
    def test_ingestion_skew_between_ledgers_still_matches(self, conn):
        # VPS regression: the upstream ledger ingested the event at wall
        # clock now while V3 ingested the SAME event 5 minutes earlier
        # (captured_at = signal + 200 ms).  The audited window follows the
        # original EVENT time, so both rows are in scope and the pair must
        # MATCH (was: matched=0 / v3_only_gap=0 because window_start jumped
        # to the later first-ingestion).
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        candidate = make_candidate()
        # Delayed upstream ingestion: recovered delivery ~10 min after the
        # detection, while the V3 hook landed it in the original cycle.
        status = capture_with_status(
            store, candidate, env=ENABLED,
            clock=lambda: SIGNAL_TIME + timedelta(minutes=10),
        )
        assert status == CAPTURED
        insert_v3(conn, candidate)  # prompt ingestion (signal + 200 ms)

        result = reconcile_coverage(conn)
        assert result["matched"] == 1
        assert result["outside_window_v3"] == 0
        assert result["lineage_mismatches"] == 0
        assert result["status"] == COVERAGE_PASS

    def test_v3_event_before_upstream_activity_is_not_a_gap(self, conn):
        # Mismatched activity periods: a V3 event detected BEFORE the
        # upstream capture's first confirmed event is outside-window
        # evidence — never a confirmed COVERAGE_GAP — while in-window
        # unmatched events still become gaps after grace (no weakened gap
        # detection).
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        assert capture_with_status(store, make_candidate(), env=ENABLED) == CAPTURED
        # V3-only event from before the upstream ledger's first event.
        insert_v3(
            conn,
            make_candidate(
                symbol="ETHUSDT",
                setup_id=uuid4(),
                detected_at=NOW - timedelta(hours=1),  # post-freeze, pre-window
            ),
        )

        result = reconcile_coverage(conn, as_of=NOW + timedelta(hours=2))
        assert result["outside_window_v3"] == 1
        assert result["v3_only_gap"] == 0  # pre-window absence is not an error
        assert result["upstream_only_gap"] == 1  # in-window gap still detected

    def test_event_times_anchor_the_window_not_ingestion(self, conn):
        # Both ledgers ingested their rows long ago but the EVENT time is
        # recent: the rows are in scope (the window follows the signal) and
        # the pair matches.
        activate(conn)
        store = PgUpstreamSourceStore(conn)
        candidate = make_candidate()
        assert capture_with_status(
            store, candidate, env=ENABLED,
            clock=lambda: SIGNAL_TIME + timedelta(minutes=30),
        ) == CAPTURED
        insert_v3(conn, candidate)
        result = reconcile_coverage(conn)
        assert result["matched"] == 1
        assert result["outside_window_upstream"] == 0
        assert result["outside_window_v3"] == 0
