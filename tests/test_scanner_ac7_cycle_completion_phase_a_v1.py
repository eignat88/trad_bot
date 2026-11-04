"""SCANNER_AC7_INCOMPLETE_CYCLE_RECOVERY_V1 — Phase A (observability).

Phase A makes an incomplete Scanner cycle VISIBLE and EXPLAINABLE without
changing any behaviour:

* a full 50/50 cycle stays ``COMPLETE`` and keeps its historical event
  stream (no new ``CYCLE_COMPLETION`` row);
* 46/50 and 44/50 cycles are classified ``PARTIAL`` and carry the exact
  list of unprocessed symbols with the reason of each exclusion;
* Bybit ``retCode=10006``, ``BYBIT_API_SKIP``, ``BREAKER_OPEN``,
  ``deadline`` and the limiter queue wait are recorded as structured
  context (result / reason / queue_wait_ms / ret_code / telemetry label);
* ``dds.scanner_run.status`` keeps the exact ``COMPLETED``/``PARTIAL``
  vocabulary and the schema CHECK is untouched;
* ``run_scan_cycle``'s 3-tuple contract, ``symbols_scanned`` semantics and
  ``save_run_stat`` compatibility are preserved;
* Phase A issues **no additional Bybit request** (no recovery in this
  phase) — asserted through the HTTP call count of a real ``BybitClient``.

No PostgreSQL, no network, no trading side effect: every collaborator is an
in-memory fake.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import scanner_runner
from app.config import Settings
from app.exchange.bybit_client import (
    BybitClient,
    BybitRateLimitError,
    _RateLimiter,
)
from app.exchange.global_rate_limiter import GlobalBybitRateLimiter
from app.scanners.cycle_completion_v1 import (
    CATEGORY_FAILED,
    CATEGORY_SKIPPED,
    COMPLETION_COMPLETE,
    COMPLETION_FAILED,
    COMPLETION_PARTIAL,
    CYCLE_COMPLETION_EVENT,
    ERROR_MARKET_DATA,
    ERROR_RATE_LIMIT_SKIP,
    ERROR_SCAN,
    REQUEST_STATE_PRE_SEND_SKIP,
    REQUEST_STATE_RATE_LIMIT_RESPONSE,
    REQUEST_STATE_UNKNOWN,
    REQUEST_STATES,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    SCHEMA,
    TELEMETRY_LIMITER_SKIP,
    TELEMETRY_RATE_LIMIT,
    CycleCompletionReport,
    classify_symbol_failure,
    parse_rate_limit_denial,
    parse_rate_limit_message,
)

ROOT = Path(__file__).resolve().parent.parent

#: A 50-symbol universe: the six symbols named in the 2026-11-04 incident
#: report (cycles #162/#168/#174) plus 44 filler symbols.
SYMBOLS = [
    "DOGEUSDT", "ENAUSDT", "SKHYNIXUSDT", "1000PEPEUSDT", "XAGUSDT",
    "SPCXUSDT",
    *[f"SYM{i:02d}USDT" for i in range(44)],
]
#: The six symbols that were dropped in cycle #174 (44/50).
INCIDENT_SKIPS = ["DOGEUSDT", "ENAUSDT", "SKHYNIXUSDT", "1000PEPEUSDT",
                  "XAGUSDT", "SPCXUSDT"]
#: A filler symbol (market data / scan failures target it).
FILLER = SYMBOLS[6]
SCANNER = "TEST_SCANNER"
RUN_ID = 42


# =========================================================== collaborators


class _Repository:
    """Minimal ``ScannerRepository`` contract used by ``run_scan_cycle``."""

    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.events: list[dict] = []
        self.setups: list = []
        self.run_stats: list[tuple[str, dict]] = []
        self.finished: list[dict] = []
        self._use_pg = False

    def finish_run(self, run_id, **kwargs):
        """Records the status main() wrote to dds.scanner_run."""
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
        return [e for e in self.events if e["event_type"] == CYCLE_COMPLETION_EVENT]


class _Orchestrator:
    """Scans everything, produces no candidates (no trading side effect)."""

    scanners = {SCANNER: object()}

    def scan_all_with_stats(self, ctx, **kwargs):
        return [], {SCANNER: {
            "candidates_found": 0, "setups_saved": 0,
            "errors_count": 0, "duration_ms": 1.0,
        }}


class _ExplodingOrchestrator(_Orchestrator):
    """Market data is fine, the analysis itself raises."""

    def scan_all_with_stats(self, ctx, **kwargs):
        raise RuntimeError("simulated scan failure")


class _KlineResponse:
    status = 200
    headers = None

    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _candles_payload(rows: int = 60, start_ms: int = 1_790_000_000_000,
                     step_ms: int = 3_600_000):
    """A valid Bybit V5 kline body (newest-first, like the real endpoint)."""
    return {"retCode": 0, "retMsg": "OK", "result": {"list": [
        [str(start_ms + i * step_ms), "1", "2", "0.5", "1.5", "100"]
        for i in range(rows)
    ]}}


class _KlineSession:
    """Real ``BybitClient`` transport double.

    Symbols in ``skip`` answer with a genuine ``retCode=10006`` (the incident
    code); every other symbol receives valid candles for the 1h interval and
    empty lists elsewhere — enough for ``build_market_context`` to succeed.
    """

    def __init__(self, skip_symbols=()) -> None:
        self.skip = set(skip_symbols)
        self.calls: list[tuple] = []

    def get(self, url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        self.calls.append((url, params.get("symbol"), params.get("interval")))
        if params.get("symbol") in self.skip:
            return _KlineResponse({"retCode": 10006, "retMsg": "Too many visits",
                                   "result": {"list": []}})
        if params.get("interval") == "60":
            return _KlineResponse(_candles_payload())
        return _KlineResponse({"retCode": 0, "retMsg": "OK",
                               "result": {"list": []}})

    def post(self, *args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("POST is not expected in this test")


class _NoHttpSession:
    """Fails the test the moment a limiter deny lets an HTTP request out."""

    def __init__(self) -> None:
        self.calls: list = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        raise AssertionError(f"HTTP attempted despite limiter deny: {url}")

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(url)
        raise AssertionError(f"HTTP attempted despite limiter deny: {url}")


def _run_cycle(*, symbols, build, settings=None, report=None, repo=None,
               client=None, orchestrator=None):
    """Drive ``run_scan_cycle`` with a controlled market-context builder."""
    repo = repo if repo is not None else _Repository()
    report = report if report is not None else CycleCompletionReport()
    settings = settings if settings is not None else Settings(scanner_workers=8)
    orchestrator = orchestrator if orchestrator is not None else _Orchestrator()
    scanner_runner.build_market_context = build
    try:
        result = scanner_runner.run_scan_cycle(
            client if client is not None else object(),
            orchestrator, repo, symbols, RUN_ID, settings, report=report,
        )
    finally:
        # Always restore the module attribute even on failure.
        import app.scanners.context_builder as _cb
        scanner_runner.build_market_context = _cb.build_market_context
    return (*result, repo, report)


def _ok_build(client, symbol, settings):
    return object()


def _deny_error(symbol: str, detail: str) -> BybitRateLimitError:
    """A denial WITHOUT structured attributes — the message-parsing path."""
    if detail.startswith("rate-limit cooldown"):
        return BybitRateLimitError(
            f"request deferred: rate-limit cooldown open ({detail}")
    return BybitRateLimitError(
        f"{symbol}: /v5/market/kline deferred by shared rate limiter ({detail}) "
        "— not sent (fail-closed)")


def _build_with(skips: dict[str, BaseException]):
    def build(client, symbol, settings):
        if symbol in skips:
            raise skips[symbol]
        return object()
    return build


def _payload_of(repo: _Repository) -> dict:
    events = repo.cycle_completion_events()
    assert len(events) == 1, (
        f"exactly one CYCLE_COMPLETION event expected, got {len(events)}")
    assert events[0]["run_id"] == RUN_ID
    assert events[0]["symbol"] == ""
    return events[0]["payload"]


# ================================================================ AC1 50/50


def test_full_cycle_50_of_50_is_complete_and_writes_no_cycle_event():
    total, scanned, failed, repo, report = _run_cycle(
        symbols=SYMBOLS, build=_ok_build)

    assert (total, scanned, failed) == (0, 50, 0)
    assert report.completion_status == COMPLETION_COMPLETE
    assert report.run_status == RUN_STATUS_COMPLETED
    assert report.failed_symbols == []
    assert report.skipped_symbols == []
    assert report.scan_error_symbols == []
    assert report.processed_count == 50

    payload = report.to_payload()
    assert payload["schema"] == SCHEMA
    assert payload["expected_symbols"] == 50
    assert payload["processed_symbols"] == 50
    assert payload["failed_count"] == 0
    assert payload["skipped_count"] == 0
    json.dumps(payload)                       # JSONB-safe

    # A complete cycle keeps its historical event stream: no new row.
    assert repo.cycle_completion_events() == []
    assert repo.errors == []
    # save_run_stat compatibility: one row per scanner, symbols_scanned == 50.
    assert len(repo.run_stats) == 1
    stat_name, stat_values = repo.run_stats[0]
    assert stat_name == SCANNER
    assert stat_values["symbols_scanned"] == 50
    assert stat_values["setups_saved"] == 0
    assert stat_values["candidates_found"] == 0


def test_empty_universe_stays_complete():
    total, scanned, failed, repo, report = _run_cycle(
        symbols=[], build=_ok_build)
    assert (total, scanned, failed) == (0, 0, 0)
    assert report.completion_status == COMPLETION_COMPLETE
    assert report.run_status == RUN_STATUS_COMPLETED
    assert repo.cycle_completion_events() == []


# =============================================================== AC2 46/50


def test_partial_cycle_46_of_50_records_four_rate_limit_skips():
    skips = {
        "DOGEUSDT": _deny_error(
            "DOGEUSDT", "BREAKER_OPEN: breaker_cooldown_exceeds_deadline"),
        "ENAUSDT": _deny_error("ENAUSDT", "SKIPPED: deadline"),
        "XAGUSDT": BybitRateLimitError(
            "XAGUSDT: Rate limited on /v5/market/kline after 1 attempt(s) "
            "(rate-limit retry cap)"),
        "SPCXUSDT": BybitRateLimitError(
            "request deferred: rate-limit cooldown open (2.00s remaining)"),
    }
    symbols = [*sorted(skips), *[s for s in SYMBOLS if s not in skips]]

    total, scanned, failed, repo, report = _run_cycle(
        symbols=symbols, build=_build_with(skips))

    # 3-tuple contract + symbols_scanned semantics are unchanged.
    assert (total, scanned, failed) == (0, 46, 4)
    assert report.processed_count == 46
    assert report.completion_status == COMPLETION_PARTIAL
    assert report.run_status == RUN_STATUS_PARTIAL

    # AC2: the exact list of unprocessed symbols.
    assert sorted(report.skipped_symbols) == sorted(skips)
    assert report.failed_symbols == []
    # unprocessed == skipped (no failure happened), in universe order.
    assert report.unprocessed_symbols == report.skipped_symbols

    by_symbol = {o.symbol: o for o in report.skips}
    assert by_symbol["DOGEUSDT"].result == "BREAKER_OPEN"
    assert by_symbol["DOGEUSDT"].reason == "breaker_cooldown_exceeds_deadline"
    assert by_symbol["ENAUSDT"].result == "SKIPPED"
    assert by_symbol["ENAUSDT"].reason == "deadline"
    assert by_symbol["XAGUSDT"].result == "RATE_LIMIT"
    assert by_symbol["XAGUSDT"].reason == "rate-limit retry cap"
    assert by_symbol["SPCXUSDT"].result == "RATE_LIMIT"
    assert by_symbol["SPCXUSDT"].reason == "process_local_cooldown:2.00s"
    # P1: the transport fact is told apart per skip, not from the exception
    # type. T3/T4 = pre-send, T1 = a real Bybit answer, T6 = unknown.
    assert by_symbol["DOGEUSDT"].request_state == REQUEST_STATE_PRE_SEND_SKIP
    assert by_symbol["ENAUSDT"].request_state == REQUEST_STATE_PRE_SEND_SKIP
    assert by_symbol["XAGUSDT"].request_state == REQUEST_STATE_RATE_LIMIT_RESPONSE
    assert by_symbol["SPCXUSDT"].request_state == REQUEST_STATE_UNKNOWN
    # P2: telemetry_result is never fabricated.
    assert by_symbol["DOGEUSDT"].telemetry_result == TELEMETRY_LIMITER_SKIP
    assert by_symbol["ENAUSDT"].telemetry_result == TELEMETRY_LIMITER_SKIP
    assert by_symbol["XAGUSDT"].telemetry_result == TELEMETRY_RATE_LIMIT
    assert by_symbol["SPCXUSDT"].telemetry_result is None
    for outcome in report.skips:
        assert outcome.error_type == ERROR_RATE_LIMIT_SKIP
        assert outcome.category == CATEGORY_SKIPPED
        assert outcome.rate_limit is True
        assert outcome.request_state in REQUEST_STATES

    # dds.scanner_error rows: one per skip, typed as a rate-limit skip.
    assert len(repo.errors) == 4
    assert {e["error_type"] for e in repo.errors} == {ERROR_RATE_LIMIT_SKIP}
    assert {e["symbol"] for e in repo.errors} == set(skips)
    assert all(e["run_id"] == RUN_ID for e in repo.errors)
    assert any("breaker_cooldown_exceeds_deadline" in e["error_message"]
               for e in repo.errors)

    payload = _payload_of(repo)
    assert payload["completion_status"] == COMPLETION_PARTIAL
    assert payload["run_status"] == RUN_STATUS_PARTIAL
    assert payload["expected_symbols"] == 50
    assert payload["processed_symbols"] == 46
    assert payload["failed_count"] == 0
    assert payload["skipped_count"] == 4
    assert sorted(payload["skipped_symbols"]) == sorted(skips)
    assert sorted(payload["unprocessed_symbols"]) == sorted(skips)
    assert payload["scan_error_count"] == 0
    assert all(o["reason"] for o in payload["outcomes"])
    # P1: payload carries the transport fact, aggregated and per symbol.
    assert payload["rate_limit_response_count"] == 1
    assert payload["limiter_pre_send_skip_count"] == 2
    assert payload["request_state_unknown_count"] == 1
    assert payload["rate_limit_response_symbols"] == ["XAGUSDT"]
    assert sorted(payload["limiter_pre_send_skip_symbols"]) == [
        "DOGEUSDT", "ENAUSDT"]
    assert payload["request_state_unknown_symbols"] == ["SPCXUSDT"]
    states = {o["symbol"]: o["request_state"] for o in payload["outcomes"]}
    assert states["XAGUSDT"] == REQUEST_STATE_RATE_LIMIT_RESPONSE
    assert states["SPCXUSDT"] == REQUEST_STATE_UNKNOWN
    telemetry = {o["symbol"]: o["telemetry_result"] for o in payload["outcomes"]}
    assert telemetry["XAGUSDT"] == TELEMETRY_RATE_LIMIT
    assert telemetry["SPCXUSDT"] is None
    json.dumps(payload)

    # save_run_stat compatibility: per-scanner symbols_scanned == 46.
    assert repo.run_stats[0][1]["symbols_scanned"] == 46


def test_partial_cycle_log_summary_names_every_skip_with_its_reason():
    skips = {"DOGEUSDT": _deny_error(
        "DOGEUSDT", "BREAKER_OPEN: breaker_cooldown_exceeds_deadline")}
    symbols = ["DOGEUSDT", *[s for s in SYMBOLS if s != "DOGEUSDT"]]
    _, _, _, _, report = _run_cycle(symbols=symbols, build=_build_with(skips))

    summary = report.log_summary()
    assert "completion_status=PARTIAL" in summary
    assert "run_status=PARTIAL" in summary
    assert "expected=50" in summary
    assert "processed=49" in summary
    assert "skipped=1" in summary
    assert "DOGEUSDT:SKIPPED" in summary
    assert "reason=breaker_cooldown_exceeds_deadline" in summary


# =============================================================== AC2 44/50


def test_partial_cycle_44_of_50_real_bybit_10006_is_structured(monkeypatch):
    """Production incident shape: six symbols answered with retCode=10006.

    A REAL ``BybitClient`` classifies 10006, exhausts its rate-limit budget
    and fails closed; the report must carry the original Bybit context and
    the cycle must issue NO request beyond the historical four-per-symbol
    (Phase A adds no retry, no recovery).
    """
    monkeypatch.setattr(
        BybitClient, "_global_limiter", _RateLimiter(rps=1_000_000.0))
    skip = list(INCIDENT_SKIPS)
    symbols = [*skip, *[s for s in SYMBOLS if s not in skip]]
    settings = Settings(
        scanner_workers=8, bybit_timeout=2, bybit_max_attempts=3,
        bybit_retry_backoff=0.0, bybit_rate_limit_max_retries=0,
        bybit_rate_limit_retry_backoff=0.0, bybit_rate_limit_cooldown_s=0.0,
    )
    session = _KlineSession(skip)
    client = BybitClient(settings, session)
    repo = _Repository()
    report = CycleCompletionReport()

    total, scanned, failed = scanner_runner.run_scan_cycle(
        client, _Orchestrator(), repo, symbols, RUN_ID, settings,
        report=report)

    assert (total, scanned, failed) == (0, 44, 6)
    assert report.completion_status == COMPLETION_PARTIAL
    assert report.run_status == RUN_STATUS_PARTIAL
    assert sorted(report.skipped_symbols) == sorted(skip)
    assert report.failed_symbols == []

    for outcome in report.skips:
        assert outcome.ret_code == 10006
        assert outcome.result == "RATE_LIMIT"
        assert outcome.reason == "rate-limit retry cap"
        assert outcome.telemetry_result == TELEMETRY_RATE_LIMIT
        # T1 (review P1): the request WAS sent — Bybit itself answered.
        assert outcome.request_state == REQUEST_STATE_RATE_LIMIT_RESPONSE
        assert outcome.error_type == ERROR_RATE_LIMIT_SKIP
        assert "Rate limited on /v5/market/kline" in outcome.message

    assert {e["error_type"] for e in repo.errors} == {ERROR_RATE_LIMIT_SKIP}
    assert len(repo.errors) == 6

    payload = _payload_of(repo)
    assert payload["completion_status"] == COMPLETION_PARTIAL
    assert payload["expected_symbols"] == 50
    assert payload["processed_symbols"] == 44
    assert payload["skipped_count"] == 6
    assert sorted(payload["skipped_symbols"]) == sorted(skip)
    assert all(o["ret_code"] == 10006 for o in payload["outcomes"])
    # P1: all six were real responses, none was a pre-send limiter deny.
    assert payload["rate_limit_response_count"] == 6
    assert payload["limiter_pre_send_skip_count"] == 0
    assert payload["request_state_unknown_count"] == 0
    assert all(o["request_state"] == REQUEST_STATE_RATE_LIMIT_RESPONSE
               for o in payload["outcomes"])
    json.dumps(payload)

    # AC9 / Phase A scope: exactly one attempt per skipped symbol (the 5m
    # request that answered 10006) plus the historical four requests for
    # each of the 44 processed symbols — no extra call was introduced.
    assert len(session.calls) == 6 + 44 * 4


def test_partial_cycle_44_of_50_records_the_six_symbol_list():
    """AC2 wording: cycle 44/50 → PARTIAL + the list of six symbols."""
    skip = list(INCIDENT_SKIPS)
    skips = {
        s: _deny_error(s, "BREAKER_OPEN: breaker_cooldown_exceeds_deadline")
        for s in skip
    }
    symbols = [*skip, *[s for s in SYMBOLS if s not in skip]]
    total, scanned, failed, repo, report = _run_cycle(
        symbols=symbols, build=_build_with(skips))

    assert (total, scanned, failed) == (0, 44, 6)
    assert report.completion_status == COMPLETION_PARTIAL
    assert sorted(report.skipped_symbols) == sorted(skip)
    assert sorted(_payload_of(repo)["unprocessed_symbols"]) == sorted(skip)


# ================================================ real shared-limiter deny


def test_breaker_open_deny_is_recorded_as_bybit_api_skip_with_queue_wait(
        tmp_path, monkeypatch):
    """A genuine shared-limiter BREAKER_OPEN deny: 0/50 → FAILED, no HTTP.

    The exception is produced by the REAL client against the REAL limiter
    (tripped through the public acquire/record_result API), so the recorded
    ``limiter``/``reason``/``queue_wait_ms``/``telemetry_result`` are the
    production values of ``BYBIT_API_SKIP reason=breaker_cooldown_exceeds_deadline``.
    """
    monkeypatch.setattr(
        BybitClient, "_global_limiter", _RateLimiter(rps=1_000_000.0))
    settings = Settings(
        scanner_workers=8,
        global_bybit_rate_limiter_enabled=True,
        global_limiter_state_path=str(tmp_path / "shared_state.json"),
        global_limiter_breaker_cooldowns_s=(5.0, 30.0, 120.0),
        global_limiter_recovery_success_target=1,
        global_limiter_max_wait_s=0.01,
        bybit_timeout=2, bybit_max_attempts=1, bybit_retry_backoff=0.0,
        bybit_rate_limit_max_retries=0, bybit_rate_limit_retry_backoff=0.0,
        bybit_rate_limit_cooldown_s=0.0, bybit_retry_deadline_s=1.0,
    )
    session = _NoHttpSession()
    client = BybitClient(settings, session)
    limiter: GlobalBybitRateLimiter = client._shared_limiter()
    grant = limiter.acquire("P1")
    assert grant.granted, grant
    limiter.record_result(grant.grant_id, "P1", "RATE_LIMIT")  # trips breaker
    assert not limiter.acquire("P1").granted, "breaker did not open"

    # 1) the genuine deny carries the full structured context …
    with pytest.raises(BybitRateLimitError) as excinfo:
        scanner_runner.build_market_context(client, SYMBOLS[0], settings)
    deny = excinfo.value
    assert session.calls == []                      # … and sent no HTTP
    assert deny.limiter_result == "BREAKER_OPEN"
    assert deny.limiter_reason == "breaker_cooldown_exceeds_deadline"
    assert deny.telemetry_result == "BYBIT_API_SKIP"
    assert isinstance(deny.queue_wait_ms, int)
    assert deny.limiter_label.startswith("limiter=BREAKER_OPEN")
    assert "reason=breaker_cooldown_exceeds_deadline" in deny.limiter_label
    assert "queue_wait_ms=" in deny.limiter_label

    # 2) … and every symbol of the cycle records it the same way.
    repo = _Repository()
    report = CycleCompletionReport()
    total, scanned, failed = scanner_runner.run_scan_cycle(
        client, _Orchestrator(), repo, SYMBOLS, RUN_ID, settings,
        report=report)

    assert (total, scanned, failed) == (0, 0, 50)
    assert report.completion_status == COMPLETION_FAILED
    # DB vocabulary unchanged: a fully failed cycle still maps to PARTIAL,
    # exactly like the historical `"COMPLETED" if failed == 0 else "PARTIAL"`.
    assert report.run_status == RUN_STATUS_PARTIAL
    assert len(report.skipped_symbols) == 50
    assert all(o.result == "BREAKER_OPEN" for o in report.skips)
    assert all(o.reason == "breaker_cooldown_exceeds_deadline"
               for o in report.skips)
    assert all(o.telemetry_result == TELEMETRY_LIMITER_SKIP for o in report.skips)
    assert all(isinstance(o.queue_wait_ms, int) for o in report.skips)
    # T3 (review P1): the deny provably never reached the wire.
    assert all(o.request_state == REQUEST_STATE_PRE_SEND_SKIP
               for o in report.skips)
    assert len(repo.errors) == 50
    assert {e["error_type"] for e in repo.errors} == {ERROR_RATE_LIMIT_SKIP}

    payload = _payload_of(repo)
    assert payload["completion_status"] == COMPLETION_FAILED
    assert payload["run_status"] == RUN_STATUS_PARTIAL
    assert payload["skipped_count"] == 50
    assert all(o["telemetry_result"] == TELEMETRY_LIMITER_SKIP
               for o in payload["outcomes"])
    assert all(o["request_state"] == REQUEST_STATE_PRE_SEND_SKIP
               for o in payload["outcomes"])
    assert all(isinstance(o["queue_wait_ms"], int)
               for o in payload["outcomes"])
    assert payload["limiter_pre_send_skip_count"] == 50
    assert payload["rate_limit_response_count"] == 0
    assert payload["request_state_unknown_count"] == 0
    assert sorted(payload["limiter_pre_send_skip_symbols"]) == sorted(SYMBOLS)
    json.dumps(payload)

    # Deny path never reaches the network — and Phase A added no request.
    assert session.calls == []


# =========================================== ordinary failures stay ordinary


def test_market_data_failure_is_still_market_data_error_not_a_skip():
    def build(client, symbol, settings):
        if symbol == FILLER:
            raise ConnectionError("simulated transport failure")
        return object()

    total, scanned, failed, repo, report = _run_cycle(
        symbols=SYMBOLS, build=build)

    assert (total, scanned, failed) == (0, 49, 1)
    assert report.completion_status == COMPLETION_PARTIAL
    assert report.failed_symbols == [FILLER]
    assert report.skipped_symbols == []
    assert repo.errors[0]["error_type"] == ERROR_MARKET_DATA
    assert repo.errors[0]["symbol"] == FILLER
    payload = _payload_of(repo)
    assert payload["failed_count"] == 1
    assert payload["skipped_count"] == 0
    assert payload["failed_symbols"] == [FILLER]
    outcome = payload["outcomes"][0]
    assert outcome["symbol"] == FILLER
    assert outcome["category"] == CATEGORY_FAILED
    assert outcome["rate_limit"] is False
    assert outcome["result"] is None


def test_scan_failure_keeps_symbols_scanned_and_is_reported_separately():
    total, scanned, failed, repo, report = _run_cycle(
        symbols=SYMBOLS, build=_ok_build, orchestrator=_ExplodingOrchestrator())

    # symbols_scanned parity: the context WAS built for all 50 symbols, the
    # analysis failure only lands in the failed counter.
    assert (total, scanned, failed) == (0, 50, 50)
    assert report.processed_count == 50
    assert report.completion_status == COMPLETION_PARTIAL
    assert report.run_status == RUN_STATUS_PARTIAL
    assert len(report.scan_error_symbols) == 50
    assert all(o.error_type == ERROR_SCAN for o in report.scan_failures)
    assert all(o.stage == "SCAN" for o in report.scan_failures)
    assert all(o.category == CATEGORY_FAILED for o in report.scan_failures)
    assert report.skipped_symbols == []
    assert {e["error_type"] for e in repo.errors} == {ERROR_SCAN}
    # save_run_stat keeps its pre-existing meaning: the per-scanner row only
    # counts symbols whose ANALYSIS returned (a scan failure never reaches
    # symbol_stats) — Phase A must not change that.
    assert repo.run_stats[0][1]["symbols_scanned"] == 0
    payload = _payload_of(repo)
    assert payload["processed_symbols"] == 50
    assert payload["scan_error_count"] == 50
    assert payload["skipped_count"] == 0
    assert payload["failed_count"] == 0


# ================================================== classification unit tests


def test_classify_prefers_structured_attributes_over_message():
    exc = BybitRateLimitError(
        "DOGEUSDT: /v5/market/kline deferred by shared rate limiter "
        "(SKIPPED: deadline) — not sent (fail-closed)",
        limiter_result="SKIPPED", limiter_reason="deadline",
        queue_wait_ms=30000, telemetry_result="BYBIT_API_SKIP", ret_code=None,
    )
    outcome = classify_symbol_failure("DOGEUSDT", exc)
    assert outcome.category == CATEGORY_SKIPPED
    assert outcome.error_type == ERROR_RATE_LIMIT_SKIP
    assert outcome.result == "SKIPPED"
    assert outcome.reason == "deadline"
    assert outcome.queue_wait_ms == 30000          # AC4: the wait time
    assert outcome.telemetry_result == "BYBIT_API_SKIP"
    payload = outcome.to_payload()
    assert payload["queue_wait_ms"] == 30000
    assert payload["ret_code"] is None


def test_parse_rate_limit_message_covers_the_incident_wordings():
    assert parse_rate_limit_message(
        "X: /v5/market/kline deferred by shared rate limiter "
        "(BREAKER_OPEN: breaker_cooldown_exceeds_deadline) — not sent "
        "(fail-closed)") == ("BREAKER_OPEN", "breaker_cooldown_exceeds_deadline")
    assert parse_rate_limit_message(
        "X: /v5/market/kline deferred by shared rate limiter (SKIPPED: deadline) "
        "— not sent (fail-closed)") == ("SKIPPED", "deadline")
    assert parse_rate_limit_message(
        "X: Rate limited on /v5/market/kline after 1 attempt(s) "
        "(rate-limit retry cap)") == ("RATE_LIMIT", "rate-limit retry cap")
    assert parse_rate_limit_message(
        "request deferred: rate-limit cooldown open (12.34s remaining)"
    ) == ("RATE_LIMIT", "process_local_cooldown:12.34s")


def test_report_status_edge_cases():
    empty = CycleCompletionReport(expected_symbols=0)
    assert empty.completion_status == COMPLETION_COMPLETE
    assert empty.run_status == RUN_STATUS_COMPLETED

    all_failed = CycleCompletionReport(expected_symbols=3)
    for i in range(3):
        all_failed.record_outcome(classify_symbol_failure(
            f"S{i}", ConnectionError("down")))
    assert all_failed.completion_status == COMPLETION_FAILED
    assert all_failed.run_status == RUN_STATUS_PARTIAL   # DB vocab mapping

    partial = CycleCompletionReport(expected_symbols=3)
    partial.record_processed("S0")
    partial.record_outcome(classify_symbol_failure("S1", ConnectionError("x")))
    assert partial.completion_status == COMPLETION_PARTIAL


# ================================================ compatibility / guards


def test_schema_and_run_status_vocabulary_are_untouched():
    schema = (ROOT / "app" / "db" / "schema.sql").read_text(encoding="utf-8")
    assert (
        "status IN ('RUNNING', 'COMPLETED', 'FAILED', 'PARTIAL', 'ABORTED')"
        in schema
    ), "dds.scanner_run.status CHECK must stay byte-for-byte unchanged"
    # Phase A must never write the invalid 'COMPLETE' value to the DB.
    assert "status=\"COMPLETE\"" not in scanner_runner.__doc__
    source = (ROOT / "scanner_runner.py").read_text(encoding="utf-8")
    assert "status=\"COMPLETE\"" not in source
    assert "status='COMPLETE'" not in source


def test_scanner_error_error_type_has_no_filtered_consumer():
    """Review P2 (compatibility): ``RATE_LIMIT_SKIP`` cannot drop a metric.

    Audit of every ``dds.scanner_error`` consumer:

    * ``mart/002_scanner_overview`` counts ALL rows (``COUNT(*)``);
    * ``mart/008_scanner_health`` counts ALL rows (1h/24h windows) and takes
      ``MAX(created_at)``;
    * ``system-health{,-v2}`` Grafana panels SELECT ``error_type`` as a
      column — they never filter on it;
    * ``sql/research/me_short_a_source_intake_diagnostics_v1`` filters on a
      different literal (``ME_SHORT_A_SOURCE_INTAKE_ERROR``);
    * Python only INSERTs (``ScannerRepository.save_error``) — no reader.

    So renaming a limiter denial from ``MARKET_DATA_ERROR`` to
    ``RATE_LIMIT_SKIP`` keeps the row count identical (exactly one row per
    source failure) and only changes the label shown for those rows. This
    test keeps the "no filtered consumer" property true.
    """
    offenders: list[str] = []
    for root in ("sql", "monitoring"):
        for path in sorted((ROOT / root).rglob("*")):
            if path.suffix not in {".sql", ".json"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for literal in ("MARKET_DATA_ERROR", "RATE_LIMIT_SKIP"):
                if literal in text:
                    offenders.append(f"{path.relative_to(ROOT)}:{literal}")
    assert offenders == [], f"error_type literal used by a consumer: {offenders}"
    # the counting marts must not gain a type filter later on either
    for mart in ("002_scanner_overview.sql", "008_scanner_health.sql"):
        text = (ROOT / "sql" / "mart" / mart).read_text(encoding="utf-8")
        assert "error_type" not in text, mart
    # no Python reader over dds.scanner_error exists
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "FROM DDS.SCANNER_ERROR" not in text.upper(), path


def test_main_logs_completion_status_and_uses_the_preserved_run_status():
    source = (ROOT / "scanner_runner.py").read_text(encoding="utf-8")
    main_src = source[source.index("def main()"):]
    # explicit PARTIAL in the operator's cycle-done line …
    assert "completion_status=%s" in main_src
    assert "status=%s" in main_src
    # … a dedicated warning naming every dropped symbol …
    assert 'cycle #%d incomplete: %s' in main_src
    # … and the DB column keeps the preserved mapping.
    assert "status=report.run_status" in main_src
    # The report is passed in as an out-parameter without changing the
    # positional contract of run_scan_cycle.
    assert "report=report" in main_src
    assert "prospective_obs=prospective_obs" in main_src


def test_structural_market_data_contract_is_preserved():
    """The Issue #253 fail-closed block must keep its exact shape."""
    source = (ROOT / "scanner_runner.py").read_text(encoding="utf-8")
    result_at = source.index("ctx = future.result()")
    scan_at = source.index("orchestrator.scan_all_with_stats")
    setup_at = source.index("repository.save_setup")
    assert result_at < setup_at
    block = source[result_at:scan_at]
    assert "except Exception" in block
    assert 'error_type="MARKET_DATA_ERROR"' in block
    assert block.index("except Exception") < block.index("continue")


def test_run_scan_cycle_contract_is_unchanged():
    import ast

    tree = ast.parse((ROOT / "scanner_runner.py").read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "run_scan_cycle")
    positional = [a.arg for a in node.args.args]
    # Historical positional contract preserved, report added keyword-only.
    assert positional == [
        "client", "orchestrator", "repository", "symbols", "run_id",
        "settings", "expectancy_filter", "prospective_obs",
        "me_early_mae_v2_pipeline", "me_capture_spool",
    ]
    assert [a.arg for a in node.args.kwonlyargs] == ["report"]
    assert node.args.kw_defaults[-1] is not None      # report defaults to None


# ======================================= review SCANNER_AC7_PHASE_A fixes
# (P1 response-vs-pre-send, P2 telemetry / error_type / abort path)


class _Status429Session:
    """Every public GET answers HTTP 429 — a rate limit with no body."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get(self, url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        self.calls.append((url, params.get("symbol"), params.get("interval")))
        return _Status429Response()


class _Status429Response:
    status = 429
    reason = "Too Many Requests"
    headers = None

    def raise_for_status(self):       # never reached: 429 branches first
        raise AssertionError("the 429 branch must run before raise_for_status")

    def json(self):                   # never reached either
        raise AssertionError("HTTP 429 carries no body")


def test_t2_http_429_is_rate_limit_response_without_ret_code(monkeypatch):
    """T2: an HTTP 429 IS a sent request — and it has no body ``retCode``."""
    monkeypatch.setattr(
        BybitClient, "_global_limiter", _RateLimiter(rps=1_000_000.0))
    settings = Settings(
        scanner_workers=8, bybit_timeout=2, bybit_max_attempts=3,
        bybit_retry_backoff=0.0, bybit_rate_limit_max_retries=0,
        bybit_rate_limit_retry_backoff=0.0, bybit_rate_limit_cooldown_s=0.0,
    )
    session = _Status429Session()
    client = BybitClient(settings, session)
    repo = _Repository()
    report = CycleCompletionReport()

    total, scanned, failed = scanner_runner.run_scan_cycle(
        client, _Orchestrator(), repo, SYMBOLS, RUN_ID, settings,
        report=report)

    assert (total, scanned, failed) == (0, 0, 50)
    assert report.completion_status == COMPLETION_FAILED
    for outcome in report.skips:
        assert outcome.request_state == REQUEST_STATE_RATE_LIMIT_RESPONSE
        assert outcome.telemetry_result == TELEMETRY_RATE_LIMIT
        assert outcome.result == TELEMETRY_RATE_LIMIT
        assert outcome.ret_code is None            # 429 has no body retCode
        assert "Rate limited on" in outcome.message
    payload = _payload_of(repo)
    assert payload["rate_limit_response_count"] == 50
    assert payload["limiter_pre_send_skip_count"] == 0
    # exactly one attempt per symbol: no retry, no recovery pass
    assert len(session.calls) == 50


def test_t4_t5_pre_send_messages_are_limiter_pre_send_skip():
    """T4/T5: shared-limiter refusals are proven NOT to have been sent."""
    deadline = classify_symbol_failure("ENAUSDT", BybitRateLimitError(
        "ENAUSDT: /v5/market/kline deferred by shared rate limiter "
        "(SKIPPED: deadline) — not sent (fail-closed)"))
    assert deadline.request_state == REQUEST_STATE_PRE_SEND_SKIP
    assert deadline.telemetry_result == TELEMETRY_LIMITER_SKIP
    assert (deadline.result, deadline.reason) == ("SKIPPED", "deadline")

    revoked = classify_symbol_failure("XAGUSDT", BybitRateLimitError(
        "XAGUSDT: /v5/market/kline limiter grant revoked before send "
        "(epoch moved) — not sent (fail-closed)"))
    assert revoked.request_state == REQUEST_STATE_PRE_SEND_SKIP
    assert revoked.telemetry_result == TELEMETRY_LIMITER_SKIP
    assert (revoked.result, revoked.reason) == ("REVOKED", "grant_revoked")

    unavailable = classify_symbol_failure("SPCXUSDT", BybitRateLimitError(
        "SPCXUSDT: /v5/market/kline shared rate limiter unavailable "
        "(TimeoutError) — not sent (fail-closed)"))
    assert unavailable.request_state == REQUEST_STATE_PRE_SEND_SKIP
    assert unavailable.telemetry_result == TELEMETRY_LIMITER_SKIP
    assert unavailable.result == "LIMITER_UNAVAILABLE"

    # structured attributes alone are enough (no message parsing needed)
    structured = classify_symbol_failure("DOGEUSDT", BybitRateLimitError(
        "any message", limiter_result="BREAKER_OPEN",
        limiter_reason="breaker_cooldown_exceeds_deadline"))
    assert structured.request_state == REQUEST_STATE_PRE_SEND_SKIP
    assert structured.telemetry_result == TELEMETRY_LIMITER_SKIP

    # a real Bybit answer parsed out of the message is the OPPOSITE story
    response = classify_symbol_failure("XAGUSDT", BybitRateLimitError(
        "XAGUSDT: Rate limited on /v5/market/kline after 2 attempt(s) "
        "(rate-limit retry cap)"))
    assert response.request_state == REQUEST_STATE_RATE_LIMIT_RESPONSE
    assert response.telemetry_result == TELEMETRY_RATE_LIMIT
    assert response.ret_code is None


def test_t6_local_cooldown_is_unknown_without_invented_telemetry():
    """T6: the process-local cooldown proves neither story — no guessing."""
    outcome = classify_symbol_failure("SPCXUSDT", BybitRateLimitError(
        "request deferred: rate-limit cooldown open (12.34s remaining)"))
    assert outcome.rate_limit is True
    assert outcome.category == CATEGORY_SKIPPED
    assert outcome.request_state == REQUEST_STATE_UNKNOWN
    assert outcome.telemetry_result is None      # never a fabricated skip
    assert outcome.reason == "process_local_cooldown:12.34s"


def test_t7_legacy_error_keeps_every_field_unknown():
    """T7: a pre-Phase-A BybitRateLimitError carries no facts — none appear."""
    legacy = classify_symbol_failure(
        "OLDUSDT", BybitRateLimitError("OLDUSDT: some unrecognised wording"))
    assert legacy.rate_limit is True
    assert legacy.category == CATEGORY_SKIPPED
    assert legacy.result == TELEMETRY_RATE_LIMIT
    assert legacy.reason is None
    assert legacy.request_state == REQUEST_STATE_UNKNOWN
    assert legacy.telemetry_result is None
    assert legacy.queue_wait_ms is None
    assert legacy.ret_code is None

    # an ordinary failure has no transport state at all
    plain = classify_symbol_failure("XUSDT", ConnectionError("boom"))
    assert plain.rate_limit is False
    assert plain.request_state is None
    assert plain.telemetry_result is None
    assert plain.error_type == ERROR_MARKET_DATA


def test_t11_abort_path_keeps_failed_status_and_cannot_mask(
        caplog, monkeypatch):
    """T11: the abort diagnostics never touch run status, never raise."""
    caplog.set_level("WARNING")
    report = CycleCompletionReport()
    report.expected_symbols = 50
    report.record_processed("DOGEUSDT")
    report.record_outcome(classify_symbol_failure("ENAUSDT", BybitRateLimitError(
        "ENAUSDT: /v5/market/kline deferred by shared rate limiter "
        "(BREAKER_OPEN: breaker_cooldown_exceeds_deadline) — not sent "
        "(fail-closed)")))

    repo = _Repository()
    scanner_runner.report_aborted_cycle(repo, report, cycle=175, run_id=RUN_ID)

    # the helper must not finish/write the run row — main() already did
    assert repo.finished == []
    assert repo.errors == []                      # no double error rows
    events = repo.cycle_completion_events()
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["aborted"] is True
    assert payload["run_status"] == RUN_STATUS_FAILED    # status is FAILED
    assert payload["completion_status"] == COMPLETION_PARTIAL
    assert payload["processed_symbols"] == 1
    assert payload["limiter_pre_send_skip_count"] == 1
    assert "cycle #175 aborted" in caplog.text
    assert "run_status=FAILED" in caplog.text
    json.dumps(payload)

    # at most ONE diagnostic row per cycle, even if the helper runs again
    scanner_runner.report_aborted_cycle(repo, report, cycle=175, run_id=RUN_ID)
    assert len(repo.cycle_completion_events()) == 1

    # a cycle that never started produces no diagnostic row at all
    untouched = _Repository()
    scanner_runner.report_aborted_cycle(
        untouched, CycleCompletionReport(), cycle=176, run_id=None)
    assert untouched.cycle_completion_events() == []
    assert untouched.finished == []

    # a broken diagnostics store must not raise out of the helper
    class _BrokenEventRepo(_Repository):
        def save_event(self, *args, **kwargs):
            raise RuntimeError("diagnostics storage unavailable")

    broken = _BrokenEventRepo()
    scanner_runner.report_aborted_cycle(broken, report, cycle=177, run_id=None)
    assert broken.finished == []


def test_t11_main_writes_failed_before_abort_diagnostics():
    """Ordering guard: original exception → FAILED row → diagnostics."""
    source = (ROOT / "scanner_runner.py").read_text(encoding="utf-8")
    main_src = source[source.index("def main()"):]
    exception_at = main_src.index('logger.exception("cycle #%d failed"')
    failed_at = main_src.index('status="FAILED", error_count=1')
    diagnostics_at = main_src.index(
        "report_aborted_cycle(repository, report, cycle, run_id)")
    assert exception_at < failed_at < diagnostics_at
    # the report object exists before the try, so it is always bound here
    assert main_src.index("report = CycleCompletionReport()") < exception_at
    # the helper itself swallows its own failures
    helper_src = source[
        source.index("def report_aborted_cycle("): source.index("def main()")]
    assert helper_src.count("except Exception") >= 2
    assert "finish_run" not in helper_src          # never touches status


def test_t12_diagnostic_save_failure_never_changes_the_cycle_result():
    """T12: a failing CYCLE_COMPLETION write cannot alter the cycle."""
    class _BrokenEventRepo(_Repository):
        def save_event(self, *args, **kwargs):
            raise RuntimeError("diagnostics storage unavailable")

    skip = {"DOGEUSDT": _deny_error(
        "DOGEUSDT", "BREAKER_OPEN: breaker_cooldown_exceeds_deadline")}
    symbols = ["DOGEUSDT", *[s for s in SYMBOLS if s != "DOGEUSDT"]]
    repo = _BrokenEventRepo()
    total, scanned, failed, repo, report = _run_cycle(
        symbols=symbols, build=_build_with(skip), repo=repo)

    assert (total, scanned, failed) == (0, 49, 1)
    assert report.completion_status == COMPLETION_PARTIAL
    assert report.run_status == RUN_STATUS_PARTIAL
    # the regular writes still happened: errors + per-scanner run stats
    assert len(repo.errors) == 1
    assert repo.errors[0]["error_type"] == ERROR_RATE_LIMIT_SKIP
    assert repo.run_stats[0][1]["symbols_scanned"] == 49
    assert repo.finished == []
