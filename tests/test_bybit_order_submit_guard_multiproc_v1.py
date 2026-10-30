"""Cross-process order-submit guard tests — code review #5709889450 (P1).

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
