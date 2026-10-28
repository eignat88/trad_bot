"""ME_SHORT_A_PHASE1_LINEAGE_CONSISTENCY_FIX_V1 (Issues #202 / #206 / #208).

Regression matrix for the confirmed Phase 1 linkage inconsistency class:

* KORUUSDT 2026-10-28 05:35:14 UTC and DOGEUSDT 2026-10-28 08:40:52 UTC
  were written with ``link_status = 'LINKED'`` while ``phase1_linked =
  FALSE`` / ``phase1_event_key = NULL``, even though a Phase 1 observation
  with the SAME ``event_key`` existed. The Phase 1 snapshot is committed by
  a different process/transaction than the runtime's lineage write, so at
  READ COMMITTED the linkage lookup can miss it and append-only +
  ``ON CONFLICT DO NOTHING`` made the false record permanent.

The fix under test:

* the Phase 1 linkage lookup is retried a BOUNDED number of times before
  the irreversible write (READ COMMITTED commit-visibility race);
* a confirmed Phase 1 row -> the correct LINKED lineage (unchanged);
* a missing Phase 1 row -> the explicit ``PHASE1_PENDING`` research
  exception — never a false LINKED relation, never an unbounded Source
  Intake stall;
* a SELECT error stays fail-closed (retried, never written as "absent");
* a write that neither inserts nor already exists is retried, never lost.

Immutability: the existing append-only rows (including the KORUUSDT and
DOGEUSDT incident rows) are never updated or deleted; incomplete lineage is
excluded from the Issue #206 eligible cohort instead of being repaired.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from app.research import me_short_a_forward_outcome_lineage_v1 as lineage
from app.research import me_short_a_forward_outcome_runtime_v1 as mod
from app.research.me_short_a_ref_exec_alignment_audit_v1 import (
    OK as LINEAGE_OK,
    eligible_for_decision,
    validate_forward_event,
)

import test_me_short_a_forward_outcome_runtime_v1 as base

ROOT = Path(__file__).resolve().parent.parent

# The two confirmed incidents of the defect class (task specification).
KORUUSDT_SIGNAL = "2026-10-28T05:35:14+00:00"
DOGEUSDT_SIGNAL = "2026-10-28T08:40:52+00:00"


@pytest.fixture(autouse=True)
def _capture_flags(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_LINEAGE_CAPTURE_ENABLED", "1")
    monkeypatch.setenv("ME_SHORT_A_SOURCE_INTAKE_ENABLED", "1")
    yield


# ── a connection double with an explicit commit-visibility model ────────


class RaceConn(base.FakeConnection):
    """READ COMMITTED / autocommit=False commit-visibility model.

    The Phase 1 writer is a separate transaction on a separate connection:
    its row is INVISIBLE to this connection's SELECTs until it COMMITs.
    ``reveal_on_attempt`` models the concurrent commit landing between two
    of the runtime's READ-ONLY linkage lookups.
    """

    def __init__(self, *, reveal_on_attempt=None, committed_phase1=None,
                 **kwargs):
        super().__init__(**kwargs)
        self.phase1_lookups: dict[str, int] = {}
        self.reveal_on_attempt = reveal_on_attempt
        self.committed_phase1 = committed_phase1

    def route_select(self, head, params):
        if "ME_SHORT_A_FILL_FEASIBILITY_V1" in head:
            key = params[0] if params else None
            self.phase1_lookups[key] = self.phase1_lookups.get(key, 0) + 1
            if (self.reveal_on_attempt is not None
                    and self.committed_phase1 is not None
                    and self.phase1_lookups[key] >= self.reveal_on_attempt):
                # the concurrent Phase 1 transaction has committed now
                if self.committed_phase1 not in self.phase1_rows:
                    self.phase1_rows.append(self.committed_phase1)
        return super().route_select(head, params)


class FailingLineageWriteConn(base.FakeConnection):
    """The guarded lineage INSERT fails (e.g. CHECK violation before the
    migration is applied); every other statement works."""

    def note_write(self, sql, params):
        if sql.startswith(f"INSERT INTO {base.LINEAGE_TABLE}"):
            self.inserts.append((sql, params))   # the attempt is recorded
            raise RuntimeError("lineage insert failed")
        return super().note_write(sql, params)


def _key():
    return lineage.event_key(
        base.SYMBOL, lineage.DIRECTION, base.SIGNAL,
        base.REF, base.STOP, base.TARGET)


def _run(conn, candidates, *, cfg=None, now=base.NOW):
    runtime, _ = base.build(conn=conn, cfg=cfg, candidates=candidates, now=now)
    runtime.run_cycle()
    return runtime


def _lineage_payload(conn, index=0):
    return base._payload(conn.persisted_for(base.LINEAGE_TABLE)[index])


# ── Phase 1 available -> correct linked lineage ────────────────────────


def test_phase1_available_creates_the_correct_linked_lineage():
    candidate = base.make_candidate()
    key = _key()
    conn = base.FakeConnection()
    conn.phase1_rows = [[key, [42]]]
    _run(conn, [candidate])
    payload = _lineage_payload(conn)
    assert payload["link_status"] == "LINKED"
    assert payload["phase1_linked"] is True
    assert payload["phase1_event_key"] == key
    assert payload["phase1_observation_id"] == 42
    checks = json.loads(payload["linkage_checks"])
    assert checks["phase1_observed"] is True
    assert checks["phase1_link_attempts"] == 1    # no retry was needed


# ── Phase 1 temporarily absent: bounded retry wins the commit race ─────


def test_temporarily_absent_phase1_is_linked_after_the_bounded_retry():
    """READ COMMITTED race: the Phase 1 transaction commits after the
    runtime's first lookup. The bounded retry observes it and the lineage
    record is the correct LINKED one — the KORUUSDT/DOGEUSDT outcome."""
    candidate = base.make_candidate()
    key = _key()
    conn = RaceConn(reveal_on_attempt=2, committed_phase1=[key, [42]])
    _run(conn, [candidate])
    payload = _lineage_payload(conn)
    assert payload["link_status"] == "LINKED"
    assert payload["phase1_linked"] is True
    assert payload["phase1_event_key"] == key
    checks = json.loads(payload["linkage_checks"])
    assert checks["phase1_link_attempts"] == 2
    assert conn.phase1_lookups[key] == 2          # bounded, stopped on find
    # autocommit=False: every append-only writer commits its own explicit
    # transaction — the concurrent Phase 1 writer's COMMIT is exactly what
    # makes its row visible to the runtime's next READ COMMITTED lookup.
    assert conn.commits >= 2


def test_retry_sleep_spacing_is_honoured_between_lookups():
    sleeps = []
    candidate = base.make_candidate()
    conn = RaceConn(reveal_on_attempt=3, committed_phase1=[_key(), [7]])
    cfg = replace(base.config(), phase1_link_retry_attempts=3,
                  phase1_link_retry_sleep_seconds=1.5)
    runtime = mod.ForwardOutcomeRuntime(
        conn,
        config=cfg,
        candle_source=mod.InMemoryCandleSource([]),
        candidate_source=lambda _since: [candidate],
        clock=base.clock_at(base.NOW),
        runner_id="runtime-under-test",
        sleep=sleeps.append,
    )
    runtime.run_cycle()
    assert sleeps == [1.5, 1.5]                   # attempts - 1 spacings
    assert _lineage_payload(conn)["link_status"] == "LINKED"


# ── Phase 1 never appears: observable exception, intake keeps flowing ──


def test_absent_phase1_never_produces_a_linked_record():
    candidate = base.make_candidate()
    conn = base.FakeConnection()
    conn.phase1_rows = []
    _run(conn, [candidate])
    payload = _lineage_payload(conn)
    assert payload["link_status"] == "PHASE1_PENDING"
    assert payload["quarantine_reason"] == "phase1_not_observed"
    assert payload["phase1_linked"] is False
    assert payload["phase1_event_key"] is None
    assert payload["phase1_observation_id"] is None
    checks = json.loads(payload["linkage_checks"])
    assert checks["phase1_observed"] is False
    assert checks["phase1_link_attempts"] == 3    # bounded retry exhausted


def test_retry_is_strictly_bounded_and_the_intake_keeps_flowing():
    """One absent Phase 1 snapshot costs a bounded number of lookups and
    never blocks the Source Intake: the next candidate on the same page is
    still handled in the same cycle."""
    first = base.make_candidate()
    second = base.make_candidate(
        signal_time=base.SIGNAL + timedelta(minutes=5),
        recent_high_window=tuple(base.make_window(
            base.SIGNAL + timedelta(minutes=5))))
    key2 = mod.event_key_for(second)
    conn = RaceConn()
    conn.phase1_rows = [[key2, [8]]]              # only the second event
    cfg = replace(base.config(), phase1_link_retry_attempts=3)
    runtime, _ = base.build(conn=conn, cfg=cfg, candidates=[first, second])
    runtime.run_cycle()

    key1 = _key()
    assert conn.phase1_lookups[key1] == 3         # bounded, then terminal
    assert conn.phase1_lookups[key2] == 1         # found immediately
    rows = {base._payload(r)["event_key"]: base._payload(r)
            for r in conn.persisted_for(base.LINEAGE_TABLE)}
    assert rows[key1]["link_status"] == "PHASE1_PENDING"
    assert rows[key2]["link_status"] == "LINKED"
    # the stream advanced past the unconfirmed event: both were handled
    assert runtime.metrics.lineages_recorded == 2


def test_phase1_pending_is_an_observable_research_exception():
    """The exception is observable: honest row state + diagnostics."""
    candidate = base.make_candidate()
    conn = base.FakeConnection()
    _run(conn, [candidate])
    payload = _lineage_payload(conn)
    checks = json.loads(payload["linkage_checks"])
    assert checks["phase1_observed"] is False
    assert checks["phase1_link_attempts"] >= 1
    assert checks["source_signal_present"] is True
    audit_sql = (ROOT / "sql" / "research"
                 / "me_short_a_forward_outcome_lineage_audit_v1.sql"
                 ).read_text(encoding="utf-8")
    assert "link_status = 'PHASE1_PENDING'" in audit_sql
    # the frozen status view keeps classifying an unconfirmed link as
    # MISSING_LINEAGE (migration 078 body is unchanged)
    m078 = (ROOT / "sql" / "migrations"
            / "078_me_short_a_forward_only_outcome_lineage_v1.sql"
            ).read_text(encoding="utf-8")
    assert "OR l.phase1_linked = FALSE" in m078


def test_record_event_lineage_never_claims_linked_without_phase1():
    conn = base.FakeConnection()
    assert lineage.record_event_lineage(
        conn, symbol=base.SYMBOL, signal_time=base.SIGNAL,
        reference_price=base.REF, stop_price=base.STOP,
        target_price=base.TARGET, phase1_link_attempts=3) is True
    params = conn.inserts[0][1]
    assert params[13] == "PHASE1_PENDING"
    assert params[14] == "phase1_not_observed"
    assert params[18] is None and params[19] is False and params[20] is None
    checks = json.loads(params[15])
    assert checks["phase1_observed"] is False
    assert checks["phase1_link_attempts"] == 3


# ── SELECT error: fail-closed, never flattened into "absent" ───────────


def test_select_error_is_fail_closed_and_recovered_on_the_next_cycle():
    candidate = base.make_candidate()
    key = _key()
    conn = base.FakeConnection()
    conn.fail_select = "FILL_FEASIBILITY"        # least-privilege error
    conn.phase1_rows = [[key, [42]]]
    runtime, _ = base.build(conn=conn, candidates=[candidate])
    runtime.run_cycle()
    # no lineage row at all: an ERROR is never recorded as an unconfirmed
    # link — that would be a permanent false record on an append-only table
    assert conn.persisted_for(base.LINEAGE_TABLE) == []
    assert runtime.metrics.errors >= 1

    conn.fail_select = None                       # transient error cleared
    runtime2, _ = base.build(conn=conn, candidates=[candidate])
    runtime2.run_cycle()
    payload = _lineage_payload(conn)
    assert payload["link_status"] == "LINKED"
    assert payload["phase1_linked"] is True


def test_failed_lineage_write_is_retried_not_silently_lost():
    candidate = base.make_candidate()
    key = _key()
    conn = FailingLineageWriteConn()
    conn.phase1_rows = [[key, [42]]]
    runtime, _ = base.build(conn=conn, candidates=[candidate])
    runtime.run_cycle()
    # the guarded INSERT failed AND the table has no row: fail closed —
    # the candidate is retried, never dropped, never falsely recorded
    assert conn.persisted_for(base.LINEAGE_TABLE) == []
    assert runtime.metrics.errors >= 1
    assert len(conn.inserts_for(base.LINEAGE_TABLE)) == 1


# ── redelivery / restart: exactly once, nothing lost ───────────────────


def test_redelivery_after_restart_never_duplicates_or_loses():
    candidate = base.make_candidate()
    key = _key()
    conn = base.FakeConnection()
    conn.phase1_rows = [[key, [42]]]
    _run(conn, [candidate])
    # restart: fresh process, same append-only tables, same stream re-read
    # from the immutable freeze_ts bound
    _run(conn, [candidate])
    for table in (base.SOURCE_TABLE, base.LINEAGE_TABLE, base.OUTCOME_TABLE):
        assert len(conn.persisted_for(table)) == 1, table


def test_restart_mid_retry_never_loses_the_candidate():
    """A process death between the candidate read and the lineage write
    leaves no row; the restarted runtime re-reads the same stream and
    completes the capture exactly once."""
    candidate = base.make_candidate()
    key = _key()
    conn = base.FakeConnection()
    conn.fail_select = "FILL_FEASIBILITY"
    _run(conn, [candidate])
    assert conn.persisted_for(base.LINEAGE_TABLE) == []

    conn.fail_select = None
    conn.phase1_rows = [[key, [7]]]
    _run(conn, [candidate])
    assert len(conn.persisted_for(base.LINEAGE_TABLE)) == 1
    assert _lineage_payload(conn)["link_status"] == "LINKED"


def test_late_phase1_commit_after_the_terminal_record_is_never_repaired():
    """The Phase 1 transaction commits only after the terminal record was
    written. The append-only row must stay exactly as written (no repair,
    no duplicate on re-delivery), while outcome advancement continues."""
    candidate = base.make_candidate()
    key = _key()
    conn = base.FakeConnection()                  # Phase 1 not visible yet
    _run(conn, [candidate])
    payload = _lineage_payload(conn)
    assert payload["link_status"] == "PHASE1_PENDING"

    conn.phase1_rows = [[key, [42]]]              # the late commit lands
    _run(conn, [candidate])                       # re-delivery of the event
    assert len(conn.persisted_for(base.LINEAGE_TABLE)) == 1
    again = _lineage_payload(conn)
    assert again["link_status"] == "PHASE1_PENDING"   # immutable
    assert again["phase1_linked"] is False
    # the event is not lost: its outcome still advances
    assert len(conn.persisted_for(base.OUTCOME_TABLE)) == 1


def test_no_update_delete_or_truncate_is_ever_issued():
    candidate = base.make_candidate()
    conn = base.FakeConnection()
    _run(conn, [candidate])
    _run(conn, [candidate])                       # re-delivery included
    assert conn.statements, "the flow must have issued statements"
    for sql, _ in conn.statements:
        head = sql.lstrip().upper()
        assert not head.startswith(("UPDATE", "DELETE", "TRUNCATE")), sql


# ── cohort #206: incomplete lineage is excluded, never repaired ────────


def _evidence_row(**overrides):
    row = {
        "event_key": "0" * 64,
        "symbol": "KORUUSDT",
        "direction": "SHORT",
        "signal_time": KORUUSDT_SIGNAL,
        "reference_price": "10.5",
        "stop_price": "10.6",
        "target_price": "10.2",
        "no_lookahead_flags": ["recent_high_window_closed_before_signal"],
        "source_quality_flags": None,
        "missing_fields": None,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("symbol,signal_time", [
    ("KORUUSDT", KORUUSDT_SIGNAL),
    ("DOGEUSDT", DOGEUSDT_SIGNAL),
])
def test_koru_doge_defect_class_stays_excluded_and_immutable(
        symbol, signal_time):
    """The confirmed incident rows (LINKED + phase1_linked = FALSE) are
    excluded from the eligible cohort by read-only evaluation — the rows
    themselves are never modified (append-only)."""
    evidence = _evidence_row(symbol=symbol, signal_time=signal_time)
    source = _evidence_row(symbol=symbol, signal_time=signal_time)
    lineage_row = _evidence_row(
        symbol=symbol, signal_time=signal_time,
        event_key_integrity=True, link_status="LINKED",
        phase1_event_key=None, phase1_linked=False,
        source_signal_present=True)
    verdict = validate_forward_event(
        evidence, source, lineage_row, None, base.NOW)
    assert verdict["eligible"] is False
    assert "PHASE1_NOT_LINKED" in verdict["reasons"]
    assert "PHASE1_EVENT_KEY_MISMATCH" in verdict["reasons"]


def test_phase1_pending_lineage_is_excluded_from_the_eligible_cohort():
    row = {
        "lineage_status": LINEAGE_OK,
        "lookahead_flags": None,
        "forward_validated": True,
        "forward_link_status": "PHASE1_PENDING",
        "forward_event_key_integrity": True,
        "forward_phase1_linked": False,
        "forward_outcome_status": "MATCHED_FINALIZED",
        "forward_is_final": True,
    }
    assert eligible_for_decision(row, require_forward=True) is False
    row["forward_link_status"] = "LINKED"
    assert eligible_for_decision(row, require_forward=True) is False
    row["forward_phase1_linked"] = True
    assert eligible_for_decision(row, require_forward=True) is True


# ── independent Source Intake capture is preserved ─────────────────────


@pytest.mark.parametrize("route", ["REGIME_FILTER_REJECTED", "SHADOW_CONTROL"])
def test_independent_capture_stays_intact_for_the_two_routes(route):
    """The Phase 1 linkage consistency fix must not gate the research-only
    Source Intake: a REGIME_FILTER_REJECTED / SHADOW_CONTROL candidate is
    captured independently of any Phase 1 state (requirement: independent
    capture preserved)."""
    import test_me_short_a_source_lineage_gap_fix_v1 as gap
    import test_me_short_a_source_intake_v1 as writer_base

    conn = writer_base.FakeConnection(
        activation_row=("RUNNING", gap.INTAKE_FREEZE_TS))
    candidate = gap.make_me_short_candidate()
    repo = gap.capture_route(conn, candidate, route)
    assert repo.errors == []
    assert len(conn.inserts) == 1
    _, params = conn.inserts[0]
    assert route in params


# ── schema: migration 081 only widens the link_status CHECK ────────────


def test_migration_081_widens_only_the_link_status_check():
    body = (ROOT / "sql" / "migrations"
            / "081_me_short_a_phase1_lineage_consistency_v1.sql"
            ).read_text(encoding="utf-8")
    assert ("CHECK (link_status IN "
            "('PENDING','LINKED','QUARANTINED','PHASE1_PENDING'))") in body
    assert ("DROP CONSTRAINT IF EXISTS "
            "me_short_a_event_lineage_v1_link_status_check") in body
    assert "chk_me_short_a_event_lineage_phase1_pending" in body
    assert "phase1_linked = FALSE" in body
    for raw in body.splitlines():
        line = raw.strip().upper()
        assert not line.startswith(
            ("UPDATE", "DELETE", "TRUNCATE", "DROP TABLE", "ALTER COLUMN")), \
            f"forbidden statement in migration: {raw}"
