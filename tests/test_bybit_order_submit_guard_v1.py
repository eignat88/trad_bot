"""Order-submit guard hardening tests — code review #5709376849 (P1).

Coverage:
* indeterminate create_order outcome is NEVER released by a timer — only
  definitive evidence (parsed API result, HTTP 4xx, or explicit
  reconciliation against the exchange) lifts the block;
* safe operation identity: every submission carries an ``orderLinkId``
  recorded in the persistent journal before the send;
* restart recovery: journal state survives a process crash; a crash while
  ``in_flight`` reloads as ``unknown`` (fail-closed);
* concurrent POSTs: ``in_flight`` persists until the operation actually
  finishes (blocked first HTTP request, second identical call refused);
* explicit reconciliation procedure: a confirmed exchange record resolves
  with evidence; an absent record stays an UNRESOLVED BLOCKER;
* the operator override requires explicit manual confirmation.

All tests run on fake transport — no Bybit, no real orders, no network.
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
            {"orderLinkId": "rb-rec-1", "orderStatus": "Filled"}]}}),
    ])
    results = make_client(session).reconcile_order_submissions()

    assert results[0]["resolution"] == "resolved"
    assert "Filled" in results[0]["detail"]
    assert len(session.calls) == 1              # read-only, no order traffic
    # block lifted WITH evidence -> a new submission is a fresh decision
    assert guard.begin(FP, order_link_id="rb-rec-2") is True
    stored = json.loads(journal.read_text(encoding="utf-8"))
    evidence = [e.get("evidence", "") for e in stored["entries"].values()]
    assert any("exchange-confirmed" in e for e in evidence)


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


def test_cli_mark_resolved_requires_confirm_manual(tmp_path):
    from tools.bybit_order_submit_reconcile_v1 import main

    journal = tmp_path / "journal.json"
    guard = retry_budget.OrderSubmitGuard(str(journal))
    guard.begin(FP, order_link_id="rb-cli-2")
    guard.mark_unknown(FP, order_link_id="rb-cli-2",
                       reason="timeout — outcome UNKNOWN")

    with pytest.raises(SystemExit):
        main(["--config", str(tmp_path / "no-config.yaml"),
              "--journal", str(journal), "--mark-resolved", "rb-cli-2"])
    # without explicit manual evidence the block is untouched
    guard2 = retry_budget.OrderSubmitGuard(str(journal))
    assert guard2.begin(FP, order_link_id="rb-cli-3") is False

    code = main(["--config", str(tmp_path / "no-config.yaml"),
                 "--journal", str(journal), "--mark-resolved", "rb-cli-2",
                 "--confirm-manual", "checked in Bybit UI: order filled"])
    assert code == 0
    guard3 = retry_budget.OrderSubmitGuard(str(journal))
    assert guard3.begin(FP, order_link_id="rb-cli-4") is True
    stored = json.loads(journal.read_text(encoding="utf-8"))
    assert any("manual operator resolution" in e.get("evidence", "")
               for e in stored["entries"].values())
