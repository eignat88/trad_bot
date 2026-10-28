"""Runtime blocker regressions for ``ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1``.

Two contracts are pinned here:

**1. Candidate intake is a strict, lossless keyset scan.**
``DbScannerSetupCandidateSource`` must start at the NEW immutable ``freeze_ts``,
page on the composite key ``(detected_at, setup_id)`` so rows sharing a
timestamp are never dropped at a page boundary, drain batches larger than one
page, survive a re-run and resume after a failure — while a pre-freeze backlog
never blocks or consumes the selection.

**2. ``source_signal_present`` reflects the append-only table, not this INSERT.**
A re-run (``ON CONFLICT DO NOTHING``) and a partial recovery must never be
recorded as ``source_signal_missing``, and a confirmation or Phase 1 lookup
that fails for real (least-privilege role, connection) must be surfaced and
retried instead of being flattened into a permanent ``MISSING_LINEAGE`` /
``QUARANTINED`` on an append-only table.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.research import me_short_a_forward_outcome_lineage_v1 as lineage
from app.research import me_short_a_forward_outcome_runtime_v1 as mod
from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
    DEFAULT_PAGE_SIZE,
    DbScannerSetupCandidateSource,
)

import test_me_short_a_forward_outcome_runtime_v1 as base

FREEZE = base.FREEZE
SIGNAL = base.SIGNAL
NOW = base.NOW
SYMBOL = base.SYMBOL
REF = base.REF
STOP = base.STOP
TARGET = base.TARGET

SOURCE_TABLE = base.SOURCE_TABLE
LINEAGE_TABLE = base.LINEAGE_TABLE
HEARTBEAT_TABLE = base.HEARTBEAT_TABLE

HISTORICAL_ROWS = 1750          # > 1700 pre-freeze rows
NEW_ROWS = 640                  # > 500 post-freeze rows


@pytest.fixture(autouse=True)
def _capture_flag(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_LINEAGE_CAPTURE_ENABLED", "1")
    yield


# ── a faithful keyset table ────────────────────────────────────────────


class TableConn(base.FakeConnection):
    """``dds.scanner_setup`` double with real keyset semantics.

    Applies exactly what the production SQL says: the scanner/direction
    predicate, ``detected_at > freeze``, the composite cursor
    ``detected_at > ? OR (detected_at = ? AND setup_id > ?)``, a total
    ``(detected_at, setup_id)`` order and a ``LIMIT``.
    """

    def __init__(self, rows, **kwargs):
        super().__init__(**kwargs)
        self.rows = list(rows)

    # -- scanner_setup --
    def route_select(self, head, params):
        if "DDS.SCANNER_SETUP" in head:
            return self._scanner_page(head, params)
        return super().route_select(head, params)

    def _scanner_page(self, head, params):
        scanner, direction, lower = params[0], params[1], params[2]
        limit = int(params[-1])
        assert "OFFSET" not in head, "keyset pagination must not use OFFSET"
        cursor = None if len(params) == 4 else (params[3], params[5])
        out = []
        for row in sorted(self.rows,
                          key=lambda r: (r["detected_at"], str(r["setup_id"]))):
            if row["scanner_name"] != scanner or row["direction"] != direction:
                continue
            if row["detected_at"] <= lower:
                continue
            if cursor is not None:
                det, sid = cursor
                if not (row["detected_at"] > det
                        or (row["detected_at"] == det
                            and str(row["setup_id"]) > str(sid))):
                    continue
            out.append(self._as_row(row))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _as_row(row):
        return [
            row["setup_id"], row["symbol"], row["detected_at"],
            row["reference_price"], row["invalidation_price"],
            row["target_1"], row["entry_zone_low"], row["entry_zone_high"],
            row["signal_candle_open_time"], row["features"],
            row["market_regime"], row["status"],
        ]


def make_db_row(setup_id, detected_at, *, symbol=SYMBOL,
                ref=100.0, stop=100.2, target=98.0, scanner="MOMENTUM_EXHAUSTION",
                direction="SHORT"):
    return {
        "setup_id": setup_id,
        "symbol": symbol,
        "detected_at": detected_at,
        "reference_price": ref,
        "invalidation_price": stop,
        "target_1": target,
        "entry_zone_low": None,
        "entry_zone_high": None,
        "signal_candle_open_time": None,
        "features": {},
        "market_regime": "TREND_START",
        "status": "READY_TO_TRADE",
        "scanner_name": scanner,
        "direction": direction,
    }


def historical_rows(count=HISTORICAL_ROWS):
    """Rows at or before the immutable freeze: must never be read back."""
    return [
        make_db_row(f"hist-{i:05d}", FREEZE - timedelta(minutes=count - i))
        for i in range(count)
    ]


def new_rows(count=NEW_ROWS, *, start=None, step_minutes=1):
    """Post-freeze rows on the 5m grid, one per minute after the freeze."""
    base_time = start or (FREEZE + timedelta(minutes=1))
    return [
        make_db_row(f"new-{i:05d}",
                    base_time + timedelta(minutes=i * step_minutes))
        for i in range(count)
    ]


def build_source(rows, *, page_size=DEFAULT_PAGE_SIZE):
    conn = TableConn(rows)
    return conn, DbScannerSetupCandidateSource(
        conn, mod.InMemoryCandleSource([]), page_size=page_size)


def build_runtime(conn, source, *, now=NOW, page_size=DEFAULT_PAGE_SIZE):
    return mod.ForwardOutcomeRuntime(
        conn,
        config=base.config(),
        candle_source=mod.InMemoryCandleSource([]),
        candidate_source=source,
        clock=base.clock_at(now),
        runner_id="runtime-under-test",
        page_size=page_size,
    )


# ── 1. keyset pagination ──────────────────────────────────────────────


def test_fetch_page_requires_the_new_immutable_freeze():
    conn, source = build_source([])
    with pytest.raises(ValueError):
        source.fetch_page(None, freeze_ts=None, page_size=10)
    with pytest.raises(ValueError):
        source.fetch(None)
    with pytest.raises(ValueError):
        source(None)
    # the freeze is what the activation checkpoint froze, never a soft cursor
    page = source.fetch_page(None, freeze_ts=FREEZE, page_size=10)
    assert page.rows_read == 0
    assert page.candidates == ()


def test_query_uses_a_composite_keyset_not_an_offset():
    conn, source = build_source(new_rows(3))
    source.fetch_page(None, freeze_ts=FREEZE, page_size=10)
    source.fetch_page((FREEZE + timedelta(minutes=1), "new-00000"),
                      freeze_ts=FREEZE, page_size=10)
    first_sql, first_params = conn.statements[0]
    second_sql, second_params = conn.statements[1]

    assert "OFFSET" not in first_sql and "OFFSET" not in second_sql
    # the immutable freeze is a strict lower bound in BOTH variants
    assert first_sql.count("%s") == 4
    assert second_sql.count("%s") == 7
    assert "s.detected_at > %s" in first_sql
    assert "s.detected_at > %s" in second_sql
    # composite tie-breaker: a plain `detected_at > ?` loses same-timestamp rows
    assert ("s.detected_at > %s OR (s.detected_at = %s AND s.setup_id > %s)"
            in " ".join(second_sql.split()))
    assert second_params[3] == second_params[4]   # the same bound twice
    assert second_params[5] == "new-00000"
    assert "ORDER BY s.detected_at ASC, s.setup_id ASC" in first_sql
    assert first_params[2] == FREEZE              # bound as a parameter


def test_pre_freeze_backlog_is_never_read_and_never_blocks_a_page():
    rows = historical_rows() + new_rows()
    conn, source = build_source(rows, page_size=500)
    page = source.fetch_page(None, freeze_ts=FREEZE, page_size=500)

    assert page.rows_read == 500
    assert len(page.candidates) == 500
    for candidate in page.candidates:
        assert candidate.signal_time > FREEZE
    assert all(c.event_id.startswith("new-") for c in page.candidates)
    # one page of 500 post-freeze rows despite 1750 older rows existing
    assert conn.statements[0][1][2] == FREEZE


def test_more_than_500_new_rows_are_all_observed_in_one_cycle():
    rows = historical_rows() + new_rows(NEW_ROWS)
    conn, source = build_source(rows, page_size=500)
    runtime = build_runtime(conn, source)

    runtime.run_cycle()

    assert runtime.metrics.candidates_seen == NEW_ROWS
    assert runtime.metrics.signals_captured == NEW_ROWS
    assert runtime.metrics.lineages_recorded == NEW_ROWS
    assert runtime.metrics.skipped_pre_freeze == 0
    assert runtime.metrics.errors == 0
    assert len(conn.persisted_for(SOURCE_TABLE)) == NEW_ROWS
    assert len(conn.persisted_for(LINEAGE_TABLE)) == NEW_ROWS
    # 640 rows over pages of 500 -> exactly two pages, then a short one
    assert runtime.candidate_cursor == (
        FREEZE + timedelta(minutes=NEW_ROWS), f"new-{NEW_ROWS - 1:05d}")


def test_rows_sharing_a_timestamp_are_all_returned_exactly_once():
    shared = FREEZE + timedelta(minutes=1)
    rows = [make_db_row(f"same-{i:03d}", shared) for i in range(20)]
    conn, source = build_source(rows, page_size=500)
    page = source.fetch_page(None, freeze_ts=FREEZE, page_size=500)
    assert len(page.candidates) == 20
    assert len({c.event_id for c in page.candidates}) == 20


def test_identical_timestamps_at_a_page_boundary_are_not_dropped():
    """The classic loss: `detected_at > cursor` throws away the siblings that
    share the boundary row's timestamp."""
    shared = FREEZE + timedelta(minutes=5)
    rows = (
        [make_db_row(f"before-{i:03d}", FREEZE + timedelta(minutes=i + 1))
         for i in range(3)]
        + [make_db_row(f"same-{i:03d}", shared) for i in range(6)]
        + [make_db_row(f"after-{i:03d}", shared + timedelta(minutes=1))
           for i in range(3)]
    )
    conn, source = build_source(rows, page_size=4)
    seen = []
    cursor = None
    for _ in range(10):
        page = source.fetch_page(cursor, freeze_ts=FREEZE, page_size=4)
        seen.extend(page.candidates)
        if page.rows_read <= 0 or page.exhausted:
            break
        cursor = page.next_cursor

    ids = [c.event_id for c in seen]
    assert len(ids) == 12
    assert len(set(ids)) == 12            # nothing lost, nothing duplicated
    assert ids.count("same-005") == 1     # the boundary row itself
    assert [i for i in ids if i.startswith("same-")] == [
        f"same-{n:03d}" for n in range(6)]


def test_pages_partition_the_result_set_without_loss_or_duplication():
    rows = historical_rows(200) + new_rows(377)
    conn, source = build_source(rows, page_size=100)
    seen = []
    cursor = None
    pages = 0
    while True:
        page = source.fetch_page(cursor, freeze_ts=FREEZE, page_size=100)
        pages += 1
        seen.extend(page.candidates)
        if page.rows_read <= 0 or page.exhausted:
            break
        cursor = page.next_cursor
    ids = [c.event_id for c in seen]
    assert pages == 4                       # three full pages + one short page
    assert len(ids) == 377
    assert len(set(ids)) == 377
    assert ids == [f"new-{n:05d}" for n in range(377)]


def test_repeated_run_is_idempotent_and_loses_nothing():
    rows = historical_rows(100) + new_rows(60)
    conn, source = build_source(rows, page_size=25)
    runtime = build_runtime(conn, source)

    runtime.run_cycle()
    first_source = len(conn.persisted_for(SOURCE_TABLE))
    first_lineage = len(conn.persisted_for(LINEAGE_TABLE))
    assert first_source == 60 and first_lineage == 60

    runtime.run_cycle()          # same process: the cursor already moved on
    assert len(conn.persisted_for(SOURCE_TABLE)) == first_source
    assert len(conn.persisted_for(LINEAGE_TABLE)) == first_lineage
    assert runtime.metrics.candidates_seen == 60       # not re-read
    assert runtime.metrics.lineages_recorded == 60
    assert runtime.metrics.errors == 0

    # a genuine re-run: fresh runner, cursor reset to the immutable freeze
    fresh = build_runtime(conn, source)
    fresh.run_cycle()
    assert fresh.metrics.candidates_seen == 60         # everything re-read
    assert len(conn.persisted_for(SOURCE_TABLE)) == first_source
    assert len(conn.persisted_for(LINEAGE_TABLE)) == first_lineage
    assert fresh.metrics.signals_captured == 0         # ON CONFLICT DO NOTHING
    assert fresh.metrics.lineages_recorded == 0
    assert fresh.metrics.errors == 0


def test_resume_after_a_mid_page_failure_loses_no_candidates():
    rows = new_rows(6)
    conn, source = build_source(rows, page_size=3)
    probe = source.fetch_page(None, freeze_ts=FREEZE, page_size=100)
    victim = mod.event_key_for(probe.candidates[4])

    conn.fail_select_keys = {victim}
    runtime = build_runtime(conn, source, page_size=3)

    runtime.run_cycle()
    # page 1 fully handled -> cursor moved; page 2 stalled on row #5
    assert runtime.metrics.lineages_recorded == 4
    assert runtime.metrics.errors >= 1
    assert "permission denied" in (runtime.diagnostics().last_error or "")
    assert runtime.candidate_cursor == (
        FREEZE + timedelta(minutes=3), "new-00002")
    assert len(conn.persisted_for(LINEAGE_TABLE)) == 4

    conn.fail_select_keys = set()
    runtime.run_cycle()
    assert len(conn.persisted_for(LINEAGE_TABLE)) == 6    # 6 unique events
    assert len(conn.persisted_for(SOURCE_TABLE)) == 6
    assert runtime.metrics.lineages_recorded == 6        # zero duplicates
    assert runtime.metrics.errors >= 1                   # the failure stays
    assert runtime.candidate_cursor[1] == "new-00005"


def test_a_stalled_page_never_advances_the_cursor():
    rows = new_rows(9)
    conn, source = build_source(rows, page_size=3)
    probe = source.fetch_page(None, freeze_ts=FREEZE, page_size=3)
    conn.fail_select_keys = {mod.event_key_for(probe.candidates[0])}
    runtime = build_runtime(conn, source, page_size=3)

    for _ in range(4):
        runtime.run_cycle()
    # the very first candidate blocks intake at the head of page 1 every time
    assert runtime.candidate_cursor is None
    assert len(conn.persisted_for(LINEAGE_TABLE)) == 0
    assert runtime.metrics.errors >= 4
    assert "stalled" in (runtime.diagnostics().last_error or "").lower() or \
        "permission denied" in (runtime.diagnostics().last_error or "")


def test_rejected_rows_are_counted_and_do_not_stall_intake():
    rows = new_rows(5)
    rows[2]["reference_price"] = None          # unrepresentable as a Candidate
    conn, source = build_source(rows, page_size=500)
    page = source.fetch_page(None, freeze_ts=FREEZE, page_size=500)
    assert page.rejected == ("new-00002",)
    assert len(page.candidates) == 4
    # the cursor still tracks the rejected row, so it cannot block the scan
    assert page.cursor[1] == "new-00004"


# ── 2. source_signal_present is the TABLE's state ─────────────────────


def test_partial_recovery_still_reports_source_signal_present_true():
    """Process died after the source signal and before the lineage row.

    On the next run the INSERT is a no-op, yet the lineage record must report
    the source signal as PRESENT — otherwise the append-only table is stuck
    with a bogus ``source_signal_missing`` / ``MISSING_LINEAGE`` forever.
    """
    candidate = base.make_candidate()
    key = mod.event_key_for(candidate)
    conn = base.FakeConnection(conflict_skip=True)
    conn.source_signal_rows = [[key]]          # already captured earlier

    runtime, _ = base.build(conn=conn, candidates=[candidate])
    runtime.run_cycle()

    rows = conn.inserts_for(LINEAGE_TABLE)
    assert len(rows) == 1
    payload = base._payload(rows[0])
    assert payload["source_signal_present"] is True
    # ME_SHORT_A_PHASE1_LINEAGE_CONSISTENCY_FIX_V1: with no Phase 1 row
    # observed after the bounded retry, the record no longer claims LINKED
    # (that falsely implied a fully confirmed relation) — the explicit
    # PHASE1_PENDING exception carries the honest state.
    assert payload["link_status"] == "PHASE1_PENDING"
    assert payload["quarantine_reason"] == "phase1_not_observed"
    assert payload["linkage_checks"].find(
        'source_signal_present": true') >= 0
    # the INSERT itself was a no-op and is reported as such
    assert runtime.metrics.signals_captured == 0
    assert runtime.metrics.skipped_idempotent >= 1
    assert runtime.metrics.lineages_recorded == 0      # ON CONFLICT DO NOTHING


def test_rerun_after_a_complete_capture_records_nothing_new():
    candidate = base.make_candidate()
    conn = base.FakeConnection()
    runtime, _ = base.build(conn=conn, candidates=[candidate])
    runtime.run_cycle()
    assert len(conn.persisted_for(LINEAGE_TABLE)) == 1

    # full re-run: the source signal row now exists from the first run
    again = mod.ForwardOutcomeRuntime(
        conn,
        config=base.config(),
        candle_source=mod.InMemoryCandleSource([]),
        candidate_source=lambda _since: [candidate],
        clock=base.clock_at(NOW),
        runner_id="runtime-under-test",
    )
    again.run_cycle()
    assert len(conn.persisted_for(SOURCE_TABLE)) == 1
    assert len(conn.persisted_for(LINEAGE_TABLE)) == 1
    payload = base._payload(conn.persisted_for(LINEAGE_TABLE)[0])
    assert payload["source_signal_present"] is True
    # no Phase 1 row was ever observed for this event: the honest
    # PHASE1_PENDING state, never a false LINKED relation
    assert payload["link_status"] == "PHASE1_PENDING"


def test_unconfirmed_source_signal_is_retried_never_recorded_as_missing():
    candidate = base.make_candidate()
    conn = base.FakeConnection(conflict_skip=True)   # nothing actually lands
    conn.source_signal_rows = []                    # and the row is not there

    runtime, _ = base.build(conn=conn, candidates=[candidate])
    runtime.run_cycle()

    # No lineage row at all: recording `source_signal_present = false` would be
    # permanent and unrepairable on an append-only table.
    assert conn.inserts_for(LINEAGE_TABLE) == []
    assert runtime.metrics.errors >= 1
    assert "source_signal" in (runtime.diagnostics().last_error or "")
    assert runtime.candidate_cursor is None or runtime.candidate_cursor == (
        candidate.signal_time, candidate.event_id)


def test_source_signal_confirmation_permission_error_is_surfaced_and_retried():
    """The collector role has INSERT-only on the capture tables; a runtime
    running under such a role must fail loudly, not guess 'absent'."""
    candidate = base.make_candidate()
    conn = base.FakeConnection()
    conn.fail_select = "SOURCE_SIGNAL"

    runtime, _ = base.build(conn=conn, candidates=[candidate])
    runtime.run_cycle()

    assert conn.inserts_for(LINEAGE_TABLE) == []
    assert runtime.metrics.errors >= 1
    message = runtime.diagnostics().last_error or ""
    assert "permission denied" in message
    assert "source_signal" in message
    assert runtime.metrics.lineages_recorded == 0


def test_phase1_lookup_permission_error_is_surfaced_and_never_faked_as_absent():
    """A failed Phase 1 lookup must not become `phase1_linked = FALSE`: that is
    written once and turns the event into a permanent MISSING_LINEAGE."""
    candidate = base.make_candidate()
    conn = base.FakeConnection()
    conn.fail_select = "FILL_FEASIBILITY"

    runtime, _ = base.build(conn=conn, candidates=[candidate])
    runtime.run_cycle()

    assert conn.inserts_for(LINEAGE_TABLE) == []
    assert runtime.metrics.errors >= 1
    message = runtime.diagnostics().last_error or ""
    assert "permission denied" in message
    assert "Phase 1" in message
    assert runtime.metrics.lineages_recorded == 0


def test_phase1_lookup_surfaces_the_underlying_db_error_type():
    conn = base.FakeConnection()
    conn.fail_select = "FILL_FEASIBILITY"
    runtime, _ = base.build(conn=conn, candidates=[])
    with pytest.raises(mod.DatabaseAccessError) as excinfo:
        runtime._phase1_lookup("a" * 64)
    assert "PermissionError" in str(excinfo.value)


def test_phase1_lookup_parses_source_observation_ids_from_json_text():
    """pg8000 (the documented fallback) returns jsonb as TEXT."""
    conn = base.FakeConnection()
    key = "b" * 64
    conn.phase1_rows = [[key, "[42, 43]"]]
    runtime, _ = base.build(conn=conn, candidates=[])
    link = runtime._phase1_lookup(key)
    assert link.event_key == key
    assert link.observation_id == 42
    assert link.source_observation_ids == (42, 43)


def test_phase1_lookup_tolerates_tuple_scalar_and_bytes_id_shapes():
    conn = base.FakeConnection()
    key = "c" * 64
    runtime, _ = base.build(conn=conn, candidates=[])

    conn.phase1_rows = [[key, (7, 8)]]
    assert runtime._phase1_lookup(key).source_observation_ids == (7, 8)

    conn.phase1_rows = [[key, 9]]
    assert runtime._phase1_lookup(key).source_observation_ids == (9,)

    conn.phase1_rows = [[key, b'["11"]']]
    assert runtime._phase1_lookup(key).source_observation_ids == (11,)

    conn.phase1_rows = [[key, "not-json"]]
    assert runtime._phase1_lookup(key).observation_id is None

    conn.phase1_rows = [[key, None]]
    link = runtime._phase1_lookup(key)
    assert link.observation_id is None
    assert link.source_observation_ids == ()


def test_phase1_absence_is_still_reported_honestly():
    candidate = base.make_candidate()
    conn = base.FakeConnection()
    conn.phase1_rows = []
    runtime, _ = base.build(conn=conn, candidates=[candidate])
    runtime.run_cycle()
    payload = base._payload(conn.persisted_for(LINEAGE_TABLE)[0])
    assert payload["phase1_linked"] is False
    assert payload["phase1_event_key"] is None
    assert payload["source_signal_present"] is True
    # ME_SHORT_A_PHASE1_LINEAGE_CONSISTENCY_FIX_V1: "honestly" now means the
    # explicit PHASE1_PENDING exception — the old LINKED + phase1_linked
    # FALSE combination was the confirmed KORUUSDT/DOGEUSDT defect class.
    assert payload["link_status"] == "PHASE1_PENDING"


def test_forward_only_bound_is_enforced_by_the_query_and_the_runtime():
    rows = historical_rows(50) + new_rows(10)
    conn, source = build_source(rows, page_size=500)
    runtime = build_runtime(conn, source)
    runtime.run_cycle()
    assert runtime.metrics.skipped_pre_freeze == 0
    for row in conn.persisted_for(SOURCE_TABLE):
        payload = base._payload(row)
        assert payload["signal_time"] > FREEZE
