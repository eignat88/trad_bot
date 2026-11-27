"""Issue #324 / H1-E — C.1/C.2 offline integration tests on an ISOLATED
test PostgreSQL (never a working database).

Gate: the tests SKIP cleanly when no isolated test PostgreSQL is
reachable.  Target: a scratch database created and dropped by the tests
themselves; the reviewed DDL proposal is applied there and ONLY there.

Durable-safety matrix covered: reader pagination, cursor + restart,
bounded ceiling, backlog > cycle limit, commit failure rollback,
DEFERRED + restart durability, concurrent redelivery exactly-once,
redelivery-after-restart no-duplication, pre-freeze rejection (store AND
direct-write trigger), freeze / fingerprint fail-closed behaviour.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.research.me_entry_quality_h1e_v1.capture import (
    CaptureConfig,
    CaptureCursor,
    LedgerWrite,
    PendingRetry,
    PendingRetryUpdate,
    run_capture_cycle,
)
from app.research.me_entry_quality_h1e_v1.classification import (
    STATUS_CLASSIFIED,
    STATUS_DEFERRED,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    H1EFreeze,
    admit_source_row,
)
from app.research.me_entry_quality_h1e_v1.protocol import EXPERIMENT_ID
from app.research.me_entry_quality_h1e_v1.ledger import (
    ActivationFingerprintMismatch,
    FreezeTsMismatch,
    LedgerIdentityConflict,
    PreFreezeWrite,
    SimulatedCrash,
)
from tests.h1e_v1_fixtures import (
    FIXTURE_ASOF_US,
    FIXTURE_FREEZE_US,
    make_classification_record,
    make_source_row,
)

SCRATCH_DB = "h1e_phasec_test"

DSN_DEFAULT = "host=localhost port=5432 user=postgres dbname=postgres"
DSN_ADMIN = os.environ.get("H1E_TEST_PG_DSN", DSN_DEFAULT)
DSN_SCRATCH = DSN_ADMIN.rsplit("=", 1)[0] + "=" + SCRATCH_DB

DDL_PROPOSAL = (
    Path(__file__).resolve().parents[1]
    / "sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql"
)

SOURCE_TABLE_DDL = """
CREATE SCHEMA IF NOT EXISTS research_me_upstream_coverage;
CREATE TABLE IF NOT EXISTS research_me_upstream_coverage.source_event (
    source_event_key text PRIMARY KEY,
    experiment_id text,
    source_identity text,
    symbol text,
    signal_time timestamptz,
    captured_at timestamptz,
    activation_freeze_ts timestamptz,
    source_fingerprint text,
    lineage jsonb,
    lineage_hash text
)
"""

FREEZE_US = FIXTURE_FREEZE_US
FINGERPRINT = "fp-test-324"


def _connect(dsn: str):
    import psycopg2

    return psycopg2.connect(dsn)


def _usable() -> bool:
    try:
        connection = _connect(DSN_ADMIN)
        connection.close()
        return True
    except Exception:  # noqa: BLE001 — unreachable test DB is a clean skip
        return False


pytestmark = pytest.mark.skipif(
    not _usable(), reason="isolated test PostgreSQL unavailable")


@pytest.fixture(scope="module")
def pg_dsn():
    """Create the scratch database + reviewed DDL ONCE per module."""
    connection = _connect(DSN_ADMIN)
    connection.autocommit = True
    with connection.cursor() as statement:
        statement.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
        statement.execute(f"CREATE DATABASE {SCRATCH_DB}")
    connection.close()

    scratch = _connect(DSN_SCRATCH)
    ddl = DDL_PROPOSAL.read_text(encoding="utf-8")
    ddl = ddl.replace("BEGIN;", "").replace("COMMIT;", "")
    with scratch.cursor() as statement:
        statement.execute(ddl)
        statement.execute(SOURCE_TABLE_DDL)
    scratch.commit()
    scratch.close()
    yield DSN_SCRATCH

    connection = _connect(DSN_ADMIN)
    connection.autocommit = True
    with connection.cursor() as statement:
        statement.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    connection.close()


@pytest.fixture()
def activation(pg_dsn):
    """One APPROVED activation row (administrative contour, test only)."""
    from app.research.me_entry_quality_h1e_infra_v1.pg_source_reader import (
        us_to_datetime,
    )

    freeze = replace(
        H1EFreeze.synthetic_fixture(FREEZE_US),
        protocol_fingerprint=FINGERPRINT,
    )
    connection = _connect(pg_dsn)
    with connection.cursor() as statement:
        statement.execute(
            "INSERT INTO research_me_entry_quality_h1e.h1e_activation "
            "(experiment_id, status, freeze_ts, protocol_fingerprint, "
            "approval_ref, approved_by) VALUES (%s, 'APPROVED', %s, %s, "
            "'test-approval', 'tests') "
            "ON CONFLICT (experiment_id) DO NOTHING",
            (EXPERIMENT_ID, us_to_datetime(FREEZE_US), FINGERPRINT),
        )
    connection.commit()
    connection.close()
    return freeze


# ─── collaborators ─────────────────────────────────────────────────────────


def _connection_factory(dsn: str):
    def factory():
        connection = _connect(dsn)
        connection.autocommit = False
        return connection

    return factory


def _store(dsn: str, activation, *, worker_id: str):
    from app.research.me_entry_quality_h1e_infra_v1.pg_ledger_store import (
        PgH1EStore,
    )

    return PgH1EStore(
        _connection_factory(dsn),
        activation=activation,
        worker_id=worker_id,
    )


def _reader(dsn: str):
    from app.research.me_entry_quality_h1e_infra_v1.pg_source_reader import (
        PgSourceReader,
    )

    return PgSourceReader(_connection_factory(dsn))


def _insert_source_rows(dsn: str, rows) -> None:
    connection = _connect(dsn)
    with connection.cursor() as statement:
        for row in rows:
            statement.execute(
                "INSERT INTO research_me_upstream_coverage.source_event "
                "(source_event_key, experiment_id, source_identity, symbol, "
                "signal_time, captured_at, activation_freeze_ts, "
                "source_fingerprint, lineage, lineage_hash) VALUES (%s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (source_event_key) DO NOTHING",
                (
                    row["source_event_key"],
                    EXPERIMENT_ID,
                    row["source_identity"],
                    row["symbol"],
                    row["signal_time"],
                    row["captured_at"],
                    row["activation_freeze_ts"],
                    row["source_fingerprint"],
                    json.dumps(row["lineage"]),
                    row["lineage_hash"],
                ),
            )
    connection.commit()
    connection.close()


def _window_cursor(window_us: int) -> CaptureCursor:
    """Start cursor at the test's own captured window (strictly before it)."""
    return CaptureCursor(window_us - 1, "\uffff")


def _source_rows(prefix: str, count: int, *, window_us: int):
    """Rows in a DISJOINT captured_at window (no cross-test interleaving)."""
    rows = []
    for index in range(count):
        rows.append(make_source_row(
            identity=f"ident:{prefix}-{index:03d}",
            signal_time_us=window_us + 100_000 + index * 1_000_000,
            captured_us=window_us + index * 1_000_000,
            candle_open_ms=1_700_000_000_000,
        ))
    return rows


def _write(activation, label: str, *, captured_us: int,
           event_kind=STATUS_CLASSIFIED, attempt_count=1,
           fingerprint=None, freeze_ts_us=None):
    row = make_source_row(
        identity=f"ident:{label}",
        signal_time_us=FIXTURE_ASOF_US,
        captured_us=captured_us,
        candle_open_ms=1_700_000_000_000,
    )
    decision = admit_source_row(row, freeze=activation)
    assert decision.accepted
    event = decision.event
    record = make_classification_record(event)
    fp = fingerprint or FINGERPRINT
    return LedgerWrite(
        ledger_identity=f"{event.source_event_key}|{fp}",
        source_event_key=event.source_event_key,
        protocol_fingerprint=fp,
        event_kind=event_kind,
        preserved_hash=event.preserved_hash,
        payload=event.to_dict(),
        classification=record.to_dict(),
        attempt_count=attempt_count,
        captured_at_us=captured_us,
        activation_id=EXPERIMENT_ID,
        h1e_freeze_ts_us=(
            freeze_ts_us if freeze_ts_us is not None else FREEZE_US),
    )


def _count(dsn: str, sql: str, params=()) -> int:
    connection = _connect(dsn)
    with connection.cursor() as statement:
        statement.execute(sql, params)
        result = statement.fetchone()[0]
    connection.close()
    return int(result)


# ─── C.1 reader: pagination + cursor + ceiling ─────────────────────────────


def test_source_reader_pagination_preserves_every_event(pg_dsn):
    window = FREEZE_US + 10_000_000_000
    rows = _source_rows("pg-page", 25, window_us=window)
    _insert_source_rows(pg_dsn, rows)
    reader = _reader(pg_dsn)
    seen: list[str] = []
    cursor = _window_cursor(window)
    while True:
        batch = reader.read_after(cursor, limit=10)
        if not batch:
            break
        assert len(batch) <= 10
        seen.extend(doc["source_event_key"] for doc in batch)
        cursor = CaptureCursor.after_rows(batch)
    # strict capture order, every event exactly once
    assert seen == [row["source_event_key"] for row in rows]
    assert len(set(seen)) == 25


def test_source_reader_cursor_restart_and_ceiling(pg_dsn):
    window = FREEZE_US + 20_000_000_000
    rows = _source_rows("pg-restart", 6, window_us=window)
    _insert_source_rows(pg_dsn, rows)

    def _us(value) -> int:
        return int(value.timestamp() * 1_000_000)

    reader = _reader(pg_dsn)
    first = reader.read_after(_window_cursor(window), limit=4)
    assert [d["source_event_key"] for d in first] == [
        r["source_event_key"] for r in rows[:4]]
    cursor = CaptureCursor(_us(first[-1]["captured_at"]),
                           first[-1]["source_event_key"])
    # strict > boundary: the cursor row itself never comes back
    second = reader.read_after(cursor, limit=10)
    assert [d["source_event_key"] for d in second] == [
        r["source_event_key"] for r in rows[4:]]
    # a fresh reader instance (process restart) resumes identically
    reader2 = _reader(pg_dsn)
    assert reader2.read_after(cursor, limit=10) == second
    # bounded ceiling: rows captured after the ceiling are never returned
    ceiling = _us(rows[1]["captured_at"])
    capped = reader2.read_after(_window_cursor(window), limit=10,
                                ceiling_captured_us=ceiling)
    assert [d["source_event_key"] for d in capped] == [
        r["source_event_key"] for r in rows[:2]]


# ─── C.2 store: atomicity / durability / exactly-once ──────────────────────


def test_backlog_larger_than_cycle_limit_is_processed_in_bounded_cycles(
        pg_dsn, activation):
    window = FREEZE_US + 30_000_000_000
    rows = _source_rows("pg-backlog", 25, window_us=window)
    _insert_source_rows(pg_dsn, rows)
    worker = "pg-backlog"
    store = _store(pg_dsn, activation, worker_id=worker)
    reader = _reader(pg_dsn)

    def classifier(event, prior_deferrals=0):
        return make_classification_record(event)

    config = CaptureConfig(batch_limit=10, allow_fixture_freeze=True)
    run_capture_cycle(reader=reader, store=store, classifier=classifier,
                      freeze=activation, cursor=_window_cursor(window),
                      config=config)
    done = _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_classification")
    assert done == 10  # strictly bounded: 25 -> 10 now
    run_capture_cycle(reader=reader, store=store, classifier=classifier,
                      freeze=activation, cursor=store.checkpoint, config=config)
    done = _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_classification")
    assert done == 20
    run_capture_cycle(reader=reader, store=store, classifier=classifier,
                      freeze=activation, cursor=store.checkpoint, config=config)
    assert _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_classification") == 25
    # every identity finalized exactly once
    assert _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_status_event") == 25


def test_commit_failure_rolls_back_the_whole_batch(pg_dsn, activation):
    store = _store(pg_dsn, activation, worker_id="pg-crash")
    write = _write(activation, "pg-crash-1", captured_us=FREEZE_US + 1_000)
    retry = PendingRetry(
        ledger_identity=write.ledger_identity,
        source_event_key=write.source_event_key,
        attempt_count=1,
        deferred_reason="H1E_TEST_DEFERRAL",
        next_attempt_us=FREEZE_US + 2_000,
        preserved_hash=write.preserved_hash,
    )
    store.fault_inject_commit = True
    with pytest.raises(SimulatedCrash):
        store.commit_batch(
            [write], CaptureCursor(FREEZE_US + 1_000, "\uffff"),
            pending=[PendingRetryUpdate(write.ledger_identity, retry)])
    # NOTHING from the failed batch exists (atomic rollback)
    assert _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_event "
                  "WHERE ledger_identity = %s",
                  (write.ledger_identity,)) == 0
    assert _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_status_event "
                  "WHERE ledger_identity = %s",
                  (write.ledger_identity,)) == 0
    assert _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_pending_retry") == 0
    assert _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_checkpoint "
                  "WHERE worker_id = %s", ("pg-crash",)) == 0


def test_deferred_row_survives_restart(pg_dsn, activation):
    worker = "pg-defer"
    store = _store(pg_dsn, activation, worker_id=worker)
    row = make_source_row(
        identity="ident:pg-defer-1",
        signal_time_us=FIXTURE_ASOF_US,
        captured_us=FREEZE_US + 2_000,
        candle_open_ms=1_700_000_000_000,
    )
    event = admit_source_row(row, freeze=activation).event
    write = LedgerWrite(
        ledger_identity=f"{event.source_event_key}|{FINGERPRINT}",
        source_event_key=event.source_event_key,
        protocol_fingerprint=FINGERPRINT,
        event_kind=STATUS_DEFERRED,
        preserved_hash=event.preserved_hash,
        payload=event.to_dict(),
        classification=make_classification_record(
            event, status=STATUS_DEFERRED).to_dict(),
        attempt_count=1,
        captured_at_us=FREEZE_US + 2_000,
        activation_id=EXPERIMENT_ID,
        h1e_freeze_ts_us=FREEZE_US,
    )
    retry = PendingRetry(
        ledger_identity=write.ledger_identity,
        source_event_key=write.source_event_key,
        attempt_count=1,
        deferred_reason="H1E_HTF_TIMEOUT",
        next_attempt_us=FREEZE_US + 9_000,
        preserved_hash=write.preserved_hash,
    )
    store.commit_batch([write], CaptureCursor(FREEZE_US + 2_000, "\uffff"),
                       pending=[PendingRetryUpdate(write.ledger_identity, retry)])
    # fresh objects = process restart; the deferred row is DURABLE
    store2 = _store(pg_dsn, activation, worker_id=worker)
    assert store2.pending_retries() == (retry,)
    entry = store2.lookup(write.ledger_identity)
    assert entry is not None
    assert entry.journal[-1]["event_kind"] == STATUS_DEFERRED
    # the retry settles CLASSIFIED and leaves the durable queue atomically
    final = _write(activation, "pg-defer-1", captured_us=FREEZE_US + 3_000,
                   event_kind=STATUS_CLASSIFIED, attempt_count=2)
    store2.commit_batch([final], CaptureCursor(FREEZE_US + 3_000, "\uffff"),
                        pending=[PendingRetryUpdate(final.ledger_identity, None)])
    assert store2.pending_retries() == ()
    assert store2.lookup(final.ledger_identity).journal[-1]["event_kind"] == (
        STATUS_CLASSIFIED)


def test_concurrent_redelivery_is_exactly_once(pg_dsn, activation):
    write = _write(activation, "pg-conc-1", captured_us=FREEZE_US + 4_000)
    errors: list[BaseException] = []

    def deliver():
        local = _store(pg_dsn, activation, worker_id="pg-conc")
        try:
            local.commit_batch([write], CaptureCursor(FREEZE_US + 4_000, "z"))
        except BaseException as exc:  # noqa: BLE001 — recorded for the assert
            errors.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_event "
                  "WHERE ledger_identity = %s",
                  (write.ledger_identity,)) == 1
    # never finalized twice
    assert _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_status_event "
                  "WHERE ledger_identity = %s AND event_kind IN "
                  "('CLASSIFIED', 'DEFERRED')",
                  (write.ledger_identity,)) == 1
    assert _count(pg_dsn, "SELECT COUNT(*) FROM "
                  "research_me_entry_quality_h1e.h1e_classification "
                  "WHERE ledger_identity = %s",
                  (write.ledger_identity,)) == 1


def test_redelivery_after_restart_never_duplicates(pg_dsn, activation):
    worker = "pg-replay"
    store = _store(pg_dsn, activation, worker_id=worker)
    write = _write(activation, "pg-replay-1", captured_us=FREEZE_US + 5_000)
    store.commit_batch([write], CaptureCursor(FREEZE_US + 5_000, "z"))
    # at-least-once source: the SAME write is re-served after a restart
    store2 = _store(pg_dsn, activation, worker_id=worker)
    store2.commit_batch([write], CaptureCursor(FREEZE_US + 5_000, "z"))
    entry = store2.lookup(write.ledger_identity)
    assert entry is not None
    assert len(entry.journal) == 1
    assert entry.preserved_hash == write.preserved_hash


# ─── fail-closed edges ─────────────────────────────────────────────────────


def test_pre_freeze_event_rejected_by_store_and_by_trigger(
        pg_dsn, activation):
    store = _store(pg_dsn, activation, worker_id="pg-prefreeze")
    write = _write(activation, "pg-prefreeze-1", captured_us=FREEZE_US + 6_000)
    # tamper the payload to a pre-freeze signal time (evil delivery)
    evil = replace(write, payload={**write.payload,
                                   "signal_time_us": FREEZE_US})
    with pytest.raises(PreFreezeWrite):
        store.commit_batch([evil], CaptureCursor(FREEZE_US + 6_000, "z"))
    # direct-write bypass: the DATABASE trigger refuses too
    connection = _connect(pg_dsn)
    with pytest.raises(Exception) as caught:
        with connection.cursor() as statement:
            statement.execute(
                "INSERT INTO research_me_entry_quality_h1e.h1e_event "
                "(ledger_identity, source_event_key, h1e_protocol_fingerprint, "
                "h1e_activation_id, source_identity, symbol, signal_time_us, "
                "signal_time, signal_time_ms, captured_at_us, "
                "source_freeze_ts_us, h1e_freeze_ts_us, lineage_hash, "
                "source_fingerprint, preserved_hash, payload) VALUES (%s, %s, "
                "%s, %s, %s, %s, %s, "
                "(%s::bigint * INTERVAL '1 microsecond' "
                "+ TIMESTAMP WITH TIME ZONE 'epoch'), %s, %s, %s, %s, %s, %s, "
                "%s, %s::jsonb)",
                (
                    "pg-prefreeze-bypass|" + FINGERPRINT,
                    "pg-prefreeze-bypass", FINGERPRINT, EXPERIMENT_ID,
                    "ident:pg-prefreeze-bypass", "BTCUSDT",
                    FREEZE_US,
                    FREEZE_US, FREEZE_US // 1000,
                    FREEZE_US + 6_000, FREEZE_US, FREEZE_US,
                    "hash", "fp", "preserved", "{}",
                ),
            )
    connection.close()
    assert "H1E_PRE_FREEZE_EVENT" in str(caught.value)


def test_freeze_and_fingerprint_fail_closed(pg_dsn, activation):
    store = _store(pg_dsn, activation, worker_id="pg-fp")
    wrong_fp = _write(activation, "pg-fp-1", captured_us=FREEZE_US + 7_000,
                      fingerprint="attacker-fp")
    with pytest.raises(ActivationFingerprintMismatch):
        store.commit_batch([wrong_fp], CaptureCursor(FREEZE_US + 7_000, "z"))
    wrong_freeze = _write(activation, "pg-fp-2", captured_us=FREEZE_US + 7_000,
                          freeze_ts_us=FREEZE_US + 1)
    with pytest.raises(FreezeTsMismatch):
        store.commit_batch([wrong_freeze], CaptureCursor(FREEZE_US + 7_000, "z"))
    # identity collision with a different preserved hash is fail-closed
    good = _write(activation, "pg-fp-3", captured_us=FREEZE_US + 7_000)
    store.commit_batch([good], CaptureCursor(FREEZE_US + 7_000, "z"))
    forged = replace(good, preserved_hash="tampered-hash")
    with pytest.raises(LedgerIdentityConflict):
        store.commit_batch([forged], CaptureCursor(FREEZE_US + 7_000, "z"))
