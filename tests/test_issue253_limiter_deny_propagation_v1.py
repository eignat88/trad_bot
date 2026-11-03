"""ISSUE253 — end-to-end propagation of a shared-limiter deny.

Every test here drives a REAL entrypoint (Scanner, Research evaluator,
HTF Phase-2 V1 observer source, SRR evaluator, Paper) with a REAL
``BybitClient`` whose shared ``GlobalBybitRateLimiter`` has genuinely
tripped its breaker, and proves the whole chain:

    limiter deny -> BybitRateLimitError BEFORE any HTTP attempt
                 -> the entrypoint's own guard prevents any outcome /
                    checkpoint / candidate / trade advancement
                 -> the next cycle can retry (recovery)

No database, no network, no frozen research policy, no scoring, no trading
gate and no freeze is touched: every collaborator that would persist
something is an in-memory fake that FAILS the test if it is written to.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import ScannerUniverseSettings, Settings
from app.exchange.bybit_client import BybitClient, BybitRateLimitError
from app.exchange.global_rate_limiter import GlobalBybitRateLimiter
from app.scanners.context_builder import build_market_context
from app.scanners.models import SetupCandidate

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- collaborators
class _Session:
    """Records every HTTP attempt; raises unless explicitly unblocked.

    A limiter deny must leave ``calls`` EMPTY — any URL here proves the
    request escaped the gate.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.blocked = True

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(f"GET {url}")
        if self.blocked:
            raise AssertionError(f"HTTP attempted despite limiter deny: {url}")
        return _OkResponse()

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(f"POST {url}")
        if self.blocked:
            raise AssertionError(f"HTTP attempted despite limiter deny: {url}")
        return _OkResponse()


class _OkResponse:
    status = 200

    def __init__(self, payload=None):
        self._payload = payload or {"retCode": 0, "retMsg": "OK",
                                    "result": {"list": []}}

    def raise_for_status(self):
        return None

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        global_bybit_rate_limiter_enabled=True,
        global_limiter_state_path=str(tmp_path / "shared_state.json"),
        global_limiter_breaker_cooldowns_s=(5.0, 30.0, 120.0),
        global_limiter_recovery_success_target=1,
        global_limiter_max_wait_s=0.01,
        bybit_timeout=2,
        bybit_max_attempts=1,
        bybit_retry_backoff=0.0,
        bybit_rate_limit_max_retries=0,
        bybit_rate_limit_retry_backoff=0.0,
        bybit_rate_limit_cooldown_s=0.0,
        bybit_retry_deadline_s=1.0,
    )
    values.update(overrides)
    return Settings(**values)


def _denying_client(tmp_path: Path, **overrides) -> tuple[BybitClient, _Session, Settings]:
    """A client whose shared limiter REALLY denies (breaker OPEN).

    The breaker is tripped through the public acquire/record_result API —
    no state-file surgery — so the deny every test observes is the genuine
    BREAKER_OPEN path, not a stub.
    """
    settings = _settings(tmp_path, **overrides)
    session = _Session()
    client = BybitClient(settings, session)
    limiter: GlobalBybitRateLimiter = client._shared_limiter()
    grant = limiter.acquire("P1")
    assert grant.granted, grant
    limiter.record_result(grant.grant_id, "P1", "RATE_LIMIT")  # trips breaker
    denied = limiter.acquire("P1")
    assert not denied.granted, "breaker did not open"
    assert session.calls == []          # tripping it must not send HTTP
    return client, session, settings


# ------------------------------------------------------------ fake persistence
class _ScriptedCursor:
    def __init__(self, conn: "_ScriptedConn") -> None:
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.sql.append(" ".join(str(sql).split()))
        self._step = len(self._conn.sql) - 1

    def fetchone(self):
        return self._conn.script[self._step].get("one")

    def fetchall(self):
        return list(self._conn.script[self._step].get("all", ()))

    def close(self):
        pass


class _ScriptedConn:
    """Records SQL; returns a per-statement scripted result.

    ``script[i]`` answers the i-th ``execute`` (``one``/``all``).
    Any INSERT / UPDATE / DELETE is a test failure — that is exactly the
    "no incorrect outcome advancement" property under test.
    """

    def __init__(self, script) -> None:
        self.script = script
        self.sql: list[str] = []

    def cursor(self):
        return _ScriptedCursor(self)

    def commit(self):
        pass

    @property
    def writes(self):
        return [s for s in self.sql
                if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]


class _PaperRepo:
    """Minimal in-memory repository used by PaperTradingEngine."""

    def __init__(self) -> None:
        self.saved: list = []
        self.closed: list = []

    def get_open_paper_trades(self):
        return []

    def get_paper_risk_state(self):
        return {"daily_loss_usdt": 0.0, "consecutive_losses": 0}

    def get_paper_safety_gate_state(self):
        return {"is_blocked": False, "reason": None, "blocked_since": None}

    def set_paper_safety_gate_mode(self, mode):
        pass

    def insert_paper_safety_event(self, event):
        pass

    def block_paper_safety_gate(self, reason, blocked_since):
        pass

    def get_latest_paper_account_snapshot(self):
        return None

    def get_paper_trade_by_setup(self, setup_id):
        return None

    def save_paper_trade(self, trade):
        self.saved.append(trade)
        return len(self.saved)

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        pass

    def save_dca_state(self, *args, **kwargs):
        pass


def _candidate() -> SetupCandidate:
    return SetupCandidate(
        setup_id="SETUP-1",
        scanner_name="TEST",
        symbol="BTCUSDT",
        direction="LONG",
        score=90.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        invalidation_price=90.0,
        target_1=120.0,
    )


def _source_text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def clean_process_state():
    from app.exchange import retry_budget
    retry_budget.reset_process_state()
    yield
    retry_budget.reset_process_state()


@pytest.fixture(autouse=True)
def fast_local_limiter(monkeypatch):
    """The pre-existing process-local limiter must never throttle a test."""
    from app.exchange.bybit_client import _RateLimiter
    monkeypatch.setattr(BybitClient, "_global_limiter",
                        _RateLimiter(rps=1_000_000.0))


# ===========================================================================
# 1. SCANNER
# ===========================================================================
def test_scanner_universe_deny_raises_before_any_http(tmp_path):
    """The dynamic universe refresh is the FIRST Bybit call of a cycle.

    It is deliberately unwrapped: a deny aborts the process before any scan
    run is opened, so no run row, no signal and no outcome can be written.
    """
    client, session, settings = _denying_client(
        tmp_path, scanner_universe=ScannerUniverseSettings(mode="dynamic"))
    from scanner_runner import get_scanner_universe

    with pytest.raises(BybitRateLimitError):
        get_scanner_universe(client, settings)

    assert session.calls == []


def test_scanner_market_context_deny_raises_before_any_http(tmp_path):
    """A denied symbol never becomes a MarketContext, hence never a scan."""
    client, session, settings = _denying_client(tmp_path)

    with pytest.raises(BybitRateLimitError):
        build_market_context(client, "BTCUSDT", settings)

    assert session.calls == []


def test_scanner_runner_guard_keeps_a_denied_symbol_out_of_the_scan():
    """Structural contract of scanner_runner.run_scan_cycle.

    ``future.result()`` must be inside an ``except Exception`` that records
    ``MARKET_DATA_ERROR`` and ``continue`` — i.e. the symbol is dropped
    BEFORE ``orchestrator.scan_all_with_stats`` and before any ``save_setup``.
    """
    src = _source_text("scanner_runner.py")
    result_at = src.index("ctx = future.result()")
    scan_at = src.index("orchestrator.scan_all_with_stats")
    block = src[result_at:scan_at]

    assert "except Exception" in block
    assert 'error_type="MARKET_DATA_ERROR"' in block
    assert "continue" in block
    assert block.index("except Exception") < block.index("continue")
    # ...and setup persistence only happens AFTER a context exists
    assert src.index("ctx = future.result()") < src.index("repository.save_setup")


# ===========================================================================
# 2. RESEARCH EVALUATOR (prospective OOS + generic candle fetch)
# ===========================================================================
def _prospective_row(symbol: str, signal_time: datetime):
    return (1, symbol, signal_time, "EXP-1", "LONG",
            100.0, 90.0, 120.0, 130.0,
            None, None, None, {}, None, None, None, None, None, False)


def test_research_evaluator_deny_writes_no_outcome(tmp_path):
    """Deny -> ``_get_candles`` returns [] -> guard -> ZERO outcome SQL."""
    from app.research.prospective_evaluator import ProspectiveOOSEvaluator

    started = datetime.now(timezone.utc) - timedelta(hours=1)
    signal_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    conn = _ScriptedConn([
        {"one": (started,)},                       # experiment.started_at
        {"all": []},                               # direction lifecycle
        {"all": [_prospective_row("BTCUSDT", signal_time)]},
    ])
    client, session, _ = _denying_client(tmp_path)
    evaluator = ProspectiveOOSEvaluator(conn, client)

    stats = evaluator.run_evaluation_cycle("EXP-1")

    assert session.calls == []                     # no HTTP escaped the gate
    assert conn.writes == []                       # no outcome row written
    assert stats["finalized"] == 0
    assert stats["signals_checked"] == 1
    assert stats["errors"] >= 1                    # counted, not finalized


def test_generic_research_fetch_deny_returns_empty(tmp_path):
    """The generic evaluator's own fetch swallows the deny to ``[]``."""
    from app.research.evaluator import ResearchEvaluator

    client, session, _ = _denying_client(tmp_path)
    evaluator = ResearchEvaluator.__new__(ResearchEvaluator)
    evaluator.client = client

    candles = evaluator._get_candles_for_symbol(
        "BTCUSDT",
        datetime.now(timezone.utc) - timedelta(hours=1),
        datetime.now(timezone.utc),
    )

    assert candles == []
    assert session.calls == []


# ===========================================================================
# 3. HTF PHASE-2 V1 OBSERVER (Bybit candle source)
# ===========================================================================
def test_htf_v1_source_deny_is_rate_limited_and_fail_closed(tmp_path):
    """A deny is translated into the source's own RATE_LIMITED reason.

    The observer only catches ``CandleSourceError``; a raw
    ``BybitRateLimitError`` escaping the source would abort the run instead
    of blocking one symbol, so the translation is part of the contract.
    """
    from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
        REASON_RATE_LIMITED,
        BybitCandleSourceError,
        BybitPhase2CandleSource,
    )

    client, session, _ = _denying_client(tmp_path)
    source = BybitPhase2CandleSource(client)

    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")

    assert excinfo.value.reason == REASON_RATE_LIMITED
    assert session.calls == []


def test_htf_v1_source_block_never_reaches_the_cursor():
    """Structural contract of the observer: deny -> SOURCE_BLOCKED -> continue.

    ``_advance_cursor`` may only be CALLED from ``process_symbol_bars``, and
    ``process_symbol_bars`` may only be called from the code AFTER the
    ``CandleSourceError`` handler — so a blocked symbol can never move its
    cursor.
    """
    import re

    src = _source_text(
        "app/research/htf_baseline_short_phase2_observer_runner_v1.py")

    handler_at = src.index("except CandleSourceError as exc:")
    block = src[handler_at:handler_at + 900]
    assert '"action": "SOURCE_BLOCKED"' in block
    assert "continue" in block

    process_def = src.index("def process_symbol_bars")
    next_def = src.index("\ndef ", process_def + 5)

    advance_calls = [m.start() for m in
                     re.finditer(r"(?<!def )_advance_cursor\(", src)]
    assert advance_calls, "the cursor must have a writer"
    assert all(process_def < c < next_def for c in advance_calls), (
        "_advance_cursor may only be called from process_symbol_bars")

    process_calls = [m.start() for m in
                     re.finditer(r"(?<!def )process_symbol_bars\(", src)]
    assert process_calls, "process_symbol_bars must be invoked"
    assert all(c > handler_at for c in process_calls), (
        "process_symbol_bars must only run after the SOURCE_BLOCKED guard")


# ===========================================================================
# 4. SRR EVALUATOR
# ===========================================================================
def test_srr_evaluator_deny_writes_no_outcome(tmp_path, monkeypatch):
    """Deny -> ``[]`` -> guard -> ``_save_outcome_partial`` is never called."""
    from app.shadow.srr_research_evaluator import SRRResearchEvaluator

    client, session, _ = _denying_client(tmp_path)
    evaluator = SRRResearchEvaluator(object(), client)

    monkeypatch.setattr(
        evaluator, "get_eligible_signals",
        lambda limit=5000: [{
            "signal_id": 1, "symbol": "BTCUSDT",
            "signal_time": datetime.now(timezone.utc) - timedelta(minutes=30),
        }])

    def _forbidden(*a, **k):
        raise AssertionError("outcome persisted despite limiter deny")

    monkeypatch.setattr(evaluator, "_save_outcome_partial", _forbidden)

    stats = evaluator.run_evaluation_cycle()

    assert session.calls == []
    assert stats["signals_checked"] == 1
    assert stats["outcomes_created"] == 0
    assert stats["outcomes_updated"] == 0
    assert stats["finalized"] == 0
    assert stats["errors"] == 1


# ===========================================================================
# 5. PAPER
# ===========================================================================
def test_paper_deny_opens_no_trades(tmp_path):
    """Paper never fetches candles: only tickers, and a deny yields `{}`."""
    from app.paper.engine import PaperTradingEngine
    import paper_runner

    client, session, settings = _denying_client(tmp_path)

    prices = paper_runner._get_prices(client, ["BTCUSDT"])
    assert prices == {}
    assert session.calls == []

    repo = _PaperRepo()
    engine = PaperTradingEngine(settings, repo)
    opened = engine.check_entries([_candidate()], prices)

    assert opened == []          # price is None -> skip, no trade recorded
    assert repo.saved == []


# ===========================================================================
# 6. RECOVERY — the next cycle works once the breaker cooldown expires
# ===========================================================================
def test_recovery_allows_the_same_entrypoint_after_cooldown(tmp_path):
    """deny -> cooldown -> RECOVERY grants -> the entrypoint sends again.

    Short, explicit cooldowns (0.05/0.10/0.20 s) keep this deterministic;
    ``recovery_success_target=1`` lets the single successful request close
    the breaker. The discriminator is the HTTP call count: a deny must send
    NOTHING, a recovered cycle must send EXACTLY one request.
    """
    import paper_runner

    cooldowns = (0.05, 0.10, 0.20)
    client, session, _ = _denying_client(
        tmp_path,
        global_limiter_breaker_cooldowns_s=cooldowns,
        global_limiter_recovery_success_target=1,
    )

    assert paper_runner._get_prices(client, ["BTCUSDT"]) == {}
    assert session.calls == []                    # deny: nothing left

    # ...breaker cooldown expires, the operator-visible next cycle retries
    time.sleep(max(cooldowns) + 0.10)
    session.blocked = False                       # network is fine again

    paper_runner._get_prices(client, ["BTCUSDT"])
    assert len(session.calls) == 1                # recovered: request sent
