"""Deterministic main-loop simulations for the AC7 recovery scheduler.

Offline: the REAL ``main()`` loop AST with fake I/O and a fake clock
(:mod:`test_scanner_ac7_recovery_loop_v1`), no Bybit, no PostgreSQL.

Scenarios (issue #304 mandatory matrix):
* prolonged API outage — at most ONE expedited recovery per outage onset;
* repeated Bybit 10006 / API denial (RATE_LIMIT_RESPONSE) — never expedites;
* restart while a recovery is pending — fail-closed, no storm;
* every cycle (including the recovery rescan) is a NEW cycle with a NEW
  run_id — no cycle duplication, no run_id reuse;
* the OOS evaluator cadence is per-cycle — recovery cannot double-run it.
"""
from test_scanner_ac7_recovery_loop_v1 import (
    COMPLETE,
    SKIP_PRE_SEND,
    SKIP_RESPONSE,
    run_main_loop,
)


def _expedited(delays, normal=260):
    return [i for i, d in enumerate(delays) if d < normal]


def test_prolonged_outage_allows_exactly_one_recovery():
    """complete → 6 incomplete limiter cycles (outage) → the FIRST
    incomplete cycle gets one expedited rescan; every later cycle of the
    same outage keeps the normal schedule (no recovery storm)."""
    cycle_specs = [COMPLETE] + [(0, 50, SKIP_PRE_SEND)] * 6

    run_ids, finished, delays, _, state, start_calls = run_main_loop(
        cycle_specs)

    assert delays == [260, 130, 260, 260, 260, 260, 260]
    assert _expedited(delays) == [1]

    # The outage never re-arms the one-shot guard (no complete cycle).
    assert state["ac7_recovery_attempted"] is True

    # Each cycle — including the recovery rescan — is one ordinary loop
    # iteration: exactly one start_run per cycle, all run_ids distinct.
    assert len(start_calls) == len(cycle_specs)
    assert run_ids == list(range(1, len(cycle_specs) + 1))
    assert len(set(run_ids)) == len(run_ids)
    assert [rid for rid, _ in finished] == run_ids


def test_repeated_10006_api_denial_never_expedites():
    """Repeated Bybit 10006 answers (RATE_LIMIT_RESPONSE — the request DID
    reach Bybit) must not trigger the expedited rescan: only pre-send limiter
    denials do.  Four consecutive incomplete response-cycles after a
    complete one stay on the normal schedule and leave the guard open."""
    cycle_specs = [COMPLETE] + [(0, 50, SKIP_RESPONSE)] * 4

    run_ids, _, delays, _, state, _ = run_main_loop(cycle_specs)

    assert delays == [260, 260, 260, 260, 260]
    assert _expedited(delays) == []
    assert state["ac7_recovery_attempted"] is False


def test_mixed_response_and_pre_send_still_single_recovery():
    """A cycle with both Bybit-answered rate limits and pre-send limiter
    skips is still limiter-driven → exactly one recovery, then normal."""
    cycle_specs = [
        COMPLETE,
        (0, 50, SKIP_RESPONSE),
        (25, 25, SKIP_PRE_SEND),
        (25, 25, SKIP_PRE_SEND),
    ]

    _, _, delays, _, state, _ = run_main_loop(cycle_specs)

    assert delays == [260, 260, 130, 260]
    assert state["ac7_recovery_attempted"] is True


def test_restart_while_recovery_pending_is_fail_closed():
    """Process 1 ends with a pending (armed) recovery after an incomplete
    cycle.  After a restart the guard is initialized CLOSED
    (``ac7_recovery_attempted = True`` in main()) — the first incomplete
    cycle is NOT expedited; a complete cycle re-arms exactly one shot."""
    # --- process 1: complete, then incomplete → recovery armed.
    run_ids1, _, delays1, _, state1, _ = run_main_loop(
        [COMPLETE, (0, 50, SKIP_PRE_SEND)])
    assert delays1 == [260, 130]
    assert state1["ac7_recovery_attempted"] is True      # pending recovery

    # --- process 2 (restart): fresh state, guard closed at startup.
    cycle_specs = [
        (0, 50, SKIP_PRE_SEND),   # incomplete right after restart
        COMPLETE,                 # first complete cycle re-arms the guard
        (0, 50, SKIP_PRE_SEND),   # now exactly one recovery is allowed
        (0, 50, SKIP_PRE_SEND),   # outage continues → normal schedule
    ]
    run_ids2, _, delays2, _, state2, _ = run_main_loop(cycle_specs)

    assert delays2 == [260, 260, 130, 260]
    assert _expedited(delays2) == [2]
    assert state2["ac7_recovery_attempted"] is True

    # Restart reuses nothing: process 2 runs its own cycle ids 1..N.
    assert run_ids2 == [1, 2, 3, 4]


def test_recovery_rescan_gets_fresh_run_id_and_no_duplicate_cycles():
    """The recovery rescan is a NEW cycle: a fresh ``start_run`` row (new
    run_id, ``now()`` timestamps) and exactly one run row per cycle — the
    scheduler can neither spawn an extra cycle nor reuse a previous
    run_id."""
    cycle_specs = [
        COMPLETE,
        (0, 50, SKIP_PRE_SEND),   # incomplete → recovery scheduled
        COMPLETE,                 # the recovery rescan (130 s later)
    ]

    run_ids, finished, delays, _, _, start_calls = run_main_loop(
        cycle_specs)

    assert delays == [260, 130, 260]
    # Exactly one start_run per cycle; nothing appended for the recovery
    # decision itself (it only changes the sleep duration).
    assert len(start_calls) == len(cycle_specs)
    assert run_ids == [1, 2, 3]
    assert len(set(run_ids)) == 3
    # One finish_run per started run — no duplicated run rows.
    assert sorted(rid for rid, _ in finished) == run_ids
    # Universe snapshot persisted exactly once per run.
    assert all("symbols_total" in c for c in start_calls)


def test_oos_evaluator_cadence_unchanged_by_recovery():
    """The OOS evaluator runs exactly once per cycle (its own cadence),
    independent of the expedited sleep — a recovery cannot double-run it or
    create extra evaluator passes."""
    cycle_specs = [
        COMPLETE,
        (0, 50, SKIP_PRE_SEND),
        COMPLETE,
    ]

    run_ids, _, delays, oos_calls, _, _ = run_main_loop(
        cycle_specs,
        oos_interval=1,
        source_only=False,
    )

    assert delays == [260, 130, 260]
    assert len(oos_calls) == len(run_ids) == 3


def test_default_off_keeps_original_schedule_and_cadence():
    """SCANNER_AC7_RECOVERY_ENABLED default OFF: identical cycle count,
    identical delays, no guard bookkeeping — original Scanner behaviour."""
    cycle_specs = [COMPLETE, (0, 50, SKIP_PRE_SEND), COMPLETE]

    run_ids, finished, delays, oos_calls, state, start_calls = run_main_loop(
        cycle_specs,
        enabled=False,
        oos_interval=1,
        source_only=False,
    )

    assert delays == [260, 260, 260]
    assert len(start_calls) == 3
    assert len(oos_calls) == 3
    assert [s for _, s in finished] == ["COMPLETED", "PARTIAL", "COMPLETED"]


def test_zero_of_fifty_incident_297_shape():
    """Incident #297 shape: 0/50 with 50 limiter pre-send skips right after
    a complete cycle → one 130 s recovery, nothing else changes."""
    cycle_specs = [COMPLETE, (0, 50, SKIP_PRE_SEND), COMPLETE]

    run_ids, finished, delays, _, _, _ = run_main_loop(cycle_specs)

    assert delays == [260, 130, 260]
    assert finished[1] == (2, "PARTIAL")
    assert run_ids == [1, 2, 3]
