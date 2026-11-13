"""Contour orchestration for ME_LONG_V2_INVERSE_SHORT_OOS2_V1.

``InverseShortContour`` runs ONE cycle of the independent paper contour:

    resolve activation (fail closed) → exits first (STOP → TP → TIMEOUT)
    → prospective entries at the first observed market price.

``run_dry_run`` is the pre-activation validation path required by the
readiness criteria: it exercises the COMPLETE real code path (activation
evaluation, capture, entry sizing, fees/slippage/funding, all three exit
types, Net PnL accounting) against an in-memory store and a scripted clock —
no database, no production state, no activation fixation required.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.config import Settings
from app.scanners.models import SetupCandidate, SetupState

from .activation import ActivationIdentity, resolve_activation
from .capture import event_from_candidate
from .engine import InverseShortPaperEngine
from .models import ActivationRecord, InverseShortEvent
from .protocol import (
    EXPERIMENT_ID,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_SCANNER,
    protocol_fingerprint,
)
from .store import InMemoryInverseShortStore, InverseShortStore

logger = logging.getLogger(__name__)

DRY_RUN_SYMBOLS = {
    "PARTIAL_TPS": "DRYTPUSDT",
    "STOP_LOSS": "DRYSLUSDT",
    "TIMEOUT": "DRYTOUSDT",
}

#: Expected TERMINAL exit reason per dry-run scenario (Issue #276: the TP
#: scenario walks the whole partial ladder and completes at TP3).
DRY_RUN_EXPECTED_EXIT = {
    "PARTIAL_TPS": "TAKE_PROFIT_3",
    "STOP_LOSS": "STOP_LOSS",
    "TIMEOUT": "TIMEOUT",
}


class InverseShortContour:
    """One running instance of the independent prospective paper contour."""

    def __init__(
        self,
        settings: Settings,
        store: InverseShortStore,
        *,
        clock: Callable[[], datetime] | None = None,
        require_activation: bool = True,
        activation_enabled: bool | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.require_activation = require_activation
        # None → runtime feature flag decides; dry-run passes True.
        self._activation_enabled = activation_enabled

        self.activation, self.activation_reason = resolve_activation(
            store,
            enabled=(
                True if not require_activation else self._activation_enabled
            ),
        )
        self.engine = InverseShortPaperEngine(
            settings, store, clock=self._clock,
            activation=self.activation, require_activation=require_activation,
        )
        logger.info(
            "me inverse short oos2 contour constructed: activated=%s mode=%s "
            "reason=%s open_positions=%d balance=%.2f",
            self.activated, self.activation_mode, self.activation_reason,
            len(self.engine.open_positions), self.engine.balance,
        )

    # ------------------------------------------------------------------
    @property
    def activated(self) -> bool:
        if not self.require_activation:
            return True
        return self.activation is not None

    @property
    def exit_only(self) -> bool:
        """True while DRAINING: MANAGE_EXISTING only, no NEW_ENTRIES.

        A restart that reads a DRAINING row with a valid identity lands
        directly in this mode — the contour never has to pass RUNNING again
        (review P2-2).
        """
        return self.activation is not None and self.activation.exit_only

    @property
    def activation_mode(self) -> str:
        """``FULL`` (RUNNING) / ``EXIT_ONLY`` (DRAINING) / ``NONE`` (no identity)."""
        if self.activation is None:
            return "NONE"
        return self.activation.mode

    def refresh_activation(self) -> bool:
        """Re-read the activation row so a runtime change takes effect."""
        if not self.require_activation:
            return True
        self.activation, self.activation_reason = resolve_activation(
            self.store, enabled=self._activation_enabled,
        )
        self.engine.activation = self.activation
        return self.activated

    # ------------------------------------------------------------------
    def run_cycle(
        self,
        prices: dict[str, float],
        funding_rates_percent: dict[str, float | None] | None = None,
        bars: dict[str, dict[str, float]] | None = None,
    ) -> dict[str, Any]:
        """Exits first, then prospective entries.  Never raises on activation."""
        self.refresh_activation()

        # MANAGE_EXISTING always runs (STOP/TP/TIMEOUT, funding
        # reconciliation, outbox replay, pending-close recovery) — including
        # EXIT_ONLY (DRAINING) and fail-closed states, so an open position
        # is never stranded.
        closed = self.engine.check_exits(prices, funding_rates_percent, bars)
        opened: list[Any] = []
        if self.activated and not self.exit_only:
            opened = self.engine.open_entries(prices)
        elif self.exit_only:
            # NEW_ENTRIES refused: no new source capture, no new entries and
            # no reuse of CAPTURED events (the engine gate refuses too —
            # defence in depth).
            logger.debug(
                "me inverse short oos2 contour EXIT_ONLY: entries skipped, "
                "managing %d existing position(s)",
                len(self.engine.open_positions),
            )
        else:
            logger.debug(
                "me inverse short oos2 contour dormant: %s", self.activation_reason,
            )

        account = self.engine.mark_to_market(prices)
        # Persist the mark for the dashboard's UNREALIZED PnL panel (Issue
        # #276).  Telemetry only: never fatal for the cycle.
        try:
            self.engine.persist_marks()
        except Exception:  # pragma: no cover - persist_marks is non-fatal
            logger.warning(
                "me inverse short oos2 mark persistence unavailable",
                exc_info=True,
            )
        # Review P1: a pending-events store failure must never abort the
        # cycle result AFTER the exits ran — report it honestly instead.
        # EXIT_ONLY (DRAINING) does not request prospective entry events at
        # all, so no store read happens for them in that mode.
        if self.exit_only:
            pending_events_count = 0
        else:
            try:
                pending_events_count = len(self.engine.pending_events())
            except Exception:
                pending_events_count = -1
                logger.warning(
                    "me inverse short oos2 pending entry events unavailable (store "
                    "read failed); entries fail closed, exits of this cycle "
                    "already ran and are unaffected", exc_info=True,
                )
        return {
            "activated": self.activated,
            "activation_reason": self.activation_reason,
            "exit_only": self.exit_only,
            "entries": len(opened),
            "exits": len(closed),
            "exit_reasons": [p.exit_reason for p in closed],
            "opened": opened,
            "closed": closed,
            "balance": account["balance"],
            "equity": account["equity"],
            "pending_events": pending_events_count,
            "pending_events_unavailable": pending_events_count < 0,
            "open_positions": len(self.engine.open_positions),
            "pending_closes": len(self.engine.pending_closes),
            # Fail-closed state (P1 durability risk / P2 accounting trust).
            "durability_risk": self.engine.durability_risk,
            "accounting_consistent": self.engine.accounting_consistent,
        }


# ---------------------------------------------------------------------------
# Dry-run (pre-activation readiness validation)
# ---------------------------------------------------------------------------

@dataclass
class _ScriptedClock:
    """Deterministic clock for dry-run + tests (timezone-aware)."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


def dry_run_activation() -> ActivationRecord:
    """In-memory activation identity used ONLY by the dry-run store."""
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        protocol_fingerprint=protocol_fingerprint(),
        freeze_ts=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )


def _dry_run_candidate(
    symbol: str, price: float, detected_at: datetime,
) -> SetupCandidate:
    """Synthetic ORIGINAL OOS2 source event (LONG setup — the contour inverts it)."""
    open_ms = int(detected_at.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name=SOURCE_SCANNER,
        scanner_version="1.0.0",
        symbol=symbol,
        direction="LONG",
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        detected_at=detected_at,
        setup_started_at=detected_at,
        signal_candle_open_time=open_ms,
        reference_price=price,
        entry_zone_low=price * 0.998,
        entry_zone_high=price * 1.001,
        invalidation_price=price * 0.975,
        target_1=price * 1.03,
        score=45.0,
        market_regime="HIGH_VOLATILITY",
        state=SetupState.SETUP_READY,
        features={
            "exhaustion_magnitude": 0.01,
            "rsi_14": 70.0,
            "rsi_delta_3": 2.0,
            "entry_confirmation_rsi_rising": True,
        },
    )


def run_dry_run(
    settings: Settings,
    *,
    scenarios: tuple[str, ...] = ("PARTIAL_TPS", "STOP_LOSS", "TIMEOUT"),
    funding_rate_percent: float = 0.01,
) -> dict[str, Any]:
    """Exercise the complete contour without activation, DB or live data.

    Three synthetic source events are captured through the REAL capture path,
    entered SHORT at the first observed price and driven to one terminal exit
    each:

    * ``PARTIAL_TPS`` — the Issue #276 ladder demonstrated step by step:
      price 100 → 94.5 (TP1 leg: 50% closed, position stays OPEN) →
      92.5 (TP2 leg: 75% of the remainder, 12.5% original left) →
      89 (TP3 terminal: the last 12.5%) — with the leg quantities
      50 / 37.5 / 12.5 reported for a base size of 100;
    * ``STOP_LOSS`` — intrabar stop touch with the midpoint still below the
      stop → clean STOP_LOSS of the full remaining quantity (STOP_FIRST);
    * ``TIMEOUT`` — 128h pass the immutable 7200-minute deadline, with one
      funding settlement modelled (funding accrues on the remaining size).
    """
    clock = _ScriptedClock(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    store = InMemoryInverseShortStore(
        dry_run_activation(), initial_balance=settings.initial_balance,
    )
    contour = InverseShortContour(
        settings, store, clock=clock, activation_enabled=True,
    )
    if not contour.activated:
        return {"ok": False, "stage": "activation", "reason": contour.activation_reason}

    activation = ActivationIdentity(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        protocol_fingerprint=protocol_fingerprint(),
        freeze_ts=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    base_price = 100.0
    prices: dict[str, float] = {}
    capture_statuses: dict[str, str] = {}
    for scenario in scenarios:
        symbol = DRY_RUN_SYMBOLS.get(scenario, f"DRY{scenario[:6]}USDT")
        prices[symbol] = base_price
        candidate = _dry_run_candidate(symbol, base_price, clock())
        try:
            event: InverseShortEvent = event_from_candidate(
                candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
                activation=activation, captured_at=clock(),
            )
        except ValueError as exc:
            return {"ok": False, "stage": "capture", "reason": str(exc)}
        capture_statuses[symbol] = store.save_event(event)

    # 1) First observed price → prospective SHORT entries.
    first_cycle = contour.run_cycle(dict(prices))
    if first_cycle["entries"] != len(scenarios):
        return {
            "ok": False, "stage": "entry",
            "reason": (
                f"expected {len(scenarios)} entries, opened {first_cycle['entries']}"
            ),
            "capture": capture_statuses,
            "cycle": {
                k: v for k, v in first_cycle.items()
                if k not in ("opened", "closed")
            },
        }

    # 2) Drive each leg to its terminal exit (exits run before entries).
    tp_symbol = DRY_RUN_SYMBOLS["PARTIAL_TPS"]
    sl_symbol = DRY_RUN_SYMBOLS["STOP_LOSS"]
    to_symbol = DRY_RUN_SYMBOLS["TIMEOUT"]

    bars: dict[str, dict[str, float]] = {}
    exit_report: list[dict[str, Any]] = []
    ladder_report: list[dict[str, Any]] = []

    if "PARTIAL_TPS" in scenarios:
        # Stage 1: touch ONLY TP1 (between 0.95 and 0.93 of entry) — a
        # PARTIAL leg fires and the position stays OPEN with 50% remaining.
        for stage_name, price_factor in (
            ("TAKE_PROFIT_1", 0.945),
            ("TAKE_PROFIT_2", 0.925),
        ):
            prices[tp_symbol] = base_price * price_factor
            result = contour.run_cycle(dict(prices))
            open_tp = contour.engine.open_positions.get(
                next(
                    (k for k, p in contour.engine.open_positions.items()
                     if p.symbol == tp_symbol),
                    "",
                )
            )
            ladder_report.append({
                "stage": stage_name,
                "price": prices[tp_symbol],
                "position_open": open_tp is not None,
                "remaining_size": (
                    open_tp.remaining_size if open_tp is not None else 0.0
                ),
                "stages_executed": list(open_tp.executed_stages)
                if open_tp is not None else [],
                "exit_reasons": result.get("exit_reasons", []),
            })
        # Stage 3: below TP3 → the remaining 12.5% closes TERMINALLY.
        prices[tp_symbol] = base_price * 0.89
        result = contour.run_cycle(dict(prices))
        exit_report.append({"scenario": "PARTIAL_TPS", **{
            k: v for k, v in result.items()
            if k in ("exit_reasons", "balance")
        }})

    if "STOP_LOSS" in scenarios:
        # Intradabar stop touch: high >= stop while the midpoint is still
        # below the stop → clean STOP_LOSS (no gap), STOP_FIRST policy.
        open_sl = next(
            (p for p in contour.engine.open_positions.values() if p.symbol == sl_symbol),
            None,
        )
        if open_sl is not None:
            prices[sl_symbol] = base_price
            bars[sl_symbol] = {
                "high": open_sl.stop_price * 1.001,
                "low": base_price * 0.995,
            }
        result = contour.run_cycle(dict(prices), bars=bars)
        exit_report.append({"scenario": "STOP_LOSS", **{
            k: v for k, v in result.items()
            if k in ("exit_reasons", "balance")
        }})

    if "TIMEOUT" in scenarios:
        # 128h > the immutable 7200-minute deadline; 16 whole funding periods.
        clock.advance(hours=128)
        result = contour.run_cycle(
            dict(prices), funding_rates_percent={
                to_symbol: funding_rate_percent,
            },
        )
        exit_report.append({"scenario": "TIMEOUT", **{
            k: v for k, v in result.items()
            if k in ("exit_reasons", "balance")
        }})

    closed = contour.engine.closed_positions()
    legs = store.list_close_legs()
    snapshot = contour.engine.snapshot(dict(prices))
    expected_exits = sorted(
        DRY_RUN_EXPECTED_EXIT[s] for s in scenarios
        if s in DRY_RUN_EXPECTED_EXIT
    )
    ok = sorted(p.exit_reason for p in closed) == expected_exits
    return {
        "ok": ok,
        "capture": capture_statuses,
        "entries": first_cycle["entries"],
        "closed": [
            {
                "symbol": p.symbol,
                "exit_reason": p.exit_reason,
                "entry_market": round(p.entry_market_price, 6),
                "stop": round(p.stop_price, 6),
                "tp1": round(p.target_price, 6),
                "tp2": round(p.tp2_price, 6),
                "tp3": round(p.tp3_price, 6),
                "position_size": p.position_size,
                "remaining_size": p.remaining_size,
                "stages_executed": list(p.executed_stages),
                "funding_paid": round(p.funding_paid, 6),
                "net_pnl": round(p.net_pnl, 4),
            }
            for p in sorted(closed, key=lambda p: p.symbol)
        ],
        # Issue #276 evidence: every executed leg (partials + terminal) with
        # its quantity — the ladder closes 50% / 37.5% / 12.5% of original.
        "legs": [
            {
                "position_key": leg.position_key[:32],
                "exit_reason": leg.exit_reason,
                "is_terminal": leg.is_terminal,
                "quantity": leg.quantity,
                "remaining_after": leg.remaining_after,
                "fill_price": round(leg.fill_price, 6),
                "fee": round(leg.exit_fee, 6),
                "net_pnl": round(leg.net_pnl, 4),
            }
            for leg in sorted(
                legs, key=lambda l: (l.position_key, l.leg_seq),
            )
        ],
        "ladder": ladder_report,
        "exit_cycles": exit_report,
        "snapshot": snapshot,
        "net_pnl": snapshot["net_pnl"],
        "immutability_repairs": getattr(store, "immutability_repairs", 0),
    }
