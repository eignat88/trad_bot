"""Cross-process order-submit guard tests — code review #5709889450 (P1)
and #5710398920 (R3 restart recovery).

Two PROCESSES sharing one order-submit journal must never send the same
order twice. Coverage (all on fake transport — no Bybit, no real orders):

* two processes, identical fingerprint, both calling create_order with the
  first HTTP POST blocked: EXACTLY ONE POST reaches the network;
* two processes, distinct fingerprints: no lost updates — every journal
  record from both processes survives;
* crash AFTER reservation (before POST): the second process cannot send;
* crash AFTER POST (before response): the journal keeps the reservation and
  a restart keeps the block;
* journal lock held by another process: submissions fail closed with
  ZERO HTTP attempts;
* concurrent reconciliation and create_order on one journal stay consistent.

R3 (review #5710398920) — crash/restart recovery tests A/B/C/F/G:

* Test A — crash before POST: 0 duplicate POST, the operation becomes
  reconciliation-visible and the recovered state survives restarts;
* Test B — crash after POST, before response: repeat POST blocked, strict
  orderLinkId reconciliation resolves the ORIGINAL order with evidence;
* Test C — owner still alive: the active operation is NOT declared orphaned,
  repeat POST forbidden, the owner's record untouched;
* Test F — crash during the recovery transition (before / after the durable
  write): fail-closed, journal never corrupted, later restart recovers;
* Test G — concurrent recovery/reconciliation of two processes: no lost
  updates, no corruption, no repeat POST, no wrongful unlock.

Workers are module-level functions (Windows spawn-safe) and coordinate via
``multiprocessing`` primitives. Real OS locks are exercised on a real file —
the production path of ``OrderSubmitGuard``.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import time

import pytest

from app.config import Settings
from app.exchange import retry_budget
from app.exchange.bybit_client import BybitClient, BybitError, BybitOrderUncertainError

FP = ("BTCUSDT", "Buy", "1", "Market")


class FakeResp:
    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else {
            "retCode": 0, "retMsg": "OK", "result": {"list": []}}
        self.status = status
        self.headers = None

    def raise_for_status(self):
        if not 200 <= self.status < 300:
            raise BybitError(f"HTTP {self.status}")

    def json(self):
        return self.payload


def _live_settings(**overrides):
    values = dict(
        trading_mode="live",
        live_trading_enabled=True,
        live_safety_ready=True,
        bybit_api_key="APIKEY_SENTINEL_1234567890",
        bybit_api_secret="APISECRET_SENTINEL_0987654321",
        bybit_timeout=5,
        bybit_max_attempts=3,
        bybit_retry_backoff=0.0,
    )
    values.update(overrides)
    return Settings(**values)


# ------------------------------------------------------------- workers
# NOTE: workers exchange results via small files, not multiprocessing.Queue
# — Windows named pipes are unavailable in this sandbox; plain files keep
# the tests portable and the coordination explicit (join + read).
def _write_result(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(text))


def _worker_blocking_post(journal, post_log, result_file, entered, release):
    """Process A: submit with a POST that blocks until ``release``."""
    class Session:
        def post(self, url, data=None, headers=None, timeout=None):
            with open(post_log, "a", encoding="utf-8") as fh:
                fh.write("POST-A\n")
            entered.set()
            if not release.wait(30):
                raise RuntimeError("release never came")
            return FakeResp({"retCode": 0, "retMsg": "OK",
                             "result": {"orderId": "EX-A"}})
    try:
        client = BybitClient(_live_settings(
            bybit_order_submit_journal_path=journal), Session())
        client.create_order(*FP, order_link_id="rb-mp-1")
        _write_result(result_file, "ok")
    except Exception as exc:  # noqa: BLE001
        _write_result(result_file, f"error:{type(exc).__name__}")


def _worker_second_submit(journal, post_log, result_file):
    """Process B: identical submission while A's POST is in flight."""
    class Session:
        def post(self, url, data=None, headers=None, timeout=None):
            with open(post_log, "a", encoding="utf-8") as fh:
                fh.write("POST-B\n")
            return FakeResp()
    try:
        client = BybitClient(_live_settings(
            bybit_order_submit_journal_path=journal), Session())
        client.create_order(*FP, order_link_id="rb-mp-1")
        _write_result(result_file, "ok")
    except BybitOrderUncertainError:
        _write_result(result_file, "blocked")
    except Exception as exc:  # noqa: BLE001
        _write_result(result_file, f"error:{type(exc).__name__}")


def _worker_guard_entries(journal, tag, count, result_file):
    """Guard-level UNKNOWN reservations with distinct fingerprints."""
    guard = retry_budget.OrderSubmitGuard(journal)
    done = 0
    for i in range(count):
        fp = (f"SYM{tag}{i}", "Buy", "1", "Market")
        olink = f"rb-mp-{tag}-{i}"
        if guard.begin(fp, order_link_id=olink):
            guard.mark_unknown(fp, order_link_id=olink,
                               reason="indeterminate — outcome UNKNOWN")
            done += 1
    _write_result(result_file, done)


def _worker_crash_after_reserve(journal, olink):
    """CRASH right after the durable reservation (before any POST)."""
    guard = retry_budget.OrderSubmitGuard(journal)
    if not guard.begin(FP, order_link_id=olink):
        os._exit(2)
    os._exit(0)   # simulated crash: no cleanup, no completion


def _worker_reserve_many_then_exit(journal, fps, olinks):
    """Reserve SEVERAL records in one process, then the process exits.

    The owner's witness lock dies with the process, so every record is an
    orphaned ``in_flight`` record for any later recovery. Records of THIS
    process are never recovered while it lives (live-witness registry).
    """
    guard = retry_budget.OrderSubmitGuard(journal)
    for fp, olink in zip(fps, olinks):
        if not guard.begin(tuple(fp), order_link_id=olink):
            os._exit(2)
    os._exit(0)


def _worker_crash_after_post(journal, post_log, olink):
    """CRASH after the POST left the process (before any response)."""
    class Session:
        def post(self, url, data=None, headers=None, timeout=None):
            with open(post_log, "a", encoding="utf-8") as fh:
                fh.write("POST\n")
            os._exit(1)   # simulated crash mid-flight
    try:
        client = BybitClient(_live_settings(
            bybit_order_submit_journal_path=journal), Session())
        client.create_order(*FP, order_link_id=olink)
    except Exception:  # noqa: BLE001
        pass


def _worker_hold_lock(lock_path, hold_s, ready):
    """Hold the cross-process journal lock for a while."""
    with retry_budget._JournalFileLock(lock_path, 60.0):
        ready.set()
        time.sleep(hold_s)


def _worker_reconcile_loop(journal, rounds, result_file):
    """Concurrent reconciliation on the shared journal (fake transport)."""
    class Session:
        def get(self, url, params=None, timeout=None):
            return FakeResp()
    client = BybitClient(_live_settings(
        bybit_order_submit_journal_path=journal), Session())
    retry_budget.get_order_guard().configure(journal)
    outcomes = 0
    for _ in range(rounds):
        outcomes += len(client.reconcile_order_submissions())
    _write_result(result_file, outcomes)


# ------------------------------------------- R3 recovery workers
def _worker_recover_and_reconcile(journal, olink, confirm, post_log, result_file):
    """Process B: restart recovery + strict reconciliation (fake transport).

    First attempts an identical re-POST: it must be refused with ZERO HTTP
    attempts (a repeat POST is forbidden until the previous outcome is
    confirmed). ``confirm=True`` makes the fake exchange return the exact
    original order (strict orderLinkId match).
    """
    class Session:
        def post(self, url, data=None, headers=None, timeout=None):
            with open(post_log, "a", encoding="utf-8") as fh:
                fh.write("POST-B\n")
            return FakeResp()

        def get(self, url, params=None, timeout=None):
            if confirm:
                return FakeResp({"retCode": 0, "result": {"list": [{
                    "orderLinkId": olink, "symbol": "BTCUSDT",
                    "category": "linear", "orderStatus": "Filled",
                    "orderId": "EX-RECOVERED"}]}})
            return FakeResp()
    out: dict = {}
    client = BybitClient(_live_settings(
        bybit_order_submit_journal_path=journal), Session())
    retry_budget.get_order_guard().configure(journal)   # restart recovery
    try:
        client.create_order(*FP, order_link_id=f"{olink}-retry")
        out["resubmit"] = "sent"                # must NEVER happen
    except BybitOrderUncertainError:
        out["resubmit"] = "blocked"
    except Exception as exc:  # noqa: BLE001
        out["resubmit"] = f"error:{type(exc).__name__}"
    try:
        out["results"] = client.reconcile_order_submissions()
    except Exception as exc:  # noqa: BLE001
        out["results"] = f"error:{type(exc).__name__}"
    _write_result(result_file, json.dumps(out))


def _worker_crash_during_recovery(journal, mode):
    """Crash IN THE RECOVERY TRANSITION of ``configure()`` (Test F).

    ``before-durable-write`` — die after the in-memory transition but before
    any durable write; ``after-durable-write`` — die right after the durable
    write of the recovery transition.
    """
    orig_write = retry_budget.OrderSubmitGuard._write

    def write_then_exit(self):
        ok = orig_write(self)
        os._exit(4 if ok else 5)

    def exit_before_write(self):
        os._exit(3)

    if mode == "before-durable-write":
        retry_budget.OrderSubmitGuard._write = exit_before_write
    else:
        retry_budget.OrderSubmitGuard._write = write_then_exit
    retry_budget.OrderSubmitGuard(journal)      # configure -> recovery -> crash
    os._exit(0)                                 # reached only without a transition


def _worker_recovery_reconcile_loop(journal, rounds, post_log, result_file):
    """Concurrent recovery + reconciliation (Test G, fake transport).

    Never POSTs: a POST attempt is recorded and fails the worker.
    """
    class Session:
        def get(self, url, params=None, timeout=None):
            return FakeResp()

        def post(self, url, data=None, headers=None, timeout=None):
            with open(post_log, "a", encoding="utf-8") as fh:
                fh.write("POST\n")
            raise AssertionError("recovery/reconciliation must never POST")
    client = BybitClient(_live_settings(
        bybit_order_submit_journal_path=journal), Session())
    guard = retry_budget.get_order_guard()
    guard.configure(journal)                    # restart recovery
    first = guard.last_recovery_report() or {}
    recovered = len(first.get("recovered", []))
    outcomes = 0
    for _ in range(rounds):
        report = guard.recover_orphaned_in_flight()
        recovered += len(report["recovered"])
        outcomes += len(client.reconcile_order_submissions())
    _write_result(result_file,
                  json.dumps({"recovered": recovered, "outcomes": outcomes}))


# ------------------------------------------------------------- tests
def _start(ctx, target, args):
    proc = ctx.Process(target=target, args=args)
    proc.start()
    return proc


def test_multiproc_same_fingerprint_exactly_one_post(tmp_path):
    """AC: two processes, one identical create_order call each, first HTTP
    POST blocked — exactly ONE POST reaches the network."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    post_log = str(tmp_path / "post.log")
    res_a, res_b = str(tmp_path / "res_a.txt"), str(tmp_path / "res_b.txt")
    entered, release = ctx.Event(), ctx.Event()

    a = _start(ctx, _worker_blocking_post,
               (journal, post_log, res_a, entered, release))
    assert entered.wait(30), "process A reached its POST"
    b = _start(ctx, _worker_second_submit, (journal, post_log, res_b))
    b.join(60)
    assert b.exitcode == 0

    # while A is still in flight, B must be blocked and send nothing
    with open(post_log, encoding="utf-8") as fh:
        assert fh.read().splitlines() == ["POST-A"]
    with open(res_b, encoding="utf-8") as fh:
        assert fh.read() == "blocked"

    release.set()
    a.join(60)
    assert a.exitcode == 0
    with open(res_a, encoding="utf-8") as fh:
        assert fh.read() == "ok"
    with open(post_log, encoding="utf-8") as fh:
        assert fh.read().splitlines() == ["POST-A"]   # still exactly one


def test_multiproc_different_fingerprints_no_lost_updates(tmp_path):
    """AC: two processes writing different records — none is lost."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    res_a, res_b = str(tmp_path / "res_a.txt"), str(tmp_path / "res_b.txt")
    procs = [_start(ctx, _worker_guard_entries, (journal, tag, 5, res))
             for tag, res in (("A", res_a), ("B", res_b))]
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    with open(res_a, encoding="utf-8") as fh:
        assert fh.read() == "5"
    with open(res_b, encoding="utf-8") as fh:
        assert fh.read() == "5"

    with open(journal, encoding="utf-8") as fh:
        data = json.load(fh)
    entries = retry_budget._validate_journal_document(data)
    assert len(entries) == 10                    # 5 + 5, no lost updates


def test_multiproc_crash_after_reservation_blocks_second(tmp_path):
    """AC: process 1 crashes after reserving, before POST — process 2 must
    NOT send; a restart keeps the block (as UNKNOWN)."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    post_log = str(tmp_path / "post.log")

    p = _start(ctx, _worker_crash_after_reserve, (journal, "rb-crash-1"))
    p.join(60)
    assert p.exitcode == 0                       # the crash was clean here

    class Session:
        def post(self, url, data=None, headers=None, timeout=None):
            with open(post_log, "a", encoding="utf-8") as fh:
                fh.write("POST\n")
            return FakeResp()
    session = Session()
    client = BybitClient(_live_settings(
        bybit_order_submit_journal_path=journal), session)
    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-crash-1")
    assert not os.path.exists(post_log)          # 0 HTTP attempts

    # restart view: the crashed in_flight reloads as UNKNOWN (fail-closed)
    guard = retry_budget.OrderSubmitGuard(journal)
    state, _olink = guard.blocking_entry(FP)
    assert state == "unknown"
    assert guard.begin(FP, order_link_id="rb-crash-1-2") is False


def test_multiproc_crash_after_post_before_response_keeps_block(tmp_path):
    """AC: crash after the POST left, before the response — the journal
    keeps the reservation and no duplicate POST happens after restart."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    post_log = str(tmp_path / "post.log")

    p = _start(ctx, _worker_crash_after_post,
               (journal, post_log, "rb-crash-2"))
    p.join(60)
    assert p.exitcode == 1                       # simulated crash
    with open(post_log, encoding="utf-8") as fh:
        assert fh.read().splitlines() == ["POST"]

    guard = retry_budget.OrderSubmitGuard(journal)
    state, _olink = guard.blocking_entry(FP)
    assert state == "unknown"                    # crash recovery
    assert guard.begin(FP, order_link_id="rb-crash-2-2") is False

    class Session:
        def post(self, url, data=None, headers=None, timeout=None):
            with open(post_log, "a", encoding="utf-8") as fh:
                fh.write("POST\n")
            return FakeResp()
    client = BybitClient(_live_settings(
        bybit_order_submit_journal_path=journal), Session())
    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-crash-2-3")
    with open(post_log, encoding="utf-8") as fh:
        assert fh.read().splitlines() == ["POST"]   # still exactly one


def test_multiproc_lock_held_by_other_process_fails_closed(tmp_path,
                                                           monkeypatch):
    """AC: lock acquisition failure -> zero HTTP attempts (fail-closed)."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    lock_path = journal + ".lock"
    ready = ctx.Event()
    holder = _start(ctx, _worker_hold_lock, (lock_path, 8.0, ready))
    assert ready.wait(30), "holder acquired the lock"

    monkeypatch.setattr(retry_budget, "ORDER_LOCK_TIMEOUT_S", 0.3)

    class Session:
        def post(self, url, data=None, headers=None, timeout=None):
            raise AssertionError("no HTTP attempt may happen")
    session = Session()
    client = BybitClient(_live_settings(
        bybit_order_submit_journal_path=journal), session)
    with pytest.raises(BybitOrderUncertainError):
        client.create_order(*FP, order_link_id="rb-lock-1")
    holder.join(60)
    assert holder.exitcode == 0


def test_multiproc_concurrent_reconciliation_and_create(tmp_path):
    """Concurrent reconciliation and reservations on one journal stay
    consistent: journal validates, all records survive."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    res_w = str(tmp_path / "res_w.txt")
    res_r = str(tmp_path / "res_r.txt")
    writer = _start(ctx, _worker_guard_entries, (journal, "C", 5, res_w))
    reconciler = _start(ctx, _worker_reconcile_loop, (journal, 3, res_r))
    for p in (writer, reconciler):
        p.join(90)
        assert p.exitcode == 0

    with open(journal, encoding="utf-8") as fh:
        data = json.load(fh)
    entries = retry_budget._validate_journal_document(data)  # checksum ok
    assert len(entries) == 5                     # every record survived


# ------------------------------------------------- R3 recovery tests
def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _single_entry(journal):
    entries = retry_budget._validate_journal_document(_read_json(journal))
    assert len(entries) == 1
    return next(iter(entries.values()))


# ---- Test A — crash before POST ------------------------------------

def test_A_crash_before_post_zero_duplicate_post_recovery_visible(tmp_path):
    """Test A: crash before POST -> 0 duplicate POST, the unfinished
    operation becomes reconciliation-visible and the recovered state
    survives repeated restarts (journal stays valid)."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    post_log = str(tmp_path / "post.log")
    res_b = str(tmp_path / "res_b.txt")

    p = _start(ctx, _worker_crash_after_reserve, (journal, "rb-mp-A-1"))
    p.join(60)
    assert p.exitcode == 0                       # process A died (crash)

    b = _start(ctx, _worker_recover_and_reconcile,
               (journal, "rb-mp-A-1", False, post_log, res_b))
    b.join(60)
    assert b.exitcode == 0
    out = json.loads(_read_text(res_b))
    assert out["resubmit"] == "blocked"          # repeat POST forbidden
    assert not os.path.exists(post_log)          # 0 HTTP attempts
    # recovery visible: the operation participates in reconciliation
    assert [r["order_link_id"] for r in out["results"]] == ["rb-mp-A-1"]
    assert out["results"][0]["resolution"] == "UNRESOLVED_BLOCKER"

    # journal valid + recovered state survives ANOTHER restart
    only = _single_entry(journal)
    assert only["state"] == "unknown"
    assert only["recovery"]["from_state"] == "in_flight"
    assert only["order_link_id"] == "rb-mp-A-1"
    assert only["fingerprint"] == [str(x) for x in FP]
    guard = retry_budget.OrderSubmitGuard(journal)   # restart #2
    assert guard.last_recovery_report()["recovered"] == []   # idempotent
    assert guard.blocking_entry(FP) == ("unknown", "rb-mp-A-1")
    assert guard.begin(FP, order_link_id="rb-mp-A-2") is False


# ---- Test B — crash after POST, before response --------------------

def test_B_crash_after_post_before_response_reconciles_exact_order_link_id(
        tmp_path):
    """Test B: one initial POST, zero repeat POST; after recovery the strict
    orderLinkId reconciliation confirms the ORIGINAL order and the operation
    becomes ``resolved`` with audit evidence."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    post_log = str(tmp_path / "post.log")
    res_b = str(tmp_path / "res_b.txt")

    p = _start(ctx, _worker_crash_after_post,
               (journal, post_log, "rb-mp-B-1"))
    p.join(60)
    assert p.exitcode == 1                       # simulated crash
    assert _read_text(post_log).splitlines() == ["POST"]   # exactly one

    b = _start(ctx, _worker_recover_and_reconcile,
               (journal, "rb-mp-B-1", True, post_log, res_b))
    b.join(60)
    assert b.exitcode == 0
    out = json.loads(_read_text(res_b))
    assert out["resubmit"] == "blocked"          # zero repeat POST
    assert out["results"][0]["resolution"] == "resolved"
    detail = out["results"][0]["detail"]
    assert "orderLinkId=rb-mp-B-1" in detail
    assert "orderStatus=Filled" in detail
    assert "verified_at=" in detail
    assert _read_text(post_log).splitlines() == ["POST"]   # still exactly one

    only = _single_entry(journal)                # journal valid
    assert only["state"] == "resolved"
    assert "exchange-confirmed" in only["evidence"]
    assert only["order_link_id"] == "rb-mp-B-1"          # audit preserved
    assert only["fingerprint"] == [str(x) for x in FP]
    assert only["recovery"]["from_state"] == "in_flight"
    assert only["owner"]                                  # ownership audit kept


# ---- Test C — owner still alive ------------------------------------

def test_C_owner_alive_not_orphaned_no_resubmit_state_intact(tmp_path):
    """Test C: while process A's POST is blocked (owner alive), recovery must
    NOT declare the operation orphaned; a repeat POST stays blocked and the
    owner's durable record is untouched."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    post_log = str(tmp_path / "post.log")
    res_a, res_b = str(tmp_path / "res_a.txt"), str(tmp_path / "res_b.txt")
    entered, release = ctx.Event(), ctx.Event()

    a = _start(ctx, _worker_blocking_post,
               (journal, post_log, res_a, entered, release))
    assert entered.wait(30), "process A reached its POST"
    before = _read_json(journal)                 # durable in_flight record

    b = _start(ctx, _worker_recover_and_reconcile,
               (journal, "rb-mp-1", False, post_log, res_b))
    b.join(60)
    assert b.exitcode == 0
    out = json.loads(_read_text(res_b))
    assert out["resubmit"] == "blocked"          # repeat POST forbidden
    assert [r["resolution"] for r in out["results"]] == [
        retry_budget.ORDER_RESOLUTION_OWNER_ALIVE]
    assert "NOT orphaned" in out["results"][0]["detail"]
    assert _read_text(post_log).splitlines() == ["POST-A"]  # B sent nothing

    assert _read_json(journal) == before         # owner's state untouched
    assert next(iter(before["entries"].values()))["state"] == "in_flight"

    release.set()
    a.join(60)
    assert a.exitcode == 0
    assert _read_text(res_a) == "ok"
    final = _single_entry(journal)               # owner completed its own op
    assert final["state"] == "resolved"
    assert final["owner"] == next(iter(before["entries"].values()))["owner"]
    assert _read_text(post_log).splitlines() == ["POST-A"]  # exactly one


# ---- Test F — crash during the recovery transition -----------------

def test_F_crash_during_recovery_before_durable_write(tmp_path):
    """Test F: crash BEFORE the durable write of the recovery transition —
    the journal is untouched and valid; a later restart recovers cleanly
    with zero duplicate POST."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")

    p = _start(ctx, _worker_crash_after_reserve, (journal, "rb-mp-F-1"))
    p.join(60)
    assert p.exitcode == 0

    p2 = _start(ctx, _worker_crash_during_recovery,
                (journal, "before-durable-write"))
    p2.join(60)
    assert p2.exitcode == 3                      # crashed before the write
    assert _single_entry(journal)["state"] == "in_flight"   # untouched+valid

    guard = retry_budget.OrderSubmitGuard(journal)   # clean restart
    assert guard.blocking_entry(FP) == ("unknown", "rb-mp-F-1")
    assert guard.begin(FP, order_link_id="rb-mp-F-2") is False
    assert _single_entry(journal)["state"] == "unknown"


def test_F_crash_during_recovery_after_durable_write(tmp_path):
    """Test F: crash AFTER the durable write of the recovery transition —
    the recovered state is persisted and further restarts are idempotent."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")

    p = _start(ctx, _worker_crash_after_reserve, (journal, "rb-mp-F-3"))
    p.join(60)
    assert p.exitcode == 0

    p2 = _start(ctx, _worker_crash_during_recovery,
                (journal, "after-durable-write"))
    p2.join(60)
    assert p2.exitcode == 4                      # crashed after the write
    only = _single_entry(journal)                # recovered state persisted
    assert only["state"] == "unknown"
    assert only["recovery"]["from_state"] == "in_flight"

    before = _read_text(journal)
    guard = retry_budget.OrderSubmitGuard(journal)   # restart: idempotent
    assert guard.last_recovery_report()["recovered"] == []
    assert guard.begin(FP, order_link_id="rb-mp-F-4") is False
    assert _read_text(journal) == before         # nothing rewritten
    assert _single_entry(journal)["state"] == "unknown"


# ---- Test G — concurrent recovery ----------------------------------

def test_G_concurrent_recovery_no_lost_updates_no_unlock_no_post(tmp_path):
    """Test G: two processes run recovery/reconciliation concurrently over
    one journal — no lost updates, no journal corruption, no repeat POST and
    no wrongful unlock (each record transitions exactly once)."""
    ctx = mp.get_context("spawn")
    journal = str(tmp_path / "journal.json")
    post_log = str(tmp_path / "post.log")
    res1, res2 = str(tmp_path / "res1.txt"), str(tmp_path / "res2.txt")

    fps = [(f"SYM-G-{i}", "Buy", "1", "Market") for i in range(4)]
    olinks = [f"rb-mp-G-{i}" for i in range(4)]
    seed = _start(ctx, _worker_reserve_many_then_exit,
                  (journal, fps, olinks))        # 4 orphaned in_flight records
    seed.join(60)
    assert seed.exitcode == 0

    w1 = _start(ctx, _worker_recovery_reconcile_loop,
                (journal, 3, post_log, res1))
    w2 = _start(ctx, _worker_recovery_reconcile_loop,
                (journal, 3, post_log, res2))
    for w in (w1, w2):
        w.join(90)
        assert w.exitcode == 0

    assert not os.path.exists(post_log)          # recovery/reconcile never POST
    entries = retry_budget._validate_journal_document(_read_json(journal))
    assert len(entries) == 4                     # no lost updates / duplicates
    for i, fp in enumerate(fps):
        entry = entries[retry_budget.OrderSubmitGuard._key(fp, f"rb-mp-G-{i}")]
        assert entry["state"] == "unknown"       # never auto-resolved
        assert entry["recovery"]["from_state"] == "in_flight"

    # each record was recovered EXACTLY once across both processes
    out1 = json.loads(_read_text(res1))
    out2 = json.loads(_read_text(res2))
    assert out1["recovered"] + out2["recovered"] == 4

    # repeat POST still forbidden for every recovered operation
    guard = retry_budget.OrderSubmitGuard(journal)
    for i, fp in enumerate(fps):
        assert guard.begin(fp, order_link_id=f"rb-mp-G-new-{i}") is False
