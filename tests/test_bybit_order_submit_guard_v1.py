"""Order-submit guard hardening tests — code reviews #5709376849 / #5709889450.

Coverage:
* indeterminate create_order outcome is NEVER released by a timer — only
  definitive evidence (parsed API result, HTTP 4xx, or STRICT exchange
  confirmation) lifts the block;
* safe operation identity: every submission carries an ``orderLinkId``
  recorded in the persistent journal before the send;
* restart recovery: journal state survives a process crash; a crash while
  ``in_flight`` reloads as ``unknown`` (fail-closed);
* concurrent POSTs: ``in_flight`` persists until the operation actually
  finishes (blocked first HTTP request, second identical call refused);
* two guard instances on one journal path serialize correctly;
* strict reconciliation identity (review #5709889450 P1): exact non-empty
  ``orderLinkId`` + symbol + category + accepted order status — every other
  answer is an explicit UNRESOLVED BLOCKER (parameterized acceptance table);
* durable journal persistence (review #5709889450 P2): atomic unique temp
  file + flush + fsync + os.replace + dir fsync, checksum + schema
  validation, fault injection at every write stage, corrupted journals fail
  closed and are NEVER auto-reset to an empty one;
* manual operator resolution (review #5709889450 P2): NOT IMPLEMENTED —
  the UNKNOWN block is kept, free-form CLI strings never unlock.

All tests run on fake transport — no Bybit, no real orders, no network.
Cross-process tests live in test_bybit_order_submit_guard_multiproc_v1.py.
"""
from __future__ import annotations

import json
import threading

import pytest

import app.exchange.bybit_client as bybit_client_module
from app.config import Settings
from app.exchange import retry_budget
from app.exchange.bybit_client import (
    BybitClient,
    BybitError,
    BybitOrderUncertainError,
    _RateLimiter,
)


class FakeResponse:
    def __init__(self, payload=None, status=200, http_error=False):
        self.payload = payload if payload is not None else {
            "retCode": 0, "retMsg": "OK", "result": {"list": []},
        }
        self.status = status
        self.headers = None
        self._http_error = http_error

    def raise_for_status(self):
        if self._http_error or not 200 <= self.status < 300:
            raise BybitError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class ScriptedSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {}), timeout))
        return self._next()

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data, timeout))
        return self._next()

    def _next(self):
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_client(session, **overrides):
    defaults = dict(bybit_timeout=5, bybit_max_attempts=3,
                    bybit_retry_backoff=0.0)
    defaults.update(overrides)
    return BybitClient(Settings(**defaults), session)


def live_settings(**overrides):
    values = dict(
        trading_mode="live",
        live_trading_enabled=True,
        live_safety_ready=True,
        bybit_api_key="APIKEY_SENTINEL_1234567890",
        bybit_api_secret="APISECRET_SENTINEL_0987654321",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def fast_limiter(monkeypatch):
    monkeypatch.setattr(
        BybitClient, "_global_limiter", _RateLimiter(rps=1_000_000.0))


FP = ("BTCUSDT", "Buy", "1", "Market")


# ----------------------------------------------------------------------
# P1 — concurrent POSTs: in_flight persists until actual completion
# ----------------------------------------------------------------------

def test_concurrent_identical_post_blocked_while_in_flight():
    entered = threading.Event()
    release = threading.Event()
    results = {}

    class BlockingSession:
        def __init__(self):
            self.calls = []

        def post(self, url, data=None, headers=None, timeout=None):
            self.calls.append(("POST", url, data))
            entered.set()
            assert release.wait(timeout=10)      # hold the HTTP request open
            return FakeResponse(
                {"retCode": 0, "retMsg": "OK", "result": {"orderId": "1"}})

    session = BlockingSession()
    client = BybitClient(live_settings(), session)

    def submit():
        try:
            results["payload"] = client.create_order("BTCUSDT", "Buy", 1)
        except BaseException as exc:            # noqa: BLE001 — surfaced below
            results["error"] = exc

    thread = threading.Thread(target=submit)
    thread.start()
    assert entered.wait(timeout=10)             # first POST is now on the wire

    with pytest.raises(BybitOrderUncertainError, match="in_flight"):
        client.create_order("BTCUSDT", "Buy", 1)
    assert len(session.calls) == 1              # no concurrent POST left the process

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert results.get("payload", {}).get("retCode") == 0

    # the operation actually finished (definitive outcome) -> a NEW
    # identical submission is a fresh decision and may proceed
    payload = client.create_order("BTCUSDT", "Buy", 1)
    assert payload["retCode"] == 0
    assert len(session.calls) == 2


def test_in_flight_state_persists_until_actual_completion(tmp_path):
    """in_flight is never released by a TTL (review #5709376849 P1) — only
    by the operation's own completion."""
    guard = retry_budget.OrderSubmitGuard(str(tmp_path / "journal.json"))
    assert guard.begin(FP, order_link_id="rb-inflight-1") is True
    for _ in range(3):
        # any number of identical attempts while in flight are refused,
        # regardless of elapsed time (there is no expiry at all)
        assert guard.begin(FP, order_link_id="rb-inflight-x") is False
    assert guard.blocking_entry(FP)[0] == "in_flight"
    guard.mark_resolved(FP, order_link_id="rb-inflight-1",
                        evidence="operation finished")
    assert guard.begin(FP, order_link_id="rb-inflight-2") is True


# ----------------------------------------------------------------------
# P1 — UNKNOWN is never timer-released; identity + journal persistence
# ----------------------------------------------------------------------

def test_unknown_state_persists_beyond_any_window(tmp_path):
    guard = retry_budget.OrderSubmitGuard(str(tmp_path / "journal.json"))
    guard.begin(FP, order_link_id="rb-unknown-1")
    guard.mark_unknown(FP, order_link_id="rb-unknown-1",
                       reason="timeout — outcome UNKNOWN")
    # no clock, no TTL: identical submission stays blocked indefinitely
    assert guard.begin(FP, order_link_id="rb-unknown-2") is False
    assert guard.blocking_entry(FP) == ("unknown", "rb-unknown-1")


def test_journal_records_safe_operation_identity(tmp_path):
    """Every submission carries an orderLinkId journaled BEFORE the send."""
    session = ScriptedSession([TimeoutError("slow")])
    client = BybitClient(live_settings(), session)
    with pytest.raises(TimeoutError):
        client.create_order("BTCUSDT", "Buy", 1,
                            order_link_id="op-2026-10-30-001")
    # the identity travels in the signed POST body ...
    body = session.calls[0][2]
    assert "op-2026-10-30-001" in body
    # ... and is journaled with the UNKNOWN outcome
    journal = json.loads(
        (tmp_path / "bybit_order_submit_journal.json").read_text(
            encoding="utf-8"))
    entries = journal["entries"]
    entry = next(iter(entries.values()))
    assert entry["order_link_id"] == "op-2026-10-30-001"
    assert entry["state"] == "unknown"
    assert "timeout" in entry["reason"]


# ----------------------------------------------------------------------
# P1 — restart recovery
# ----------------------------------------------------------------------

def test_unknown_state_survives_restart(tmp_path):
    journal = tmp_path / "journal.json"
    guard1 = retry_budget.OrderSubmitGuard(str(journal))
    guard1.begin(FP, order_link_id="rb-restart-1")
    guard1.mark_unknown(FP, order_link_id="rb-restart-1",
                        reason="transport failure — outcome UNKNOWN")
    del guard1                                  # process exits

    guard2 = retry_budget.OrderSubmitGuard(str(journal))   # restart
    assert guard2.begin(FP, order_link_id="rb-restart-2") is False
    assert guard2.blocking_entry(FP) == ("unknown", "rb-restart-1")


def test_crash_while_in_flight_reloads_as_unknown(tmp_path):
    """A crash mid-send leaves an in_flight record; after restart it is
    treated as UNKNOWN — fail-closed, never silently released."""
    journal = tmp_path / "journal.json"
    guard1 = retry_budget.OrderSubmitGuard(str(journal))
    guard1.begin(FP, order_link_id="rb-crash-1")
    del guard1                                  # crash before any resolution

    guard2 = retry_budget.OrderSubmitGuard(str(journal))
    assert guard2.blocking_entry(FP)[0] == "unknown"
    assert guard2.begin(FP, order_link_id="rb-crash-2") is False


# ----------------------------------------------------------------------
# P1 — explicit reconciliation procedure
# ----------------------------------------------------------------------

def test_reconcile_confirmed_record_resolves_with_evidence(tmp_path):
    journal = tmp_path / "journal.json"
    guard = retry_budget.get_order_guard()
    guard.configure(str(journal))
    guard.begin(FP, order_link_id="rb-rec-1")
    guard.mark_unknown(FP, order_link_id="rb-rec-1",
                       reason="timeout — outcome UNKNOWN")

    session = ScriptedSession([
        FakeResponse({"retCode": 0, "result": {"list": [
            {"orderLinkId": "rb-rec-1", "symbol": "BTCUSDT",
             "category": "linear", "orderStatus": "Filled",
             "orderId": "EX-1"}]}}),
    ])
    results = make_client(session).reconcile_order_submissions()

    assert results[0]["resolution"] == "resolved"
    assert "Filled" in results[0]["detail"]
    assert len(session.calls) == 1              # realtime confirmed -> no history
    # block lifted WITH a verifiable confirmation record
    assert guard.begin(FP, order_link_id="rb-rec-2") is True
    stored = json.loads(journal.read_text(encoding="utf-8"))
    evidence = [e.get("evidence", "") for e in stored["entries"].values()]
    confirming = [e for e in evidence if "exchange-confirmed" in e]
    assert confirming, "confirmation record stored in journal"
    record = confirming[0]
    assert "orderLinkId=rb-rec-1" in record
    assert "symbol=BTCUSDT" in record
    assert "orderStatus=Filled" in record
    assert "verified_at=" in record


def test_reconcile_absent_record_is_explicit_unresolved_blocker(tmp_path):
    """No exchange record is NEVER treated as 'safe to resend' — the block
    stays (fail-closed) and the result names an explicit blocker."""
    journal = tmp_path / "journal.json"
    guard = retry_budget.get_order_guard()
    guard.configure(str(journal))
    guard.begin(FP, order_link_id="rb-rec-none")
    guard.mark_unknown(FP, order_link_id="rb-rec-none",
                       reason="5xx — outcome UNKNOWN")

    session = ScriptedSession([
        FakeResponse({"retCode": 0, "result": {"list": []}}),   # realtime
        FakeResponse({"retCode": 0, "result": {"list": []}}),   # history
    ])
    results = make_client(session).reconcile_order_submissions()

    assert results[0]["resolution"] == "UNRESOLVED_BLOCKER"
    assert len(session.calls) == 2              # both read-only endpoints tried
    assert guard.begin(FP, order_link_id="rb-rec-none-2") is False  # still blocked


def test_reconcile_query_failure_keeps_block(tmp_path):
    journal = tmp_path / "journal.json"
    guard = retry_budget.get_order_guard()
    guard.configure(str(journal))
    guard.begin(FP, order_link_id="rb-rec-fail")
    guard.mark_unknown(FP, order_link_id="rb-rec-fail",
                       reason="timeout — outcome UNKNOWN")

    session = ScriptedSession([FakeResponse(status=500)])
    results = make_client(session).reconcile_order_submissions()
    assert results[0]["resolution"] == "UNRESOLVED_BLOCKER"
    assert guard.begin(FP, order_link_id="rb-rec-fail-2") is False


# ----------------------------------------------------------------------
# P1 — operator override requires explicit manual confirmation
# ----------------------------------------------------------------------

def test_cli_lists_blockers_offline(tmp_path, capsys):
    from tools.bybit_order_submit_reconcile_v1 import main

    journal = tmp_path / "journal.json"
    guard = retry_budget.OrderSubmitGuard(str(journal))
    guard.begin(FP, order_link_id="rb-cli-1")
    guard.mark_unknown(FP, order_link_id="rb-cli-1",
                       reason="timeout — outcome UNKNOWN")

    code = main(["--config", str(tmp_path / "no-config.yaml"),
                 "--journal", str(journal), "--list"])
    out = capsys.readouterr().out
    assert code == 0
    assert "rb-cli-1" in out


# ----------------------------------------------------------------------
# P1 (review #5709889450) — STRICT orderLinkId / symbol / category match
# ----------------------------------------------------------------------

def _row(order_link_id="rb-x", symbol="BTCUSDT", category="linear",
         status="Filled", **extra):
    row = {"symbol": symbol, "category": category, "orderStatus": status,
           "orderId": "EX-1"}
    if order_link_id is not None:
        row["orderLinkId"] = order_link_id
    row.update(extra)
    return row


# Acceptance table (review #5709889450 §3): response -> expected outcome.
_STRICT_MATCH_CASES = [
    ("exact_match_valid_status", [_row()], [], "resolved"),
    ("exact_match_open_status", [_row(status="New")], [], "resolved"),
    ("empty_order_link_id", [_row(order_link_id="")], [], "UNRESOLVED_BLOCKER"),
    ("missing_order_link_id",
     [_row(order_link_id=None)], [], "UNRESOLVED_BLOCKER"),
    ("different_order_link_id",
     [_row(order_link_id="rb-OTHER")], [], "UNRESOLVED_BLOCKER"),
    ("different_symbol",
     [_row(symbol="ETHUSDT")], [], "UNRESOLVED_BLOCKER"),
    ("different_category",
     [_row(category="spot")], [], "UNRESOLVED_BLOCKER"),
    ("unknown_order_status",
     [_row(status="Mystery")], [], "UNRESOLVED_BLOCKER"),
    ("missing_order_status",
     [_row(status=None)], [], "UNRESOLVED_BLOCKER"),
    ("foreign_rows_only",
     [_row(order_link_id="rb-OTHER"), _row(order_link_id="")],
     [], "UNRESOLVED_BLOCKER"),
    ("order_not_found", [], [], "UNRESOLVED_BLOCKER"),
    ("history_confirms_when_realtime_does_not",
     [], [_row(status="Cancelled")], "resolved"),
]


@pytest.mark.parametrize(
    "realtime_rows,history_rows,expected",
    [case[1:] for case in _STRICT_MATCH_CASES],
    ids=[case[0] for case in _STRICT_MATCH_CASES])
def test_strict_order_match_acceptance_table(tmp_path, realtime_rows,
                                             history_rows, expected):
    """Only an exact non-empty orderLinkId + symbol + category + accepted
    status is confirmation; everything else stays UNRESOLVED_BLOCKER."""
    journal = tmp_path / "journal.json"
    guard = retry_budget.get_order_guard()
    guard.configure(str(journal))
    guard.begin(FP, order_link_id="rb-x")
    guard.mark_unknown(FP, order_link_id="rb-x",
                       reason="timeout — outcome UNKNOWN")

    session = ScriptedSession([
        FakeResponse({"retCode": 0, "result": {"list": realtime_rows}}),
        FakeResponse({"retCode": 0, "result": {"list": history_rows}}),
    ])
    results = make_client(session).reconcile_order_submissions()

    assert results[0]["resolution"] == expected
    if expected == "resolved":
        assert "exchange-confirmed" in results[0]["detail"]
        assert "orderLinkId=rb-x" in results[0]["detail"]
        assert guard.begin(FP, order_link_id="rb-x-2") is True   # unblocked
    else:
        assert "UNRESOLVED_BLOCKER" == results[0]["resolution"]
        assert guard.begin(FP, order_link_id="rb-x-2") is False  # still blocked


def test_strict_match_malformed_response_fails_closed(tmp_path):
    """A malformed response is NEVER a confirmation — blocked (fail-closed)."""
    journal = tmp_path / "journal.json"
    guard = retry_budget.get_order_guard()
    guard.configure(str(journal))
    guard.begin(FP, order_link_id="rb-mal")
    guard.mark_unknown(FP, order_link_id="rb-mal",
                       reason="timeout — outcome UNKNOWN")

    session = ScriptedSession([
        FakeResponse({"retCode": 0, "result": "garbage-not-a-dict"}),
    ])
    results = make_client(session).reconcile_order_submissions()
    assert results[0]["resolution"] == "UNRESOLVED_BLOCKER"
    assert guard.begin(FP, order_link_id="rb-mal-2") is False


def test_strict_match_history_only_queried_when_realtime_uncertain(tmp_path):
    """realtime confirmed -> history is not consulted; uncertain -> history."""
    journal = tmp_path / "journal.json"
    guard = retry_budget.get_order_guard()
    guard.configure(str(journal))
    guard.begin(FP, order_link_id="rb-hist")
    guard.mark_unknown(FP, order_link_id="rb-hist",
                       reason="timeout — outcome UNKNOWN")

    session = ScriptedSession([
        FakeResponse({"retCode": 0, "result": {"list": [
            _row(order_link_id="rb-hist", status="PartiallyFilled")]}}),
    ])
    results = make_client(session).reconcile_order_submissions()
    assert results[0]["resolution"] == "resolved"
    assert len(session.calls) == 1


# ----------------------------------------------------------------------
# P2 (review #5709889450) — durable journal persistence + fault injection
# ----------------------------------------------------------------------

def _fresh_guard(journal):
    """Bind the process singleton guard (the one create_order uses)."""
    guard = retry_budget.get_order_guard()
    guard.configure(str(journal))
    return guard


def _expect_fail_closed_no_send(journal):
    """A new submission must be refused with zero HTTP attempts."""
    session = ScriptedSession([])
    client = BybitClient(live_settings(
        bybit_timeout=5, bybit_max_attempts=3, bybit_retry_backoff=0.0,
        bybit_order_submit_journal_path=str(journal)), session)
    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-fault-check")
    assert session.calls == []            # 0 HTTP attempts


def test_fails_closed_before_temp_file_write(tmp_path, monkeypatch):
    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)

    def boom(*_a, **_k):
        raise OSError("injected: cannot create temp file")
    monkeypatch.setattr(retry_budget.os, "makedirs", boom)

    with pytest.raises(BybitOrderUncertainError):
        guard.begin(FP, order_link_id="rb-f1")
    monkeypatch.undo()
    _expect_fail_closed_no_send(journal)


def test_fails_closed_during_write(tmp_path, monkeypatch):
    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)

    class _FailingFile:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def write(self, *_a, **_k):
            raise OSError("injected: write failure")

    monkeypatch.setattr(retry_budget.os, "fdopen", lambda *_a, **_k: _FailingFile())
    with pytest.raises(BybitOrderUncertainError):
        guard.begin(FP, order_link_id="rb-f2")
    monkeypatch.undo()
    _expect_fail_closed_no_send(journal)


def test_fails_closed_after_flush_before_fsync(tmp_path, monkeypatch):
    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)

    def boom(*_a, **_k):
        raise OSError("injected: fsync failure")
    monkeypatch.setattr(retry_budget.os, "fsync", boom)

    with pytest.raises(BybitOrderUncertainError):
        guard.begin(FP, order_link_id="rb-f3")
    monkeypatch.undo()
    _expect_fail_closed_no_send(journal)


def test_fails_closed_after_fsync_before_replace(tmp_path, monkeypatch):
    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)

    def boom(*_a, **_k):
        raise OSError("injected: replace failure")
    monkeypatch.setattr(retry_budget.os, "replace", boom)

    with pytest.raises(BybitOrderUncertainError):
        guard.begin(FP, order_link_id="rb-f4")
    monkeypatch.undo()
    _expect_fail_closed_no_send(journal)
    # the reservation must NOT be visible as durable state
    assert not journal.exists()


def test_fails_closed_after_replace_before_dir_sync(tmp_path, monkeypatch):
    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)

    def boom(*_a, **_k):
        raise OSError("injected: directory fsync failure")
    monkeypatch.setattr(retry_budget, "_fsync_dir", boom)

    with pytest.raises(BybitOrderUncertainError):
        guard.begin(FP, order_link_id="rb-f5")
    monkeypatch.undo()
    # fail-closed: even though the file was replaced, the submission was
    # refused and the durable reservation still blocks
    _expect_fail_closed_no_send(journal)
    # a fresh process instance (restart view) also sees the reservation
    guard2 = retry_budget.OrderSubmitGuard(str(journal))
    assert guard2.begin(FP, order_link_id="rb-f5-2") is False


def test_fails_closed_on_reload_failure(tmp_path, monkeypatch):
    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)
    guard.begin(FP, order_link_id="rb-f6")

    def boom(*_a, **_k):
        raise ValueError("injected: reload failure")
    monkeypatch.setattr(retry_budget.json, "load", boom)

    with pytest.raises(BybitOrderUncertainError):
        guard.begin(FP, order_link_id="rb-f6-2")
    monkeypatch.undo()
    _expect_fail_closed_no_send(journal)


def test_invalid_checksum_fails_closed_without_autorecovery(tmp_path):
    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)
    guard.begin(FP, order_link_id="rb-f7")

    # tamper with the content WITHOUT fixing the checksum
    data = json.loads(journal.read_text(encoding="utf-8"))
    only = next(iter(data["entries"].values()))
    only["state"] = "resolved"                 # attacker: claim it is resolved
    journal.write_text(json.dumps(data, indent=1, sort_keys=True),
                       encoding="utf-8")

    guard2 = _fresh_guard(journal)
    with pytest.raises(BybitOrderUncertainError):
        guard2.begin(FP, order_link_id="rb-f7-2")
    # the corrupted journal is NOT reset to an empty/valid one
    still = json.loads(journal.read_text(encoding="utf-8"))
    assert next(iter(still["entries"].values()))["state"] == "resolved"
    assert still["schema"] == retry_budget.ORDER_JOURNAL_SCHEMA
    with pytest.raises(ValueError):
        retry_budget._validate_journal_document(still)


def test_corrupted_json_fails_closed_without_autorecovery(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_text("{this is not json", encoding="utf-8")

    guard = _fresh_guard(journal)
    with pytest.raises(BybitOrderUncertainError):
        guard.begin(FP, order_link_id="rb-f8")
    # no automatic empty-journal recovery after a read error
    assert journal.read_text(encoding="utf-8") == "{this is not json"
    _expect_fail_closed_no_send(journal)


def test_journal_schema_and_checksum_present_on_every_write(tmp_path):
    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)
    guard.begin(FP, order_link_id="rb-ok")
    data = json.loads(journal.read_text(encoding="utf-8"))
    assert data["schema"] == retry_budget.ORDER_JOURNAL_SCHEMA
    retry_budget._validate_journal_document(data)   # verifies the checksum


# ----------------------------------------------------------------------
# P2 (review #5709889450) — manual resolution is NOT IMPLEMENTED
# ----------------------------------------------------------------------

def test_guard_resolve_manual_never_unlocks(tmp_path):
    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)
    guard.begin(FP, order_link_id="rb-man-1")
    guard.mark_unknown(FP, order_link_id="rb-man-1",
                       reason="timeout — outcome UNKNOWN")

    with pytest.raises(retry_budget.ManualResolutionNotImplemented):
        guard.resolve_manual(order_link_id="rb-man-1",
                             operator_id="ops-1", reference="ticket-1",
                             symbol="BTCUSDT", status_check="filled in UI",
                             reason="verified manually")
    # the UNKNOWN block is kept — free-form strings never unlock
    assert guard.begin(FP, order_link_id="rb-man-1-2") is False


def test_cli_manual_resolution_is_not_implemented_and_keeps_block(
        tmp_path, capsys):
    from tools.bybit_order_submit_reconcile_v1 import main

    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)
    guard.begin(FP, order_link_id="rb-man-2")
    guard.mark_unknown(FP, order_link_id="rb-man-2",
                       reason="timeout — outcome UNKNOWN")

    code = main(["--config", str(tmp_path / "no-config.yaml"),
                 "--journal", str(journal),
                 "--manual-resolution",
                 "--operator-id", "ops-1", "--reference", "ticket-1",
                 "--order-link-id", "rb-man-2", "--symbol", "BTCUSDT",
                 "--status-check", "order filled in Bybit UI",
                 "--reason", "verified manually"])
    out = capsys.readouterr()
    assert code == 3
    assert "MANUAL_RESOLUTION_NOT_IMPLEMENTED" in out.err
    assert "manual_resolution_record" in out.out
    guard2 = _fresh_guard(journal)
    assert guard2.begin(FP, order_link_id="rb-man-2-2") is False   # kept


def test_cli_manual_resolution_without_requisites_still_rejected(tmp_path, capsys):
    """Even a fully-formatted manual attempt needs real authorization —
    missing requisites are reported and the block never lifts."""
    from tools.bybit_order_submit_reconcile_v1 import main

    journal = tmp_path / "journal.json"
    guard = _fresh_guard(journal)
    guard.begin(FP, order_link_id="rb-man-3")
    guard.mark_unknown(FP, order_link_id="rb-man-3",
                       reason="timeout — outcome UNKNOWN")

    code = main(["--config", str(tmp_path / "no-config.yaml"),
                 "--journal", str(journal),
                 "--manual-resolution",
                 "--order-link-id", "rb-man-3"])
    out = capsys.readouterr()
    assert code == 3
    assert "MANUAL_RESOLUTION_NOT_IMPLEMENTED" in out.err
    assert "missing_requisites" in out.out
    guard2 = _fresh_guard(journal)
    assert guard2.begin(FP, order_link_id="rb-man-3-2") is False   # kept


# ----------------------------------------------------------------------
# Multi-configuration safety: several guard instances on ONE journal path
# ----------------------------------------------------------------------

def test_two_guard_instances_same_journal_path_serialize(tmp_path):
    """Two configurations sharing one journal path must not both submit
    the identical order (the cross-process lock file serializes them)."""
    journal = tmp_path / "journal.json"
    guard_a = retry_budget.OrderSubmitGuard(str(journal))
    guard_b = retry_budget.OrderSubmitGuard(str(journal))
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt(guard):
        barrier.wait(timeout=10)
        results.append(guard.begin(FP, order_link_id="rb-2cfg"))

    t1 = threading.Thread(target=attempt, args=(guard_a,))
    t2 = threading.Thread(target=attempt, args=(guard_b,))
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)
    assert sorted(results) == [False, True]     # exactly one reservation
