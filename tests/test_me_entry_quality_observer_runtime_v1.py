"""Issue #296: observer lifecycle unit tests.

Covers the PR #303 review remediation: periodic source integrity audit
inside the runtime contour, fail-closed BLOCKED state on invalid source,
rejection-ledger diagnostics, eventual-consistency coverage lag vs gap,
restart idempotency and transactional batch atomicity.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_entry_quality_v1.observer import (
    EntryQualityObserver,
)
from app.research.me_entry_quality_v1.repository import SourceIntegrityError

from test_me_entry_quality_source_observer_v1 import (
    candidate,
)


UTC = timezone.utc
FREEZE = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)

CLEAN_AUDIT = {
    "status": "SOURCE_COVERAGE_VALIDATED",
    "reason": "AUDIT_CLEAN",
    "ledger_coverage": "V3_LEDGER_COVERAGE_VALIDATED",
    "upstream_source_coverage": "UPSTREAM_SOURCE_COVERAGE_UNPROVEN",
    "full_source_validated": False,
    "eligible_source": 0,
    "unprocessed": 0,
    "unprocessed_stale": 0,
    "accepted": 0,
    "rejected": 0,
    "accepted_without_source": 0,
    "rejected_in_cohort": 0,
    "lineage_mismatches": 0,
    "orphan_research_events": 0,
    "ledger_experiment_mismatches": 0,
    "integrity_conflicts": 0,
}


def audit_with(**overrides):
    row = dict(CLEAN_AUDIT)
    row.update(overrides)
    return row


class FakeRepository:
    """Transaction-faithful fake.

    Writes are staged and applied only on the ``record_health`` commit,
    mirroring PgObserverRepository's single-transaction contract
    (events + processing ledger + state + health commit atomically).
    """

    def __init__(self):
        self.activation = {
            "status": "RUNNING",
            "freeze_ts": FREEZE,
        }
        self.contour_state = None
        self.rows = []
        self.saved = {}
        self.rejected = {}
        self.states = []
        self.health = []
        self.audit_result = dict(CLEAN_AUDIT)
        self.audit_calls = 0
        self.fetch_calls = 0
        self.fetch_freeze_args = []
        self.save_error = None  # (match_key|"*", exception)
        self.redeliver = False
        self._staged = []

    # -- lock / transaction plumbing --

    def acquire_processing_lock(self):
        return True

    def rollback(self):
        self._staged.clear()

    def commit(self):
        for op in self._staged:
            if op[0] == "save":
                self.saved[op[1]] = op[2]
            elif op[0] == "reject":
                self.rejected[op[1]] = op[2]
            elif op[0] == "state":
                self.states.append({
                    "state": op[1],
                    "reason_code": op[2],
                    "details": op[3],
                })
            elif op[0] == "health":
                self.health.append({
                    "status": op[1],
                    "source_rows_seen": op[2],
                    "events_inserted": op[3],
                    "diagnostics": op[4],
                })
        self._staged.clear()

    # -- repository contract --

    def load_activation(self):
        return self.activation

    def load_contour_state(self):
        return self.contour_state

    def fetch_pending_events(self, freeze_ts, limit):
        self.fetch_calls += 1
        self.fetch_freeze_args.append(freeze_ts)
        if self.redeliver:
            return list(self.rows[:limit])
        pending = [
            row for row in self.rows
            if row.get("event_key") not in self.saved
            and row.get("event_key") not in self.rejected
        ]
        return pending[:limit]

    def audit_source_integrity(self, *, gap_grace_seconds, as_of=None):
        self.audit_calls += 1
        self.audit_gap_grace = gap_grace_seconds
        return dict(self.audit_result)

    def save_event(self, event):
        if self.save_error is not None:
            match, exc = self.save_error
            if match in ("*", event.event_key):
                raise exc
        if event.event_key in self.saved or any(
            op[0] == "save" and op[1] == event.event_key
            for op in self._staged
        ):
            return "DUPLICATE"
        self._staged.append(("save", event.event_key, event))
        return "INSERTED"

    def mark_invalid(self, event_key, reason_code):
        if not event_key:
            raise ValueError("INVALID_REJECTION_RECORD")
        self._staged.append(("reject", event_key, reason_code))

    def record_state(self, state, reason_code, details):
        self._staged.append(("state", state, reason_code, details))

    def record_health(self, status, source_rows_seen,
                      events_inserted, diagnostics):
        self._staged.append(
            ("health", status, source_rows_seen, events_inserted,
             diagnostics)
        )
        self.commit()


# ── basic lifecycle ─────────────────────────────────────────────────


def test_not_activated():
    repo = FakeRepository()
    repo.activation = None

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "NOT_ACTIVATED"


def test_not_running():
    repo = FakeRepository()
    repo.activation["status"] = "PAUSED"

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "NOT_RUNNING"


def test_insert_new_event():
    repo = FakeRepository()
    repo.rows = [candidate()]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "OK"
    assert result.events_inserted == 1
    assert len(repo.saved) == 1


def test_duplicate_delivery():
    repo = FakeRepository()
    repo.rows = [candidate()]
    # Upstream redelivers the same event on every cycle.
    repo.redeliver = True
    observer = EntryQualityObserver(repo)

    first = observer.run_once()
    second = observer.run_once()

    assert first.events_inserted == 1
    assert second.events_inserted == 0
    assert second.duplicates == 1
    assert len(repo.saved) == 1


def test_pre_freeze_event():
    repo = FakeRepository()
    row = candidate()
    row["signal_time"] = FREEZE

    repo.rows = [row]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "BLOCKED"
    assert result.blocked_reason == "INVALID_SOURCE_EVENT"
    assert result.invalid == 1
    assert result.events_inserted == 0


def test_incomplete_lineage():
    repo = FakeRepository()
    row = candidate()
    row["lineage"].pop("source_features")

    repo.rows = [row]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "BLOCKED"
    assert result.invalid == 1
    assert len(repo.saved) == 0


def test_empty_batch():
    repo = FakeRepository()

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "OK"
    assert result.source_rows_seen == 0
    assert result.events_inserted == 0


def test_invalid_batch_limit():
    repo = FakeRepository()

    try:
        EntryQualityObserver(repo, batch_limit=0)
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert str(exc) == "INVALID_BATCH_LIMIT"


def test_invalid_observer_options():
    repo = FakeRepository()

    with pytest.raises(ValueError, match="INVALID_AUDIT_INTERVAL"):
        EntryQualityObserver(repo, audit_interval_seconds=0)

    with pytest.raises(ValueError, match="INVALID_GAP_GRACE"):
        EntryQualityObserver(repo, coverage_gap_grace_seconds=-1)


# ── scenario 13: invalid source → fail-closed, diagnostics saved ────


def test_invalid_event_fail_closed_with_diagnostics():
    repo = FakeRepository()
    row = candidate()
    row["lineage"]["source_features"]["rsi_14"] = None

    repo.rows = [row]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "BLOCKED"
    assert result.blocked_reason == "INVALID_SOURCE_EVENT"

    # Durable rejection ledger entry with a concrete reason_code.
    assert repo.rejected[row["event_key"]] == "FEATURE_RSI14_NOT_FINITE"

    # Contour state transition is recorded (append-only ledger).
    assert repo.states[-1]["state"] == "BLOCKED"
    assert repo.states[-1]["reason_code"] == "INVALID_SOURCE_EVENT"

    # Rejection diagnostics survive the block in source_health.
    health = repo.health[-1]
    assert health["status"] == "SOURCE_INCOMPLETE"
    diagnostics = health["diagnostics"]
    assert diagnostics["invalid"] == 1
    assert diagnostics["rejections"] == [{
        "event_key": row["event_key"],
        "reason_code": "FEATURE_RSI14_NOT_FINITE",
    }]
    assert diagnostics["validation_errors"] == {
        "FEATURE_RSI14_NOT_FINITE": 1,
    }
    assert diagnostics["block_details"]["events_admitted"] == 0


def test_invalid_event_not_admitted_to_cohort():
    # A tainted batch admits NOTHING: the valid sibling event is
    # deferred, not lost (it stays pending for post-recovery processing).
    repo = FakeRepository()
    good = candidate()
    bad = candidate()
    bad["event_key"] = "b" * 64
    bad["symbol"] = "BTCUSDT"
    bad["lineage"]["source_features"]["rsi_14"] = 250.0

    repo.rows = [good, bad]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "BLOCKED"
    assert result.events_inserted == 0
    assert repo.saved == {}
    assert repo.rejected == {
        bad["event_key"]: "FEATURE_RSI14_OUT_OF_RANGE",
    }


def test_invalid_event_key_diagnostic_only():
    # Rows without a usable event_key cannot be keyed into the ledger;
    # their rejection diagnostics are preserved in source_health.
    repo = FakeRepository()
    row = candidate()
    row["event_key"] = ""
    row["lineage"]["source_features"]["rsi_14"] = "68"

    repo.rows = [row]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "BLOCKED"
    assert repo.rejected == {}
    assert repo.health[-1]["diagnostics"]["rejections"] == [{
        "event_key": "",
        "reason_code": "INVALID_EVENT_KEY",
    }]


# ── scenario 15: BLOCKED → no further eligible capture ──────────────


def test_blocked_state_stops_capture():
    repo = FakeRepository()
    repo.contour_state = {
        "state": "BLOCKED",
        "reason_code": "INTEGRITY_CONFLICT",
    }
    repo.rows = [candidate()]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "BLOCKED"
    assert result.blocked_reason == "INTEGRITY_CONFLICT"
    assert repo.fetch_calls == 0
    assert repo.saved == {}
    assert repo.health == []


# ── scenario 16: manual recovery without freeze_ts reset ────────────


def test_recovery_preserves_freeze_and_backfills_nothing():
    repo = FakeRepository()
    good = candidate()
    bad = candidate()
    bad["event_key"] = "b" * 64
    bad["lineage"]["source_features"]["entry_confirmation_rsi_rising"] = "yes"
    repo.rows = [good, bad]

    observer = EntryQualityObserver(repo)
    frozen_before = repo.activation["freeze_ts"]

    blocked = observer.run_once()
    assert blocked.status == "BLOCKED"

    # Administrative recovery contour: append RESUMED, never touch
    # activation or freeze_ts.
    repo.contour_state = {
        "state": "RESUMED",
        "reason_code": "MANUAL_RECOVERY",
    }
    repo.rows = [good]

    resumed = observer.run_once()

    assert resumed.status == "OK"
    assert resumed.events_inserted == 1
    assert repo.activation["freeze_ts"] is frozen_before
    # The freeze boundary observed by every fetch is unchanged.
    assert all(ts == frozen_before for ts in repo.fetch_freeze_args)
    # Rejected events stay out of the eligible cohort forever.
    assert bad["event_key"] not in repo.saved


# ── scenario 11: restart → no duplicates ────────────────────────────


def test_restart_creates_no_duplicates():
    repo = FakeRepository()
    repo.rows = [candidate()]

    first = EntryQualityObserver(repo).run_once()
    assert first.events_inserted == 1

    # A fresh observer instance (process restart) over the same ledger.
    second = EntryQualityObserver(repo).run_once()
    assert second.events_inserted == 0
    assert len(repo.saved) == 1


def test_redelivered_event_is_duplicate():
    repo = FakeRepository()
    repo.rows = [candidate()]
    repo.redeliver = True

    first = EntryQualityObserver(repo).run_once()
    second = EntryQualityObserver(repo).run_once()

    assert first.events_inserted == 1
    assert second.duplicates == 1
    assert second.events_inserted == 0
    assert len(repo.saved) == 1


# ── scenario 12: batch error → no partially written data ────────────


def test_batch_error_commits_nothing():
    repo = FakeRepository()
    good = candidate()
    bad = candidate()
    bad["event_key"] = "b" * 64
    repo.rows = [good, bad]
    repo.save_error = (bad["event_key"], RuntimeError("DB_DOWN"))

    with pytest.raises(RuntimeError, match="DB_DOWN"):
        EntryQualityObserver(repo).run_once()

    # The whole batch rolls back: no events, no ledger, no health row.
    assert repo.saved == {}
    assert repo.rejected == {}
    assert repo.states == []
    assert repo.health == []


def test_save_conflict_blocks_deterministically():
    # A conflicting duplicate payload is a deterministic integrity
    # violation: BLOCKED (exit 5), never an endless restart loop.
    repo = FakeRepository()
    repo.rows = [candidate()]
    repo.save_error = (
        "*",
        SourceIntegrityError("EVENT_KEY_LINEAGE_CONFLICT"),
    )

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "BLOCKED"
    assert result.blocked_reason == "EVENT_KEY_LINEAGE_CONFLICT"
    assert repo.states[-1]["reason_code"] == "EVENT_KEY_LINEAGE_CONFLICT"
    assert repo.health[-1]["diagnostics"]["blocked_reason"] == (
        "EVENT_KEY_LINEAGE_CONFLICT"
    )


# ── periodic source integrity audit ─────────────────────────────────


def test_audit_integrity_conflict_blocks_immediately():
    repo = FakeRepository()
    repo.audit_result = audit_with(
        status="INTEGRITY_CONFLICT",
        reason="INTEGRITY_CONFLICT",
        integrity_conflicts=2,
    )
    repo.rows = [candidate()]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "BLOCKED"
    assert result.blocked_reason == "INTEGRITY_CONFLICT"
    assert repo.fetch_calls == 0
    assert repo.states[-1]["reason_code"] == "INTEGRITY_CONFLICT"


def test_audit_coverage_gap_blocks():
    # Scenario 10: persistently missing (stale unprocessed) events.
    repo = FakeRepository()
    repo.audit_result = audit_with(
        status="COVERAGE_GAP",
        reason="COVERAGE_GAP",
        unprocessed=3,
        unprocessed_stale=3,
    )
    repo.rows = [candidate()]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "BLOCKED"
    assert result.blocked_reason == "COVERAGE_GAP"
    assert repo.fetch_calls == 0


def test_audit_coverage_lag_does_not_block():
    # Scenario 9: a fresh unprocessed event is eventual-consistency
    # lag until the grace window expires — capture keeps running.
    repo = FakeRepository()
    repo.audit_result = audit_with(
        status="COVERAGE_LAG",
        reason="COVERAGE_LAG",
        unprocessed=1,
        unprocessed_stale=0,
    )
    repo.rows = [candidate()]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "OK"
    assert result.events_inserted == 1
    assert repo.states == []
    assert result.audit_status == "COVERAGE_LAG"


def test_audit_runs_periodically():
    # Escalation/periodicity contract: first cycle audits, later cycles
    # only after audit_interval_seconds elapsed.
    ticks = [0.0]
    repo = FakeRepository()
    repo.rows = []
    observer = EntryQualityObserver(
        repo,
        audit_interval_seconds=300.0,
        clock=lambda: ticks[0],
    )

    observer.run_once()
    assert repo.audit_calls == 1

    ticks[0] = 299.0
    observer.run_once()
    assert repo.audit_calls == 1

    ticks[0] = 300.0
    observer.run_once()
    assert repo.audit_calls == 2


def test_audit_gap_grace_forwarded():
    repo = FakeRepository()
    EntryQualityObserver(
        repo,
        coverage_gap_grace_seconds=120,
    ).run_once()

    assert repo.audit_gap_grace == 120


# ── scenario 14: upstream coverage unknown → no SOURCE_VALIDATED ────


def test_clean_ledger_still_never_source_validated():
    repo = FakeRepository()
    repo.rows = [candidate()]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "OK"
    gate = result.diagnostics["verdict_gate"]
    assert gate["source_validated"] is False
    assert gate["source_coverage_status"] == "SOURCE_COVERAGE_UNKNOWN"
    assert gate["v3_ledger_coverage_validated"] is True
    assert gate["final_oos_verdict_allowed"] is False
    assert gate["upstream_source_coverage"] == (
        "UPSTREAM_SOURCE_COVERAGE_UNPROVEN"
    )


def test_blocked_gate_forbids_final_verdict():
    repo = FakeRepository()
    repo.audit_result = audit_with(
        status="INTEGRITY_CONFLICT",
        integrity_conflicts=1,
    )

    result = EntryQualityObserver(repo).run_once()

    gate = result.diagnostics["verdict_gate"]
    assert gate["source_validated"] is False
    assert gate["final_oos_verdict_allowed"] is False
    assert gate["source_coverage_status"] == "INTEGRITY_CONFLICT"
