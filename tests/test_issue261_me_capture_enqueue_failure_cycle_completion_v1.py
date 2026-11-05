"""Issue #261 — a ME capture spool enqueue failure must stay PARTIAL.

Root cause under test
---------------------
``run_scan_cycle`` counted a failed ``me_capture_spool.enqueue()`` in its
``failed`` counter (both in the regular-candidate branch and in the
OBSERVE_ONLY branch) but recorded NOTHING in the ``CycleCompletionReport``.
Consequence in production: ``dds.scanner_run.symbols_failed > 0`` while
``status=COMPLETED``, and no ``CYCLE_COMPLETION`` diagnostic row at all.

What this file asserts
----------------------

* an enqueue failure (regular ME candidate AND OBSERVE_ONLY ME candidate)
  makes the cycle ``completion_status=PARTIAL`` / ``run_status=PARTIAL`` and
  produces EXACTLY ONE ``CYCLE_COMPLETION`` event carrying the cause;
* the failure is classified ``ME_CAPTURE_ERROR`` — never
  ``MARKET_DATA_ERROR``, ``RATE_LIMIT_SKIP`` or ``SCAN_ERROR`` — and no
  ``dds.scanner_error`` row is invented for it;
* a successful enqueue keeps the full cycle ``COMPLETED`` with the
  historical event stream (no new row);
* counters stay consistent: the returned ``failed`` == ``report.failed_total``
  == payload ``failed_total`` == ``symbols_failed`` even when one symbol
  accumulates several errors, with no double counting;
* the trading side is untouched: the setup is still saved, the 3-tuple
  contract of ``run_scan_cycle`` is unchanged.

No PostgreSQL, no network, no live spool: every collaborator is a fake.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import scanner_runner
from app.config import Settings
from app.scanners.cycle_completion_v1 import (
    CATEGORY_FAILED,
    COMPLETION_COMPLETE,
    COMPLETION_PARTIAL,
    CYCLE_COMPLETION_EVENT,
    ERROR_MARKET_DATA,
    ERROR_ME_CAPTURE,
    ERROR_RATE_LIMIT_SKIP,
    ERROR_SCAN,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_PARTIAL,
    SCHEMA,
    STAGE_ME_CAPTURE,
    CycleCompletionReport,
    classify_me_capture_failure,
)
from app.scanners.models import SetupCandidate, SetupState
from app.exchange.bybit_client import BybitRateLimitError

ME_NAME = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"
RUN_ID = 6161
SPOOL_ERROR = "simulated spool write failure"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]


# =========================================================== collaborators


class _Repository:
    """Minimal ``ScannerRepository`` contract used by ``run_scan_cycle``."""

    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.events: list[dict] = []
        self.setups: list = []
        self.run_stats: list[tuple[str, dict]] = []
        self.finished: list[dict] = []

    def finish_run(self, run_id, **kwargs):
        self.finished.append({"run_id": run_id, **kwargs})

    def save_error(self, *, symbol, scanner_name, error_type, error_message,
                   run_id=None, retry_count=0):
        self.errors.append({
            "symbol": symbol, "scanner_name": scanner_name,
            "error_type": error_type, "error_message": error_message,
            "run_id": run_id,
        })

    def save_event(self, event_type, scanner_name, symbol, run_id=None, **kwargs):
        self.events.append({
            "event_type": event_type, "scanner_name": scanner_name,
            "symbol": symbol, "run_id": run_id,
            "payload": kwargs.get("payload"),
        })

    def save_setup(self, candidate, run_id=None):
        self.setups.append(candidate)

    def save_run_stat(self, run_id, scanner_name, **values):
        self.run_stats.append((scanner_name, dict(values)))

    def cycle_completion_events(self) -> list[dict]:
        return [e for e in self.events
                if e["event_type"] == CYCLE_COMPLETION_EVENT]


class _Orchestrator:
    """Scans every symbol; optionally emits ME candidates (regular or
    OBSERVE_ONLY) and/or fails the scan of selected symbols."""

    def __init__(self, candidates=None, observe_candidates=None,
                 fail_symbols=()) -> None:
        self.scanners = {ME_NAME: object()}
        self.candidates = dict(candidates or {})
        self.observe_candidates = dict(observe_candidates or {})
        self.fail_symbols = set(fail_symbols)

    def scan_all_with_stats(self, ctx, **kwargs):
        symbol = getattr(ctx, "symbol", None)
        if symbol in self.fail_symbols:
            raise RuntimeError(f"simulated scan failure for {symbol}")
        candidates = list(self.candidates.get(symbol, []))
        stats = {
            ME_NAME: {
                "symbols_scanned": 1,
                "candidates_found": len(candidates),
                "setups_saved": 0,
                "errors_count": 0,
                "duration_ms": 1.0,
            },
        }
        observe = list(self.observe_candidates.get(symbol, []))
        if observe:
            stats["_observe_candidates"] = observe
        return candidates, stats


class _ExplodingSpool:
    """Real spool behaviour on a dead/closed SQLite handle (Issue #261)."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def enqueue(self, candidate) -> None:
        self.attempts.append(candidate.symbol)
        raise RuntimeError(SPOOL_ERROR)


class _RecordingSpool:
    def __init__(self) -> None:
        self.enqueued: list = []

    def enqueue(self, candidate) -> None:
        self.enqueued.append(candidate)


def _me_candidate(symbol: str) -> SetupCandidate:
    return SetupCandidate(
        scanner_name=ME_NAME,
        scanner_version="1.2.0",
        symbol=symbol,
        direction="LONG",
        detected_at=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc),
        state=SetupState.SETUP_READY,     # scanners' default: overridden
        score=70.0,                        # downstream to DETECTED anyway
        features={"oos_clean_observer_version": "1.2.0"},
    )


def _ok_build(client, symbol, settings):
    return SimpleNamespace(symbol=symbol)


def _run_cycle(*, symbols, orchestrator, spool, report=None, repo=None):
    repo = repo if repo is not None else _Repository()
    report = report if report is not None else CycleCompletionReport()
    settings = Settings(scanner_workers=8)
    scanner_runner.build_market_context = _ok_build
    try:
        result = scanner_runner.run_scan_cycle(
            object(), orchestrator, repo, symbols, RUN_ID, settings,
            me_capture_spool=spool, report=report,
        )
    finally:
        import app.scanners.context_builder as _cb
        scanner_runner.build_market_context = _cb.build_market_context
    return (*result, repo, report)


def _payload_of(repo: _Repository) -> dict:
    events = repo.cycle_completion_events()
    assert len(events) == 1, (
        f"exactly one CYCLE_COMPLETION event expected, got {len(events)}")
    assert events[0]["run_id"] == RUN_ID
    return events[0]["payload"]


def _me_outcomes(payload: dict) -> list[dict]:
    return [o for o in payload["outcomes"]
            if o["error_type"] == ERROR_ME_CAPTURE]


# ============================================== regular ME candidate branch


def test_regular_me_candidate_enqueue_failure_is_partial_with_one_event():
    spool = _ExplodingSpool()
    orchestrator = _Orchestrator(
        candidates={"BTCUSDT": [_me_candidate("BTCUSDT")]})

    total, scanned, failed, repo, report = _run_cycle(
        symbols=SYMBOLS, orchestrator=orchestrator, spool=spool)

    # 3-tuple contract unchanged: the failed enqueue still counts in
    # ``failed`` exactly as before (Issue: only the REPORTING was missing).
    assert (total, scanned, failed) == (1, 2, 1)
    assert spool.attempts == ["BTCUSDT"]

    # THE FIX: the report now knows about the loss.
    assert report.processed_count == 2          # both contexts were built
    assert report.completion_status == COMPLETION_PARTIAL
    assert report.run_status == RUN_STATUS_PARTIAL
    assert report.me_capture_error_symbols == ["BTCUSDT"]
    assert report.failed_symbols == []
    assert report.skipped_symbols == []
    assert report.scan_error_symbols == []
    assert report.failed_total == failed == 1

    # No misclassification: nothing was written to dds.scanner_error.
    assert repo.errors == []
    # Trading side untouched: the setup is still persisted.
    assert len(repo.setups) == 1

    payload = _payload_of(repo)
    assert payload["schema"] == SCHEMA
    assert payload["completion_status"] == COMPLETION_PARTIAL
    assert payload["run_status"] == RUN_STATUS_PARTIAL
    assert payload["expected_symbols"] == 2
    assert payload["processed_symbols"] == 2
    assert payload["failed_count"] == 0
    assert payload["skipped_count"] == 0
    assert payload["scan_error_count"] == 0
    assert payload["me_capture_error_count"] == 1
    assert payload["me_capture_error_symbols"] == ["BTCUSDT"]
    assert payload["failed_total"] == 1
    # the capture-loss symbol is PROCESSED, never "unprocessed"
    assert payload["unprocessed_symbols"] == []
    assert payload["failed_symbols"] == []

    outcomes = _me_outcomes(payload)
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["symbol"] == "BTCUSDT"
    assert outcome["stage"] == STAGE_ME_CAPTURE
    assert outcome["category"] == CATEGORY_FAILED
    assert outcome["rate_limit"] is False
    assert outcome["request_state"] is None
    assert SPOOL_ERROR in outcome["message"]
    json.dumps(payload)                          # JSONB-safe


# ================================================== OBSERVE_ONLY branch


def test_observe_only_me_candidate_enqueue_failure_is_partial_with_one_event():
    spool = _ExplodingSpool()
    orchestrator = _Orchestrator(
        observe_candidates={"ETHUSDT": [_me_candidate("ETHUSDT")]})

    total, scanned, failed, repo, report = _run_cycle(
        symbols=SYMBOLS, orchestrator=orchestrator, spool=spool)

    assert (total, scanned, failed) == (0, 2, 1)
    assert spool.attempts == ["ETHUSDT"]

    assert report.completion_status == COMPLETION_PARTIAL
    assert report.run_status == RUN_STATUS_PARTIAL
    assert report.me_capture_error_symbols == ["ETHUSDT"]
    assert report.failed_symbols == []
    assert report.scan_error_symbols == []
    assert report.failed_total == failed == 1
    assert repo.errors == []
    assert len(repo.setups) == 1                 # DETECTED, still saved

    payload = _payload_of(repo)
    assert payload["completion_status"] == COMPLETION_PARTIAL
    assert payload["run_status"] == RUN_STATUS_PARTIAL
    assert payload["me_capture_error_count"] == 1
    assert payload["me_capture_error_symbols"] == ["ETHUSDT"]
    assert payload["failed_total"] == 1
    assert payload["failed_count"] == payload["scan_error_count"] == 0
    outcomes = _me_outcomes(payload)
    assert len(outcomes) == 1
    assert SPOOL_ERROR in outcomes[0]["message"]
    json.dumps(payload)


# ============================================== successful enqueue stays COMPLETED


def test_successful_enqueue_keeps_the_cycle_completed_without_new_event():
    spool = _RecordingSpool()
    orchestrator = _Orchestrator(
        candidates={"BTCUSDT": [_me_candidate("BTCUSDT")]})

    total, scanned, failed, repo, report = _run_cycle(
        symbols=SYMBOLS, orchestrator=orchestrator, spool=spool)

    assert (total, scanned, failed) == (1, 2, 0)
    assert len(spool.enqueued) == 1
    assert report.completion_status == COMPLETION_COMPLETE
    assert report.run_status == RUN_STATUS_COMPLETED
    assert report.failed_total == 0
    # historical event stream: a complete cycle writes NO new row
    assert repo.cycle_completion_events() == []
    assert repo.errors == []
    payload = report.to_payload()
    assert payload["me_capture_error_count"] == 0
    assert payload["me_capture_error_symbols"] == []
    assert payload["failed_total"] == 0
    json.dumps(payload)


# ============================ enqueue failure together with a scan failure


def test_enqueue_failure_plus_scan_error_counts_each_once():
    spool = _ExplodingSpool()
    orchestrator = _Orchestrator(
        candidates={"BTCUSDT": [_me_candidate("BTCUSDT")]},
        fail_symbols={"ETHUSDT"},
    )

    total, scanned, failed, repo, report = _run_cycle(
        symbols=SYMBOLS, orchestrator=orchestrator, spool=spool)

    # both contexts were built; the two DIFFERENT failures count once each
    assert (total, scanned, failed) == (1, 2, 2)
    assert report.processed_count == 2
    assert report.completion_status == COMPLETION_PARTIAL
    assert report.run_status == RUN_STATUS_PARTIAL

    # no double accounting: each error lands in exactly one class
    assert report.scan_error_symbols == ["ETHUSDT"]
    assert report.me_capture_error_symbols == ["BTCUSDT"]
    assert report.failed_symbols == []
    assert report.failed_total == failed == 2

    # only the ordinary scan error reaches dds.scanner_error
    assert len(repo.errors) == 1
    assert repo.errors[0]["error_type"] == ERROR_SCAN
    assert repo.errors[0]["symbol"] == "ETHUSDT"

    payload = _payload_of(repo)
    assert payload["completion_status"] == COMPLETION_PARTIAL
    assert payload["scan_error_count"] == 1
    assert payload["me_capture_error_count"] == 1
    assert payload["failed_count"] == 0
    assert payload["failed_total"] == 2          # == the run's symbols_failed
    assert payload["scan_error_symbols"] == ["ETHUSDT"]
    assert payload["me_capture_error_symbols"] == ["BTCUSDT"]
    json.dumps(payload)


# ============================ several enqueue failures on ONE symbol


def test_two_enqueue_failures_on_one_symbol_count_twice_listed_once():
    spool = _ExplodingSpool()
    orchestrator = _Orchestrator(
        candidates={"BTCUSDT": [_me_candidate("BTCUSDT"),
                                _me_candidate("BTCUSDT")]})

    total, scanned, failed, repo, report = _run_cycle(
        symbols=SYMBOLS, orchestrator=orchestrator, spool=spool)

    # ``failed`` counts every lost capture (unchanged historical behaviour)
    assert (total, scanned, failed) == (2, 2, 2)
    assert report.completion_status == COMPLETION_PARTIAL
    assert report.run_status == RUN_STATUS_PARTIAL
    assert report.failed_total == failed == 2

    # per-failure count vs per-symbol list
    assert len(report.me_capture_failures) == 2
    assert report.me_capture_error_symbols == ["BTCUSDT"]

    payload = _payload_of(repo)
    assert payload["me_capture_error_count"] == 2
    assert payload["me_capture_error_symbols"] == ["BTCUSDT"]
    assert payload["failed_total"] == 2
    assert len(_me_outcomes(payload)) == 2
    json.dumps(payload)


# ================================================== classification contract


def test_classify_me_capture_failure_is_its_own_error_class():
    outcome = classify_me_capture_failure("BTCUSDT", RuntimeError(SPOOL_ERROR))
    assert outcome.stage == STAGE_ME_CAPTURE
    assert outcome.error_type == ERROR_ME_CAPTURE
    assert outcome.category == CATEGORY_FAILED
    assert outcome.rate_limit is False
    assert outcome.result is None
    assert outcome.reason is None
    assert outcome.request_state is None
    assert outcome.telemetry_result is None
    assert SPOOL_ERROR in outcome.message
    # never one of the three existing classes
    assert ERROR_ME_CAPTURE not in (ERROR_MARKET_DATA, ERROR_RATE_LIMIT_SKIP,
                                    ERROR_SCAN)
    assert outcome.error_type not in (
        ERROR_MARKET_DATA, ERROR_RATE_LIMIT_SKIP, ERROR_SCAN)

    # even a limiter-flavoured exception keeps its own class (no Bybit/limiter
    # request is involved in a local spool write)
    limiter = classify_me_capture_failure(
        "BTCUSDT", BybitRateLimitError("10006: rate limited"))
    assert limiter.error_type == ERROR_ME_CAPTURE
    assert limiter.rate_limit is False

    # record_outcome routes it into its own bucket, leaving the others intact
    report = CycleCompletionReport(expected_symbols=1)
    report.record_processed("BTCUSDT")
    report.record_outcome(outcome)
    assert report.me_capture_failures == [outcome]
    assert report.failures == [] and report.skips == []
    assert report.scan_failures == []
    assert report.completion_status == COMPLETION_PARTIAL
    assert report.run_status == RUN_STATUS_PARTIAL
    assert report.is_complete is False


def test_log_summary_names_the_me_capture_loss():
    spool = _ExplodingSpool()
    orchestrator = _Orchestrator(
        candidates={"BTCUSDT": [_me_candidate("BTCUSDT")]})
    _, _, _, _, report = _run_cycle(
        symbols=SYMBOLS, orchestrator=orchestrator, spool=spool)

    summary = report.log_summary()
    assert "completion_status=PARTIAL" in summary
    assert "run_status=PARTIAL" in summary
    assert "me_capture_errors=1" in summary
    assert "BTCUSDT:FAILED" in summary
    assert f"result={ERROR_ME_CAPTURE}" in summary
