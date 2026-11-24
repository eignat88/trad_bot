"""Real PostgreSQL integration for the Issue #296 source observer.

Covers the review scenario table of PR #303 against a REAL PostgreSQL
server, inside a FULLY ISOLATED scratch environment:

* an OWN scratch database (created + dropped by this module);
* a minimal stand-in of ``dds.me_long_v2_inverse_short_v3_event`` in the
  scratch database — production tables are NEVER touched;
* migrations 092 + 093 applied transactionally (no COMMIT executed);
* no production service, schema or data is read or written.

SAFETY
------
* OPT-IN ONLY: the module skips unless ``ME_EQ296_PG_INTEGRATION=1``.
* Only connects to the configured TEST server
  (``ME_EQ296_PG_IT_HOST/PORT/USER``, default ``127.0.0.1:5432 postgres``)
  and only ever writes inside its own ``me_eq296_it_*`` scratch database.

Scenario coverage (task table): 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
13, 14, 15, 16.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.research.me_entry_quality_v1.contracts import (  # noqa: E402
    EXPERIMENT_ID,
    SOURCE_SCANNER,
    ValidatedSourceEvent,
)
from app.research.me_entry_quality_v1.integrity import (  # noqa: E402
    audit_source_integrity,
    verdict_gate,
)
from app.research.me_entry_quality_v1.observer import (  # noqa: E402
    EntryQualityObserver,
)
from app.research.me_entry_quality_v1.repository import (  # noqa: E402
    PgObserverRepository,
    SourceIntegrityError,
)

HOST = os.environ.get("ME_EQ296_PG_IT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ME_EQ296_PG_IT_PORT", "5432"))
USER = os.environ.get("ME_EQ296_PG_IT_USER", "postgres")

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    ROOT / "sql" / "migrations" / "092_me_entry_quality_source_observer_v1.sql",
    ROOT / "sql" / "migrations" / "093_me_entry_quality_observer_state_v1.sql",
)

SCRATCH_PREFIX = "me_eq296_it_"

FREEZE = datetime.now(timezone.utc) - timedelta(hours=2)

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
    if os.environ.get("ME_EQ296_PG_INTEGRATION") != "1":
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
    reason="ME_EQ296_PG_INTEGRATION != 1 or no test PostgreSQL at "
           f"{HOST}:{PORT}; Issue #296 PG integration skipped",
)


# ── scratch database lifecycle ──────────────────────────────────────


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
        cur.execute("DROP SCHEMA IF EXISTS research_me_entry_quality CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS dds CASCADE")
        cur.execute(SOURCE_TABLE_SQL)

        for migration in MIGRATIONS:
            body = migration.read_text(encoding="utf-8")
            body = body.split("BEGIN;", 1)[1].rsplit("COMMIT;", 1)[0]
            cur.execute(body)

        # Frozen activation row (the administrative contour writes it).
        cur.execute(
            """
            INSERT INTO research_me_entry_quality.activation
                (experiment_id, status, freeze_ts, protocol_version)
            VALUES (%s, 'RUNNING', %s, '1.0.0')
            """,
            (EXPERIMENT_ID, FREEZE),
        )

    connection.commit()
    yield connection
    connection.rollback()
    connection.close()


def source_row(
    *,
    key: str | None = None,
    symbol: str = "ETHUSDT",
    rsi_14=68.0,
    rsi_delta_3=2.4,
    flag=True,
    signal_time: datetime | None = None,
    created_at: datetime | None = None,
    extra_lineage: dict | None = None,
) -> dict:
    signal = signal_time or (datetime.now(timezone.utc) - timedelta(minutes=2))

    lineage = {
        "source_identity": "it-source",
        "source_signal_time": signal.isoformat(),
        "source_signal_candle_open_time": int(signal.timestamp() * 1000),
        "source_scanner": SOURCE_SCANNER,
        "source_direction": "LONG",
        "source_features": {
            "rsi_14": rsi_14,
            "rsi_delta_3": rsi_delta_3,
            "entry_confirmation_rsi_rising": flag,
        },
        "source_pipeline_fingerprint": "it-fingerprint",
    }
    if extra_lineage:
        lineage.update(extra_lineage)

    return {
        "event_key": key or ("a" * 64),
        "symbol": symbol,
        "signal_time": signal,
        "created_at": created_at or (signal + timedelta(seconds=1)),
        "captured_at": signal + timedelta(milliseconds=100),
        "source_scanner": SOURCE_SCANNER,
        "source_scanner_version": "1.0.0",
        "source_direction": "LONG",
        "source_setup_id": "IT-001",
        "capture_route": "PRE_FILTER_SOURCE",
        "entry_timeframe": "5m",
        "signal_candle_open_time": int(signal.timestamp() * 1000),
        "source_reference_price": 123.45,
        "lineage": lineage,
    }


def insert_source(conn, row: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dds.me_long_v2_inverse_short_v3_event (
                event_key, symbol, signal_time, created_at, captured_at,
                source_scanner, source_scanner_version, source_direction,
                source_setup_id, capture_route, entry_timeframe,
                signal_candle_open_time, source_reference_price, lineage
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                row["event_key"], row["symbol"], row["signal_time"],
                row["created_at"], row["captured_at"],
                row["source_scanner"], row["source_scanner_version"],
                row["source_direction"], row["source_setup_id"],
                row["capture_route"], row["entry_timeframe"],
                row["signal_candle_open_time"],
                row["source_reference_price"],
                json.dumps(row["lineage"]),
            ),
        )


def fetch_all(conn, sql: str, params=()) -> list:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def new_observer(conn, **kwargs) -> EntryQualityObserver:
    return EntryQualityObserver(PgObserverRepository(conn), **kwargs)


# ── scenario 1: correct source event → ACCEPTED ─────────────────────


def test_correct_source_event_accepted(conn):
    insert_source(conn, source_row())

    result = new_observer(conn).run_once()

    assert result.status == "OK"
    assert result.events_inserted == 1

    processed = fetch_all(
        conn,
        "SELECT processing_status, reason_code "
        "FROM research_me_entry_quality.processed_event",
    )
    assert processed == [("ACCEPTED", None)]

    events = fetch_all(
        conn,
        "SELECT event_key, lineage_hash "
        "FROM research_me_entry_quality.source_event",
    )
    assert len(events) == 1


# ── scenarios 2-5: invalid source → REJECTED, fail-closed ───────────


@pytest.mark.parametrize(
    "features,reason",
    [
        # NaN/Infinity cannot physically exist inside a JSONB lineage —
        # those rejects are covered by the contract unit tests; here we
        # cover everything a real JSONB source can deliver.
        ({"rsi_14": None}, "FEATURE_RSI14_NOT_FINITE"),
        ({"rsi_14": 150.0}, "FEATURE_RSI14_OUT_OF_RANGE"),
        ({"rsi_14": -1}, "FEATURE_RSI14_OUT_OF_RANGE"),
        ({"rsi_14": "68.0"}, "FEATURE_RSI14_INVALID_TYPE"),
        ({"rsi_14": True}, "FEATURE_RSI14_INVALID_TYPE"),
        ({"rsi_delta_3": None}, "FEATURE_RSI_DELTA_3_NOT_FINITE"),
        ({"rsi_delta_3": "2.4"}, "FEATURE_RSI_DELTA_3_INVALID_TYPE"),
        ({"entry_confirmation_rsi_rising": "true"},
         "FEATURE_CONFIRMATION_FLAG_INVALID_TYPE"),
        ({"entry_confirmation_rsi_rising": 1},
         "FEATURE_CONFIRMATION_FLAG_INVALID_TYPE"),
    ],
)
def test_invalid_source_rejected_and_fail_closed(conn, features, reason):
    row = source_row()
    row["lineage"]["source_features"].update(features)
    insert_source(conn, row)

    result = new_observer(conn).run_once()

    assert result.status == "BLOCKED"
    assert result.blocked_reason == "INVALID_SOURCE_EVENT"

    processed = fetch_all(
        conn,
        "SELECT processing_status, reason_code "
        "FROM research_me_entry_quality.processed_event",
    )
    assert processed == [("REJECTED", reason)]

    # Invalid events never enter the eligible cohort.
    events = fetch_all(
        conn,
        "SELECT event_key FROM research_me_entry_quality.source_event",
    )
    assert events == []

    # Rejection diagnostics survive the block (scenario 13).
    health = fetch_all(
        conn,
        "SELECT status, diagnostics "
        "FROM research_me_entry_quality.source_health",
    )
    assert health[-1][0] == "SOURCE_INCOMPLETE"
    assert health[-1][1]["invalid"] == 1


# ── scenario 6: changed source lineage → INTEGRITY_CONFLICT ─────────


def test_changed_source_lineage_is_integrity_conflict(conn):
    insert_source(conn, source_row())
    new_observer(conn).run_once()

    # Upstream drift: the immutable source identity changed.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dds.me_long_v2_inverse_short_v3_event "
            "SET lineage = lineage || '{\"source_identity\": \"changed\"}'"
        )
    conn.commit()

    audit = audit_source_integrity(conn)

    assert audit["status"] == "INTEGRITY_CONFLICT"
    assert audit["lineage_mismatches"] == 1

    gate = verdict_gate(contour_state="ACTIVE", audit=audit)
    assert gate["source_validated"] is False
    assert gate["final_oos_verdict_allowed"] is False


# ── scenario 7: ACCEPTED without source_event → INTEGRITY_CONFLICT ──


def test_accepted_without_source_event_is_integrity_conflict(conn):
    insert_source(conn, source_row(key="d" * 64))

    # A processed ACCEPTED row with no research source_event at all.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research_me_entry_quality.processed_event "
            "(event_key, experiment_id, processing_status) "
            "VALUES (%s, %s, 'ACCEPTED')",
            ("d" * 64, EXPERIMENT_ID),
        )
    conn.commit()

    audit = audit_source_integrity(conn)

    assert audit["status"] == "INTEGRITY_CONFLICT"
    assert audit["accepted_without_source"] == 1


# ── scenario 8: research event without V3 source event ──────────────


def test_orphan_research_event_is_integrity_conflict(conn):
    insert_source(conn, source_row())

    # Research event whose original V3 event does not exist.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research_me_entry_quality.source_event ("
            "event_key, experiment_id, symbol, signal_time, "
            "source_captured_at, source_created_at, activation_freeze_ts, "
            "source_scanner, source_direction, lineage, lineage_hash"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "e" * 64, EXPERIMENT_ID, "BTCUSDT",
                FREEZE + timedelta(minutes=5),
                FREEZE + timedelta(minutes=5),
                FREEZE + timedelta(minutes=5),
                FREEZE,
                SOURCE_SCANNER, "LONG", "{}", "x" * 64,
            ),
        )
    conn.commit()

    audit = audit_source_integrity(conn)

    assert audit["status"] == "INTEGRITY_CONFLICT"
    assert audit["orphan_research_events"] == 1


# ── scenario 9: fresh unprocessed event → COVERAGE_LAG ──────────────


def test_fresh_unprocessed_event_is_coverage_lag(conn):
    insert_source(conn, source_row())

    audit = audit_source_integrity(conn, gap_grace_seconds=900)

    assert audit["status"] == "COVERAGE_LAG"
    assert audit["unprocessed"] == 1
    assert audit["unprocessed_stale"] == 0


# ── scenario 10: persistently missing event → COVERAGE_GAP ──────────


def test_stale_unprocessed_event_is_coverage_gap(conn):
    insert_source(
        conn,
        source_row(created_at=FREEZE + timedelta(minutes=1)),
    )

    audit = audit_source_integrity(
        conn,
        gap_grace_seconds=60,
        as_of=datetime.now(timezone.utc),
    )

    assert audit["status"] == "COVERAGE_GAP"
    assert audit["unprocessed_stale"] == 1

    # Escalation: a persistent gap blocks the contour.
    result = new_observer(conn, coverage_gap_grace_seconds=60).run_once()
    assert result.status == "BLOCKED"
    assert result.blocked_reason == "COVERAGE_GAP"


# ── scenario 11: observer restart → no duplicates ───────────────────


def test_restart_creates_no_duplicates(conn):
    insert_source(conn, source_row())

    first = new_observer(conn).run_once()
    assert first.events_inserted == 1

    # A brand-new observer instance over the same durable ledgers.
    second = new_observer(conn).run_once()
    assert second.events_inserted == 0

    events = fetch_all(
        conn,
        "SELECT event_key FROM research_me_entry_quality.source_event",
    )
    assert len(events) == 1


# ── scenario 12: batch error → no partially written data ────────────


def test_batch_error_leaves_no_partial_data(conn):
    insert_source(conn, source_row(key="1" * 64))
    insert_source(conn, source_row(key="2" * 64, symbol="BTCUSDT"))

    class ExplodingRepository(PgObserverRepository):
        def save_event(self, event):
            if event.event_key == "2" * 64:
                raise RuntimeError("INJECTED_DB_ERROR")
            return super().save_event(event)

    with pytest.raises(RuntimeError, match="INJECTED_DB_ERROR"):
        EntryQualityObserver(ExplodingRepository(conn)).run_once()

    conn.rollback()

    for table in ("source_event", "processed_event", "source_health"):
        rows = fetch_all(
            conn,
            f"SELECT COUNT(*) FROM research_me_entry_quality.{table}",
        )
        assert rows[0][0] == 0, f"partial data found in {table}"


# ── scenario 14: upstream coverage unknown → no SOURCE_VALIDATED ────


def test_clean_ledger_never_yields_full_source_validated(conn):
    insert_source(conn, source_row())
    result = new_observer(conn).run_once()
    assert result.status == "OK"

    audit = audit_source_integrity(conn)
    assert audit["status"] == "SOURCE_COVERAGE_VALIDATED"
    assert audit["ledger_coverage"] == "V3_LEDGER_COVERAGE_VALIDATED"
    assert audit["upstream_source_coverage"] == (
        "UPSTREAM_SOURCE_COVERAGE_UNPROVEN"
    )
    assert audit["full_source_validated"] is False

    gate = verdict_gate(contour_state="ACTIVE", audit=audit)
    assert gate["source_validated"] is False
    assert gate["source_coverage_status"] == "SOURCE_COVERAGE_UNKNOWN"
    assert gate["final_oos_verdict_allowed"] is False


def test_missing_freeze_never_validates_coverage(conn):
    # The pre-freeze state (activation row without freeze_ts) is reached
    # by re-creating the unfrozen activation row — never by mutating a
    # frozen one (IMMUTABLE_FREEZE_TS forbids that by design).
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM research_me_entry_quality.activation "
            "WHERE experiment_id = %s",
            (EXPERIMENT_ID,),
        )
        cur.execute(
            "INSERT INTO research_me_entry_quality.activation "
            "(experiment_id, status, freeze_ts) "
            "VALUES (%s, 'READY', NULL)",
            (EXPERIMENT_ID,),
        )
    conn.commit()

    audit = audit_source_integrity(conn)

    assert audit["status"] == "SOURCE_COVERAGE_UNKNOWN"
    assert audit["reason"] == "MISSING_FREEZE_TS"


# ── scenario 15: BLOCKED → no further eligible capture ──────────────


def test_blocked_contour_captures_nothing_more(conn):
    insert_source(conn, source_row(key="1" * 64))
    row = source_row(key="2" * 64, symbol="BTCUSDT")
    row["lineage"]["source_features"]["rsi_14"] = None
    insert_source(conn, row)

    first = new_observer(conn).run_once()
    assert first.status == "BLOCKED"

    # New source keeps arriving; a blocked contour must not capture it.
    insert_source(conn, source_row(key="3" * 64, symbol="SOLUSDT"))

    second = new_observer(conn).run_once()
    assert second.status == "BLOCKED"
    assert second.blocked_reason == "INVALID_SOURCE_EVENT"

    events = fetch_all(
        conn,
        "SELECT event_key FROM research_me_entry_quality.source_event",
    )
    assert [e[0] for e in events] == []

    states = fetch_all(
        conn,
        "SELECT state, reason_code "
        "FROM research_me_entry_quality.observer_state_event ORDER BY id",
    )
    assert states[-1] == ("BLOCKED", "INVALID_SOURCE_EVENT")


# ── scenario 16: recovery without freeze reset or backfill ──────────


def test_recovery_preserves_freeze_and_backfills_nothing(conn):
    bad = source_row(key="2" * 64, symbol="BTCUSDT")
    bad["lineage"]["source_features"]["rsi_14"] = 999.0
    insert_source(conn, bad)
    # A pre-freeze row: must NEVER be backfilled after recovery.
    pre_freeze = source_row(
        key="3" * 64,
        symbol="SOLUSDT",
        signal_time=FREEZE - timedelta(minutes=5),
    )
    pre_freeze["created_at"] = FREEZE - timedelta(minutes=5)
    pre_freeze["captured_at"] = FREEZE - timedelta(minutes=5)
    insert_source(conn, pre_freeze)

    assert new_observer(conn).run_once().status == "BLOCKED"

    # Administrative recovery contour (deploy/sql/296_observer_recovery.sql).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research_me_entry_quality.observer_state_event "
            "(experiment_id, state, reason_code, details) "
            "VALUES (%s, 'RESUMED', 'MANUAL_RECOVERY', '{}')",
            (EXPERIMENT_ID,),
        )
    conn.commit()

    insert_source(conn, source_row(key="1" * 64))
    result = new_observer(conn).run_once()

    assert result.status == "OK"
    assert result.events_inserted == 1

    activation = fetch_all(
        conn,
        "SELECT freeze_ts FROM research_me_entry_quality.activation",
    )
    assert activation[0][0] == FREEZE

    events = fetch_all(
        conn,
        "SELECT event_key FROM research_me_entry_quality.source_event",
    )
    assert [e[0] for e in events] == ["1" * 64]


# ── repository conflict handling (P2: conflicting reprocessing) ─────


def test_conflicting_duplicate_payload_raises(conn):
    repo = PgObserverRepository(conn)

    def build_event(lineage_identity: str) -> ValidatedSourceEvent:
        row = source_row()
        row["lineage"]["source_identity"] = lineage_identity
        from app.research.me_entry_quality_v1.contracts import (
            validate_source_event,
        )
        return validate_source_event(row, freeze_ts=FREEZE)

    first = build_event("identity-a")
    assert repo.save_event(first) == "INSERTED"

    # Same event_key, different payload → deterministic conflict.
    with pytest.raises(SourceIntegrityError, match="EVENT_KEY_LINEAGE_CONFLICT"):
        repo.save_event(build_event("identity-b"))


# ── P2: activation and source immutability guards ───────────────────


def test_freeze_ts_is_immutable_and_source_append_only(conn):
    from psycopg2 import errors

    with pytest.raises(errors.RaiseException, match="IMMUTABLE_FREEZE_TS"):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE research_me_entry_quality.activation "
                "SET freeze_ts = %s WHERE experiment_id = %s",
                (FREEZE + timedelta(days=1), EXPERIMENT_ID),
            )
    conn.rollback()

    insert_source(conn, source_row())
    new_observer(conn).run_once()

    with pytest.raises(errors.RaiseException, match="SOURCE_EVENT_IMMUTABLE"):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM research_me_entry_quality.source_event"
            )
    conn.rollback()

    with pytest.raises(errors.RaiseException, match="OBSERVER_STATE_IMMUTABLE"):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO research_me_entry_quality.observer_state_event "
                "(experiment_id, state, reason_code) "
                "VALUES (%s, 'BLOCKED', 'X')",
                (EXPERIMENT_ID,),
            )
            cur.execute(
                "DELETE FROM research_me_entry_quality.observer_state_event"
            )
    conn.rollback()
