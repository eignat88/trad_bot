"""Second Codex review fixes for ME_LONG_V2_INVERSE_SHORT_V1 (PR #267).

Mandatory fault-injection coverage for the three review findings (no
PostgreSQL required — the in-memory store runs behind a fault-injecting
subclass that can fail staging, terminal UPDATEs and balance reads
independently, per key and per call):

P1 — retry unstaged close snapshots
    (1) stage fails + UPDATE fails → stage retry → recovery;
    (2) stage fails repeatedly → never falsely claim durable recovery
        (UPDATE deferred until staging commits, risk reported, entries
        fail closed);
    (6) restart → exact terminal facts retained once the outbox was
        committed.

P2 — retry failed balance reconciliation
    (3) UPDATE succeeds + balance read fails → marker retained, later
        resync succeeds;
    (8) new entries blocked while the accounting is inconsistent.

P2 — preserve unpersisted local PnL
    (4) two pending closes across different symbols → both PnLs retained
        across a ledger resync (and across a restart);
    (5) out-of-order successful retries → final balance equals the
        persisted ledger;
    (7) no duplicate funding, exit fee or realized PnL.

Plus a migration-compatibility guard: when the outbox table is absent
(migration 086 not applied, ``stage_close()`` returns False) the terminal
UPDATE must still be attempted and the risk reported only until it commits.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.me_long_v2_inverse_short_v1 import (
    ActivationRecord,
    InMemoryInverseShortStore,
    InverseShortContour,
    capture_with_status,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_v1.capture import CAPTURED
from app.paper.me_long_v2_inverse_short_v1.models import (
    InverseShortPosition,
    POSITION_STATUS_CLOSED,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID,
    MAX_HOLD_MINUTES,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
)
from app.scanners.models import SetupCandidate, SetupState

ENGINE_LOGGER = "app.paper.me_long_v2_inverse_short_v1.engine"

PINNED_PROTOCOL_FINGERPRINT = (
    "4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c"
)
FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)


# ======================================================================
# Shared helpers (mirrors the helpers of the close-durability suite)
# ======================================================================

class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def _settings(**overrides) -> Settings:
    defaults = dict(
        initial_balance=10_000.0,
        risk_per_trade=0.005,
        max_symbol_exposure=0.20,
        max_open_positions=3,
        taker_fee=0.00055,
        slippage_percent=0.0005,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )
    defaults.update(overrides)
    return dataclasses.replace(Settings(), **defaults)


def _activation() -> ActivationRecord:
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        protocol_fingerprint=protocol_fingerprint(),
        freeze_ts=FREEZE_TS,
    )


def _candidate(
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    *,
    detected_at: datetime = SIGNAL_AT,
    candle_ms: int | None = None,
) -> SetupCandidate:
    if candle_ms is None:
        candle_ms = int(detected_at.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name=SOURCE_SCANNER,
        scanner_version="1.0.0",
        symbol=symbol,
        direction=SOURCE_DIRECTION,
        entry_timeframe="5m",
        detected_at=detected_at,
        setup_started_at=detected_at,
        signal_candle_open_time=candle_ms,
        reference_price=price,
        entry_zone_low=price * 0.998,
        entry_zone_high=price * 1.001,
        invalidation_price=price * 0.975,
        target_1=price * 1.03,
        score=45.0,
        market_regime="RANGE",
        state=SetupState.SETUP_READY,
        features={"exhaustion_magnitude": 0.01, "rsi_14": 70.0},
    )


def _capture(store, candidate, *, clock=None) -> str:
    return capture_with_status(
        store, candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"}, clock=clock,
    )


class _FaultStore(InMemoryInverseShortStore):
    """In-memory store with INJECTED, independently switchable faults.

    * ``fail_stage`` — the next N ``stage_close`` calls raise.
    * ``fail_close_updates`` — the next N ``update_position`` calls for a
      CLOSED position raise (OPEN/funding updates are never failed here).
    * ``fail_update_keys`` — per-position_key countdown of additional
      CLOSED-update failures (lets a test invert the persistence order).
    * ``fail_balance_reads`` — the next N ``balance_components`` calls
      raise (a failed reconciliation must keep every recovery marker).
    """

    def __init__(
        self,
        activation: ActivationRecord | None = None,
        *,
        initial_balance: float = 10_000.0,
        fail_stage: int = 0,
        fail_close_updates: int = 0,
        fail_balance_reads: int = 0,
    ) -> None:
        super().__init__(activation, initial_balance=initial_balance)
        self.fail_stage = int(fail_stage)
        self.fail_close_updates = int(fail_close_updates)
        self.fail_balance_reads = int(fail_balance_reads)
        self.fail_update_keys: dict[str, int] = {}
        self.stage_attempts = 0
        self.close_update_attempts = 0
        self.balance_reads = 0
        # Successful CLOSED-update order (proves out-of-order retries).
        self.closed_update_success_order: list[str] = []

    def stage_close(self, position: InverseShortPosition) -> bool:
        self.stage_attempts += 1
        if self.fail_stage > 0:
            self.fail_stage -= 1
            raise RuntimeError("INJECTED_STAGE_FAILURE")
        return super().stage_close(position)

    def update_position(self, position: InverseShortPosition) -> None:
        if position.status == POSITION_STATUS_CLOSED:
            self.close_update_attempts += 1
            remaining = self.fail_update_keys.get(position.position_key, 0)
            if remaining > 0:
                self.fail_update_keys[position.position_key] = remaining - 1
                raise RuntimeError("INJECTED_CLOSE_UPDATE_FAILURE")
            if self.fail_close_updates > 0:
                self.fail_close_updates -= 1
                raise RuntimeError("INJECTED_CLOSE_UPDATE_FAILURE")
            self.closed_update_success_order.append(position.position_key)
        return super().update_position(position)

    def balance_components(self) -> dict[str, float]:
        self.balance_reads += 1
        if self.fail_balance_reads > 0:
            self.fail_balance_reads -= 1
            raise RuntimeError("INJECTED_BALANCE_READ_FAILURE")
        return super().balance_components()


def _faulty(
    *, fail_stage: int = 0, fail_close_updates: int = 0,
    fail_balance_reads: int = 0,
) -> _FaultStore:
    return _FaultStore(
        _activation(), initial_balance=_settings().initial_balance,
        fail_stage=fail_stage, fail_close_updates=fail_close_updates,
        fail_balance_reads=fail_balance_reads,
    )


def _open_position(
    store: InMemoryInverseShortStore,
    *,
    clock: _Clock | None = None,
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    settings: Settings | None = None,
):
    """Capture one source event and open its SHORT position at `price`."""
    settings = settings or _settings()
    clock = clock or _Clock(SIGNAL_AT)
    contour = InverseShortContour(
        settings, store, clock=clock, activation_enabled=True,
    )
    assert _capture(store, _candidate(symbol, price, detected_at=clock()),
                    clock=clock) == CAPTURED
    cycle = contour.run_cycle({symbol: price})
    assert cycle["entries"] == 1, cycle
    position = next(iter(contour.engine.open_positions.values()))
    return contour, clock, position


def _open_pair(store, *, clock: _Clock | None = None, price: float = 100.0,
               settings: Settings | None = None):
    """Open BTCUSDT first and ETHUSDT second on ONE contour.

    The BTC signal is captured 5 minutes earlier, so the close order in
    ``check_exits`` is deterministic: BTCUSDT, then ETHUSDT.
    """
    settings = settings or _settings()
    clock = clock or _Clock(SIGNAL_AT)
    contour = InverseShortContour(
        settings, store, clock=clock, activation_enabled=True,
    )
    assert _capture(store, _candidate("BTCUSDT", price, detected_at=clock()),
                    clock=clock) == CAPTURED
    clock.advance(minutes=5)
    assert _capture(store, _candidate("ETHUSDT", price, detected_at=clock()),
                    clock=clock) == CAPTURED
    cycle = contour.run_cycle({"BTCUSDT": price, "ETHUSDT": price})
    assert cycle["entries"] == 2, cycle
    positions = {p.symbol: p for p in contour.engine.open_positions.values()}
    assert set(positions) == {"BTCUSDT", "ETHUSDT"}
    return contour, clock, positions


# Exact-terminal-facts comparison (byte-for-byte equality, no approx).
_FACT_FIELDS = (
    "position_key", "symbol", "direction", "entry_market_price",
    "entry_price", "entry_fee", "stop_price", "target_price",
    "position_size", "entered_at", "deadline_at", "status",
    "funding_paid", "funding_periods", "exit_price", "exit_reason",
    "exit_fee", "slippage", "gross_pnl", "net_pnl", "closed_at",
    "duration_sec",
)


def _assert_same_facts(actual: InverseShortPosition,
                       expected: InverseShortPosition) -> None:
    got, want = actual.to_dict(), expected.to_dict()
    for field in _FACT_FIELDS:
        assert got[field] == want[field], (
            f"terminal fact {field} changed: {got[field]!r} != {want[field]!r}"
        )


# ======================================================================
# P1 (1) — stage fails + UPDATE fails → stage retry → recovery
# ======================================================================

class TestP1StageRetryThenRecovery:
    def test_stage_fail_and_update_fail_then_stage_retry_recovers(self):
        settings = _settings()
        store = _faulty(fail_stage=1, fail_close_updates=2)
        contour, clock, position = _open_position(store, settings=settings)
        key = position.position_key

        clock.advance(minutes=MAX_HOLD_MINUTES)
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["exits"] == 1 and cycle["pending_closes"] == 1
        facts = dataclasses.replace(contour.engine.pending_closes[key])

        # BOTH writes failed: nothing durable anywhere, risk reported.
        assert store.stage_attempts == 1
        assert store.close_update_attempts == 1
        assert store.list_pending_closes() == []
        assert contour.engine.durability_risk is True
        assert contour.engine.unstaged_close_keys == (key,)
        snap = contour.engine.snapshot()
        assert snap["durability_risk"] is True
        assert snap["unstaged_closes"] == 1

        # Retry: staging is retried FIRST and commits; only then is the
        # terminal UPDATE attempted (it fails once more).
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0})
        assert store.stage_attempts == 2
        assert store.close_update_attempts == 2
        staged = store.list_pending_closes()
        assert len(staged) == 1
        _assert_same_facts(staged[0], facts)   # exact facts are durable now
        assert contour.engine.durability_risk is False
        assert cycle2["pending_closes"] == 1

        # Recovery: the terminal UPDATE commits with the SAME facts.
        cycle3 = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle3["pending_closes"] == 0
        assert store.list_pending_closes() == []
        closed = store.list_positions(status=POSITION_STATUS_CLOSED)
        assert len(closed) == 1
        _assert_same_facts(closed[0], facts)
        assert contour.engine.pending_closes == {}
        assert contour.engine.durability_risk is False
        assert contour.engine.uncommitted_deltas == {}
        assert contour.engine.balance == pytest.approx(
            store.balance_components()["balance"]
        )
        assert contour.engine.balance == pytest.approx(
            settings.initial_balance + facts.net_pnl
        )

    def test_repeated_stage_failure_never_claims_durable_recovery(self, caplog):
        settings = _settings()
        store = _faulty(fail_stage=10_000, fail_close_updates=10_000)
        contour, clock, position = _open_position(store, settings=settings)
        key = position.position_key

        clock.advance(minutes=MAX_HOLD_MINUTES)
        with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle({"BTCUSDT": 100.0})
            assert cycle["exits"] == 1 and cycle["pending_closes"] == 1
            # A NEW event on an unrelated symbol is ready to trade.
            assert _capture(
                store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
                clock=clock,
            ) == CAPTURED
            for _ in range(3):
                clock.advance(hours=1)
                blocked = contour.run_cycle(
                    {"BTCUSDT": 100.0, "ETHUSDT": 100.0},
                )
                # Entries fail closed while the snapshot is memory-only …
                assert blocked["entries"] == 0
                assert blocked["durability_risk"] is True
                assert blocked["accounting_consistent"] is True

        # … staging is retried every cycle, and the terminal UPDATE is
        # NEVER attempted before staging has committed.
        assert store.stage_attempts == 1 + 3
        assert store.close_update_attempts == 1
        # Nothing durable → recovery is never claimed as guaranteed.
        assert store.list_pending_closes() == []
        assert contour.engine.durability_risk is True
        assert contour.engine.unstaged_close_keys == (key,)
        snap = contour.engine.snapshot()
        assert snap["durability_risk"] is True
        assert snap["unstaged_closes"] == 1
        assert "UNRESOLVED close durability risk" in caplog.text
        assert "Recovery is NOT guaranteed" in caplog.text
        assert "entries refused (fail closed)" in caplog.text

        # Staging finally commits → durable, entries unblocked, close done.
        store.fail_stage = 0
        store.fail_close_updates = 0
        recovered = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert recovered["entries"] == 1
        assert recovered["pending_closes"] == 0
        assert contour.engine.durability_risk is False
        assert contour.engine.pending_closes == {}
        assert len(store.list_positions(status=POSITION_STATUS_CLOSED)) == 1
        assert contour.engine.balance == pytest.approx(
            store.balance_components()["balance"]
        )


# ======================================================================
# P1 (6) — restart → exact terminal facts retained when outbox committed
# ======================================================================

class TestP1RestartRecoversCommittedOutbox:
    def test_restart_retains_exact_terminal_facts_when_outbox_committed(self):
        settings = _settings()
        store = _faulty(fail_stage=1, fail_close_updates=10_000)
        contour, clock, position = _open_position(store, settings=settings)
        key = position.position_key
        entered_at, deadline_at = position.entered_at, position.deadline_at

        clock.advance(minutes=MAX_HOLD_MINUTES)
        assert contour.run_cycle({"BTCUSDT": 100.0})["pending_closes"] == 1
        # Stage failed AND the UPDATE failed → nothing durable yet.
        assert store.list_pending_closes() == []
        original = dataclasses.replace(contour.engine.pending_closes[key])

        # The stage retry commits the outbox (UPDATE still failing).
        assert contour.run_cycle({"BTCUSDT": 100.0})["pending_closes"] == 1
        staged = store.list_pending_closes()
        assert len(staged) == 1
        _assert_same_facts(staged[0], original)

        # ---- RESTART at a DIFFERENT market price: the exact terminal
        # facts come back from the durable outbox, never recomputed.
        store.fail_close_updates = 0
        contour2 = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        engine2 = contour2.engine
        assert key in engine2.pending_closes
        _assert_same_facts(engine2.pending_closes[key], original)
        assert engine2.durability_risk is False      # staged → recovery durable
        assert engine2.balance_recovery_pending == (key,)
        assert engine2.accounting_consistent is False
        assert engine2.balance == pytest.approx(
            store.balance_components()["balance"]
        )

        clock.advance(hours=1)
        cycle = contour2.run_cycle({"BTCUSDT": 137.0})
        assert cycle["exits"] == 0                    # never re-evaluated
        assert cycle["pending_closes"] == 0           # flush persisted it
        final = store.list_positions(status=POSITION_STATUS_CLOSED)[0]
        _assert_same_facts(final, original)           # verbatim, not at 137.0
        assert final.entered_at == entered_at
        assert final.deadline_at == deadline_at
        assert engine2.balance_recovery_pending == ()
        assert engine2.accounting_consistent is True
        assert engine2.balance == pytest.approx(
            settings.initial_balance + final.net_pnl
        )
        assert engine2.balance == pytest.approx(
            store.balance_components()["balance"]
        )
        assert store.net_pnl_summary()["closed_trades"] == 1.0
        assert store.net_pnl_summary()["net_pnl"] == pytest.approx(
            final.net_pnl
        )


# ======================================================================
# P2 — retry failed balance reconciliation ((3) and (8))
# ======================================================================

class TestP2BalanceReconciliationRetry:
    def test_update_succeeds_balance_read_fails_then_later_resync_succeeds(self):
        settings = _settings()
        store = _faulty(fail_close_updates=1)
        contour, clock, position = _open_position(store, settings=settings)
        key = position.position_key

        clock.advance(minutes=MAX_HOLD_MINUTES)
        assert contour.run_cycle({"BTCUSDT": 100.0})["pending_closes"] == 1
        assert len(store.list_pending_closes()) == 1
        facts = dataclasses.replace(contour.engine.pending_closes[key])

        # RESTART: the close resumes from the outbox with a balance-recovery
        # marker (its realized PnL is not in the memory ledger yet).
        store.fail_close_updates = 0
        contour2 = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        engine2 = contour2.engine
        assert key in engine2.pending_closes
        assert engine2.balance_recovery_pending == (key,)
        balance_before_flush = engine2.balance
        # A new event on a DIFFERENT symbol is ready to enter.
        assert _capture(
            store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED

        # UPDATE commits, but the balance read fails: the recovery marker
        # must NOT be discarded and new entries must be refused.
        store.fail_balance_reads = 100
        cycle = contour2.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle["pending_closes"] == 0             # row flipped CLOSED
        assert engine2.balance_recovery_pending == (key,)   # marker retained
        assert engine2.accounting_consistent is False
        assert cycle["accounting_consistent"] is False
        assert cycle["entries"] == 0                    # fail closed
        # Memory ledger untouched by the failed read (no re-calculated close).
        assert engine2.balance == pytest.approx(balance_before_flush)
        assert store.list_pending_closes() == []
        closed = store.list_positions(status=POSITION_STATUS_CLOSED)
        assert len(closed) == 1
        _assert_same_facts(closed[0], facts)            # still exactly once

        # The read recovers → the resync succeeds and unblocks entries.
        store.fail_balance_reads = 0
        cycle2 = contour2.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert engine2.balance_recovery_pending == ()
        assert engine2.accounting_consistent is True
        assert cycle2["accounting_consistent"] is True
        assert cycle2["entries"] == 1
        assert engine2.balance == pytest.approx(
            store.balance_components()["balance"]
        )
        assert engine2.balance == pytest.approx(
            settings.initial_balance + facts.net_pnl
            - next(iter(engine2.open_positions.values())).entry_fee
            - next(iter(engine2.open_positions.values())).entry_slippage_cost
        )

    def test_new_entries_blocked_while_accounting_inconsistent(self):
        settings = _settings()
        store = _faulty()
        contour, clock, position = _open_position(store, settings=settings)
        assert _capture(
            store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED

        # A failed reconciliation marks the contour balance untrustworthy.
        store.fail_balance_reads = 100
        assert contour.engine.reconcile_balance() is False
        assert contour.engine.accounting_consistent is False

        cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle["entries"] == 0                    # fail closed …
        assert cycle["accounting_consistent"] is False  # … even for ETHUSDT
        assert len(contour.engine.open_positions) == 1  # BTC only, no new row

        # The resync succeeds → entries are allowed again.
        store.fail_balance_reads = 0
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle2["accounting_consistent"] is True
        assert cycle2["entries"] == 1
        assert len(contour.engine.open_positions) == 2
        assert contour.engine.balance == pytest.approx(
            store.balance_components()["balance"]
        )


# ======================================================================
# P2 (4) + (5) — multiple simultaneous pending closes
# ======================================================================

class TestP2SimultaneousPendingCloses:
    def test_two_pending_closes_across_symbols_retain_both_pnls(self):
        settings = _settings()
        store = _faulty(fail_close_updates=10_000)
        contour, clock, positions = _open_pair(store, settings=settings)
        btc_key = positions["BTCUSDT"].position_key
        eth_key = positions["ETHUSDT"].position_key

        clock.advance(minutes=MAX_HOLD_MINUTES)
        cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle["exits"] == 2 and cycle["pending_closes"] == 2
        assert set(contour.engine.pending_closes) == {btc_key, eth_key}
        facts = contour.engine.pending_closes
        btc_net = facts[btc_key].net_pnl
        eth_net = facts[eth_key].net_pnl
        assert contour.engine.balance == pytest.approx(
            settings.initial_balance + btc_net + eth_net
        )

        # A ledger resync must NOT overwrite either pending PnL: the
        # confirmed ledger (both rows still OPEN) plus the local uncommitted
        # deltas reproduces the memory ledger exactly.
        assert contour.engine.reconcile_balance() is True
        deltas = contour.engine.uncommitted_deltas
        assert set(deltas) == {btc_key, eth_key}
        assert contour.engine.confirmed_balance + sum(deltas.values()) == (
            pytest.approx(contour.engine.balance)
        )
        assert contour.engine.balance == pytest.approx(
            settings.initial_balance + btc_net + eth_net
        )

        # Both retries succeed → the final balance equals the ledger.
        store.fail_close_updates = 0
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle2["pending_closes"] == 0
        assert contour.engine.uncommitted_deltas == {}
        assert contour.engine.pending_closes == {}
        summary = store.net_pnl_summary()
        assert summary["closed_trades"] == 2.0
        assert summary["net_pnl"] == pytest.approx(btc_net + eth_net)
        assert contour.engine.balance == pytest.approx(
            store.balance_components()["balance"]
        )
        assert contour.engine.balance == pytest.approx(
            settings.initial_balance + btc_net + eth_net
        )

    def test_restart_recovers_two_simultaneous_pending_closes_exactly_once(self):
        settings = _settings()
        store = _faulty(fail_close_updates=10_000)
        contour, clock, positions = _open_pair(store, settings=settings)
        btc_key = positions["BTCUSDT"].position_key
        eth_key = positions["ETHUSDT"].position_key

        clock.advance(minutes=MAX_HOLD_MINUTES)
        assert contour.run_cycle(
            {"BTCUSDT": 100.0, "ETHUSDT": 100.0},
        )["pending_closes"] == 2
        btc_net = contour.engine.pending_closes[btc_key].net_pnl
        eth_net = contour.engine.pending_closes[eth_key].net_pnl
        # Both staged durably (only the UPDATEs failed).
        assert len(store.list_pending_closes()) == 2
        # One more event waits for the recovery to finish.
        assert _capture(
            store, _candidate("SOLUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED

        # ---- RESTART: both closes resume, both markers owed.
        store.fail_close_updates = 0
        contour2 = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        engine2 = contour2.engine
        assert set(engine2.pending_closes) == {btc_key, eth_key}
        assert set(engine2.balance_recovery_pending) == {btc_key, eth_key}
        assert engine2.accounting_consistent is False
        # Balance at restart reflects the still-OPEN rows (no PnL booked yet).
        assert engine2.balance == pytest.approx(
            store.balance_components()["balance"]
        )

        # Both flushes + ONE reconciliation book each PnL exactly once, and
        # the waiting entry is allowed again in the same cycle.
        cycle2 = contour2.run_cycle(
            {"BTCUSDT": 100.0, "ETHUSDT": 100.0, "SOLUSDT": 100.0},
        )
        assert cycle2["pending_closes"] == 0
        assert engine2.balance_recovery_pending == ()
        assert engine2.accounting_consistent is True
        assert cycle2["entries"] == 1                  # SOLUSDT entered
        summary = store.net_pnl_summary()
        assert summary["closed_trades"] == 2.0
        assert summary["net_pnl"] == pytest.approx(btc_net + eth_net)
        assert engine2.balance == pytest.approx(
            store.balance_components()["balance"]
        )
        assert engine2.balance == pytest.approx(
            settings.initial_balance + btc_net + eth_net
            - next(
                p for p in engine2.open_positions.values()
                if p.symbol == "SOLUSDT"
            ).entry_fee
            - next(
                p for p in engine2.open_positions.values()
                if p.symbol == "SOLUSDT"
            ).entry_slippage_cost
        )

    def test_out_of_order_retries_final_balance_equals_persisted_ledger(self):
        settings = _settings()
        store = _faulty(fail_close_updates=2)
        contour, clock, positions = _open_pair(store, settings=settings)
        btc_key = positions["BTCUSDT"].position_key
        eth_key = positions["ETHUSDT"].position_key

        clock.advance(minutes=MAX_HOLD_MINUTES)
        cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle["exits"] == 2
        # Close order: BTCUSDT first, ETHUSDT second (both UPDATEs failed).
        assert [p.position_key for p in cycle["closed"]] == [btc_key, eth_key]
        assert set(contour.engine.pending_closes) == {btc_key, eth_key}
        btc_net = contour.engine.pending_closes[btc_key].net_pnl
        eth_net = contour.engine.pending_closes[eth_key].net_pnl

        # Retry order is INVERTED: ETHUSDT persists first, BTCUSDT fails
        # once more before succeeding.
        store.fail_close_updates = 0
        store.fail_update_keys = {btc_key: 1}
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle2["pending_closes"] == 1
        store.fail_update_keys = {}
        cycle3 = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle3["pending_closes"] == 0

        # Out-of-order persistence proven, and the final balance equals
        # the persisted ledger (no ordering artefact).
        assert store.closed_update_success_order == [eth_key, btc_key]
        summary = store.net_pnl_summary()
        assert summary["closed_trades"] == 2.0
        assert summary["net_pnl"] == pytest.approx(btc_net + eth_net)
        assert contour.engine.uncommitted_deltas == {}
        assert contour.engine.balance == pytest.approx(
            store.balance_components()["balance"]
        )
        assert contour.engine.balance == pytest.approx(
            settings.initial_balance + btc_net + eth_net
        )


# ======================================================================
# P2 (7) — no duplicate funding, exit fee or realized PnL
# ======================================================================

class TestP2NoDuplicateBookings:
    def test_no_duplicate_funding_exit_fee_or_realized_pnl(self):
        settings = _settings()
        store = _faulty(fail_stage=1, fail_close_updates=2)
        contour, clock, position = _open_position(store, settings=settings)
        key = position.position_key

        # One whole funding period first (persisted while OPEN).
        clock.advance(hours=8)
        contour.run_cycle({"BTCUSDT": 100.0}, {"BTCUSDT": 0.01})
        assert position.funding_periods_charged == 1

        # Reach the immutable deadline (7200 minutes = 15 funding periods).
        clock.advance(hours=112)
        cycle = contour.run_cycle({"BTCUSDT": 100.0}, {"BTCUSDT": 0.01})
        assert cycle["exits"] == 1 and cycle["pending_closes"] == 1
        facts = dataclasses.replace(contour.engine.pending_closes[key])
        assert facts.funding_periods_charged == 15
        assert facts.funding_paid != 0.0
        assert facts.exit_fee > 0.0
        assert store.stage_attempts == 1              # staging failed
        assert store.close_update_attempts == 1       # UPDATE failed

        # Stage retry commits; the UPDATE still fails.
        assert contour.run_cycle(
            {"BTCUSDT": 100.0}, {"BTCUSDT": 0.01},
        )["pending_closes"] == 1
        assert len(store.list_pending_closes()) == 1

        # ---- RESTART → replay → persist (8 more hours pass meanwhile).
        store.fail_close_updates = 0
        clock.advance(hours=8)
        contour2 = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        cycle2 = contour2.run_cycle({"BTCUSDT": 100.0}, {"BTCUSDT": 0.01})
        assert cycle2["exits"] == 0
        assert cycle2["pending_closes"] == 0

        final = store.list_positions(status=POSITION_STATUS_CLOSED)[0]
        # Funding charged exactly once (15 periods, not 16 or 17).
        assert final.funding_periods_charged == 15
        assert final.to_dict()["funding_periods"] == 15
        assert final.funding_paid == facts.funding_paid
        # Exit fee and realized PnL booked exactly once.
        assert final.exit_fee == facts.exit_fee
        assert final.net_pnl == facts.net_pnl
        _assert_same_facts(final, facts)
        summary = store.net_pnl_summary()
        assert summary["closed_trades"] == 1.0
        assert summary["net_pnl"] == pytest.approx(facts.net_pnl)

        # Memory ledger == persisted ledger; reconciling again changes nothing.
        engine2 = contour2.engine
        assert engine2.balance == pytest.approx(
            store.balance_components()["balance"]
        )
        assert engine2.balance == pytest.approx(
            settings.initial_balance + facts.net_pnl
        )
        balance = engine2.balance
        assert engine2.reconcile_balance() is True
        assert engine2.balance == balance              # idempotent, no double book


# ======================================================================
# Migration compatibility — degraded outbox (086 absent) still persists
# ======================================================================

class TestDegradedOutboxRollout:
    def test_stage_false_still_attempts_update_and_clears_risk_on_commit(self):
        class _DegradedStore(_FaultStore):
            """Outbox table absent (migration 086): stage_close → False."""

            def stage_close(self, position: InverseShortPosition) -> bool:
                self.stage_attempts += 1
                return False

        settings = _settings()
        store = _DegradedStore(
            _activation(), initial_balance=settings.initial_balance,
            fail_close_updates=1,
        )
        contour, clock, position = _open_position(store, settings=settings)
        key = position.position_key

        clock.advance(minutes=MAX_HOLD_MINUTES)
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["exits"] == 1 and cycle["pending_closes"] == 1
        # Staging was attempted once (returned False) and the UPDATE was
        # still attempted right after — but it failed, so the snapshot is
        # memory-only: the durability risk is reported (fail closed).
        assert store.stage_attempts == 1
        assert store.close_update_attempts == 1
        assert contour.engine.durability_risk is True
        assert contour.engine.unstaged_close_keys == (key,)

        # Next cycle: staging is NOT retried (it can never succeed without
        # the table) and the terminal UPDATE is retried → committed.
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0})
        assert store.stage_attempts == 1
        assert store.close_update_attempts == 2
        assert cycle2["pending_closes"] == 0
        assert contour.engine.durability_risk is False
        assert len(store.list_positions(status=POSITION_STATUS_CLOSED)) == 1
        assert contour.engine.balance == pytest.approx(
            store.balance_components()["balance"]
        )


# ======================================================================
# Frozen protocol guards (must survive every review fix unchanged)
# ======================================================================

class TestFrozenProtocolPreserved:
    def test_protocol_fingerprint_and_geometry_are_unchanged(self):
        from app.paper.me_long_v2_inverse_short_v1.protocol import (
            STOP_LOSS_PCT,
            TAKE_PROFIT_PCT,
            describe_protocol,
        )

        assert protocol_fingerprint() == PINNED_PROTOCOL_FINGERPRINT
        assert STOP_LOSS_PCT == 0.10 and TAKE_PROFIT_PCT == 0.10
        assert MAX_HOLD_MINUTES == 7200
        described = describe_protocol()
        assert "7200" in str(described) or MAX_HOLD_MINUTES == 7200
