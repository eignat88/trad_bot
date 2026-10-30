"""R4 code-review remediation tests — Issue #229, PR #233 (BYBIT_RETRY_BUDGET_HARDENING_V1).

Coverage of the five R4 review findings, all on fake transport (no Bybit, no
network, no real orders):

* **R4-1 (P1) malformed POST response** — a well-formed JSON body WITHOUT a
  valid ``retCode`` (``{}``, ``{"retCode": null}``, wrong type) is an
  AMBIGUOUS answer: the outcome is UNKNOWN, the durable block is kept until a
  verifiable reconciliation, and at most ONE POST can ever reach the exchange;
* **R4-2 (P1) journal directory initialization** — the journal directory is
  created BEFORE the first lock transaction (fresh/empty installation, nested
  journal path, two processes racing), filesystem errors fail closed and an
  existing journal is never reset;
* **R4-3 (P1) authenticated reconciliation GET** — order queries are SIGNED
  private GETs (Bybit V5 auth contract: X-BAPI-* headers, HMAC-SHA256 over
  ``timestamp + api_key + recv_window + query``), signatures are fresh per
  attempt, credentials/signatures never reach logs or telemetry, and
  authentication failures keep the fail-closed blocker;
* **R4-4 (P1) response category validation** — the category is validated on
  the response ENVELOPE (``result.category``), the order row must carry an
  exact ``orderLinkId`` + symbol + accepted status; foreign rows/categories
  are never confirmation;
* **R4-5 (P2) HTTP 429 classification** — ``urllib.HTTPError(429)`` is a rate
  limit exactly like HTTP 200 + ``retCode=10006`` (same classification, storm
  guard, breaker, telemetry parity, ``HARD_MAX_ATTEMPTS <= 3``, no nested
  retries, and NEVER an automatic POST retry).
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import logging
import multiprocessing as mp
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

import pytest

import app.exchange.bybit_client as bybit_client_module
from app.config import Settings
from app.exchange import retry_budget
from app.exchange.api_telemetry import ApiTelemetry
from app.exchange.bybit_client import (
    BybitClient,
    BybitAuthError,
    BybitError,
    BybitOrderUncertainError,
    BybitRateLimitError,
    BybitTimeoutError,
    _RateLimiter,
)

FP = ("BTCUSDT", "Buy", "1", "Market")
API_KEY = "APIKEY_SENTINEL_1234567890"
API_SECRET = "APISECRET_SENTINEL_0987654321"
TELEMETRY_MARKER = "BYBIT_API_CALL"


# ----------------------------------------------------------------------
# Fakes (session/response doubles — the transport is never the network)
# ----------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, http_error=False):
        self.payload = payload if payload is not None else {
            "retCode": 0, "retMsg": "OK", "result": {"list": []},
        }
        self.status = status
        self.headers = headers
        self._http_error = http_error

    def raise_for_status(self):
        if self._http_error or not 200 <= self.status < 300:
            raise BybitError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class ScriptedSession:
    """Replays scripted outcomes; records every call INCLUDING auth headers."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("GET", url, dict(params or {}), timeout,
                           dict(headers or {})))
        return self._next()

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data, timeout, dict(headers or {})))
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
        bybit_api_key=API_KEY,
        bybit_api_secret=API_SECRET,
        bybit_timeout=5,
        bybit_max_attempts=3,
        bybit_retry_backoff=0.0,
    )
    values.update(overrides)
    return Settings(**values)


def order_row(order_link_id="rb-x", symbol="BTCUSDT", status="Filled",
              **extra):
    """A REAL Bybit V5 order row: no ``category`` inside the row."""
    row = {"symbol": symbol, "orderStatus": status, "orderId": "EX-1"}
    if order_link_id is not None:
        row["orderLinkId"] = order_link_id
    row.update(extra)
    return row


def order_response(rows, category="linear", ret_code=0, ret_msg="OK"):
    """A Bybit V5 /v5/order/* envelope (review R4-4 shape)."""
    result = {"list": list(rows)}
    if category is not None:
        result["category"] = category
    return FakeResponse({"retCode": ret_code, "retMsg": ret_msg,
                         "result": result})


def http_error(status):
    """An ``urllib.HTTPError`` exactly like ``urlopen`` raises for a 4xx/5xx."""
    return HTTPError("https://api.bytest/v5/order/realtime", status,
                     f"HTTP {status}", None, None)


def attempt_lines(caplog):
    return [record.getMessage() for record in caplog.records
            if record.getMessage().startswith(TELEMETRY_MARKER)]


def field(line, name):
    import re
    match = re.search(rf"(?:^| ){re.escape(name)}=(\S+)", line)
    assert match, f"{name!r} not found in line: {line}"
    return match.group(1)


def _fresh_journal(tmp_path, name="journal.json", subdirs=()):
    journal = tmp_path.joinpath(*subdirs, name)
    guard = retry_budget.OrderSubmitGuard(str(journal))
    return journal, guard


def _unknown_entry(guard, olink, fp=FP):
    assert guard.begin(fp, order_link_id=olink) is True
    guard.mark_unknown(fp, order_link_id=olink,
                       reason="simulated indeterminate outcome — UNKNOWN")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, caplog):
    """Fresh telemetry + clean retry governance per test; no real sleeping."""
    monkeypatch.setattr(bybit_client_module, "_telemetry", ApiTelemetry())
    monkeypatch.delenv("BYBIT_SERVICE_NAME", raising=False)
    monkeypatch.setattr(BybitClient, "_global_limiter", _RateLimiter(rps=10_000))
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)
    retry_budget.reset_process_state()
    caplog.set_level(logging.INFO)
    yield
    retry_budget.reset_process_state()


# ======================================================================
# R4-1 — malformed POST response (P1)
# ======================================================================

AMBIGUOUS_BODIES = [
    ("empty_object", {}),
    ("null_retcode", {"retCode": None, "retMsg": "OK",
                      "result": {"orderId": "EX-1"}}),
    ("string_retcode", {"retCode": "0", "retMsg": "OK",
                        "result": {"orderId": "EX-1"}}),
    ("float_retcode", {"retCode": 0.0, "retMsg": "OK",
                       "result": {"orderId": "EX-1"}}),
    ("bool_retcode", {"retCode": True, "retMsg": "OK",
                      "result": {"orderId": "EX-1"}}),
    ("list_retcode", {"retCode": [0], "retMsg": "OK",
                      "result": {"orderId": "EX-1"}}),
]


@pytest.mark.parametrize("body", [case[1] for case in AMBIGUOUS_BODIES],
                         ids=[case[0] for case in AMBIGUOUS_BODIES])
def test_malformed_post_response_keeps_durable_block(tmp_path, body):
    """R4-1: missing/invalid retCode is NOT a definitive rejection — the
    outcome is UNKNOWN and the block survives (fail-closed)."""
    journal = tmp_path / "journal.json"
    session = ScriptedSession([FakeResponse(body)])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)

    with pytest.raises(BybitOrderUncertainError, match="valid retCode"):
        client.create_order(*FP, order_link_id="rb-r4-1a")

    # the durable blocker is visible to any later process/review
    guard = retry_budget.OrderSubmitGuard(str(journal))
    assert guard.blocking_entry(FP) == ("unknown", "rb-r4-1a")
    assert guard.begin(FP, order_link_id="rb-r4-1b") is False


@pytest.mark.parametrize("body", [case[1] for case in AMBIGUOUS_BODIES],
                         ids=[case[0] for case in AMBIGUOUS_BODIES])
def test_two_identical_create_order_max_one_post(tmp_path, body):
    """R4-1 acceptance: two sequential identical create_order calls on an
    ambiguous answer — MAXIMUM one POST, the second is blocked."""
    journal = tmp_path / "journal.json"
    session = ScriptedSession([FakeResponse(body), FakeResponse(body)])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)

    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-r4-1c")
    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-r4-1c")

    assert [c[0] for c in session.calls] == ["POST"]     # exactly ONE POST


def test_malformed_post_response_emits_exactly_one_telemetry_record(tmp_path,
                                                                   caplog):
    """R4-1 telemetry parity: one record per HTTP attempt, the ambiguous
    attempt is labelled ERROR without inventing a retCode."""
    journal = tmp_path / "journal.json"
    session = ScriptedSession([FakeResponse({})])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)
    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-r4-1d")

    lines = attempt_lines(caplog)
    assert len(lines) == 1
    assert field(lines[0], "method") == "POST"
    assert field(lines[0], "result") == "ERROR"
    assert field(lines[0], "ret_code") == "none"
    assert field(lines[0], "attempt_index") == "1"
    assert field(lines[0], "total_attempts") == "1"


def test_malformed_post_response_released_only_by_reconciliation(tmp_path):
    """R4-1: the UNKNOWN block survives until a STRICT exchange confirmation;
    only then is a fresh submission decision possible."""
    journal = tmp_path / "journal.json"
    session = ScriptedSession([
        FakeResponse({"retCode": None}),                 # ambiguous POST
        order_response([order_row("rb-r4-1e")]),         # reconciliation
    ])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)

    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-r4-1e")

    results = client.reconcile_order_submissions()
    assert results[0]["resolution"] == "resolved"
    assert "exchange-confirmed" in results[0]["detail"]

    # the block is gone ONLY now — and a resend is then a fresh decision
    assert session.calls[1][0] == "GET"                  # no POST during reconcile
    session.outcomes.append(FakeResponse(
        {"retCode": 0, "retMsg": "OK", "result": {"orderId": "EX-2"}}))
    payload = client.create_order(*FP, order_link_id="rb-r4-1f")
    assert payload["result"]["orderId"] == "EX-2"
    assert [c[0] for c in session.calls] == ["POST", "GET", "POST"]


def test_valid_retcode_zero_is_a_definitive_answer(tmp_path):
    """Control case (R4-1): a VALID retCode keeps the established semantics."""
    journal = tmp_path / "journal.json"
    session = ScriptedSession([FakeResponse(
        {"retCode": 0, "retMsg": "OK", "result": {"orderId": "EX-1"}})])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)
    payload = client.create_order(*FP, order_link_id="rb-r4-1g")
    assert payload["result"]["orderId"] == "EX-1"
    guard = retry_budget.OrderSubmitGuard(str(journal))
    assert guard.begin(FP, order_link_id="rb-r4-1h") is True   # decided, free


# ======================================================================
# R4-2 — journal directory initialization (P1)
# ======================================================================

def test_fresh_install_missing_directory_starts_safely(tmp_path):
    """R4-2: an EMPTY installation (no journal, no parent directory) must
    start safely instead of failing closed forever."""
    journal = tmp_path / "var" / "reports" / "orders.json"
    assert not journal.parent.exists()

    guard = retry_budget.OrderSubmitGuard(str(journal))
    assert guard.begin(FP, order_link_id="rb-r4-2a") is True
    assert journal.parent.is_dir()
    assert journal.exists()
    entries = retry_budget._validate_journal_document(
        json.loads(journal.read_text(encoding="utf-8")))
    assert entries[retry_budget.OrderSubmitGuard._key(
        FP, "rb-r4-2a")]["state"] == "in_flight"


def test_create_order_end_to_end_on_missing_directory(tmp_path):
    """R4-2: the whole submit path works on a fresh install and writes the
    durable journal inside the freshly created directory."""
    journal = tmp_path / "nested" / "deeper" / "journal.json"
    session = ScriptedSession([FakeResponse(
        {"retCode": 0, "retMsg": "OK", "result": {"orderId": "EX-1"}})])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)
    payload = client.create_order(*FP, order_link_id="rb-r4-2b")
    assert payload["result"]["orderId"] == "EX-1"
    entries = retry_budget._validate_journal_document(
        json.loads(journal.read_text(encoding="utf-8")))
    assert entries[retry_budget.OrderSubmitGuard._key(
        FP, "rb-r4-2b")]["state"] == "resolved"


def test_directory_initialization_never_resets_existing_journal(tmp_path):
    """R4-2: creating/ensuring the directory NEVER resets or rewrites an
    existing journal — the durable record survives restarts byte-for-byte."""
    journal = tmp_path / "nested" / "journal.json"
    guard1 = retry_budget.OrderSubmitGuard(str(journal))
    _unknown_entry(guard1, "rb-r4-2c")
    before = journal.read_bytes()

    guard2 = retry_budget.OrderSubmitGuard(str(journal))     # restart
    assert journal.read_bytes() == before                    # untouched
    assert guard2.blocking_entry(FP) == ("unknown", "rb-r4-2c")
    assert guard2.begin(FP, order_link_id="rb-r4-2d") is False


def test_journal_directory_creation_failure_fails_closed(tmp_path, monkeypatch):
    """R4-2: a filesystem failure while creating the directory fails CLOSED
    (zero HTTP attempts) and STAYS fail-closed after the filesystem heals."""
    journal = tmp_path / "nested" / "journal.json"
    session = ScriptedSession([FakeResponse(
        {"retCode": 0, "retMsg": "OK", "result": {"orderId": "EX-1"}})])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)

    def boom(*_a, **_k):
        raise OSError("injected: cannot create journal directory")

    monkeypatch.setattr(retry_budget.os, "makedirs", boom)
    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-r4-2e")
    assert session.calls == []                  # 0 HTTP attempts

    monkeypatch.undo()                          # filesystem healed...
    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-r4-2f")
    assert session.calls == []                  # ...still fail-closed


def test_journal_directory_path_occupied_by_file_fails_closed(tmp_path):
    """R4-2: a FILE sitting on the journal directory path is a filesystem
    error — fail-closed, zero HTTP attempts."""
    blocker = tmp_path / "nested"
    blocker.write_text("not a directory", encoding="utf-8")
    journal = blocker / "journal.json"
    session = ScriptedSession([FakeResponse(
        {"retCode": 0, "retMsg": "OK", "result": {"orderId": "EX-1"}})])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)
    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-r4-2g")
    assert session.calls == []                  # 0 HTTP attempts


def _worker_dir_race(journal, tag, result_file):
    """Two processes race to create the SAME missing journal directory."""
    try:
        guard = retry_budget.OrderSubmitGuard(journal)
        fp = (f"SYM{tag}", "Buy", "1", "Market")
        ok = guard.begin(fp, order_link_id=f"rb-r4-2-{tag}")
        with open(result_file, "w", encoding="utf-8") as fh:
            json.dump({"ok": bool(ok)}, fh)
    except Exception as exc:  # noqa: BLE001
        with open(result_file, "w", encoding="utf-8") as fh:
            fh.write(f"error:{type(exc).__name__}")


def test_two_processes_race_on_missing_journal_directory(tmp_path):
    """R4-2: directory creation is race-safe across processes — no lost
    update, no fail-closed, one valid journal with BOTH reservations."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "nested" / "deep" / "journal.json")
    res_a = str(tmp_path / "res_a.json")
    res_b = str(tmp_path / "res_b.json")
    procs = [_start(ctx, _worker_dir_race, (journal, tag, res))
             for tag, res in (("A", res_a), ("B", res_b))]
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0

    for res in (res_a, res_b):
        assert json.loads(Path(res).read_text(encoding="utf-8")) == {"ok": True}

    entries = retry_budget._validate_journal_document(
        json.loads(Path(journal).read_text(encoding="utf-8")))
    assert len(entries) == 2                      # both reservations durable


def _start(ctx, target, args):
    proc = ctx.Process(target=target, args=args)
    proc.start()
    return proc


# ======================================================================
# R4-3 — authenticated reconciliation GET (P1)
# ======================================================================

def _prepare_unknown(journal, olink="rb-r4-3", fp=FP):
    """Bind the PROCESS SINGLETON guard (the one create_order and
    reconcile_order_submissions use) and park an UNKNOWN entry in it."""
    guard = retry_budget.get_order_guard()
    guard.configure(str(journal))
    _unknown_entry(guard, olink, fp)
    return guard


def test_reconciliation_query_is_signed_bybit_v5(tmp_path):
    """R4-3: order queries are SIGNED private GETs (Bybit V5 contract:
    X-BAPI-* headers, HMAC-SHA256 over timestamp+key+recv_window+query)."""
    journal = tmp_path / "journal.json"
    _prepare_unknown(journal)
    session = ScriptedSession([order_response([order_row("rb-r4-3")])])
    results = BybitClient(live_settings(), session).reconcile_order_submissions()
    assert results[0]["resolution"] == "resolved"

    method, url, params, _timeout, headers = session.calls[0]
    assert method == "GET"
    assert url.endswith("/v5/order/realtime")
    for name in ("X-BAPI-API-KEY", "X-BAPI-TIMESTAMP",
                 "X-BAPI-RECV-WINDOW", "X-BAPI-SIGN"):
        assert name in headers
    assert headers["X-BAPI-API-KEY"] == API_KEY

    query = urlencode(params)          # exactly what the transport transmits
    expected = hmac.new(
        API_SECRET.encode(),
        (headers["X-BAPI-TIMESTAMP"] + API_KEY
         + headers["X-BAPI-RECV-WINDOW"] + query).encode(),
        hashlib.sha256).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected
    assert "category=linear" in query and "orderLinkId=rb-r4-3" in query


def test_reconciliation_signs_both_endpoints(tmp_path):
    """R4-3: BOTH private queries (/v5/order/realtime and /v5/order/history)
    are authenticated — no unsigned request may reach a private endpoint."""
    journal = tmp_path / "journal.json"
    _prepare_unknown(journal, olink="rb-r4-3b")
    session = ScriptedSession([
        order_response([]),                                # realtime: nothing
        order_response([order_row("rb-r4-3b")]),           # history: confirms
    ])
    results = BybitClient(live_settings(), session).reconcile_order_submissions()
    assert results[0]["resolution"] == "resolved"
    assert len(session.calls) == 2
    for call in session.calls:
        assert call[0] == "GET"
        assert "X-BAPI-SIGN" in call[4]
        assert "X-BAPI-API-KEY" in call[4]


def test_private_get_signature_is_fresh_per_attempt(tmp_path, monkeypatch):
    """R4-3: a retried attempt gets a FRESH timestamp/signature (a stale one
    would fall outside the recv-window)."""
    journal = tmp_path / "journal.json"
    _prepare_unknown(journal, olink="rb-r4-3c")
    counter = itertools.count()
    monkeypatch.setattr(bybit_client_module.time, "time",
                        lambda: 1_700_000_000.0 + 5.0 * next(counter))
    session = ScriptedSession([
        TimeoutError("slow"),
        order_response([order_row("rb-r4-3c")]),
    ])
    results = BybitClient(live_settings(), session).reconcile_order_submissions()
    assert results[0]["resolution"] == "resolved"
    assert len(session.calls) == 2

    signatures = [call[4]["X-BAPI-SIGN"] for call in session.calls]
    timestamps = [call[4]["X-BAPI-TIMESTAMP"] for call in session.calls]
    assert timestamps[0] != timestamps[1]                 # fresh per attempt
    assert signatures[0] != signatures[1]
    for call, signature, timestamp in zip(session.calls, signatures, timestamps):
        expected = hmac.new(
            API_SECRET.encode(),
            (timestamp + API_KEY
             + call[4]["X-BAPI-RECV-WINDOW"] + urlencode(call[2])).encode(),
            hashlib.sha256).hexdigest()
        assert signature == expected


def test_auth_headers_and_signatures_never_reach_logs_or_telemetry(tmp_path,
                                                                   caplog):
    """R4-3 secret hygiene: keys, secrets, signatures and auth header names
    never appear in logs or telemetry records."""
    journal = tmp_path / "journal.json"
    _prepare_unknown(journal, olink="rb-r4-3d")
    session = ScriptedSession([order_response([order_row("rb-r4-3d")])])
    BybitClient(live_settings(), session).reconcile_order_submissions()

    sent_signature = session.calls[0][4]["X-BAPI-SIGN"]
    text = caplog.text
    assert API_KEY not in text
    assert API_SECRET not in text
    assert sent_signature not in text
    assert "X-BAPI" not in text                 # no auth header names at all
    for line in attempt_lines(caplog):
        assert sent_signature not in line
        assert "X-BAPI" not in line


@pytest.mark.parametrize("outcome,detail_marker", [
    (lambda: FakeResponse({"retCode": 10003, "retMsg": "invalid api key",
                           "result": {}}), "authentication"),
    (lambda: FakeResponse({"retCode": 10004, "retMsg": "invalid sign",
                           "result": {}}), "authentication"),
    (lambda: FakeResponse({"retCode": 10005, "retMsg": "permission denied",
                           "result": {}}), "authentication"),
    (lambda: http_error(401), "authentication"),
    (lambda: http_error(403), "authentication"),
], ids=["retCode_10003", "retCode_10004", "retCode_10005", "http_401",
        "http_403"])
def test_authentication_failure_keeps_fail_closed_blocker(tmp_path, outcome,
                                                           detail_marker):
    """R4-3: authentication errors NEVER resolve anything — the durable
    block stays and the failure is an explicit fail-closed blocker."""
    journal = tmp_path / "journal.json"
    guard = _prepare_unknown(journal, olink="rb-r4-3e")
    session = ScriptedSession([outcome()])
    results = BybitClient(live_settings(),
                          session).reconcile_order_submissions()

    assert results[0]["resolution"] == "UNRESOLVED_BLOCKER"
    assert detail_marker in results[0]["detail"]
    assert "fail-closed" in results[0]["detail"]
    assert guard.blocking_entry(FP) == ("unknown", "rb-r4-3e")   # untouched
    assert guard.begin(FP, order_link_id="rb-r4-3f") is False     # still blocked


@pytest.mark.parametrize("outcome", [
    lambda: FakeResponse({"retCode": 10003, "retMsg": "invalid api key",
                          "result": {}}),
    lambda: http_error(401),
    lambda: http_error(403),
], ids=["retCode_10003", "http_401", "http_403"])
def test_auth_denial_raises_bybit_auth_error(tmp_path, outcome):
    """R4-3: on the read-only query path an authentication denial surfaces
    as :class:`BybitAuthError` — fail-closed, exactly one HTTP attempt, no
    retry."""
    session = ScriptedSession([outcome()])
    with pytest.raises(BybitAuthError):
        BybitClient(live_settings(),
                    session).get_order_by_link_id("BTCUSDT", "rb-r4-3x")
    assert len(session.calls) == 1                    # no retry, ever


def test_reconciliation_failure_label_never_leaks_exception_text(tmp_path):
    """R4-3/R4-5 secret hygiene: an exception whose MESSAGE embeds secrets
    must never leak them into the blocker detail."""
    journal = tmp_path / "journal.json"
    guard = _prepare_unknown(journal, olink="rb-r4-3g")

    class HostileError(BybitError):
        pass

    error = HostileError(f"boom X-BAPI-SIGN=FORGED api key={API_KEY}")
    session = ScriptedSession([error, error])
    results = BybitClient(live_settings(),
                          session).reconcile_order_submissions()
    assert results[0]["resolution"] == "UNRESOLVED_BLOCKER"
    assert API_KEY not in results[0]["detail"]
    assert "FORGED" not in results[0]["detail"]
    assert "HostileError" in results[0]["detail"]
    assert guard.begin(FP, order_link_id="rb-r4-3h") is False


def test_private_get_keeps_retry_budget_and_telemetry(tmp_path, caplog):
    """R4-3: the signed private GET runs through the SAME budget machinery
    (rate-limit retry inside HARD_MAX_ATTEMPTS, one telemetry record per
    attempt) as every other request."""
    journal = tmp_path / "journal.json"
    _prepare_unknown(journal, olink="rb-r4-3i")
    session = ScriptedSession([
        FakeResponse({"retCode": 10006, "retMsg": "Too many visits",
                      "result": {}}),
        order_response([order_row("rb-r4-3i")]),
    ])
    results = BybitClient(live_settings(),
                          session).reconcile_order_submissions()
    assert results[0]["resolution"] == "resolved"
    assert len(session.calls) == 2                # one retry, budget respected
    lines = attempt_lines(caplog)
    assert [field(line, "retry_reason") for line in lines] == [
        "none", "rate_limit"]
    assert [field(line, "attempt_index") for line in lines] == ["1", "2"]
    assert all(field(line, "total_attempts") == "3" for line in lines)


# ======================================================================
# R4-4 — response category validation (P1)
# ======================================================================

def test_correct_bybit_v5_structure_confirms(tmp_path):
    """R4-4: the CORRECT Bybit V5 shape (category on the envelope, plain
    order rows) is a valid confirmation."""
    journal = tmp_path / "journal.json"
    guard = _prepare_unknown(journal, olink="rb-r4-4a")
    session = ScriptedSession([order_response([order_row("rb-r4-4a")])])
    results = BybitClient(live_settings(),
                          session).reconcile_order_submissions()
    assert results[0]["resolution"] == "resolved"
    assert "category=linear" in results[0]["detail"]
    assert guard.begin(FP, order_link_id="rb-r4-4b") is True   # unblocked


@pytest.mark.parametrize("category", [None, "", "spot", 7, ["linear"]],
                         ids=["missing_category", "empty_category",
                              "foreign_category", "non_string_category",
                              "list_category"])
def test_envelope_category_gate_blocks(tmp_path, category):
    """R4-4: missing/empty/foreign ``result.category`` is NEVER a
    confirmation — explicit fail-closed blocker."""
    journal = tmp_path / "journal.json"
    guard = _prepare_unknown(journal, olink="rb-r4-4c")
    session = ScriptedSession([order_response([order_row("rb-r4-4c")],
                                              category=category)])
    results = BybitClient(live_settings(),
                          session).reconcile_order_submissions()
    assert results[0]["resolution"] == "UNRESOLVED_BLOCKER"
    assert guard.begin(FP, order_link_id="rb-r4-4d") is False


def test_envelope_category_gate_applies_to_history_endpoint(tmp_path):
    """R4-4: the gate is on the response envelope of EVERY query — a wrong
    category on /v5/order/history cannot confirm either."""
    journal = tmp_path / "journal.json"
    guard = _prepare_unknown(journal, olink="rb-r4-4e")
    session = ScriptedSession([
        order_response([]),                                     # realtime
        order_response([order_row("rb-r4-4e")], category="spot"),  # history
    ])
    results = BybitClient(live_settings(),
                          session).reconcile_order_submissions()
    assert results[0]["resolution"] == "UNRESOLVED_BLOCKER"
    assert guard.begin(FP, order_link_id="rb-r4-4f") is False


@pytest.mark.parametrize("row,case", [
    (order_row("rb-OTHER"), "foreign_order_link_id"),
    (order_row(""), "empty_order_link_id"),
    (order_row(None), "missing_order_link_id"),
    (order_row("rb-r4-4g", symbol="ETHUSDT"), "foreign_symbol"),
    (order_row("rb-r4-4g", status="Mystery"), "invalid_status"),
    (order_row("rb-r4-4g", status=None), "missing_status"),
    (order_row("rb-r4-4g", category="spot"), "contradicting_row_category"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_foreign_or_invalid_rows_never_confirm(tmp_path, row, case):
    """R4-4: only an exact orderLinkId + symbol + accepted status confirms —
    foreign orders are never taken for our own submission."""
    journal = tmp_path / "journal.json"
    guard = _prepare_unknown(journal, olink="rb-r4-4g")
    session = ScriptedSession([order_response([row])])
    results = BybitClient(live_settings(),
                          session).reconcile_order_submissions()
    assert results[0]["resolution"] == "UNRESOLVED_BLOCKER", case
    assert guard.begin(FP, order_link_id="rb-r4-4h") is False


def test_get_order_by_link_id_returns_only_exact_confirmation(tmp_path):
    """R4-4 (read-only helper): foreign rows return None, never a match."""
    journal = tmp_path / "journal.json"
    _prepare_unknown(journal, olink="rb-r4-4i")
    session = ScriptedSession([
        order_response([order_row("rb-OTHER")]),
        order_response([order_row("rb-OTHER")]),
    ])
    client = BybitClient(live_settings(), session)
    assert client.get_order_by_link_id("BTCUSDT", "rb-r4-4i") is None


def test_duplicate_protection_and_audit_trail_after_reconciliation(tmp_path):
    """Mandatory regression (crash/duplicate-POST protection): an ACTIVE
    entry blocks the same operation identity even under another fingerprint,
    and a later fresh submission NEVER loses the reconciled record — it is
    preserved verbatim as audit history."""
    journal = tmp_path / "journal.json"
    guard = _prepare_unknown(journal, olink="rb-dup-a")

    # duplicate protection while the outcome is unresolved
    assert guard.begin(("ETHUSDT", "Sell", "2", "Limit"),
                       order_link_id="rb-dup-a") is False    # same olink
    assert guard.begin(FP, order_link_id="rb-dup-b") is False # same order

    session = ScriptedSession([order_response([order_row("rb-dup-a")])])
    results = BybitClient(live_settings(),
                          session).reconcile_order_submissions()
    assert results[0]["resolution"] == "resolved"
    assert [c[0] for c in session.calls] == ["GET"]           # no POST at all

    # a fresh decision is possible now — with the prior record preserved
    assert guard.begin(FP, order_link_id="rb-dup-a") is True
    entries = retry_budget._validate_journal_document(
        json.loads(journal.read_text(encoding="utf-8")))
    entry = entries[retry_budget.OrderSubmitGuard._key(FP, "rb-dup-a")]
    assert entry["state"] == "in_flight"
    history = entry.get("history") or []
    assert history and history[-1]["state"] == "resolved"
    assert "exchange-confirmed orderLinkId=rb-dup-a" in history[-1]["evidence"]


# ======================================================================
# R4-5 — HTTP 429 classification (P2)
# ======================================================================

def test_http_error_429_is_a_rate_limit_and_retries(tmp_path, caplog):
    """R4-5: HTTPError(429) is a rate limit regardless of how the HTTP
    response was delivered — retryable within the budget."""
    session = ScriptedSession([http_error(429), FakeResponse()])
    assert make_client(session).get_klines("BTCUSDT") == []
    assert len(session.calls) == 2

    lines = attempt_lines(caplog)
    assert [field(line, "result") for line in lines] == ["RATE_LIMIT", "OK"]
    assert [field(line, "retry_reason") for line in lines] == ["none",
                                                              "rate_limit"]
    assert [int(field(line, "attempt_index")) for line in lines] == [1, 2]
    assert all(field(line, "total_attempts") == "3" for line in lines)
    assert field(lines[0], "status_code") == "429"      # measured, not guessed
    assert field(lines[0], "status_code_source") == "measured"
    assert "retMsg=HTTP 429" in lines[0]                # secret-free label


def test_http_error_429_telemetry_parity_with_retcode_10006(caplog):
    """R4-5 telemetry parity: HTTPError(429) and HTTP 200 + retCode=10006
    produce the SAME classification/accounting (delivery independent)."""
    session = ScriptedSession([http_error(429), FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    exception_delivery = attempt_lines(caplog)

    caplog.clear()
    session = ScriptedSession([
        FakeResponse({"retCode": 10006, "retMsg": "Too many visits",
                      "result": {}}),
        FakeResponse(),
    ])
    make_client(session).get_klines("BTCUSDT")
    envelope_delivery = attempt_lines(caplog)

    # classification/accounting parity (the HTTP status itself legitimately
    # differs: 429 vs 200 — the classification must not)
    for name in ("result", "retry_reason", "attempt_index", "total_attempts",
                 "method"):
        assert [field(line, name) for line in exception_delivery] == \
               [field(line, name) for line in envelope_delivery], name
    assert [field(line, "result") for line in exception_delivery] == [
        "RATE_LIMIT", "OK"]
    # both deliveries carry the same secret-free throttle label
    assert all("retMsg=HTTP 429" in line for line in exception_delivery[:1])
    assert all("retMsg=Too many visits" in line
               for line in envelope_delivery[:1])


def test_http_error_429_exhaustion_respects_hard_max_attempts():
    """R4-5: rate-limit exhaustion on the exception path stays inside
    HARD_MAX_ATTEMPTS (<= 3) and fails closed with BybitRateLimitError."""
    session = ScriptedSession([http_error(429)] * 5)
    with pytest.raises(BybitRateLimitError):
        make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 3                    # exactly the budget
    assert len(session.calls) <= retry_budget.HARD_MAX_ATTEMPTS


def test_http_error_429_mixed_sequence_no_nested_retries():
    """R4-5: any sequence of HTTPError(429)/timeout outcomes issues at most
    HARD_MAX_ATTEMPTS HTTP requests — no nested retry layer anywhere."""
    session = ScriptedSession([http_error(429), TimeoutError("slow"),
                               http_error(429), FakeResponse()])
    with pytest.raises(BybitRateLimitError):
        make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 3
    assert len(session.calls) <= retry_budget.HARD_MAX_ATTEMPTS


def test_http_error_429_is_counted_by_storm_guard_and_opens_breaker():
    """R4-5: storm guard and breaker still govern the exception delivery —
    the throttled retry is counted against the storm cap (and refused when
    the share is exhausted) and the process breaker cooldown is opened."""
    session = ScriptedSession([http_error(429), FakeResponse()])
    with pytest.raises(BybitRateLimitError, match="storm guard"):
        make_client(session, bybit_retry_storm_max_retries=0).get_klines("BTCUSDT")
    assert len(session.calls) == 1                # the capped retry never left
    assert retry_budget.get_breaker().is_open is True


def test_timeout_semantics_unchanged_under_429_hardening():
    """R4-5: the timeout class keeps its exact behaviour (retries + concise
    exhaustion error) next to the new 429 handling."""
    session = ScriptedSession([TimeoutError("slow"), TimeoutError("slow"),
                               TimeoutError("slow"), FakeResponse()])
    with pytest.raises(BybitTimeoutError):
        make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 3


def test_deadline_still_fails_closed_on_429_retry(monkeypatch):
    """R4-5: a rate-limit retry that cannot fit into the fail-closed deadline
    is refused BEFORE the HTTP send (deadline contract preserved: at most one
    request left, fail-closed error)."""
    clock = _install_fake_clock(monkeypatch)
    session = ScriptedSession([http_error(429), FakeResponse()])
    client = make_client(session, bybit_rate_limit_retry_backoff=5.0,
                         bybit_retry_deadline_s=1.0)
    with pytest.raises(BybitError, match="deadline"):
        client.get_klines("BTCUSDT")
    assert len(session.calls) == 1                # the retry never left
    assert clock.now > 1000.0                     # backoff waited on the clock


def test_post_http_error_429_never_retried_and_keeps_block(tmp_path, caplog):
    """R4-5: a throttled POST is NEVER retried and has an INDETERMINATE
    outcome — the durable block is kept (fail-closed)."""
    journal = tmp_path / "journal.json"
    session = ScriptedSession([http_error(429), http_error(429)])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)

    with pytest.raises(BybitRateLimitError):
        client.create_order(*FP, order_link_id="rb-r4-5a")
    assert [c[0] for c in session.calls] == ["POST"]   # exactly one attempt

    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-r4-5a")
    assert [c[0] for c in session.calls] == ["POST"]   # no resend

    guard = retry_budget.OrderSubmitGuard(str(journal))
    assert guard.blocking_entry(FP) == ("unknown", "rb-r4-5a")
    line = attempt_lines(caplog)[0]
    assert field(line, "result") == "RATE_LIMIT"
    assert field(line, "status_code") == "429"


def test_post_429_telemetry_parity_across_delivery_mechanisms(tmp_path,
                                                               caplog):
    """R4-5: the SAME HTTP 429 produces the SAME journal state and telemetry
    whether it arrives as a response or as a raised urllib.HTTPError."""
    def run(outcome):
        caplog.clear()
        journal = tmp_path / f"journal-{outcome[0]}.json"
        session = ScriptedSession([outcome[1]])
        client = BybitClient(live_settings(
            bybit_order_submit_journal_path=str(journal)), session)
        with pytest.raises(BybitRateLimitError):
            client.create_order(*FP, order_link_id="rb-r4-5b")
        guard = retry_budget.OrderSubmitGuard(str(journal))
        state, _olink = guard.blocking_entry(FP)
        line = attempt_lines(caplog)[0]
        return state, {name: field(line, name) for name in
                       ("result", "status_code", "method",
                        "attempt_index", "total_attempts")}

    via_exception = run(("exc", http_error(429)))
    via_response = run(("resp", FakeResponse(status=429)))
    assert via_exception == via_response
    assert via_exception[0] == "unknown"


def test_post_429_block_lives_until_reconciliation(tmp_path):
    """R4-5: only a STRICT exchange confirmation may decide the throttled
    submission — never a timer, never an automatic retry."""
    journal = tmp_path / "journal.json"
    session = ScriptedSession([
        http_error(429),
        order_response([order_row("rb-r4-5c")]),
    ])
    client = BybitClient(live_settings(
        bybit_order_submit_journal_path=str(journal)), session)
    with pytest.raises(BybitRateLimitError):
        client.create_order(*FP, order_link_id="rb-r4-5c")

    guard = retry_budget.OrderSubmitGuard(str(journal))
    assert guard.begin(FP, order_link_id="rb-r4-5c") is False

    results = client.reconcile_order_submissions()
    assert results[0]["resolution"] == "resolved"
    assert [c[0] for c in session.calls] == ["POST", "GET"]   # no POST retry


class _FakeClock:
    """Controlled clock: sleep() advances time deterministically."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def time(self):
        return 1_700_000_000.0 + self.now


def _install_fake_clock(monkeypatch):
    """Deterministic clock for deadline tests (same pattern as
    ``test_bybit_retry_budget_v1.install_fake_clock``): the token limiter
    waits on real Event time and is neutralized so only the controlled clock
    governs the test."""
    clock = _FakeClock()
    monkeypatch.setattr(bybit_client_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(bybit_client_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(bybit_client_module.time, "time", clock.time)

    class _NoopLimiter:
        def acquire(self):
            return None

    monkeypatch.setattr(BybitClient, "_global_limiter", _NoopLimiter())
    return clock
