"""Issue #296: source integrity audit + verdict gate unit tests.

Covers the PR #303 review remediation:

* an empty/missing frozen activation must NEVER report validated
  coverage (Codex review finding, PR #303 discussion_r4667885943);
* the four coverage statuses are separated, with eventual-consistency
  lag distinct from a persistent gap;
* processing-ledger integrity checks (ACCEPTED without source_event,
  REJECTED inside the eligible cohort, research events without an
  original V3 event, lineage/identity drift, experiment mismatches);
* ``SOURCE_VALIDATED`` / final OOS verdict are never issued while the
  upstream capture completeness is unproven;
* repository guards (append-only, no autocommit, strict state values).
"""

import pytest

from app.research.me_entry_quality_v1 import integrity
from app.research.me_entry_quality_v1.contracts import EXPERIMENT_ID
from app.research.me_entry_quality_v1.integrity import (
    audit_source_integrity,
    verdict_gate,
)
from app.research.me_entry_quality_v1.repository import PgObserverRepository


FREEZE_TS = "2026-11-23T15:00:00+00:00"


class FakeCursor:
    def __init__(self, result):
        self._result = result
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._result


class FakeConnection:
    """Yields cursors with scripted results, one per cursor()."""

    def __init__(self, *results):
        self._results = list(results)
        self.cursors = []

    def cursor(self, cursor_factory=None):
        cur = FakeCursor(self._results.pop(0))
        self.cursors.append(cur)
        return cur


def counts(**overrides):
    row = {
        "eligible_source": 10,
        "unprocessed": 0,
        "unprocessed_stale": 0,
        "accepted": 8,
        "rejected": 2,
        "accepted_without_source": 0,
        "rejected_in_cohort": 0,
        "lineage_mismatches": 0,
        "orphan_research_events": 0,
        "ledger_experiment_mismatches": 0,
    }
    row.update(overrides)
    return row


def run_audit(*results, **kwargs):
    conn = FakeConnection(*results)
    return audit_source_integrity(conn, **kwargs)


# ── activation preconditions (Codex review finding) ─────────────────


def test_missing_activation_is_unknown_not_validated():
    result = run_audit(None)

    assert result["status"] == "SOURCE_COVERAGE_UNKNOWN"
    assert result["reason"] == "NOT_ACTIVATED"
    assert result["full_source_validated"] is False
    assert result["ledger_coverage"] == "V3_LEDGER_COVERAGE_UNKNOWN"


def test_missing_freeze_ts_is_unknown_not_validated():
    result = run_audit({"freeze_ts": None})

    assert result["status"] == "SOURCE_COVERAGE_UNKNOWN"
    assert result["reason"] == "MISSING_FREEZE_TS"
    assert result["full_source_validated"] is False


def test_empty_activation_never_runs_coverage_aggregate():
    # The empty audit must not even present itself as a reconciliation.
    conn = FakeConnection(None)
    audit_source_integrity(conn)

    assert len(conn.cursors) == 1


# ── status separation ───────────────────────────────────────────────


def test_clean_reconciliation_is_ledger_scoped_validation():
    result = run_audit({"freeze_ts": FREEZE_TS}, counts())

    assert result["status"] == "SOURCE_COVERAGE_VALIDATED"
    assert result["ledger_coverage"] == "V3_LEDGER_COVERAGE_VALIDATED"
    # P2: a ledger match never proves upstream capture completeness.
    assert result["upstream_source_coverage"] == (
        "UPSTREAM_SOURCE_COVERAGE_UNPROVEN"
    )
    assert result["full_source_validated"] is False


def test_fresh_unprocessed_event_is_coverage_lag():
    # Scenario 9: eventual consistency — a fresh unprocessed event is
    # not a permanent loss.
    result = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(unprocessed=2, unprocessed_stale=0),
    )

    assert result["status"] == "COVERAGE_LAG"
    assert result["unprocessed"] == 2
    assert result["unprocessed_stale"] == 0


def test_stale_unprocessed_event_is_coverage_gap():
    # Scenario 10: persistently missing event.
    result = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(unprocessed=2, unprocessed_stale=2),
    )

    assert result["status"] == "COVERAGE_GAP"
    assert result["ledger_coverage"] == "V3_LEDGER_COVERAGE_GAP"


def test_integrity_conflict_wins_over_coverage_gap():
    result = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(unprocessed=5, unprocessed_stale=5, lineage_mismatches=1),
    )

    assert result["status"] == "INTEGRITY_CONFLICT"
    assert result["integrity_conflicts"] >= 1


# ── processing ledger integrity checks (P2) ─────────────────────────


def test_accepted_without_source_event_is_conflict():
    # Scenario 7: ACCEPTED row without a corresponding source_event.
    result = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(accepted_without_source=1),
    )

    assert result["status"] == "INTEGRITY_CONFLICT"
    assert result["accepted_without_source"] == 1


def test_rejected_inside_eligible_cohort_is_conflict():
    result = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(rejected_in_cohort=1),
    )

    assert result["status"] == "INTEGRITY_CONFLICT"
    assert result["rejected_in_cohort"] == 1


def test_changed_source_lineage_is_conflict():
    # Scenario 6: original V3 lineage vs research lineage drift.
    result = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(lineage_mismatches=1),
    )

    assert result["status"] == "INTEGRITY_CONFLICT"
    assert result["lineage_mismatches"] == 1


def test_research_event_without_v3_source_is_conflict():
    # Scenario 8: research event with no corresponding original event.
    result = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(orphan_research_events=1),
    )

    assert result["status"] == "INTEGRITY_CONFLICT"
    assert result["orphan_research_events"] == 1


def test_ledger_experiment_mismatch_is_conflict():
    result = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(ledger_experiment_mismatches=1),
    )

    assert result["status"] == "INTEGRITY_CONFLICT"
    assert result["ledger_experiment_mismatches"] == 1


# ── audit contract ──────────────────────────────────────────────────


def test_audit_is_read_only_sql():
    conn = FakeConnection({"freeze_ts": FREEZE_TS}, counts())
    audit_source_integrity(conn)

    for cur in conn.cursors:
        for sql, _params in cur.executed:
            head = sql.lstrip().split(None, 1)[0].upper()
            assert head in ("SELECT", "WITH")


def test_gap_grace_and_as_of_are_injectable():
    from datetime import datetime, timezone

    as_of = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
    conn = FakeConnection({"freeze_ts": FREEZE_TS}, counts())
    audit_source_integrity(
        conn,
        gap_grace_seconds=60,
        as_of=as_of,
    )

    _sql, params = conn.cursors[1].executed[0]
    # Params: experiment_id, stale_cutoff, ... — the stale cutoff is
    # exactly as_of - gap_grace_seconds.
    stale_cutoff = params[1]
    assert stale_cutoff == datetime(
        2026, 11, 23, 15, 59, tzinfo=timezone.utc
    )


def test_invalid_gap_grace_rejected():
    conn = FakeConnection()

    for bad in (-1, "60", 1.5):
        with pytest.raises(ValueError, match="INVALID_GAP_GRACE_SECONDS"):
            audit_source_integrity(conn, gap_grace_seconds=bad)


# ── verdict gate (scenarios 4 / 14) ─────────────────────────────────


def test_gate_never_issues_source_validated_on_clean_ledger():
    audit = run_audit({"freeze_ts": FREEZE_TS}, counts())
    gate = verdict_gate(contour_state="ACTIVE", audit=audit)

    assert gate["source_validated"] is False
    assert gate["source_validated_scope"] == "NONE"
    assert gate["v3_ledger_coverage_validated"] is True
    assert gate["source_coverage_status"] == "SOURCE_COVERAGE_UNKNOWN"
    assert gate["reason"] == "UPSTREAM_SOURCE_COVERAGE_UNPROVEN"
    assert gate["final_oos_verdict_allowed"] is False


def test_gate_without_audit_is_unknown():
    gate = verdict_gate(contour_state="ACTIVE", audit=None)

    assert gate["source_validated"] is False
    assert gate["v3_ledger_coverage_validated"] is False
    assert gate["source_coverage_status"] == "SOURCE_COVERAGE_UNKNOWN"


def test_gate_forbids_verdict_on_conflict():
    audit = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(lineage_mismatches=1),
    )
    gate = verdict_gate(contour_state="ACTIVE", audit=audit)

    assert gate["source_validated"] is False
    assert gate["source_coverage_status"] == "INTEGRITY_CONFLICT"
    assert gate["final_oos_verdict_allowed"] is False


def test_gate_forbids_verdict_on_coverage_gap():
    audit = run_audit(
        {"freeze_ts": FREEZE_TS},
        counts(unprocessed=1, unprocessed_stale=1),
    )
    gate = verdict_gate(contour_state="ACTIVE", audit=audit)

    assert gate["source_validated"] is False
    assert gate["source_coverage_status"] == "COVERAGE_GAP"
    assert gate["final_oos_verdict_allowed"] is False


def test_gate_forbids_verdict_for_blocked_contour():
    gate = verdict_gate(contour_state="BLOCKED", audit=None)

    assert gate["source_validated"] is False
    assert gate["reason"] == "BLOCKED_CONTOUR"
    assert gate["final_oos_verdict_allowed"] is False


def test_gate_final_verdict_always_denied_in_phase1():
    audits = [
        None,
        run_audit(None),
        run_audit({"freeze_ts": FREEZE_TS}, counts()),
        run_audit({"freeze_ts": FREEZE_TS}, counts(lineage_mismatches=1)),
        run_audit(
            {"freeze_ts": FREEZE_TS},
            counts(unprocessed=1, unprocessed_stale=1),
        ),
    ]

    for audit in audits:
        for state in ("ACTIVE", "BLOCKED"):
            gate = verdict_gate(contour_state=state, audit=audit)
            assert gate["final_oos_verdict_allowed"] is False
            assert gate["source_validated"] is False


# ── repository guards ───────────────────────────────────────────────


class StubConnection:
    def __init__(self, *, autocommit=False, readonly=False):
        self.autocommit = autocommit
        self.readonly = readonly


def test_repository_rejects_autocommit():
    with pytest.raises(ValueError, match="AUTOCOMMIT_NOT_ALLOWED"):
        PgObserverRepository(StubConnection(autocommit=True))


def test_repository_rejects_readonly_writer():
    with pytest.raises(ValueError, match="WRITER_CONNECTION_READ_ONLY"):
        PgObserverRepository(StubConnection(readonly=True))


def test_record_state_rejects_invalid_values():
    repo = PgObserverRepository(StubConnection())

    with pytest.raises(ValueError, match="INVALID_CONTOUR_STATE"):
        repo.record_state("RUNNING", "X", {})

    with pytest.raises(ValueError, match="INVALID_STATE_REASON"):
        repo.record_state("BLOCKED", "", {})


def test_mark_invalid_rejects_empty_reason():
    repo = PgObserverRepository(StubConnection())

    with pytest.raises(ValueError, match="INVALID_REJECTION_RECORD"):
        repo.mark_invalid("a" * 64, "")


def test_constants_expose_four_audit_statuses():
    assert integrity.AUDIT_SOURCE_COVERAGE_VALIDATED == (
        "SOURCE_COVERAGE_VALIDATED"
    )
    assert integrity.AUDIT_COVERAGE_GAP == "COVERAGE_GAP"
    assert integrity.AUDIT_INTEGRITY_CONFLICT == "INTEGRITY_CONFLICT"
    assert integrity.AUDIT_SOURCE_COVERAGE_UNKNOWN == (
        "SOURCE_COVERAGE_UNKNOWN"
    )
    assert EXPERIMENT_ID == "ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1"
