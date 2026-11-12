"""Isolated SHORT paper engine for ME_LONG_V2_INVERSE_SHORT_OOS2_V1.

Design constraints (all enforced, all testable):

* **Isolation** — the contour keeps its own ``open_positions`` map keyed by
  the NAMESPACED ``position_key`` (``<strategy>:<event_key>``).  It never
  touches the production ``PaperTradingEngine.open_trades[symbol]`` map, the
  production balance, or any production risk gate.  Several contour positions
  on the SAME symbol are allowed (different source events); the production
  engine still allows only one trade per symbol — and vice versa.
* **Frozen OOS-2 LOCAL capacity** — at most ``MAX_OPEN_POSITIONS`` (12)
  concurrent OPEN positions, counted from THIS contour's own map; the global
  ``Settings.max_open_positions`` is never consulted (V1/V2/global capacity
  untouched, Issue #276).
* **Frozen geometry — three-stage PARTIAL TP ladder (Issue #276)** —
  ``stop = entry * 1.10`` (all remaining), ``tp1 = entry * 0.95`` (close 50%
  of the ORIGINAL quantity), ``tp2 = entry * 0.93`` (close 75% of the
  THEN-REMAINING quantity), ``tp3 = entry * 0.90`` (close ALL remaining);
  timeout is exactly ``entered_at + 7200 minutes`` computed ONCE at entry.
  Each stage fires at most ONCE (durable ``(position_key, leg_seq)`` leg
  identity), a partial leg NEVER flips the position CLOSED and NEVER frees
  its capacity slot, and ``STOP_FIRST`` wins an ambiguous bar.
* **Immutability** — ``entered_at``, ``deadline_at`` and the ORIGINAL
  ``position_size`` are written at entry and never recomputed: restart
  recovery restores them verbatim from the store (the store refuses to
  update them).  ``remaining_size`` is the only quantity that changes.
* **OOS2 signal-age TTL (frozen spec ``entry_max_age_minutes``)** — a source
  event may open a NEW position only while ``age < 120 minutes`` from
  ``signal_time``; at or past the TTL it is terminally disposed exactly once
  as ``EXPIRED`` (reason ``ENTRY_TTL``) or ``CAPACITY_REJECTED`` (reason
  ``ENTRY_CAP`` when the book was full), with reason + ``status_at``
  persisted.  The check runs as a sweep BEFORE the capacity early-return
  (backlogs age out even while the book is full) and again per event after
  the adoption branch (an already-committed row is ALWAYS adopted —
  protective exits attach regardless of age).
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
from datetime import datetime, timedelta, timezone
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
    EVENT_STATUS_CAPACITY_REJECTED,
    EVENT_STATUS_EXPIRED,
    InverseShortCloseLeg,
    InverseShortEvent,
    InverseShortPosition,
    LEG_SEQ_TERMINAL,
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
    leg_seq_for,
)
from .protocol import (
    ENTRY_MAX_AGE_MINUTES,
    EXIT_REASONS,
    MAX_OPEN_POSITIONS,
    QTY_DECIMALS,
    TRADE_DIRECTION,
    close_quantity,
    deadline_for,
    short_geometry,
)
from .store import InverseShortStore

logger = logging.getLogger(__name__)

# Allowed exit reasons of this contour (CHECK constraint, migration 088).
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
        # Outbox table absent (migration 088 not applied): staging can never
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
        # DIRTY partial-TP legs (Issue #276): a commit_partial_exit() that
        # failed after the leg was applied in memory.  The exact leg rows are
        # retained per position (TP1 then TP2) and replayed — identical facts,
        # same (position_key, leg_seq) identity — on every later cycle until
        # PostgreSQL accepts them; a retry NEVER books a leg twice.  While
        # non-empty the engine refuses new entries (fail closed) and reports
        # the risk; like dirty funding this state is in-memory only and is
        # NEVER claimed durable across a restart (the store rows — position
        # state AND legs — stay strictly the committed truth).
        self._dirty_legs: dict[str, list[InverseShortCloseLeg]] = {}
        # Per-key uncommitted accounting owed SPECIFICALLY by dirty legs
        # (the aggregate _uncommitted_deltas keeps the V2 contract
        # balance == confirmed + Σ deltas; this split lets a confirmed
        # funding write settle only the funding component while dirty legs
        # are still outstanding).
        self._leg_deltas: dict[str, float] = {}
        # Terminal leg rows staged together with their outbox snapshot —
        # retained until the terminal UPDATE is confirmed, so a flush retry
        # re-stages both halves as one idempotent unit.
        self._terminal_legs: dict[str, InverseShortCloseLeg] = {}
        # Open-position keys whose last_mark_price changed this cycle and
        # still need a durable UPDATE (dashboard unrealized PnL, #276).
        self._marks_dirty: set[str] = set()

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
                    "me inverse short oos2 recovery refused invalid pending close: key=%s",
                    str(snapshot.position_key)[:48],
                )
                continue
            self.pending_closes[snapshot.position_key] = snapshot
            self._pending_from_store.add(snapshot.position_key)
        if self.pending_closes:
            logger.warning(
                "me inverse short oos2 recovery resumed %d unfinished close(s); "
                "terminal facts replayed byte-for-byte, never recomputed",
                len(self.pending_closes),
            )

        restored = self.store.load_open_positions()
        for position in restored:
            if position.position_key in self.pending_closes:
                logger.warning(
                    "me inverse short oos2 recovery: key=%s kept out of the exit "
                    "algorithm (close persistence pending)",
                    position.position_key[:48],
                )
                continue
            if position.direction != TRADE_DIRECTION:
                logger.error(
                    "me inverse short oos2 recovery refused non-SHORT position: key=%s direction=%s",
                    position.position_key, position.direction,
                )
                continue
            if position.position_key in self.open_positions:
                logger.error(
                    "me inverse short oos2 recovery duplicate position_key=%s (first wins)",
                    position.position_key,
                )
                continue
            if position.deadline_at <= position.entered_at:
                logger.error(
                    "me inverse short oos2 recovery refused invalid deadline: key=%s "
                    "entered_at=%s deadline_at=%s",
                    position.position_key,
                    position.entered_at.isoformat(),
                    position.deadline_at.isoformat(),
                )
                continue
            if not self._ladder_state_valid(position):
                logger.error(
                    "me inverse short oos2 recovery refused invalid ladder state: "
                    "key=%s size=%.12g remaining=%.12g stages=%s "
                    "(protective management NOT attached — operator "
                    "intervention required)",
                    position.position_key, position.position_size,
                    position.remaining_size, position.executed_stages,
                )
                continue
            self.open_positions[position.position_key] = position
        if restored:
            logger.info(
                "me inverse short oos2 engine recovered %d open position(s); balance=$%.2f",
                len(self.open_positions), self.balance,
            )

    # ------------------------------------------------------------------
    # Entries (prospective-only, activation-gated)
    # ------------------------------------------------------------------
    def _ladder_state_valid(self, position: InverseShortPosition) -> bool:
        """Structural invariants of the OOS-2 partial ladder (Issue #276).

        * ``0 < remaining_size <= position_size`` (never negative, never
          more than the immutable original — no overclose ever);
        * stage flags are monotone (TP2 without TP1 is impossible);
        * the remaining quantity equals the exact ladder multiple of the
          original: 100% pre-TP1, 50% after TP1, 12.5% after TP2;
        * an OPEN row can never carry an executed TP3 (TP3 is terminal).
        """
        if position.position_size <= 0 or position.remaining_size <= 0:
            return False
        if position.remaining_size > position.position_size:
            return False
        if position.tp2_executed and not position.tp1_executed:
            return False
        if position.tp3_executed:
            return False  # TP3 is terminal — an OPEN row can never carry it
        expected_fraction = (
            0.125 if position.tp2_executed
            else 0.5 if position.tp1_executed
            else 1.0
        )
        expected = position.position_size * expected_fraction
        scale = max(abs(expected), 1.0)
        if abs(position.remaining_size - expected) > scale * 1e-6:
            return False
        return True

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
        UPDATE has not committed either), OR a partial-TP leg applied in
        memory is not yet in the durable leg ledger.  While True the engine
        fails closed and reports the risk — recovery is NOT guaranteed if
        the process terminates now."""
        return bool(self._unstaged_closes) or bool(self._dirty_legs)

    @property
    def dirty_leg_keys(self) -> tuple[str, ...]:
        """Positions whose partial-TP leg rows are not durable yet."""
        return tuple(sorted(self._dirty_legs))

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

    # ------------------------------------------------------------------
    # OOS2 entry admission: the frozen signal-age TTL (entry_max_age_minutes)
    # ------------------------------------------------------------------
    def _sweep_stale_events(self, max_positions: int) -> int:
        """Dispose of CAPTURED events that reached the frozen entry TTL.

        Rule (frozen spec ``entry_max_age_minutes``): ``age >= TTL`` can
        never open a NEW position, so the event is terminally transitioned
        exactly once — ``CAPACITY_REJECTED`` (reason ``ENTRY_CAP``) when the
        book was full at decision time, otherwise ``EXPIRED`` (reason
        ``ENTRY_TTL``) — with the refusal reason and ``status_at`` persisted
        on the row.  Rows are never deleted: the full source-capture history
        and lineage survive.

        Events whose position row already exists (ambiguous COMMIT still
        pending adoption) are deliberately SKIPPED: the entry loop must
        ADOPT that committed position, never dispose of its event.

        Fail-soft by design: a store failure is logged and retried next
        cycle; protective exits are never affected, and the per-event age
        re-check inside ``_open_event()`` still refuses stale entries in
        the meantime.  Not called in EXIT_ONLY (DRAINING keeps its "no
        entry-event query at all" contract).
        """
        try:
            events = self.store.list_events(status=EVENT_STATUS_CAPTURED)
        except Exception:
            logger.warning(
                "me inverse short oos2 stale sweep unavailable (store read "
                "failed; the per-event age check still refuses stale entries)",
                exc_info=True,
            )
            return 0
        if not events:
            return 0
        now = self._clock()
        ttl = timedelta(minutes=ENTRY_MAX_AGE_MINUTES)
        capacity_binding = len(self.open_positions) >= max_positions
        disposed = 0
        for event in events:
            age = now - event.signal_time
            if age < ttl:
                continue  # still admissible (fresh) — leave it CAPTURED
            position_key = build_position_key(event.event_key)
            if position_key in self.open_positions:
                # Entry committed, event mark pending: the adoption path in
                # _open_event() owns this row — never dispose of it.
                continue
            try:
                if self.store.has_position(position_key):
                    # Ambiguous COMMIT pending adoption — never dispose.
                    continue
            except Exception:
                logger.warning(
                    "me inverse short oos2 stale sweep skipped an event "
                    "(position existence unknown): key=%s",
                    event.event_key[:16], exc_info=True,
                )
                continue
            status = (
                EVENT_STATUS_CAPACITY_REJECTED
                if capacity_binding else EVENT_STATUS_EXPIRED
            )
            reason = "ENTRY_CAP" if capacity_binding else "ENTRY_TTL"
            try:
                self.store.mark_event_expired(
                    event.event_key, status, reason=reason, decided_at=now,
                )
            except Exception:
                logger.warning(
                    "me inverse short oos2 stale sweep write failed (event "
                    "stays CAPTURED; the per-event age check still refuses "
                    "it): key=%s age=%s",
                    event.event_key[:16], age, exc_info=True,
                )
                continue
            disposed += 1
            logger.info(
                "me inverse short oos2 event %s: key=%s symbol=%s signal_time=%s "
                "age=%s reason=%s (frozen entry TTL %d min)",
                status, event.event_key[:16], event.symbol,
                event.signal_time.isoformat(), age, reason,
                int(ENTRY_MAX_AGE_MINUTES),
            )
        return disposed

    def open_entries(self, prices: dict[str, float]) -> list[InverseShortPosition]:
        """Open SHORT positions for eligible CAPTURED events (age < TTL)."""
        opened: list[InverseShortPosition] = []
        if self.require_activation and self.activation is None:
            logger.warning(
                "me inverse short oos2 entries refused: contour is not activated "
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
                "me inverse short oos2 entries refused (EXIT_ONLY): activation is "
                "DRAINING — managing existing positions only, no new entries",
            )
            return opened

        # ---- OOS2 entry admission (frozen spec: entry_max_age_minutes) ----
        # Dispose of CAPTURED events that reached the entry TTL BEFORE any
        # other gate and BEFORE the capacity early-return below: a backlog
        # must age out even while the book is full or an accounting gate is
        # down — otherwise stale events would silently accumulate.  Fail-soft
        # bookkeeping (never blocks exits, never raises); the per-event age
        # re-check inside _open_event() keeps refusing stale entries even if
        # this sweep write failed.
        # CAPACITY: the OOS-2 LOCAL limit (protocol constant), never the
        # global Settings.max_open_positions (Issue #276).
        max_positions = MAX_OPEN_POSITIONS
        self._sweep_stale_events(max_positions)

        # Fail-closed gates (P1 + P2): never trade on an unresolved close
        # durability risk or on a balance that is not yet trustworthy.
        # An owed balance recovery is retried here first (a successful
        # resync unblocks entries within the same cycle).
        self._retry_balance_recovery()
        if self._unstaged_closes:
            logger.error(
                "me inverse short oos2 entries refused (fail closed): unresolved "
                "close durability risk — %d terminal snapshot(s) not durably "
                "staged; recovery is NOT guaranteed if the process terminates "
                "before a durable write succeeds: keys=%s",
                len(self._unstaged_closes), self.unstaged_close_keys,
            )
            return opened
        if not self.accounting_consistent:
            logger.warning(
                "me inverse short oos2 entries refused (fail closed): contour "
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
                "me inverse short oos2 entries refused (fail closed): %d funding "
                "write(s) not yet confirmed by PostgreSQL — waiting for "
                "accounting consistency to be restored: keys=%s",
                len(self._dirty_funding),
                ", ".join(sorted(self._dirty_funding))[:200],
            )
            return opened
        if self._dirty_legs:
            # Fail-closed (Issue #276): a partial-TP leg applied in memory
            # but not yet accepted by PostgreSQL — no new entries until the
            # leg commit confirms (protective exits keep running; the retry
            # runs before exit evaluation each cycle).
            logger.error(
                "me inverse short oos2 entries refused (fail closed): %d partial "
                "TP leg(s) not yet confirmed by PostgreSQL — durability risk "
                "reported every cycle, entries resume after the leg commit: "
                "keys=%s",
                len(self._dirty_legs),
                ", ".join(sorted(self._dirty_legs))[:200],
            )
            return opened

        if len(self.open_positions) >= max_positions:
            return opened

        # Review P1: a pending-events/store failure must NEVER propagate out
        # of open_entries — entries simply fail closed for this cycle while
        # the protective exits (already executed before this method in
        # run_cycle) stay fully functional.
        try:
            events = self.store.list_events(status=EVENT_STATUS_CAPTURED)
            for index, event in enumerate(events):
                if len(self.open_positions) >= max_positions:
                    logger.info(
                        "me inverse short oos2 entry cap reached (%d)", max_positions,
                    )
                    break
                # Review P2 (Codex): an earlier entry of THIS cycle may have
                # adopted a committed position whose fresh reconciliation
                # failed and cleared _balance_trusted — re-check accounting
                # consistency before the NEXT entry so an untrustworthy
                # balance never opens another position in the same cycle.
                # The already-restored position keeps its protective STOP/TP/
                # TIMEOUT management (exits ran before this method) and its
                # entry costs are still folded in exactly once later; the
                # deferred events stay CAPTURED for the next cycle.
                if not self.accounting_consistent:
                    logger.warning(
                        "me inverse short oos2 entry processing halted (fail "
                        "closed): accounting consistency lost during this "
                        "cycle (balance reconciliation failed) — remaining "
                        "%d candidate(s) deferred until a reconciliation "
                        "succeeds",
                        len(events) - index,
                    )
                    break
                position = self._open_event(event, prices)
                if position is not None:
                    opened.append(position)
        except Exception:
            logger.error(
                "me inverse short oos2 entries refused (fail closed): prospective "
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
                "me inverse short oos2 entry refused non-SHORT event: key=%s direction=%s",
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
                    "me inverse short oos2 entry refused: unfinished close for "
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
                    "me inverse short oos2 entry refused pre-freeze event: key=%s signal_time=%s",
                    event.event_key[:16], event.signal_time.isoformat(),
                )
                return None

        # OOS2 entry admission (frozen spec: entry_max_age_minutes): a signal
        # at or past its TTL can NEVER open a NEW position — even at the
        # instant a capacity slot frees.  Deliberately placed AFTER the
        # adoption branch above: an already-COMMITTED position row is always
        # adopted (its protective STOP/TP/TIMEOUT must attach) regardless of
        # how old the source event is; only a genuinely new entry is
        # age-gated.  Status transitions belong to the sweep in open_entries.
        entry_age = self._clock() - event.signal_time
        if entry_age >= timedelta(minutes=ENTRY_MAX_AGE_MINUTES):
            logger.warning(
                "me inverse short oos2 entry refused stale event: key=%s "
                "signal_time=%s age=%s (frozen entry TTL %d min)",
                event.event_key[:16], event.signal_time.isoformat(), entry_age,
                int(ENTRY_MAX_AGE_MINUTES),
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
                "me inverse short oos2 duplicate position suppressed: key=%s symbol=%s",
                position_key[:48], position.symbol,
            )
            return None

        try:
            self.store.mark_event_entered(event.event_key, position_key)
        except Exception:
            # Fail-open: has_position() keeps delivery idempotent next cycle.
            logger.warning(
                "me inverse short oos2 event mark-entered failed (idempotent guard active)",
                exc_info=True,
            )

        self.balance -= position.entry_fee + position.entry_slippage_cost
        self.open_positions[position_key] = position
        logger.info(
            "me inverse short oos2 entry: symbol=%s side=SHORT entry=%.6f stop=%.6f "
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
        stop, tp1, tp2, tp3 = short_geometry(stored.entry_price)
        if (
            stored.stop_price != stop
            or stored.target_price != tp1
            or stored.tp2_price != tp2
            or stored.tp3_price != tp3
        ):
            return False
        # Partial-ladder state must be structurally coherent (an already
        # partially-realized position is adopted WITH its leg history).
        if not self._ladder_state_valid(stored):
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
        are never recomputed, no second INSERT is issued, and the position
        enters ``open_positions`` immediately — so protective
        STOP/TP/TIMEOUT applies from the very next cycle EVEN when the
        accounting reconciliation read is unavailable (review P1).  Entry
        costs are never subtracted by hand (review P2-2): they are folded
        in exactly once by a successful ``reconcile_balance()`` — either
        here, or later, while the accounting-consistent gate keeps new
        entries blocked until that confirmation succeeds.  Every other
        failure path stays fail-closed: nothing is opened, booked or
        marked unless the committed row fully verifies.
        """
        if position_key in self.open_positions:
            # Already adopted in this process — never book the fees twice.
            return self.open_positions[position_key]
        try:
            stored = self.store.get_position(position_key)
        except Exception:
            logger.error(
                "me inverse short oos2 adoption read failed (entry stays "
                "fail-closed: nothing opened, nothing re-booked)",
                exc_info=True,
            )
            return None
        if stored is None:
            return None  # row genuinely absent: nothing to adopt
        if stored.status != POSITION_STATUS_OPEN:
            logger.info(
                "me inverse short oos2 duplicate position suppressed (already "
                "closed): key=%s symbol=%s status=%s",
                position_key[:48], stored.symbol, stored.status,
            )
            return None
        if not self._entry_facts_match(event, stored):
            logger.error(
                "me inverse short oos2 adoption REFUSED: committed row does not "
                "correspond to the source event (fail closed, nothing "
                "opened): key=%s event_key=%s direction=%s symbol=%s",
                position_key[:48], stored.event_key[:16], stored.direction,
                stored.symbol,
            )
            return None
        # Review P2-2 + Review P1: the committed row is ALWAYS adopted
        # once it verifies — protective STOP/TP/TIMEOUT must never be
        # held hostage by an unavailable accounting read.  Entry costs
        # are NEVER subtracted by hand; a fresh balance_components()
        # read already carries them (OPEN rows contribute entry_fee +
        # entry_slippage), so reconcile_balance() folds them into the
        # memory ledger exactly once and re-establishes
        #     memory_balance == confirmed_balance + Σ uncommitted_deltas.
        # If that read FAILS, the position is still opened for protective
        # management — but nothing is booked: _balance_trusted stays
        # False, the accounting-consistent gate keeps NEW entries blocked,
        # and the entry costs land exactly once at the next successful
        # reconciliation (which reads them from the confirmed ledger).
        accounting_confirmed = self.reconcile_balance()
        if not accounting_confirmed:
            logger.warning(
                "me inverse short oos2 adoption accounting UNCONFIRMED: "
                "confirmed balance unreadable — position opened for "
                "protective STOP/TP/TIMEOUT management only, entry costs "
                "NOT booked (they land exactly once at the next successful "
                "reconciliation), new entries blocked until then: "
                "key=%s symbol=%s",
                position_key[:48], stored.symbol,
            )
        try:
            self.store.mark_event_entered(event.event_key, position_key)
        except Exception:
            # Fail-open: has_position() keeps delivery idempotent next cycle.
            logger.warning(
                "me inverse short oos2 event mark-entered failed after adoption "
                "(idempotent guard active)",
                exc_info=True,
            )
        # NO manual fee subtraction (review P2-2): the confirmed ledger
        # carries the entry costs — either folded in just now (confirmed)
        # or folded in by the next successful reconciliation (unconfirmed,
        # review P1).  Booked never twice, protective exits always active.
        self.open_positions[position_key] = stored
        if accounting_confirmed:
            logger.warning(
                "me inverse short oos2 ADOPTED committed position after an "
                "ambiguous INSERT (no re-open, original entry facts kept, "
                "entry costs taken from the confirmed ledger exactly once): "
                "key=%s symbol=%s entered_at=%s stop=%.6f target=%.6f",
                position_key[:48], stored.symbol,
                stored.entered_at.isoformat() if stored.entered_at else "?",
                stored.stop_price, stored.target_price,
            )
        else:
            logger.warning(
                "me inverse short oos2 ADOPTED committed position after an "
                "ambiguous INSERT (accounting UNCONFIRMED — protective "
                "exits active, booking deferred to the next successful "
                "reconciliation, original entry facts kept): "
                "key=%s symbol=%s entered_at=%s stop=%.6f target=%.6f",
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
        stop_price, tp1_price, tp2_price, tp3_price = short_geometry(entry_price)
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
                "me inverse short oos2 entry sizing failed: symbol=%s balance=%.2f",
                event.symbol, balance,
            )
            return None

        entry_fee = entry_price * qty * float(self.settings.taker_fee)
        entry_slippage_cost = (entry_market - entry_price) * qty
        if balance - entry_fee - entry_slippage_cost <= 0:
            logger.warning(
                "me inverse short oos2 entry rejected: costs exceed balance: symbol=%s",
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
            target_price=tp1_price,
            tp2_price=tp2_price,
            tp3_price=tp3_price,
            position_size=qty,
            remaining_size=qty,
            risk_usdt=distance * qty,
            balance_before=balance,
            source_scanner=event.source_scanner,
            source_setup_id=event.source_setup_id,
            market_regime=event.market_regime,
            source_score=event.source_score,
        )

    # ------------------------------------------------------------------
    # Exits: STOP → (TP1 → TP2 → TP3 ladder) → TIMEOUT (STOP_FIRST)
    # ------------------------------------------------------------------
    def check_exits(
        self,
        prices: dict[str, float],
        funding_rates_percent: dict[str, float | None] | None = None,
        bars: dict[str, dict[str, float]] | None = None,
    ) -> list[InverseShortPosition]:
        """Evaluate every open position; return positions TERMINALLY closed.

        Funding (review P2-1): a MISSING rate is UNKNOWN, not a confirmed
        zero — the per-symbol mapping simply has no entry (or an explicit
        ``None``) for it and ``_apply_funding`` leaves those intervals
        unsettled.  An UNKNOWN rate never blocks STOP/TP/TIMEOUT.

        OOS-2 partial ladder (Issue #276):
          * STOP is evaluated FIRST for the WHOLE bar — ``STOP_FIRST`` means
            a stop touch closes the entire REMAINING quantity at the stop
            and no TP stage of that bar fires;
          * otherwise TP1 then TP2 fire as PARTIAL legs (each at most once,
            each at its own level price, in falling-price order — a single
            bar may fire several stages when it skips levels);
          * TP3 closes the remaining quantity (terminal, position CLOSED);
          * TIMEOUT closes the remaining quantity at the observed price and
            PRESERVES every realized TP leg;
          * a position only leaves ``open_positions`` on a TERMINAL close —
            a partial leg keeps the row OPEN and keeps its capacity slot.
        """
        # First finish any unfinished close (P1-3): its terminal snapshot is
        # replayed byte-for-byte and never re-evaluated below.
        self.flush_pending_closes()
        # Then retry any DIRTY funding snapshot (P2): persisted even when no
        # new funding interval has become due; a retry never re-accrues.
        self._retry_dirty_funding()
        # Then retry any DIRTY partial-TP leg (Issue #276): identical facts,
        # same (position_key, leg_seq) identity — a retry never books a leg
        # twice and never claims durability the database never accepted.
        self._retry_dirty_legs()

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
            terminal: InverseShortPosition | None = None

            if stop_touched:
                # STOP_FIRST: a protective stop wins over EVERY TP stage and
                # TIMEOUT on an ambiguous bar — the whole REMAINING quantity
                # closes at the stop (realized TP legs are preserved).
                gap = price > position.stop_price
                reason = "STOP_LOSS_GAP" if gap else "STOP_LOSS"
                terminal = self._close(position, position.stop_price, reason)
            else:
                # Partial ladder: TP1 -> TP2 in falling-price order.  A bar
                # that skips several levels fires every skipped stage in
                # order, each exactly once at its own level price.
                for stage, level_attr, flag_attr in (
                    ("TAKE_PROFIT_1", "target_price", "tp1_executed"),
                    ("TAKE_PROFIT_2", "tp2_price", "tp2_executed"),
                ):
                    if getattr(position, flag_attr):
                        continue  # each stage fires at most ONCE
                    level = float(getattr(position, level_attr))
                    if low > level:
                        continue  # level not touched by this bar
                    if self._partial_close(position, stage, level) == "TERMINAL":
                        # Defensive (only reachable from a corrupted state):
                        # the leg would absorb the whole remainder — finish
                        # it as a TERMINAL close of that stage instead.
                        terminal = self._close(position, level, stage)
                        break
                if terminal is None:
                    if low <= position.tp3_price:
                        # TP3 closes ALL remaining -> terminal.
                        terminal = self._close(
                            position, position.tp3_price, "TAKE_PROFIT_3",
                        )
                    elif self._clock() >= position.deadline_at:
                        # TIMEOUT closes the remaining quantity and keeps
                        # every realized TP leg's PnL.
                        terminal = self._close(position, price, "TIMEOUT")

            if terminal is not None:
                self.open_positions.pop(position_key, None)
                closed.append(terminal)

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
                    "me inverse short oos2 funding rate UNKNOWN; %d period(s) left "
                    "unsettled (obligation preserved, NOTHING booked, no zero "
                    "fabricated): symbol=%s due=%d charged=%d",
                    new_periods, position.symbol, due_periods,
                    position.funding_periods_charged,
                )
            else:
                logger.debug(
                    "me inverse short oos2 funding still UNKNOWN: symbol=%s "
                    "unsettled=%d due=%d",
                    position.symbol, new_periods, due_periods,
                )
            return

        # --- observed rate (0.0 included): settle the backlog exactly once -
        # Issue #276: funding accrues on the REMAINING (outstanding) quantity
        # only — a partially closed position funds only what is still open.
        notional = position.entry_price * position.remaining_size
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
                "me inverse short oos2 funding backlog settled at the first "
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
                # IS confirmed.  The write carried the FULL current position
                # state, so any dirty-leg ACCOUNTING rides along with it as
                # well: only the dirty-leg ROWS (audit ledger) may remain
                # owed.  No DIRTY funding snapshot is retained, and no
                # reconciliation marker survives an unconfirmed state —
                # because this state is no longer unconfirmed.
                self._uncommitted_deltas.pop(position.position_key, None)
                self._leg_deltas.pop(position.position_key, None)
                self._dirty_funding.pop(position.position_key, None)
                logger.warning(
                    "me inverse short oos2 funding write CONFIRMED applied "
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
                    "me inverse short oos2 funding persistence failed (write NOT "
                    "confirmed by PostgreSQL; exact funding snapshot "
                    "retained in memory as DIRTY for retry on later cycles; "
                    "NOT durable until a write commits and never claimed "
                    "durable): key=%s funding_paid=%.6f periods=%d",
                    position.position_key[:48], position.funding_paid,
                    position.funding_periods_charged,
                    exc_info=True,
                )
        else:
            # The row now carries the cumulative funding AND the full
            # current position state — local and store accounting for this
            # key agree again (dirty-leg accounting rode along; only dirty
            # leg ROWS, if any, remain owed and are retried separately).
            self._uncommitted_deltas.pop(position.position_key, None)
            self._leg_deltas.pop(position.position_key, None)
            self._dirty_funding.pop(position.position_key, None)
        logger.info(
            "me inverse short oos2 funding: %s periods=%d rate=%.6f%% cost=$%.4f",
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
                "me inverse short oos2 funding confirmation read failed (state "
                "stays UNCONFIRMED: dirty snapshot and local delta retained)",
                exc_info=True,
            )
            return None
        if stored is None or stored.status != POSITION_STATUS_OPEN:
            return None
        return (stored.funding_paid, stored.funding_periods_charged)

    def _retry_dirty_funding(self) -> int:
        """Persist retained DIRTY funding state (review P2 + Issue #276).

        Runs EVERY cycle — including cycles where no new funding interval
        has become due.  The write carries the CURRENT in-memory position
        state (absolute values — a retry NEVER accrues anything twice and
        never touches terminal close facts): keys with an unfinished close
        are skipped, because their snapshot (funding included) is owned and
        replayed by the pending-close/outbox mechanism.

        On success the store row carries the full current state again, so
        the local uncommitted balance delta for that key is settled with it
        (booked exactly once, never twice) and any dirty-leg ACCOUNTING is
        carried along; only dirty leg ROWS may remain owed (retried by
        ``_retry_dirty_legs``).
        """
        persisted = 0
        for position_key, snapshot in list(self._dirty_funding.items()):
            if position_key in self.pending_closes:
                # Terminal close owns this key; an OPEN snapshot must never
                # be written over a pending terminal snapshot.
                continue
            # Prefer the LIVE object: it always contains the newest
            # accounting (legs, funding), so a confirmed write settles every
            # local delta for the key in one row update.
            live = self.open_positions.get(position_key)
            to_write = live if live is not None else snapshot
            try:
                self.store.update_position(to_write)
            except Exception:
                logger.debug(
                    "me inverse short oos2 dirty funding retry failed again "
                    "(state retained): key=%s funding_paid=%.6f periods=%d",
                    position_key[:48], to_write.funding_paid,
                    to_write.funding_periods_charged, exc_info=True,
                )
                continue
            self._dirty_funding.pop(position_key, None)
            self._uncommitted_deltas.pop(position_key, None)
            self._leg_deltas.pop(position_key, None)
            persisted += 1
            logger.info(
                "me inverse short oos2 dirty funding persisted on retry: "
                "key=%s funding_paid=%.6f periods=%d",
                position_key[:48], to_write.funding_paid,
                to_write.funding_periods_charged,
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
                "me inverse short oos2 unsettled funding scan failed", exc_info=True,
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
                "me inverse short oos2 terminal write outcome UNVERIFIABLE "
                "(stored position could not be read): key=%s",
                snapshot.position_key[:48], exc_info=True,
            )
            return None
        if stored is None:
            logger.warning(
                "me inverse short oos2 terminal write outcome UNVERIFIABLE "
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
                ("remaining_size", snapshot.remaining_size),
                ("realized_legs_net", snapshot.realized_legs_net),
                ("entry_cost_allocated", snapshot.entry_cost_allocated),
            )
            if getattr(stored, name) != expected
        ]
        if mismatches:
            logger.info(
                "me inverse short oos2 terminal write NOT applied (stored row "
                "differs from the snapshot): key=%s fields=%s",
                snapshot.position_key[:48],
                ",".join(mismatches)[:200],
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Partial TP legs (Issue #276) — exactly-once, atomic, fail-closed
    # ------------------------------------------------------------------
    def _partial_close(
        self,
        position: InverseShortPosition,
        stage: str,
        level: float,
    ) -> str:
        """Execute ONE partial take-profit leg; position STAYS OPEN.

        Returns ``"OK"`` when the leg was applied (in memory always, in
        PostgreSQL best effort with dirty-leg retry), ``"TERMINAL"`` when the
        computed quantity would absorb the whole remainder (the caller then
        finishes it as a terminal close of that stage) or ``"NONE"`` when the
        ladder produced no quantity (corrupted state — logged, never
        overclosed).

        Invariants:
          * quantity from :func:`close_quantity` — rounded HALF-EVEN,
            clamped to ``[0, remaining]``, never overclosing;
          * entry cost allocated pro-rata of the ORIGINAL quantity
            (deterministic; the terminal leg takes the exact remainder);
          * balance books exactly ``(gross − exit fee − exit slippage)``
            for the leg — fees and PnL count once;
          * the durable leg commit is ONE transaction (leg row + position
            update); an ambiguous COMMIT is resolved by reading both back.
        """
        qty = close_quantity(
            stage, position.position_size, position.remaining_size,
        )
        if qty <= 0:
            logger.error(
                "me inverse short oos2 partial close produced NO quantity "
                "(state corrupted?): key=%s stage=%s size=%.12g remaining=%.12g",
                position.position_key[:48], stage,
                position.position_size, position.remaining_size,
            )
            return "NONE"
        if qty >= position.remaining_size:
            # Would close the whole remainder — not a partial leg; the
            # caller escalates to a terminal close of this stage.
            return "TERMINAL"

        slip = float(self.settings.slippage_percent)
        fill = level * (1.0 + slip)  # SHORT cover: adverse = higher
        gross = (position.entry_market_price - level) * qty
        exit_fee = fill * qty * float(self.settings.taker_fee)
        exit_slippage_cost = (fill - level) * qty
        leg_delta = gross - exit_fee - exit_slippage_cost

        # Deterministic entry-cost allocation: pro-rata of the ORIGINAL
        # quantity, rounded HALF-EVEN, clamped so Σ shares can never exceed
        # the total (the terminal leg books the exact remainder later).
        entry_total = position.entry_fee + position.entry_slippage_cost
        share = round(
            entry_total * (qty / position.position_size), QTY_DECIMALS,
        )
        share = min(
            share,
            round(entry_total - position.entry_cost_allocated, QTY_DECIMALS),
        )
        if share < 0:
            share = 0.0
        net_leg = leg_delta - share

        leg = InverseShortCloseLeg(
            position_key=position.position_key,
            leg_seq=leg_seq_for(stage),
            exit_reason=stage,
            is_terminal=False,
            quantity=qty,
            remaining_after=round(
                position.remaining_size - qty, QTY_DECIMALS,
            ),
            reference_price=level,
            fill_price=fill,
            gross_pnl=gross,
            exit_fee=exit_fee,
            exit_slippage=exit_slippage_cost,
            entry_cost_share=share,
            funding_paid_to_date=position.funding_paid,
            net_pnl=net_leg,
            cumulative_realized_net=position.realized_legs_net + leg_delta,
            executed_at=self._clock(),
            status_after=POSITION_STATUS_OPEN,
            event_key=position.event_key,
            symbol=position.symbol,
        )

        # ---- apply in memory first (single source of truth for retries) --
        position.remaining_size = leg.remaining_after
        if stage == "TAKE_PROFIT_1":
            position.tp1_executed = True
        elif stage == "TAKE_PROFIT_2":
            position.tp2_executed = True
        position.realized_legs_net += leg_delta
        position.entry_cost_allocated += share
        position.gross_pnl += gross
        position.exit_fee += exit_fee
        position.slippage_cost += exit_slippage_cost  # Σ partial-leg slip
        self.balance += leg_delta

        position_key = position.position_key
        try:
            self.store.commit_partial_exit(position, leg)
        except Exception:
            verdict = self._confirmed_leg(position, leg)
            if verdict is True:
                # AMBIGUOUS COMMIT: PostgreSQL applied the leg transaction
                # but the acknowledgement was lost.  Settle exactly like a
                # confirmed commit — the store already carries the leg AND
                # the position state it wrote (including any funding paid so
                # far), so NO delta and NO funding-dirty state may be
                # retained.  Other DIRTY legs of this key keep their rows
                # owed (their accounting already rode along with this write).
                self._leg_deltas.pop(position_key, None)
                self._uncommitted_deltas.pop(position_key, None)
                self._dirty_funding.pop(position_key, None)
                logger.warning(
                    "me inverse short oos2 partial TP leg CONFIRMED applied "
                    "despite a lost acknowledgement (no delta booked, no dirty "
                    "state retained): key=%s stage=%s qty=%.12g",
                    position_key[:48], stage, qty,
                )
            else:
                # NOT applied (or unverifiable): the exact leg row is
                # retained and replayed every cycle until PostgreSQL accepts
                # it; the local accounting delta stays uncommitted and new
                # entries are refused meanwhile (fail closed).
                legs = self._dirty_legs.setdefault(position_key, [])
                if not any(l.leg_seq == leg.leg_seq for l in legs):
                    legs.append(leg)
                self._leg_deltas[position_key] = (
                    self._leg_deltas.get(position_key, 0.0) + leg_delta
                )
                self._uncommitted_deltas[position_key] = (
                    self._uncommitted_deltas.get(position_key, 0.0)
                    + leg_delta
                )
                logger.warning(
                    "me inverse short oos2 partial TP leg persistence failed "
                    "(write NOT confirmed by PostgreSQL; exact leg retained in "
                    "memory as DIRTY for retry, NOT durable, new entries "
                    "refused): key=%s stage=%s qty=%.12g net=%.4f",
                    position_key[:48], stage, qty, net_leg,
                    exc_info=True,
                )
            return "OK"

        # ---- committed: local and store agree for this key ---------------
        # The position UPDATE carried the FULL current state: accounting for
        # this key (funding included) is confirmed.  Only OTHER dirty leg
        # ROWS of this key (their accounting already rode along) stay owed.
        remaining_dirty = [
            l for l in self._dirty_legs.get(position_key, [])
            if l.leg_seq != leg.leg_seq
        ]
        if remaining_dirty:
            self._dirty_legs[position_key] = remaining_dirty
        else:
            self._dirty_legs.pop(position_key, None)
        self._leg_deltas.pop(position_key, None)
        self._uncommitted_deltas.pop(position_key, None)
        self._dirty_funding.pop(position_key, None)
        logger.info(
            "me inverse short oos2 partial TP leg: symbol=%s stage=%s "
            "ref=%.6f fill=%.6f qty=%.12g remaining=%.12g gross=%.4f "
            "fee=%.4f slip=%.4f net=%.4f",
            position.symbol, stage, level, fill, qty,
            position.remaining_size, gross, exit_fee, exit_slippage_cost,
            net_leg,
        )
        return "OK"

    def _confirmed_leg(
        self, position: InverseShortPosition, leg: InverseShortCloseLeg,
    ) -> bool | None:
        """Verify an ambiguous partial-leg COMMIT (Issue #276).

        Returns ``True`` when PostgreSQL carries BOTH the leg row (exact
        facts) and the position state it atomically wrote, ``False`` when the
        leg row is genuinely absent, ``None`` when the outcome cannot be
        verified at all.  An unreadable outcome is NEVER treated as applied.
        """
        try:
            stored_leg = self.store.get_close_leg(
                position.position_key, leg.leg_seq,
            )
            stored_pos = self.store.get_position(position.position_key)
        except Exception:
            logger.warning(
                "me inverse short oos2 leg write outcome UNVERIFIABLE "
                "(store read failed; leg stays DIRTY, nothing double-booked): "
                "key=%s stage=%s",
                position.position_key[:48], leg.exit_reason, exc_info=True,
            )
            return None
        if stored_leg is None:
            return False
        facts_match = (
            stored_leg.exit_reason == leg.exit_reason
            and stored_leg.quantity == leg.quantity
            and stored_leg.reference_price == leg.reference_price
            and stored_leg.fill_price == leg.fill_price
            and stored_leg.gross_pnl == leg.gross_pnl
            and stored_leg.exit_fee == leg.exit_fee
            and stored_leg.net_pnl == leg.net_pnl
        )
        if not facts_match:
            logger.error(
                "me inverse short oos2 leg read-back MISMATCH (invariant "
                "violation): key=%s stage=%s",
                position.position_key[:48], leg.exit_reason,
            )
            return False
        if stored_pos is None:
            return None
        reflects = (
            stored_pos.remaining_size == position.remaining_size
            and stored_pos.realized_legs_net == position.realized_legs_net
            and bool(stored_pos.tp1_executed) == bool(position.tp1_executed)
            and bool(stored_pos.tp2_executed) == bool(position.tp2_executed)
        )
        if not reflects:
            # Single transaction: leg row present but position not updated
            # is impossible for a committed write — treat as not applied and
            # let the idempotent retry resolve it loudly.
            logger.error(
                "me inverse short oos2 leg read-back INCONSISTENT with the "
                "position row: key=%s stage=%s",
                position.position_key[:48], leg.exit_reason,
            )
            return False
        return True

    def _retry_dirty_legs(self) -> int:
        """Persist retained DIRTY partial-TP legs (Issue #276).

        Runs EVERY cycle (and before a terminal close of the same key).  Each
        replay uses the IDENTICAL leg facts and the same
        ``(position_key, leg_seq)`` identity, so a retry can never book a leg
        twice — a leg row PostgreSQL already accepted resolves as a no-op
        conflict with a read-back verification.  A successful leg commit
        writes the FULL current position state, which settles every local
        delta and dirty marker for that key at once.

        The position object is resolved from ``open_positions``, then
        ``pending_closes``, then a fresh store read (terminal-close edge: the
        key already left memory but its leg rows are still owed).
        """
        persisted = 0
        for position_key, legs in list(self._dirty_legs.items()):
            if not legs:
                self._dirty_legs.pop(position_key, None)
                continue
            position = (
                self.open_positions.get(position_key)
                or self.pending_closes.get(position_key)
            )
            if position is None:
                try:
                    position = self.store.get_position(position_key)
                except Exception:
                    logger.warning(
                        "me inverse short oos2 dirty leg retry deferred "
                        "(position row unreadable): key=%s legs=%d",
                        position_key[:48], len(legs), exc_info=True,
                    )
                    continue
                if position is None:
                    # No row at all: the leg can never attach — keep the
                    # risk visible instead of silently dropping it.
                    logger.error(
                        "me inverse short oos2 dirty leg OWNS NO POSITION ROW "
                        "(never claimed durable; risk stays reported): "
                        "key=%s legs=%d",
                        position_key[:48], len(legs),
                    )
                    continue
            try:
                for leg in legs:
                    self.store.commit_partial_exit(position, leg)
            except Exception:
                logger.debug(
                    "me inverse short oos2 dirty leg retry failed again "
                    "(legs retained): key=%s stages=%s",
                    position_key[:48],
                    [l.exit_reason for l in legs],
                    exc_info=True,
                )
                continue
            self._dirty_legs.pop(position_key, None)
            self._leg_deltas.pop(position_key, None)
            self._uncommitted_deltas.pop(position_key, None)
            # The successful write carried the current funding facts too.
            self._dirty_funding.pop(position_key, None)
            persisted += 1
            logger.info(
                "me inverse short oos2 dirty leg(s) persisted on retry: "
                "key=%s stages=%s",
                position_key[:48], [l.exit_reason for l in legs],
            )
        return persisted

    def _close(
        self,
        position: InverseShortPosition,
        exit_reference_price: float,
        reason: str,
    ) -> InverseShortPosition:
        """TERMINAL close of the REMAINING quantity (Issue #276).

        PnL model (cumulative, exact against the balance ledger): every
        committed TP leg already booked its own
        ``(gross − exit fee − exit slippage)``; the terminal close folds the
        remaining quantity's components into the SAME cumulative columns and
        computes

        ``net_pnl = Σ gross − entry_fee − Σ exit_fee − funding − (entry_slippage + Σ exit_slippage)``

        — exactly the in-process balance delta since entry, so a restart's
        ``balance_components()`` reconstruction and the in-process balance
        always agree and legs, fees or funding can never be counted twice.
        """
        if reason not in CONTOUR_EXIT_REASONS:
            raise ValueError(f"INVALID_EXIT_REASON:{reason}")

        # Issue #276: flush DIRTY partial legs of THIS position first, so
        # the terminal write can never carry leg state whose leg rows are
        # missing from the durable leg ledger.  Best effort only — a
        # protective exit is NEVER blocked by a leg-retry failure (the
        # residual risk stays reported and blocks new entries).
        if position.position_key in self._dirty_legs:
            self._retry_dirty_legs()

        closed_at = self._clock()
        slip = float(self.settings.slippage_percent)
        # SHORT cover: adverse slippage moves the fill UP.
        fill = exit_reference_price * (1.0 + slip)

        qty = position.remaining_size  # TERMINAL: the whole remainder
        gross = (
            position.entry_market_price - exit_reference_price
        ) * qty
        exit_fee = fill * qty * float(self.settings.taker_fee)
        exit_slippage_cost = (
            fill - exit_reference_price
        ) * qty
        # Cumulative totals: committed TP legs + this terminal leg.
        # ``position.slippage_cost`` holds the Σ of PARTIAL-leg exit
        # slippage while OPEN; the terminal write folds in the entry
        # slippage and the terminal exit slippage exactly once.
        gross_total = position.gross_pnl + gross
        exit_fee_total = position.exit_fee + exit_fee
        slippage_total = (
            position.slippage_cost + position.entry_slippage_cost
            + exit_slippage_cost
        )
        net_pnl = (
            gross_total
            - position.entry_fee
            - exit_fee_total
            - position.funding_paid
            - slippage_total
        )
        terminal_delta = gross - exit_fee - exit_slippage_cost

        # The terminal leg takes the EXACT remainder of the entry-cost
        # allocation, so Σ leg entry_cost_share == entry_fee + entry_slippage
        # by construction (deterministic, rounded HALF-EVEN).
        entry_cost_before = position.entry_cost_allocated
        entry_cost_remainder = round(
            (position.entry_fee + position.entry_slippage_cost)
            - entry_cost_before,
            QTY_DECIMALS,
        )
        terminal_leg = InverseShortCloseLeg(
            position_key=position.position_key,
            # A terminal leg is ALWAYS seq=3 — identity of the position's
            # final close regardless of its reason (TP1/TP2 partials own
            # seq 1 and 2).
            leg_seq=LEG_SEQ_TERMINAL,
            exit_reason=reason,
            is_terminal=True,
            quantity=qty,
            remaining_after=0.0,
            reference_price=exit_reference_price,
            fill_price=fill,
            gross_pnl=gross,
            exit_fee=exit_fee,
            exit_slippage=exit_slippage_cost,
            entry_cost_share=entry_cost_remainder,
            funding_paid_to_date=position.funding_paid,
            net_pnl=terminal_delta - entry_cost_remainder,
            cumulative_realized_net=position.realized_legs_net + terminal_delta,
            executed_at=closed_at,
            status_after=POSITION_STATUS_CLOSED,
            event_key=position.event_key,
            symbol=position.symbol,
        )
        # Retained until the staging+UPDATE are confirmed: a flush retry
        # re-stages the outbox row AND the terminal leg row together.
        self._terminal_legs[position.position_key] = terminal_leg

        self.balance += terminal_delta

        position.status = POSITION_STATUS_CLOSED
        position.remaining_size = 0.0
        if reason == "TAKE_PROFIT_3":
            position.tp3_executed = True
        position.exit_price = fill
        position.exit_reason = reason
        position.exit_fee = exit_fee_total
        position.slippage_cost = slippage_total
        position.gross_pnl = gross_total
        position.realized_legs_net = position.realized_legs_net + terminal_delta
        position.entry_cost_allocated = (
            position.entry_fee + position.entry_slippage_cost
        )
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
                "me inverse short oos2 close executed with %d UNSETTLED funding "
                "period(s) (rate UNKNOWN at exit): obligation preserved on "
                "the row and reported, NOT booked as zero; exit facts frozen: "
                "key=%s reason=%s closed_at=%s",
                unsettled, position.position_key[:48], reason,
                closed_at.isoformat(),
            )

        # ---- durable close protocol (P1 / P1-3, Issue #276 legs) ----
        # 1) Stage the EXACT terminal snapshot AND the terminal leg row
        #    durably FIRST — one idempotent transaction (outbox upsert +
        #    leg upsert on (position_key, leg_seq)): the exit facts survive
        #    a crash even if the status UPDATE below fails, and the leg
        #    ledger can never lag the position flip.  When staging fails the
        #    key is marked UNSTAGED: flush_pending_closes() retries the
        #    staging BEFORE any terminal UPDATE on subsequent cycles,
        #    entries fail closed, and the unresolved durability risk is
        #    reported every cycle (recovery is never claimed for a snapshot
        #    PostgreSQL never accepted).
        staged = False
        staging_degraded = False
        try:
            staged = bool(self.store.stage_close(position, terminal_leg))
        except Exception:
            logger.warning(
                "me inverse short oos2 close staging failed (in-process snapshot "
                "retained; outbox+leg staging will be retried before the terminal "
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
                # stage_close() returns False only when the outbox/leg tables
                # do not exist (migration 088 not applied): staging can never
                # succeed, so retries are skipped — the terminal UPDATE below
                # is the only durability left in this documented mode.
                self._staging_unavailable.add(position.position_key)
        # 2) Flip the row to CLOSED.  On failure the terminal snapshot is
        #    retained and replayed byte-for-byte (same position_key, closed_at,
        #    exit_reason, exit_price, PnL and funding) by flush_pending_closes().
        #    close_delta is EXACTLY the amount just booked into the memory
        #    ledger; it becomes a local uncommitted delta until the store
        #    ledger commits it (never booked twice).
        close_delta = terminal_delta
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
                    "me inverse short oos2 terminal close CONFIRMED applied "
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
                    "me inverse short oos2 close persistence failed (write NOT "
                    "confirmed by PostgreSQL); terminal snapshot retained "
                    "for retry and new entries blocked until a "
                    "reconciliation succeeds (outbox staged=%s, "
                    "durability_risk=%s): key=%s reason=%s net=%.4f",
                    staged, not staged, position.position_key[:48], reason,
                    net_pnl,
                )
        if terminal_applied:
            # The row now carries the FULL terminal accounting (cumulative
            # legs + funding + close facts): local and store agree for this
            # key — every local delta (funding AND leg components) settles.
            self._uncommitted_deltas.pop(position.position_key, None)
            self._leg_deltas.pop(position.position_key, None)
            # The terminal write also commits any DIRTY funding snapshot
            # (funding_paid rides along in the same row update).
            self._dirty_funding.pop(position.position_key, None)
            # Any DIRTY partial legs of this key are retried against the
            # now-committed row: their accounting effects are already inside
            # the terminal write, so only the missing LEG ROWS are inserted
            # (idempotent on (position_key, leg_seq)) — the leg ledger can
            # never lag the position flip.
            if position.position_key in self._dirty_legs:
                self._retry_dirty_legs()
            self._unstaged_closes.discard(position.position_key)
            self._staging_unavailable.discard(position.position_key)
            self._terminal_legs.pop(position.position_key, None)
            if staged:
                try:
                    self.store.clear_pending_close(position.position_key)
                except Exception:
                    # A lingering outbox row replays the IDENTICAL snapshot on
                    # the next restart — idempotent, never changes the numbers.
                    logger.debug(
                        "me inverse short oos2 close outbox clear failed "
                        "(idempotent replay active): key=%s",
                        position.position_key[:48],
                        exc_info=True,
                    )

        logger.info(
            "me inverse short oos2 exit: symbol=%s reason=%s ref=%.6f fill=%.6f "
            "qty=%.12g gross=%.4f fees=%.4f slip=%.4f funding=%.4f net=%.4f "
            "legs=%s remaining=0",
            position.symbol, reason, exit_reference_price, fill,
            qty, gross_total, position.entry_fee + exit_fee_total,
            slippage_total, position.funding_paid, net_pnl,
            position.executed_stages,
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
                    terminal_leg = self._terminal_legs.get(position_key)
                    if terminal_leg is None:
                        # The exact leg facts were never retained in-process
                        # (only reachable when staging NEVER succeeded in a
                        # prior process — the outbox row the snapshot came
                        # from already carried its leg row atomically).
                        logger.error(
                            "me inverse short oos2 re-stage impossible: terminal "
                            "leg facts unavailable in-process (degraded mode, "
                            "documented): key=%s",
                            position_key[:48],
                        )
                        self._staging_unavailable.add(position_key)
                    else:
                        try:
                            if bool(self.store.stage_close(snapshot, terminal_leg)):
                                self._unstaged_closes.discard(position_key)
                            else:
                                # Outbox/leg table absent (migration 088):
                                # staging can never succeed — fall through
                                # to the UPDATE.
                                self._staging_unavailable.add(position_key)
                        except Exception:
                            logger.warning(
                                "me inverse short oos2 close staging retry failed; "
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
                        "me inverse short oos2 close flush CONFIRMED applied "
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
                        "me inverse short oos2 close flush failed (terminal "
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
            self._leg_deltas.pop(position_key, None)
            # The replayed terminal snapshot includes the cumulative funding,
            # so any DIRTY funding marker for this key is now confirmed too.
            self._dirty_funding.pop(position_key, None)
            # Dirty partial-leg ROWS of this key are retried against the now
            # committed row (their accounting already rode along with the
            # terminal write): the leg ledger can never lag the position flip.
            if position_key in self._dirty_legs:
                self._retry_dirty_legs()
            self._unstaged_closes.discard(position_key)
            self._staging_unavailable.discard(position_key)
            self._terminal_legs.pop(position_key, None)
            flushed += 1
            try:
                self.store.clear_pending_close(position_key)
            except Exception:
                # The outbox row (if any) survives: the next restart replays
                # the IDENTICAL snapshot — idempotent, numbers unchanged.
                logger.debug(
                    "me inverse short oos2 close outbox clear failed (idempotent "
                    "replay active): key=%s",
                    position_key[:48],
                    exc_info=True,
                )
            logger.info(
                "me inverse short oos2 close flushed: key=%s reason=%s closed_at=%s "
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
                "me inverse short oos2 UNRESOLVED close durability risk: %d terminal "
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
                    "me inverse short oos2 balance recovery reconciled for %d close(s)",
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
                "me inverse short oos2 balance reconciliation failed (memory ledger "
                "kept; balance-recovery marker retained, new entries blocked "
                "until a resync succeeds)",
                exc_info=True,
            )
            return False
        self.confirmed_balance = confirmed
        restored = confirmed + sum(self._uncommitted_deltas.values())
        if restored != self.balance:
            logger.info(
                "me inverse short oos2 balance resync: $%.4f -> $%.4f "
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
            raw = prices.get(position.symbol)
            if raw is None:
                continue
            price = float(raw)
            # Persist the mark so the dashboard can compute UNREALIZED PnL
            # from the position row alone (Issue #276): the UPDATE itself is
            # written by persist_marks() at the end of the cycle.
            if position.last_mark_price != price:
                position.last_mark_price = price
                self._marks_dirty.add(position.position_key)
            move = position.entry_market_price - price  # SHORT
            unrealized += move * position.remaining_size
        return {
            "balance": self.balance,
            "unrealized_pnl": unrealized,
            "equity": self.balance + unrealized,
        }

    def persist_marks(self) -> int:
        """Write the last mark price of every open position (once/cycle).

        Non-fatal by contract: a failed write keeps the key dirty and is
        retried next cycle; a failure never aborts the cycle's exits or
        entries (the mark is dashboard telemetry, not a trading decision).
        Returns the number of positions whose mark was persisted.
        """
        persisted = 0
        for key in list(self._marks_dirty):
            position = self.open_positions.get(key)
            if position is None:
                # The position closed this cycle — its terminal UPDATE
                # already carried the final columns; nothing left to mark.
                self._marks_dirty.discard(key)
                continue
            try:
                self.store.update_position(position)
            except Exception:
                logger.warning(
                    "me inverse short oos2 mark persistence failed (retry next "
                    "cycle): key=%s",
                    key[:48], exc_info=True,
                )
                continue
            self._marks_dirty.discard(key)
            persisted += 1
        return persisted

    def snapshot(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        account = self.mark_to_market(prices)
        summary = self.store.net_pnl_summary()
        # Exposure counts only what is still OPEN (Issue #276): a partially
        # closed position contributes its REMAINING quantity only.
        gross_exposure = sum(
            p.entry_price * p.remaining_size
            for p in self.open_positions.values()
        )
        equity = account["equity"]
        total_pnl = equity - self.initial_balance
        return {
            "strategy_id": "ME_LONG_V2_INVERSE_SHORT_OOS2_V1",
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
            # OOS-2 LOCAL capacity (Issue #276): x of 12 — never the global
            # Settings.max_open_positions.
            "capacity_limit": MAX_OPEN_POSITIONS,
            "capacity_used_pct": round(
                (len(self.open_positions) / MAX_OPEN_POSITIONS * 100.0), 4,
            ),
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
            # DIRTY partial-TP leg rows (Issue #276): applied in memory,
            # not yet in the durable leg ledger — retried every cycle, never
            # claimed durable, entries refused meanwhile.
            "dirty_legs": len(self._dirty_legs),
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
                    # OOS-2 ladder: TP1 (= target), TP2, TP3 levels and the
                    # live partial-exit progress (Issue #276).
                    "target": p.target_price,
                    "tp1": p.target_price,
                    "tp2": p.tp2_price,
                    "tp3": p.tp3_price,
                    "position_size": p.position_size,
                    "remaining_size": p.remaining_size,
                    "stages_executed": list(p.executed_stages),
                    "realized_legs_net": round(p.realized_legs_net, 8),
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
