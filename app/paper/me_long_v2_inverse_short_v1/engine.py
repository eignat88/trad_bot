"""Isolated SHORT paper engine for ME_LONG_V2_INVERSE_SHORT_V1.

Design constraints (all enforced, all testable):

* **Isolation** — the contour keeps its own ``open_positions`` map keyed by
  the NAMESPACED ``position_key`` (``<strategy>:<event_key>``).  It never
  touches the production ``PaperTradingEngine.open_trades[symbol]`` map, the
  production balance, or any production risk gate.  Several contour positions
  on the SAME symbol are allowed (different source events); the production
  engine still allows only one trade per symbol — and vice versa.
* **Frozen geometry** — TP and SL are exactly 10% of the ACTUAL entry price
  (entry fill includes configured slippage); timeout is exactly
  ``entered_at + 7200 minutes`` computed ONCE at entry.
* **Immutability** — ``entered_at`` and ``deadline_at`` are written at entry
  and never recomputed: restart recovery restores them verbatim from the
  store (the store refuses to update them).
* **Realistic execution model** — taker fees on both sides, configured
  adverse slippage on both fills, funding settled per whole funding interval
  with the SHORT sign convention.
* **Fail-closed entries, fail-safe exits** — entries require a valid
  activation identity; exits (STOP / TP / TIMEOUT) always run so an open
  position can never be stranded.
* **Durability-first closes (P1)** — the exact terminal snapshot is staged
  into the durable outbox BEFORE the status flip; when staging fails it is
  retried BEFORE the terminal UPDATE on every later cycle, the unresolved
  risk is reported each cycle and new entries fail closed.  Recovery is
  only ever claimed for snapshots PostgreSQL actually accepted.
* **Deterministic accounting reconciliation (P2)** — the memory ledger is
  always ``confirmed store balance + Σ per-position uncommitted deltas``:
  a resync never overwrites realized PnL owned by other pending closes and
  never books funding, exit fees or realized PnL twice; entries are
  refused while the contour balance is not trustworthy.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from app.config import Settings

from .activation import (
    PERMISSION_NEW_ENTRIES,
    ActivationIdentity,
    is_post_freeze,
)
from .identity import position_key as build_position_key
from .models import (
    EVENT_STATUS_CAPTURED,
    InverseShortEvent,
    InverseShortPosition,
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from .protocol import (
    EXIT_REASONS,
    TRADE_DIRECTION,
    deadline_for,
    short_geometry,
)
from .store import InverseShortStore

logger = logging.getLogger(__name__)

# Allowed exit reasons of this contour (CHECK constraint, migration 085).
CONTOUR_EXIT_REASONS = frozenset(EXIT_REASONS)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InverseShortPaperEngine:
    """Independent prospective SHORT paper engine (paper-only)."""

    def __init__(
        self,
        settings: Settings,
        store: InverseShortStore,
        *,
        clock: Callable[[], datetime] | None = None,
        activation: ActivationIdentity | None = None,
        require_activation: bool = True,
    ) -> None:
        self.settings = settings
        self.store = store
        self._clock = clock or _utc_now
        self.activation = activation
        self.require_activation = require_activation

        # ISOLATION: never shared with PaperTradingEngine.
        self.open_positions: dict[str, InverseShortPosition] = {}
        # Durable close protocol (P1-3): terminal snapshots whose status flip
        # is not confirmed yet.  Retried with IDENTICAL facts, never
        # re-evaluated by STOP/TP/TIMEOUT; a symbol with an unfinished close
        # refuses new entries until the close is persisted.  MULTIPLE
        # simultaneous pending closes are fully supported (one exact
        # snapshot per position_key).
        self.pending_closes: dict[str, InverseShortPosition] = {}
        # Balance-recovery markers (P2): keys resumed from the durable outbox
        # whose realized PnL is not in the memory ledger yet.  A marker is
        # discarded ONLY after a balance reconciliation succeeds — a failed
        # ``balance_components()`` read keeps the marker and retries on the
        # next cycle, and new entries stay refused meanwhile.
        self._pending_from_store: set[str] = set()
        # Unresolved close durability risk (P1): terminal snapshots that are
        # NOT durable anywhere yet (outbox staging failed and the row is
        # still OPEN).  While non-empty the engine fails closed — no new
        # entries — and reports the risk every cycle; recovery is never
        # claimed for a snapshot PostgreSQL never accepted.
        self._unstaged_closes: set[str] = set()
        # Outbox table absent (migration 086 not applied): staging can never
        # succeed for these keys, so staging retries are skipped and the
        # terminal row UPDATE is the only remaining durability (documented
        # degraded rollout mode).  The risk stays reported until the row
        # flip commits.
        self._staging_unavailable: set[str] = set()
        # Deterministic accounting reconciliation (P2): the CONFIRMED ledger
        # balance (last successful store read) is kept strictly separate
        # from the LOCAL UNCOMMITTED deltas — per-position accounting booked
        # in memory but not yet committed to the store ledger.
        #   balance == confirmed_balance + Σ uncommitted deltas
        # so a resync can never overwrite realized PnL that belongs to other
        # pending positions and never books any PnL twice.
        self._uncommitted_deltas: dict[str, float] = {}
        self._balance_trusted: bool = True
        # Funding rate UNKNOWN (review P2-1): keys with due-but-unsettled
        # intervals while no rate was observed.  Tracked only for honest
        # one-shot warning logs; the durable unsettled state itself lives on
        # the position row (funding_periods_charged behind due_periods).
        self._funding_unknown: set[str] = set()
        # DIRTY funding snapshots (review P2): an update_position() that
        # failed after a funding accrual leaves the EXACT snapshot (funding_paid
        # and funding_periods_charged frozen at failure time) in memory, keyed
        # by position.  Every subsequent cycle retries writing it — even before
        # another funding interval becomes due — and a retry NEVER re-accrues.
        # In-memory only: a restart restores strictly the confirmed store rows
        # and never claims durability for a write the database never accepted.
        self._dirty_funding: dict[str, InverseShortPosition] = {}

        components = store.balance_components()
        self.confirmed_balance = float(
            components.get("balance", settings.initial_balance)
        )
        self.balance = self.confirmed_balance
        self.initial_balance = float(
            components.get("initial_balance", settings.initial_balance)
        )
        self._load_open_positions()

    # ------------------------------------------------------------------
    # Restart recovery
    # ------------------------------------------------------------------
    def _load_open_positions(self) -> None:
        """Restore OPEN positions; ``entered_at``/``deadline_at`` verbatim.

        Durable unfinished closes are resumed FIRST: their terminal snapshot
        is exact, so those keys are excluded from the open-position algorithm
        below — a recovered close is replayed as-is, never recomputed by
        STOP/TP/TIMEOUT.
        """
        for snapshot in self.store.list_pending_closes():
            if (
                snapshot.direction != TRADE_DIRECTION
                or snapshot.status != POSITION_STATUS_CLOSED
                or snapshot.exit_reason not in CONTOUR_EXIT_REASONS
                or snapshot.closed_at is None
                or snapshot.deadline_at <= snapshot.entered_at
            ):
                logger.error(
                    "me inverse short recovery refused invalid pending close: key=%s",
                    str(snapshot.position_key)[:48],
                )
                continue
            self.pending_closes[snapshot.position_key] = snapshot
            self._pending_from_store.add(snapshot.position_key)
        if self.pending_closes:
            logger.warning(
                "me inverse short recovery resumed %d unfinished close(s); "
                "terminal facts replayed byte-for-byte, never recomputed",
                len(self.pending_closes),
            )

        restored = self.store.load_open_positions()
        for position in restored:
            if position.position_key in self.pending_closes:
                logger.warning(
                    "me inverse short recovery: key=%s kept out of the exit "
                    "algorithm (close persistence pending)",
                    position.position_key[:48],
                )
                continue
            if position.direction != TRADE_DIRECTION:
                logger.error(
                    "me inverse short recovery refused non-SHORT position: key=%s direction=%s",
                    position.position_key, position.direction,
                )
                continue
            if position.position_key in self.open_positions:
                logger.error(
                    "me inverse short recovery duplicate position_key=%s (first wins)",
                    position.position_key,
                )
                continue
            if position.deadline_at <= position.entered_at:
                logger.error(
                    "me inverse short recovery refused invalid deadline: key=%s "
                    "entered_at=%s deadline_at=%s",
                    position.position_key,
                    position.entered_at.isoformat(),
                    position.deadline_at.isoformat(),
                )
                continue
            self.open_positions[position.position_key] = position
        if restored:
            logger.info(
                "me inverse short engine recovered %d open position(s); balance=$%.2f",
                len(self.open_positions), self.balance,
            )

    # ------------------------------------------------------------------
    # Entries (prospective-only, activation-gated)
    # ------------------------------------------------------------------
    @property
    def activated(self) -> bool:
        return (self.activation is not None) or (not self.require_activation)

    # -- accounting / durability state (P1 + P2) -------------------------
    @property
    def unstaged_close_keys(self) -> tuple[str, ...]:
        """Terminal snapshots that are not durable anywhere yet."""
        return tuple(sorted(self._unstaged_closes))

    @property
    def durability_risk(self) -> bool:
        """Unresolved durability risk: a retained terminal snapshot exists
        only in process memory (staging never committed and the status
        UPDATE has not committed either).  While True the engine fails
        closed and reports the risk — recovery is NOT guaranteed if the
        process terminates now."""
        return bool(self._unstaged_closes)

    @property
    def uncommitted_deltas(self) -> dict[str, float]:
        """Local uncommitted accounting deltas, per position_key."""
        return dict(self._uncommitted_deltas)

    @property
    def accounting_consistent(self) -> bool:
        """The contour balance is trustworthy (P2).

        False while a balance-recovery marker is owed (realized PnL of a
        recovered close not yet reconciled into the memory ledger) or after
        a failed reconciliation — new entries are refused meanwhile.
        """
        return self._balance_trusted and not self._pending_from_store

    @property
    def balance_recovery_pending(self) -> tuple[str, ...]:
        """Balance-recovery markers still owed (never dropped on a failed read)."""
        return tuple(sorted(self._pending_from_store))

    @property
    def exit_only(self) -> bool:
        """True while the activation identity is EXIT_ONLY (DRAINING).

        MANAGE_EXISTING only: exits, funding reconciliation, outbox replay
        and pending-close recovery continue; NEW_ENTRIES (new capture, new
        entries, reuse of CAPTURED events) is refused.
        """
        return self.activation is not None and self.activation.exit_only

    def open_entries(self, prices: dict[str, float]) -> list[InverseShortPosition]:
        """Open SHORT positions for CAPTURED events at the first observed price."""
        opened: list[InverseShortPosition] = []
        if self.require_activation and self.activation is None:
            logger.warning(
                "me inverse short entries refused: contour is not activated "
                "(protocol fingerprint / freeze_ts missing)"
            )
            return opened
        if self.activation is not None and not self.activation.allows(
            PERMISSION_NEW_ENTRIES,
        ):
            # EXIT_ONLY (DRAINING): CAPTURED events are never reused to open
            # new trades — MANAGE_EXISTING only (review P2-2).  NOTE: no
            # pending-entry event query happens in this mode at all (P1).
            logger.info(
                "me inverse short entries refused (EXIT_ONLY): activation is "
                "DRAINING — managing existing positions only, no new entries",
            )
            return opened

        # Fail-closed gates (P1 + P2): never trade on an unresolved close
        # durability risk or on a balance that is not yet trustworthy.
        # An owed balance recovery is retried here first (a successful
        # resync unblocks entries within the same cycle).
        self._retry_balance_recovery()
        if self._unstaged_closes:
            logger.error(
                "me inverse short entries refused (fail closed): unresolved "
                "close durability risk — %d terminal snapshot(s) not durably "
                "staged; recovery is NOT guaranteed if the process terminates "
                "before a durable write succeeds: keys=%s",
                len(self._unstaged_closes), self.unstaged_close_keys,
            )
            return opened
        if not self.accounting_consistent:
            logger.warning(
                "me inverse short entries refused (fail closed): contour "
                "balance is not trustworthy (balance recovery pending or last "
                "reconciliation failed)"
            )
            return opened
        if self._dirty_funding:
            # Fail-closed (review P2): until every retained funding write is
            # actually confirmed by PostgreSQL, memory and the persisted
            # ledger may still differ — no new entries until consistency is
            # restored (the dirty retry runs before this gate each cycle).
            logger.warning(
                "me inverse short entries refused (fail closed): %d funding "
                "write(s) not yet confirmed by PostgreSQL — waiting for "
                "accounting consistency to be restored: keys=%s",
                len(self._dirty_funding),
                ", ".join(sorted(self._dirty_funding))[:200],
            )
            return opened

        max_positions = int(self.settings.max_open_positions)
        if len(self.open_positions) >= max_positions:
            return opened

        # Review P1: a pending-events/store failure must NEVER propagate out
        # of open_entries — entries simply fail closed for this cycle while
        # the protective exits (already executed before this method in
        # run_cycle) stay fully functional.
        try:
            events = self.store.list_events(status=EVENT_STATUS_CAPTURED)
            for event in events:
                if len(self.open_positions) >= max_positions:
                    logger.info(
                        "me inverse short entry cap reached (%d)", max_positions,
                    )
                    break
                position = self._open_event(event, prices)
                if position is not None:
                    opened.append(position)
        except Exception:
            logger.error(
                "me inverse short entries refused (fail closed): prospective "
                "entry events unavailable (store read/write failed) — no "
                "entry attempted this cycle; protective exits were evaluated "
                "before this point and are unaffected",
                exc_info=True,
            )
        return opened

    def _open_event(
        self,
        event: InverseShortEvent,
        prices: dict[str, float],
    ) -> InverseShortPosition | None:
        if event.direction != TRADE_DIRECTION:
            logger.error(
                "me inverse short entry refused non-SHORT event: key=%s direction=%s",
                event.event_key[:16], event.direction,
            )
            return None

        position_key = build_position_key(event.event_key)
        if position_key in self.open_positions:
            return None  # duplicate delivery of the same source event
        for pending in self.pending_closes.values():
            if pending.symbol == event.symbol:
                # Close-persistence guard: while the previous position of this
                # symbol has an unfinished close, no new position may open.
                logger.warning(
                    "me inverse short entry refused: unfinished close for "
                    "symbol=%s (event=%s)",
                    event.symbol, event.event_key[:16],
                )
                return None
        if self.store.has_position(position_key):
            # Ambiguous COMMIT (review P1): the row EXISTS but this process
            # never opened it in memory — an INSERT PostgreSQL COMMITTED
            # while the acknowledgement was lost, or a duplicate delivery of
            # an already-committed entry.  Never treat this as an automatic
            # duplicate: verify and ADOPT the stored row instead (original
            # entry facts verbatim, no second INSERT).
            return self._adopt_stored_position(event, position_key)

        if self.require_activation and self.activation is not None:
            if not is_post_freeze(event.signal_time, self.activation.freeze_ts):
                logger.warning(
                    "me inverse short entry refused pre-freeze event: key=%s signal_time=%s",
                    event.event_key[:16], event.signal_time.isoformat(),
                )
                return None

        market_price = prices.get(event.symbol)
        if market_price is None or market_price <= 0:
            # Missing market data: skip entry, never guess a fill.
            return None

        position = self._build_position(event, position_key, float(market_price))
        if position is None:
            return None

        if not self.store.save_position(position):
            # Ambiguous COMMIT (review P1): ``ON CONFLICT DO NOTHING``
            # returned rowcount=0 — the FIRST attempt may already be
            # COMMITTED with a lost acknowledgement.  Never treat this
            # result as an automatic duplicate: read the stored row back
            # and verify it instead.
            adopted = self._adopt_stored_position(event, position_key)
            if adopted is not None:
                return adopted
            logger.info(
                "me inverse short duplicate position suppressed: key=%s symbol=%s",
                position_key[:48], position.symbol,
            )
            return None

        try:
            self.store.mark_event_entered(event.event_key, position_key)
        except Exception:
            # Fail-open: has_position() keeps delivery idempotent next cycle.
            logger.warning(
                "me inverse short event mark-entered failed (idempotent guard active)",
                exc_info=True,
            )

        self.balance -= position.entry_fee + position.entry_slippage_cost
        self.open_positions[position_key] = position
        logger.info(
            "me inverse short entry: symbol=%s side=SHORT entry=%.6f stop=%.6f "
            "target=%.6f size=%.6f deadline=%s event_key=%s",
            position.symbol, position.entry_price, position.stop_price,
            position.target_price, position.position_size,
            position.deadline_at.isoformat(), event.event_key[:16],
        )
        return position

    def _entry_facts_match(
        self, event: InverseShortEvent, stored: InverseShortPosition,
    ) -> bool:
        """Verify a committed row corresponds to THIS source event (P1).

        The identity fields must match the source event exactly, and the
        stored entry facts must be structurally the contour's own ORIGINAL
        ones: valid SHORT geometry derived from the stored entry price and
        the immutable ``deadline = entered_at + MAX_HOLD``.  Time- and
        price-dependent fields are never re-derived here — the committed
        row is authoritative.
        """
        if stored.event_key != event.event_key:
            return False
        if stored.direction != event.direction:
            return False
        if stored.direction != TRADE_DIRECTION:
            return False
        if stored.symbol != event.symbol:
            return False
        if stored.source_scanner != event.source_scanner:
            return False
        if stored.source_setup_id != event.source_setup_id:
            return False
        if not (stored.entry_price and stored.entry_price > 0):
            return False
        if not (stored.position_size and stored.position_size > 0):
            return False
        if stored.entered_at is None or stored.deadline_at is None:
            return False
        if stored.deadline_at != deadline_for(stored.entered_at):
            return False
        stop, target = short_geometry(stored.entry_price)
        if stored.stop_price != stop or stored.target_price != target:
            return False
        return True

    def _adopt_stored_position(
        self, event: InverseShortEvent, position_key: str,
    ) -> InverseShortPosition | None:
        """Adopt an already-COMMITTED position row (review P1).

        Reached when the store reports the row but this process never
        opened it in memory — the signature of an ambiguous COMMIT: an
        INSERT PostgreSQL applied while the acknowledgement was lost, or a
        duplicate delivery of an already-committed entry.

        The STORED row is the committed truth and is adopted VERBATIM:
        original ``entered_at`` / ``deadline_at`` / entry price / STOP / TP
        are never recomputed, entry fees are booked into the in-memory
        ledger exactly ONCE (they were never booked for this key), no
        second INSERT is issued, and the position enters ``open_positions``
        immediately — so protective STOP/TP/TIMEOUT applies from the very
        next cycle.  Every failure path stays fail-closed: nothing is
        opened, booked or marked unless the committed row fully verifies.
        """
        if position_key in self.open_positions:
            # Already adopted in this process — never book the fees twice.
            return self.open_positions[position_key]
        try:
            stored = self.store.get_position(position_key)
        except Exception:
            logger.error(
                "me inverse short adoption read failed (entry stays "
                "fail-closed: nothing opened, nothing re-booked)",
                exc_info=True,
            )
            return None
        if stored is None:
            return None  # row genuinely absent: nothing to adopt
        if stored.status != POSITION_STATUS_OPEN:
            logger.info(
                "me inverse short duplicate position suppressed (already "
                "closed): key=%s symbol=%s status=%s",
                position_key[:48], stored.symbol, stored.status,
            )
            return None
        if not self._entry_facts_match(event, stored):
            logger.error(
                "me inverse short adoption REFUSED: committed row does not "
                "correspond to the source event (fail closed, nothing "
                "opened): key=%s event_key=%s direction=%s symbol=%s",
                position_key[:48], stored.event_key[:16], stored.direction,
                stored.symbol,
            )
            return None
        try:
            self.store.mark_event_entered(event.event_key, position_key)
        except Exception:
            # Fail-open: has_position() keeps delivery idempotent next cycle.
            logger.warning(
                "me inverse short event mark-entered failed after adoption "
                "(idempotent guard active)",
                exc_info=True,
            )
        # Entry fees were NEVER booked in memory for this key (the original
        # open failed before the booking): book them exactly once from the
        # COMMITTED facts.  The persisted ledger already carries these
        # costs, so memory and store agree immediately — no double booking.
        self.balance -= stored.entry_fee + stored.entry_slippage_cost
        self.open_positions[position_key] = stored
        logger.warning(
            "me inverse short ADOPTED committed position after an ambiguous "
            "INSERT (no re-open, original entry facts kept, fees booked "
            "once): key=%s symbol=%s entered_at=%s stop=%.6f target=%.6f",
            position_key[:48], stored.symbol,
            stored.entered_at.isoformat() if stored.entered_at else "?",
            stored.stop_price, stored.target_price,
        )
        return stored

    def _build_position(
        self,
        event: InverseShortEvent,
        position_key: str,
        market_price: float,
    ) -> InverseShortPosition | None:
        slip = float(self.settings.slippage_percent)
        entry_market = market_price
        entry_price = entry_market * (1.0 - slip)  # SHORT sell: adverse = lower
        stop_price, target_price = short_geometry(entry_price)
        distance = stop_price - entry_price  # = STOP_LOSS_PCT * entry_price

        balance = self.balance
        risk_budget = balance * float(self.settings.risk_per_trade)
        qty = risk_budget / distance
        max_qty_by_exposure = (
            balance * float(self.settings.max_symbol_exposure) / entry_price
        )
        qty = min(qty, max_qty_by_exposure)
        if qty <= 0 or distance <= 0:
            logger.warning(
                "me inverse short entry sizing failed: symbol=%s balance=%.2f",
                event.symbol, balance,
            )
            return None

        entry_fee = entry_price * qty * float(self.settings.taker_fee)
        entry_slippage_cost = (entry_market - entry_price) * qty
        if balance - entry_fee - entry_slippage_cost <= 0:
            logger.warning(
                "me inverse short entry rejected: costs exceed balance: symbol=%s",
                event.symbol,
            )
            return None

        entered_at = self._clock()
        deadline_at = deadline_for(entered_at)  # immutable, computed ONCE

        return InverseShortPosition(
            position_key=position_key,
            event_key=event.event_key,
            symbol=event.symbol,
            direction=TRADE_DIRECTION,
            entered_at=entered_at,
            deadline_at=deadline_at,
            entry_market_price=entry_market,
            entry_price=entry_price,
            entry_fee=entry_fee,
            entry_slippage_cost=entry_slippage_cost,
            stop_price=stop_price,
            target_price=target_price,
            position_size=qty,
            risk_usdt=distance * qty,
            balance_before=balance,
            source_scanner=event.source_scanner,
            source_setup_id=event.source_setup_id,
            market_regime=event.market_regime,
            source_score=event.source_score,
        )

    # ------------------------------------------------------------------
    # Exits: STOP → TP → TIMEOUT (STOP_FIRST on a shared bar)
    # ------------------------------------------------------------------
    def check_exits(
        self,
        prices: dict[str, float],
        funding_rates_percent: dict[str, float | None] | None = None,
        bars: dict[str, dict[str, float]] | None = None,
    ) -> list[InverseShortPosition]:
        """Evaluate every open position; return positions closed this cycle.

        Funding (review P2-1): a MISSING rate is UNKNOWN, not a confirmed
        zero — the per-symbol mapping simply has no entry (or an explicit
        ``None``) for it and ``_apply_funding`` leaves those intervals
        unsettled.  An UNKNOWN rate never blocks STOP/TP/TIMEOUT.
        """
        # First finish any unfinished close (P1-3): its terminal snapshot is
        # replayed byte-for-byte and never re-evaluated below.
        self.flush_pending_closes()
        # Then retry any DIRTY funding snapshot (P2): persisted even when no
        # new funding interval has become due; a retry never re-accrues.
        self._retry_dirty_funding()

        closed: list[InverseShortPosition] = []

        for position_key, position in list(self.open_positions.items()):
            price = prices.get(position.symbol)
            if price is None or price <= 0:
                # Missing market data: hold, never guess an exit fill.
                continue

            funding_rate = (funding_rates_percent or {}).get(position.symbol)
            self._apply_funding(position, funding_rate)

            price = float(price)
            position.highest_since_entry = max(position.highest_since_entry, price)
            position.lowest_since_entry = min(position.lowest_since_entry, price)
            # MFE/MAE measured on the market mid (the gross-PnL reference).
            position.mfe = max(
                position.mfe, position.entry_market_price - position.lowest_since_entry,
            )
            position.mae = max(
                position.mae, position.highest_since_entry - position.entry_market_price,
            )

            bar = (bars or {}).get(position.symbol) or {}
            high = float(bar.get("high", price))
            low = float(bar.get("low", price))

            stop_touched = high >= position.stop_price
            tp_touched = low <= position.target_price

            if stop_touched:
                # STOP_FIRST: a protective stop always wins over TP/TIMEOUT.
                gap = price > position.stop_price
                reason = "STOP_LOSS_GAP" if gap else "STOP_LOSS"
                result = self._close(position, position.stop_price, reason)
            elif tp_touched:
                result = self._close(position, position.target_price, "TAKE_PROFIT_1")
            elif self._clock() >= position.deadline_at:
                result = self._close(position, price, "TIMEOUT")
            else:
                continue

            self.open_positions.pop(position_key, None)
            closed.append(result)

        return closed

    def _apply_funding(
        self,
        position: InverseShortPosition,
        funding_rate_percent: float | None,
    ) -> None:
        """Settle whole elapsed funding intervals (SHORT sign convention).

        Funding rate is a strict TRI-STATE (review P2-1):

          * ``None`` == **UNKNOWN** — no rate was observed for this symbol.
            NO period is advanced, NO accrual happens and NO zero is
            fabricated: the ``due - charged`` gap stays recorded on the
            position row (``funding_periods_charged`` behind ``due``) and
            survives restart until an actual rate is observed.
          * ``0.0``   == a **confirmed zero** — the due periods settle as
            an explicit zero exactly once (counter advances, balance is
            unchanged).
          * a finite number == an **observed rate** — the whole unsettled
            backlog (``due - charged``) settles at that observed rate
            EXACTLY ONCE (the counter advances atomically with the
            accrual, so replay/restart can never double-book).

        Settlement policy (EXPLICITLY FIXED — review P2-1 #4):
          * a rate is only ever used for settlement when the feed actually
            delivered it for the symbol; while the feed is down nothing is
            booked and the obligation stays open (never a confirmed zero);
          * a missing HISTORICAL rate is never silently invented or
            fabricated: settlement happens only on the first rate actually
            observed after recovery, for the entire backlog, once, and the
            settlement log records rate + backlog;
          * non-finite feed values (NaN/inf) are treated as UNKNOWN;
          * a position that CLOSES during an outage keeps its exit facts
            frozen: the unsettled gap (``due(closed_at) - charged``) stays
            recorded on the closed row and is reported as an outstanding
            obligation — it is NEVER retroactively booked (that would
            rewrite realized PnL, review P2-1 #7).
        """
        interval_seconds = float(self.settings.paper_funding_interval_hours) * 3600
        if interval_seconds <= 0:
            return
        elapsed = (self._clock() - position.entered_at).total_seconds()
        if elapsed <= 0:
            return
        due_periods = max(0, int(elapsed // interval_seconds))
        new_periods = due_periods - position.funding_periods_charged
        if new_periods <= 0:
            self._funding_unknown.discard(position.position_key)
            return

        # --- rate tri-state: UNKNOWN never advances the counter ------------
        rate: float | None
        try:
            rate = float(funding_rate_percent) if funding_rate_percent is not None else None
        except (TypeError, ValueError):
            rate = None
        if rate is not None and not math.isfinite(rate):
            rate = None  # unusable feed value == UNKNOWN, never booked
        if rate is None:
            if position.position_key not in self._funding_unknown:
                self._funding_unknown.add(position.position_key)
                logger.warning(
                    "me inverse short funding rate UNKNOWN; %d period(s) left "
                    "unsettled (obligation preserved, NOTHING booked, no zero "
                    "fabricated): symbol=%s due=%d charged=%d",
                    new_periods, position.symbol, due_periods,
                    position.funding_periods_charged,
                )
            else:
                logger.debug(
                    "me inverse short funding still UNKNOWN: symbol=%s "
                    "unsettled=%d due=%d",
                    position.symbol, new_periods, due_periods,
                )
            return

        # --- observed rate (0.0 included): settle the backlog exactly once -
        notional = position.entry_price * position.position_size
        signed_rate = rate / 100.0
        cost = notional * signed_rate * new_periods
        if position.direction == "SHORT":
            cost = -cost
        position.funding_paid += cost
        position.funding_periods_charged = due_periods
        self.balance -= cost
        self._funding_unknown.discard(position.position_key)
        if new_periods > 1:
            # Backlog settlement after an outage — logged with the EXACT
            # policy so the substitution is explicit, never silent.
            logger.info(
                "me inverse short funding backlog settled at the first "
                "observed rate after recovery (policy: entire unsettled "
                "backlog, exactly once): symbol=%s backlog=%d rate=%.6f%% "
                "cost=$%.4f",
                position.symbol, new_periods, rate, cost,
            )
        try:
            self.store.update_position(position)
        except Exception:
            # Ambiguous COMMIT (review P2): PostgreSQL may have APPLIED this
            # UPDATE with a lost acknowledgement.  Never assume the write
            # failed — verify the CONFIRMED row first; only a REAL
            # discrepancy with the store counts as not-applied.
            confirmed = self._confirmed_funding(position)
            if confirmed is not None and (
                confirmed[0] == position.funding_paid
                and confirmed[1] == position.funding_periods_charged
            ):
                # Confirmed APPLIED: the persisted ledger already carries
                # this accrual.  The local delta would DOUBLE-subtract on
                # the next reconciliation, so it is settled NOW — the state
                # IS confirmed (the delta of an OPEN key is funding-only).
                # No DIRTY snapshot is retained: nothing is left to retry,
                # and no reconciliation marker survives an unconfirmed
                # state — because this state is no longer unconfirmed.
                self._uncommitted_deltas.pop(position.position_key, None)
                self._dirty_funding.pop(position.position_key, None)
                logger.warning(
                    "me inverse short funding write CONFIRMED applied "
                    "despite a lost acknowledgement (verified against "
                    "PostgreSQL; delta settled, no dirty state retained, "
                    "no double booking): key=%s funding_paid=%.6f periods=%d",
                    position.position_key[:48], position.funding_paid,
                    position.funding_periods_charged,
                    exc_info=True,
                )
            else:
                # Genuinely not applied — or UNVERIFIABLE while the store
                # is unreachable (an unreadable state is never treated as
                # confirmed): keep the exact snapshot as DIRTY and the
                # local delta uncommitted until a write actually commits.
                self._uncommitted_deltas[position.position_key] = (
                    self._uncommitted_deltas.get(position.position_key, 0.0)
                    - cost
                )
                # DIRTY (review P2): freeze the EXACT funding snapshot so
                # every later cycle retries it — independent of new
                # intervals — without ever re-accruing or changing any
                # value.
                self._dirty_funding[position.position_key] = (
                    dataclasses.replace(position)
                )
                logger.warning(
                    "me inverse short funding persistence failed (write NOT "
                    "confirmed by PostgreSQL; exact funding snapshot "
                    "retained in memory as DIRTY for retry on later cycles; "
                    "NOT durable until a write commits and never claimed "
                    "durable): key=%s funding_paid=%.6f periods=%d",
                    position.position_key[:48], position.funding_paid,
                    position.funding_periods_charged,
                    exc_info=True,
                )
        else:
            # The row now carries the cumulative funding — local and store
            # accounting for this key agree again.
            self._uncommitted_deltas.pop(position.position_key, None)
            self._dirty_funding.pop(position.position_key, None)
        logger.info(
            "me inverse short funding: %s periods=%d rate=%.6f%% cost=$%.4f",
            position.symbol, new_periods, rate, cost,
        )

    def _confirmed_funding(
        self, position: InverseShortPosition,
    ) -> tuple[float, int] | None:
        """Read the COMMITTED funding facts for this key (review P2).

        Returns ``(funding_paid, funding_periods_charged)`` exactly as
        persisted, or ``None`` whenever the state cannot be confirmed — an
        unreadable row is NEVER treated as applied, and only an OPEN row
        confirms (on a CLOSED row any delta belongs to the terminal close,
        not to funding).
        """
        try:
            stored = self.store.get_position(position.position_key)
        except Exception:
            logger.warning(
                "me inverse short funding confirmation read failed (state "
                "stays UNCONFIRMED: dirty snapshot and local delta retained)",
                exc_info=True,
            )
            return None
        if stored is None or stored.status != POSITION_STATUS_OPEN:
            return None
        return (stored.funding_paid, stored.funding_periods_charged)

    def _retry_dirty_funding(self) -> int:
        """Persist retained DIRTY funding snapshots (review P2).

        Runs EVERY cycle — including cycles where no new funding interval
        has become due.  The frozen snapshot (``funding_paid`` /
        ``funding_periods_charged`` exactly as at failure time) is written
        byte-for-byte, so a retry NEVER accrues anything twice and never
        touches terminal close facts: keys with an unfinished close are
        skipped, because their snapshot (funding included) is owned and
        replayed by the pending-close/outbox mechanism.

        On success the store row carries the cumulative funding again, so
        the local uncommitted balance delta for that key is settled with it
        (booked exactly once, never twice).
        """
        persisted = 0
        for position_key, snapshot in list(self._dirty_funding.items()):
            if position_key in self.pending_closes:
                # Terminal close owns this key; an OPEN snapshot must never
                # be written over a pending terminal snapshot.
                continue
            try:
                self.store.update_position(snapshot)
            except Exception:
                logger.debug(
                    "me inverse short dirty funding retry failed again "
                    "(snapshot retained): key=%s funding_paid=%.6f periods=%d",
                    position_key[:48], snapshot.funding_paid,
                    snapshot.funding_periods_charged, exc_info=True,
                )
                continue
            self._dirty_funding.pop(position_key, None)
            self._uncommitted_deltas.pop(position_key, None)
            persisted += 1
            logger.info(
                "me inverse short dirty funding snapshot persisted on retry: "
                "key=%s funding_paid=%.6f periods=%d",
                position_key[:48], snapshot.funding_paid,
                snapshot.funding_periods_charged,
            )
        return persisted

    def _due_funding_periods(self, position: InverseShortPosition) -> int:
        """Whole funding intervals elapsed for this position.

        OPEN positions are measured at the current clock; CLOSED positions
        at their frozen ``closed_at`` (exit facts are never re-evaluated),
        so the due count — and with it any unsettled gap — is reproducible
        after a restart straight from the row.
        """
        interval_seconds = float(self.settings.paper_funding_interval_hours) * 3600
        if interval_seconds <= 0:
            return position.funding_periods_charged
        reference = (
            self._clock()
            if position.status == POSITION_STATUS_OPEN
            else position.closed_at
        )
        if reference is None:
            return position.funding_periods_charged
        elapsed = (reference - position.entered_at).total_seconds()
        if elapsed <= 0:
            return 0
        return max(0, int(elapsed // interval_seconds))

    def unsettled_funding_periods(self) -> int:
        """Outstanding funding obligations: due-but-unsettled intervals.

        OPEN positions are settleable — the backlog settles EXACTLY ONCE at
        the first rate observed after recovery.  CLOSED positions keep their
        exit facts frozen: the gap stays an explicitly recorded, reported
        obligation (never a confirmed zero, never retroactively booked).
        """
        total = 0
        for position in self.open_positions.values():
            total += max(
                0,
                self._due_funding_periods(position)
                - position.funding_periods_charged,
            )
        try:
            for position in self.store.list_positions(status=POSITION_STATUS_CLOSED):
                total += max(
                    0,
                    self._due_funding_periods(position)
                    - position.funding_periods_charged,
                )
        except Exception:
            logger.warning(
                "me inverse short unsettled funding scan failed", exc_info=True,
            )
        return total

    def _confirmed_close(self, snapshot: InverseShortPosition) -> bool | None:
        """Verify an ambiguous terminal UPDATE outcome (review P2).

        After a terminal COMMIT whose acknowledgement was lost, read the
        stored row back by ``position_key`` and compare the first terminal
        facts:

        * ``True`` — PostgreSQL carries ``status=CLOSED`` and the EXACT
          terminal facts of this snapshot (closed_at, exit_reason,
          exit_price, fees, funding, gross/net PnL): the write was applied
          despite the lost acknowledgement;
        * ``False`` — the row is readable but differs (the write did NOT
          apply);
        * ``None`` — the outcome could not be verified at all (row missing
          or the read failed).

        An unreadable row is NEVER treated as applied: ``False`` and
        ``None`` both keep the pending close + local delta and block new
        entries until a reconciliation proves consistency.
        """
        try:
            stored = self.store.get_position(snapshot.position_key)
        except Exception:
            logger.warning(
                "me inverse short terminal write outcome UNVERIFIABLE "
                "(stored position could not be read): key=%s",
                snapshot.position_key[:48], exc_info=True,
            )
            return None
        if stored is None:
            logger.warning(
                "me inverse short terminal write outcome UNVERIFIABLE "
                "(stored position row is absent): key=%s",
                snapshot.position_key[:48],
            )
            return None
        if stored.status != POSITION_STATUS_CLOSED:
            return False
        mismatches = [
            name for name, expected in (
                ("closed_at", snapshot.closed_at),
                ("exit_reason", snapshot.exit_reason),
                ("exit_price", snapshot.exit_price),
                ("entry_fee", snapshot.entry_fee),
                ("exit_fee", snapshot.exit_fee),
                ("slippage_cost", snapshot.slippage_cost),
                ("funding_paid", snapshot.funding_paid),
                ("funding_periods_charged", snapshot.funding_periods_charged),
                ("gross_pnl", snapshot.gross_pnl),
                ("net_pnl", snapshot.net_pnl),
            )
            if getattr(stored, name) != expected
        ]
        if mismatches:
            logger.info(
                "me inverse short terminal write NOT applied (stored row "
                "differs from the snapshot): key=%s fields=%s",
                snapshot.position_key[:48],
                ",".join(mismatches)[:200],
            )
            return False
        return True

    def _close(
        self,
        position: InverseShortPosition,
        exit_reference_price: float,
        reason: str,
    ) -> InverseShortPosition:
        """Close a position; PnL model: gross at reference prices, all costs explicit."""
        if reason not in CONTOUR_EXIT_REASONS:
            raise ValueError(f"INVALID_EXIT_REASON:{reason}")

        closed_at = self._clock()
        slip = float(self.settings.slippage_percent)
        # SHORT cover: adverse slippage moves the fill UP.
        fill = exit_reference_price * (1.0 + slip)

        gross = (
            position.entry_market_price - exit_reference_price
        ) * position.position_size
        exit_fee = fill * position.position_size * float(self.settings.taker_fee)
        exit_slippage_cost = (
            fill - exit_reference_price
        ) * position.position_size
        slippage_cost = position.entry_slippage_cost + exit_slippage_cost
        net_pnl = (
            gross
            - position.entry_fee
            - exit_fee
            - position.funding_paid
            - slippage_cost
        )

        self.balance += gross - exit_fee - exit_slippage_cost

        position.status = POSITION_STATUS_CLOSED
        position.exit_price = fill
        position.exit_reason = reason
        position.exit_fee = exit_fee
        position.slippage_cost = slippage_cost
        position.gross_pnl = gross
        position.net_pnl = net_pnl
        position.closed_at = closed_at
        position.duration_sec = (closed_at - position.entered_at).total_seconds()

        # Funding obligation audit (review P2-1 #6): if the rate feed was
        # down at exit, the due-but-unsettled intervals stay recorded on the
        # row (funding_periods_charged < due) — never force-settled to a
        # confirmed zero, never dropped, never retroactively booked (the
        # exit facts above stay frozen).
        unsettled = (
            self._due_funding_periods(position) - position.funding_periods_charged
        )
        if unsettled > 0:
            logger.warning(
                "me inverse short close executed with %d UNSETTLED funding "
                "period(s) (rate UNKNOWN at exit): obligation preserved on "
                "the row and reported, NOT booked as zero; exit facts frozen: "
                "key=%s reason=%s closed_at=%s",
                unsettled, position.position_key[:48], reason,
                closed_at.isoformat(),
            )

        # ---- durable close protocol (P1 / P1-3) ----
        # 1) Stage the EXACT terminal snapshot durably FIRST: the exit facts
        #    survive a crash even if the status UPDATE below fails.  When
        #    staging fails the key is marked UNSTAGED: flush_pending_closes()
        #    retries the staging BEFORE any terminal UPDATE on subsequent
        #    cycles, entries fail closed, and the unresolved durability risk
        #    is reported every cycle (recovery is never claimed for a
        #    snapshot the database never accepted).
        staged = False
        staging_degraded = False
        try:
            staged = bool(self.store.stage_close(position))
        except Exception:
            logger.warning(
                "me inverse short close staging failed (in-process snapshot "
                "retained; outbox staging will be retried before the terminal "
                "UPDATE on subsequent cycles): key=%s",
                position.position_key[:48],
                exc_info=True,
            )
        else:
            staging_degraded = not staged
        if staged:
            self._unstaged_closes.discard(position.position_key)
            self._staging_unavailable.discard(position.position_key)
        else:
            self._unstaged_closes.add(position.position_key)
            if staging_degraded:
                # stage_close() returns False only when the outbox table does
                # not exist (migration 086 not applied): staging can never
                # succeed, so retries are skipped — the terminal UPDATE below
                # is the only durability left in this documented mode.
                self._staging_unavailable.add(position.position_key)
        # 2) Flip the row to CLOSED.  On failure the terminal snapshot is
        #    retained and replayed byte-for-byte (same position_key, closed_at,
        #    exit_reason, exit_price, PnL and funding) by flush_pending_closes().
        #    close_delta is EXACTLY the amount just booked into the memory
        #    ledger; it becomes a local uncommitted delta until the store
        #    ledger commits it (never booked twice).
        close_delta = gross - exit_fee - exit_slippage_cost
        terminal_applied = False
        try:
            self.store.update_position(position)
            terminal_applied = True
        except Exception:
            verdict = self._confirmed_close(position)
            if verdict is True:
                # AMBIGUOUS COMMIT (review P2): PostgreSQL APPLIED the
                # terminal UPDATE but the acknowledgement was lost.  Settle
                # the local state exactly like a confirmed commit: NO
                # uncommitted close delta (the store ledger already carries
                # this PnL — booking it again would double-count at the
                # next reconcile_balance()) and NO pending close (nothing
                # is left to replay).
                terminal_applied = True
                logger.warning(
                    "me inverse short terminal close CONFIRMED applied "
                    "despite a lost acknowledgement (no uncommitted close "
                    "delta booked): key=%s reason=%s net=%.4f",
                    position.position_key[:48], reason, net_pnl,
                )
            else:
                # NOT applied, or the outcome could not be verified: keep
                # the pending close + local delta (fail closed) and refuse
                # NEW entries until a successful reconciliation proves the
                # ledger consistent (review P2: never assume either way).
                self.pending_closes[position.position_key] = position
                self._uncommitted_deltas[position.position_key] = (
                    self._uncommitted_deltas.get(position.position_key, 0.0)
                    + close_delta
                )
                self._balance_trusted = False
                logger.exception(
                    "me inverse short close persistence failed (write NOT "
                    "confirmed by PostgreSQL); terminal snapshot retained "
                    "for retry and new entries blocked until a "
                    "reconciliation succeeds (outbox staged=%s, "
                    "durability_risk=%s): key=%s reason=%s net=%.4f",
                    staged, not staged, position.position_key[:48], reason,
                    net_pnl,
                )
        if terminal_applied:
            # The row now carries the FULL terminal accounting (cumulative
            # funding + close facts): local and store agree for this key.
            self._uncommitted_deltas.pop(position.position_key, None)
            # The terminal write also commits any DIRTY funding snapshot
            # (funding_paid rides along in the same row update).
            self._dirty_funding.pop(position.position_key, None)
            self._unstaged_closes.discard(position.position_key)
            self._staging_unavailable.discard(position.position_key)
            if staged:
                try:
                    self.store.clear_pending_close(position.position_key)
                except Exception:
                    # A lingering outbox row replays the IDENTICAL snapshot on
                    # the next restart — idempotent, never changes the numbers.
                    logger.debug(
                        "me inverse short close outbox clear failed "
                        "(idempotent replay active): key=%s",
                        position.position_key[:48],
                        exc_info=True,
                    )

        logger.info(
            "me inverse short exit: symbol=%s reason=%s ref=%.6f fill=%.6f "
            "gross=%.4f fees=%.4f slip=%.4f funding=%.4f net=%.4f",
            position.symbol, reason, exit_reference_price, fill,
            gross, position.entry_fee + exit_fee, slippage_cost,
            position.funding_paid, net_pnl,
        )
        return position

    # ------------------------------------------------------------------
    # Durable close persistence (P1) + accounting reconciliation (P2)
    # ------------------------------------------------------------------
    def flush_pending_closes(self) -> int:
        """Persist retained terminal closes with their ORIGINAL facts.

        Runs every cycle (and at recovery, before any exit evaluation).  The
        retained snapshot object — position_key, closed_at, exit_reason,
        exit_price, PnL and funding — is replayed unchanged, so a retry can
        never alter the exit record or book realized PnL twice.  A position
        with a pending close is NOT in ``open_positions``, so STOP/TP/TIMEOUT
        can never re-evaluate it.

        P1 ordering: for every UNSTAGED snapshot the outbox staging is
        retried FIRST and the terminal UPDATE is only attempted once staging
        committed (or the outbox table is absent — documented degraded mode).
        P2: owed balance-recovery markers are retried after the flush and
        are NEVER discarded on a failed read.
        """
        flushed = 0
        for position_key, snapshot in list(self.pending_closes.items()):
            # -- P1: stage-before-update retry for unstaged snapshots ----
            if position_key in self._unstaged_closes:
                if position_key not in self._staging_unavailable:
                    try:
                        if bool(self.store.stage_close(snapshot)):
                            self._unstaged_closes.discard(position_key)
                        else:
                            # Outbox table absent (migration 086): staging can
                            # never succeed — fall through to the UPDATE.
                            self._staging_unavailable.add(position_key)
                    except Exception:
                        logger.warning(
                            "me inverse short close staging retry failed; "
                            "terminal UPDATE deferred to a later cycle "
                            "(exact snapshot retained in-process; recovery "
                            "NOT yet guaranteed): key=%s",
                            position_key[:48],
                            exc_info=True,
                        )
                        continue
                # Degraded mode (no outbox table): the terminal UPDATE is the
                # only durability left — attempt it below.
            try:
                self.store.update_position(snapshot)
            except Exception:
                if self._confirmed_close(snapshot) is True:
                    # AMBIGUOUS COMMIT (review P2): the replayed terminal
                    # UPDATE was applied although its acknowledgement was
                    # lost — converge exactly like a committed replay (the
                    # code below) instead of retaining a close the ledger
                    # already carries.
                    logger.warning(
                        "me inverse short close flush CONFIRMED applied "
                        "despite a lost acknowledgement: key=%s reason=%s "
                        "net=%.4f",
                        position_key[:48], snapshot.exit_reason,
                        snapshot.net_pnl,
                    )
                else:
                    # NOT confirmed: retain the snapshot + local delta and
                    # refuse NEW entries until a reconciliation succeeds.
                    self._balance_trusted = False
                    logger.warning(
                        "me inverse short close flush failed (terminal "
                        "snapshot retained, write NOT confirmed by "
                        "PostgreSQL; new entries blocked until a "
                        "reconciliation succeeds): key=%s reason=%s "
                        "closed_at=%s net=%.4f durability_risk=%s",
                        position_key[:48], snapshot.exit_reason,
                        snapshot.closed_at.isoformat() if snapshot.closed_at else None,
                        snapshot.net_pnl, position_key in self._unstaged_closes,
                        exc_info=True,
                    )
                    continue
            # -- committed (or confirmed applied despite a lost ACK): the
            #    row now carries the exact terminal facts --
            self.pending_closes.pop(position_key, None)
            self._uncommitted_deltas.pop(position_key, None)
            # The replayed terminal snapshot includes the cumulative funding,
            # so any DIRTY funding marker for this key is now confirmed too.
            self._dirty_funding.pop(position_key, None)
            self._unstaged_closes.discard(position_key)
            self._staging_unavailable.discard(position_key)
            flushed += 1
            try:
                self.store.clear_pending_close(position_key)
            except Exception:
                # The outbox row (if any) survives: the next restart replays
                # the IDENTICAL snapshot — idempotent, numbers unchanged.
                logger.debug(
                    "me inverse short close outbox clear failed (idempotent "
                    "replay active): key=%s",
                    position_key[:48],
                    exc_info=True,
                )
            logger.info(
                "me inverse short close flushed: key=%s reason=%s closed_at=%s "
                "exit=%.6f net=%.4f",
                position_key[:48], snapshot.exit_reason,
                snapshot.closed_at.isoformat() if snapshot.closed_at else None,
                snapshot.exit_price or 0.0, snapshot.net_pnl,
            )
        # P2: owed balance recovery is retried until it SUCCEEDS; the
        # markers survive any failed read (entries stay refused meanwhile).
        self._retry_balance_recovery()
        if self._unstaged_closes:
            logger.error(
                "me inverse short UNRESOLVED close durability risk: %d terminal "
                "snapshot(s) not durably staged (outbox write never committed). "
                "Recovery is NOT guaranteed if the process terminates before a "
                "durable write succeeds; new entries are refused (fail closed): "
                "keys=%s",
                len(self._unstaged_closes), self.unstaged_close_keys,
            )
        return flushed

    def _retry_balance_recovery(self) -> None:
        """Retry owed balance reconciliations (P2).

        A marker is owed once its recovered close has been flushed into the
        store row; it is discarded ONLY after a reconciliation succeeds, so
        a failed ``balance_components()`` read can never silently drop the
        recovered realized PnL.  A previously FAILED reconciliation is also
        retried here until it succeeds (entries stay refused meanwhile).
        """
        owed = [
            key for key in self._pending_from_store
            if key not in self.pending_closes
        ]
        if not owed and self._balance_trusted:
            return  # nothing to reconcile
        if self.reconcile_balance():
            for key in owed:
                self._pending_from_store.discard(key)
            if owed:
                logger.info(
                    "me inverse short balance recovery reconciled for %d close(s)",
                    len(owed),
                )

    def reconcile_balance(self) -> bool:
        """Deterministic ledger reconciliation (P2).

        ``balance = confirmed_balance + Σ uncommitted_deltas``

        * ``confirmed_balance`` — the CONFIRMED store ledger balance (last
          successful ``balance_components()`` read: initial + closed net −
          open costs, exactly as persisted);
        * ``uncommitted_deltas`` — LOCAL uncommitted accounting per
          position_key (close deltas and funding costs booked in memory but
          not yet committed by the store).

        Because the deltas are added back, a resync never overwrites
        realized PnL that belongs to other pending positions, and it can be
        retried at any time without recalculating any close or booking PnL
        twice.  Returns False (keeping the memory ledger and all recovery
        markers intact) when the store read fails.
        """
        try:
            components = self.store.balance_components()
            confirmed = float(
                components.get("balance", self.settings.initial_balance)
            )
        except Exception:
            self._balance_trusted = False
            logger.warning(
                "me inverse short balance reconciliation failed (memory ledger "
                "kept; balance-recovery marker retained, new entries blocked "
                "until a resync succeeds)",
                exc_info=True,
            )
            return False
        self.confirmed_balance = confirmed
        restored = confirmed + sum(self._uncommitted_deltas.values())
        if restored != self.balance:
            logger.info(
                "me inverse short balance resync: $%.4f -> $%.4f "
                "(confirmed=$%.4f uncommitted=%+.4f)",
                self.balance, restored, confirmed,
                sum(self._uncommitted_deltas.values()),
            )
        self.balance = restored
        self._balance_trusted = True
        return True

    # ------------------------------------------------------------------
    # Reporting (isolated accounting — nothing shared with Paper)
    # ------------------------------------------------------------------
    def mark_to_market(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        unrealized = 0.0
        for position in self.open_positions.values():
            if not prices:
                continue
            price = prices.get(position.symbol)
            if price is None:
                continue
            move = position.entry_market_price - float(price)  # SHORT
            unrealized += move * position.position_size
        return {
            "balance": self.balance,
            "unrealized_pnl": unrealized,
            "equity": self.balance + unrealized,
        }

    def snapshot(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        account = self.mark_to_market(prices)
        summary = self.store.net_pnl_summary()
        gross_exposure = sum(
            p.entry_price * p.position_size
            for p in self.open_positions.values()
        )
        equity = account["equity"]
        total_pnl = equity - self.initial_balance
        return {
            "strategy_id": "ME_LONG_V2_INVERSE_SHORT_V1",
            "direction": TRADE_DIRECTION,
            "balance": round(account["balance"], 2),
            "unrealized_pnl": round(account["unrealized_pnl"], 2),
            "equity": round(equity, 2),
            "starting_balance": round(self.initial_balance, 2),
            "net_pnl": round(total_pnl, 2),
            "net_pnl_pct": round(
                (total_pnl / self.initial_balance * 100.0) if self.initial_balance else 0.0, 4,
            ),
            "closed_trades": int(summary["closed_trades"]),
            "closed_net_pnl": round(summary["net_pnl"], 4),
            "open_positions": len(self.open_positions),
            "pending_closes": len(self.pending_closes),
            # Accounting reconciliation state (P2): the confirmed ledger
            # balance is reported separately from the local uncommitted
            # accounting deltas still owned by pending/failed writes.
            "confirmed_balance": round(self.confirmed_balance, 2),
            "uncommitted_delta": round(sum(self._uncommitted_deltas.values()), 4),
            "accounting_consistent": self.accounting_consistent,
            # Unresolved close durability risk (P1): never claim durable
            # recovery while a terminal snapshot is only in process memory.
            "durability_risk": self.durability_risk,
            "unstaged_closes": len(self._unstaged_closes),
            # DIRTY funding snapshots (P2): funding accreted in memory whose
            # persistence write failed — retried every cycle, never claimed
            # durable, never re-accrued.
            "dirty_funding": len(self._dirty_funding),
            "gross_exposure_pct": round(
                (gross_exposure / equity * 100.0) if equity else 0.0, 4,
            ),
            "activated": self.activated,
            # EXIT_ONLY (DRAINING) visibility (review P2-2) and the explicit
            # outstanding funding obligation (review P2-1): due-but-unsettled
            # intervals on OPEN (settleable) and CLOSED (frozen exit facts)
            # positions alike — an UNKNOWN rate never becomes a silent zero.
            "exit_only": self.exit_only,
            "unsettled_funding_periods": self.unsettled_funding_periods(),
            "positions": [
                {
                    "symbol": p.symbol,
                    "direction": p.direction,
                    "entry": p.entry_price,
                    "stop": p.stop_price,
                    "target": p.target_price,
                    "entered_at": p.entered_at.isoformat(),
                    "deadline_at": p.deadline_at.isoformat(),
                    "event_key": p.event_key[:16],
                }
                for p in sorted(
                    self.open_positions.values(), key=lambda p: p.entered_at,
                )
            ],
        }

    def pending_events(self) -> list[InverseShortEvent]:
        return self.store.list_events(status=EVENT_STATUS_CAPTURED)

    def closed_positions(self) -> list[InverseShortPosition]:
        return self.store.list_positions(status=POSITION_STATUS_CLOSED)
