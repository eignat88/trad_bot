"""Cross-cutting dedup contracts that make the AC7 recovery rescan safe.

The recovery mechanism only changes WHEN the next cycle starts.  What makes
a full-universe rescan safe (and rules out duplicate candidates, duplicate
OOS observations, run_id reuse and historical backfill) lives in the
storage contracts below — this file pins them so a future refactor cannot
silently break the recovery safety argument.

Offline: source-contract assertions only, no PostgreSQL, no Bybit.
"""
import inspect

from app.db.repository import ScannerRepository
from app.research import prospective_observer
from app.research.prospective_observer import ProspectiveOOSObserver

import app.scanners.recovery_policy_v1 as recovery_policy


def test_candidates_upsert_natural_key_prevents_duplicates():
    """A recovery rescan re-detects signals of already-processed symbols.
    ``dds.scanner_setup`` rows are UPSERTED on the natural key that includes
    ``signal_candle_open_time`` — the rescan updates the same row and can
    never create a duplicate candidate."""
    source = inspect.getsource(ScannerRepository._save_pg)
    assert "INSERT INTO dds.scanner_setup" in source
    assert "ON CONFLICT (" in source
    assert "signal_candle_open_time" in source
    assert "DO UPDATE SET" in source
    # The conflict target is the natural key of a detection, not run_id —
    # a new run_id must not multiply rows.
    conflict = source[source.index("ON CONFLICT ("):source.index("DO UPDATE")]
    for column in ("instrument_id", "scanner_name", "direction",
                   "entry_timeframe", "signal_candle_open_time"):
        assert column in conflict, column
    assert "run_id" not in conflict


def test_every_oos_observation_insert_is_idempotent():
    """Every ``research.prospective_observation`` INSERT is guarded by
    ``ON CONFLICT (experiment_id, source_signal_id) DO NOTHING``: re-running
    a cycle over the same signals cannot create duplicate OOS
    observations."""
    source = inspect.getsource(prospective_observer)
    insert_token = "INSERT INTO research.prospective_observation"
    inserts = [
        i for i in range(len(source))
        if source.startswith(insert_token, i)
    ]
    assert inserts, "expected prospective_observation INSERTs"
    for n, i in enumerate(inserts):
        # Bound the search to THIS statement (up to the next INSERT).
        end = inserts[n + 1] if n + 1 < len(inserts) else len(source)
        window = source[i:end]
        conflict_at = window.find("ON CONFLICT (experiment_id, source_signal_id)")
        assert conflict_at != -1, f"unguarded INSERT at offset {i}"
        assert "DO NOTHING" in window[conflict_at:conflict_at + 120]
        assert "DO UPDATE" not in window


def test_observer_is_append_only_without_update_rewrite():
    """OOS observations are append-only (the module contract): the recovery
    rescan adds nothing new for existing signals and rewrites no history."""
    source = inspect.getsource(prospective_observer)
    assert "append-only: INSERT only, no UPDATE in observe()" in source
    assert "UPDATE research.prospective_observation" not in source


def test_each_cycle_creates_a_fresh_run_row():
    """Every cycle — including the recovery rescan — is a NEW
    ``dds.scanner_run`` row: ``INSERT ... RETURNING run_id`` with
    ``now()``.  A run_id can therefore never be reused and every rescan
    gets fresh ``started_at`` timestamps."""
    source = inspect.getsource(ScannerRepository.start_run)
    assert "INSERT INTO dds.scanner_run" in source
    assert "now()" in source
    assert "RETURNING run_id" in source
    # No id parameter to reuse: the id comes exclusively from the INSERT.
    assert "run_id" not in source.split("RETURNING")[0]


def test_cycle_completion_event_is_at_most_one_per_cycle():
    """The ``CYCLE_COMPLETION`` diagnostic row is written at most once per
    cycle (``event_persisted`` guard on both the normal and the abort
    path) — an incomplete cycle followed by a recovery rescan writes one
    diagnostic row per CYCLE, never two for one cycle."""
    import scanner_runner

    source = inspect.getsource(scanner_runner)
    run_scan = source[source.index("def run_scan_cycle("):
                      source.index("def report_aborted_cycle(")]
    aborted = source[source.index("def report_aborted_cycle("):
                     source.index("def main()")]
    assert "if not report.is_complete and not report.event_persisted:" in run_scan
    assert "if report.event_persisted:" in aborted


def test_recovery_policy_performs_no_io_and_no_backfill():
    """The scheduling decision is a pure, offline policy: no SQL, no
    network, no limiter mutation — historical incomplete cycles are never
    rewritten and no signal backfill is possible from this module.

    The one read-only integration (:func:`breaker_cooldown_hint_seconds`)
    may call the limiter's ``snapshot()`` observability view and nothing
    else.
    """
    source = inspect.getsource(recovery_policy)
    for forbidden in (
        "INSERT", "UPDATE ", "DELETE", "DROP ", "ALTER ",
        "psycopg", "requests.", "urlopen", "urllib",
        "acquire(", "prepare_send", "record_result",  # limiter mutations
    ):
        assert forbidden not in source, forbidden
    # Read-only limiter view is allowed and explicitly documented.
    assert "snapshot()" in source
    assert "No Bybit calls, database writes, or limiter modifications" \
        in source


def test_recovery_scheduling_block_is_single_and_guarded():
    """Exactly ONE AC7 scheduling block exists in main() and it is guarded
    by the feature flag AND the finished-ok marker — a failed/aborted cycle
    can never reach the recovery decision."""
    import ast
    import scanner_runner
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(scanner_runner.main)))
    flags = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and any(
            isinstance(v, ast.Name) and v.id == "ac7_recovery_enabled"
            for v in node.test.values
        )
    ]
    assert len(flags) == 1
    guard_names = {
        v.id for v in flags[0].test.values if isinstance(v, ast.Name)
    }
    assert guard_names == {"ac7_recovery_enabled", "ac7_cycle_finished_ok"}


def test_prospective_observer_class_used_by_runner_is_the_deduped_one():
    """The runner observes through ProspectiveOOSObserver whose inserts are
    the idempotent ones (contract above) — no private second writer."""
    import scanner_runner

    assert scanner_runner.ProspectiveOOSObserver is ProspectiveOOSObserver
