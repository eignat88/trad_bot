"""Contour orchestration for ME_LONG_V2_INVERSE_SHORT_V1.

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
    "TAKE_PROFIT_1": "DRYTPUSDT",
    "STOP_LOSS": "DRYSLUSDT",
    "TIMEOUT": "DRYTOUSDT",
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
            "me inverse short contour constructed: activated=%s reason=%s "
            "open_positions=%d balance=%.2f",
            self.activated, self.activation_reason,
            len(self.engine.open_positions), self.engine.balance,
        )

    # ------------------------------------------------------------------
    @property
    def activated(self) -> bool:
        if not self.require_activation:
            return True
        return self.activation is not None

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
        funding_rates_percent: dict[str, float] | None = None,
        bars: dict[str, dict[str, float]] | None = None,
    ) -> dict[str, Any]:
        """Exits first, then prospective entries.  Never raises on activation."""
        self.refresh_activation()

        closed = self.engine.check_exits(prices, funding_rates_percent, bars)
        opened: list[Any] = []
        if self.activated:
            opened = self.engine.open_entries(prices)
        else:
            logger.debug(
                "me inverse short contour dormant: %s", self.activation_reason,
            )

        account = self.engine.mark_to_market(prices)
        return {
            "activated": self.activated,
            "activation_reason": self.activation_reason,
            "entries": len(opened),
            "exits": len(closed),
            "exit_reasons": [p.exit_reason for p in closed],
            "opened": opened,
            "closed": closed,
            "balance": account["balance"],
            "equity": account["equity"],
            "pending_events": len(self.engine.pending_events()),
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
    """Synthetic ORIGINAL V2 source event (LONG setup — the contour inverts it)."""
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
    scenarios: tuple[str, ...] = ("TAKE_PROFIT_1", "STOP_LOSS", "TIMEOUT"),
    funding_rate_percent: float = 0.01,
) -> dict[str, Any]:
    """Exercise the complete contour without activation, DB or live data.

    Three synthetic source events are captured through the REAL capture path,
    entered SHORT at the first observed price and driven to one terminal exit
    each — TP (price falls into target), SL (intrabar stop touch with the
    midpoint still below the stop → clean STOP_LOSS) and TIMEOUT (128h pass
    the immutable 7200-minute deadline, with one funding settlement modelled).
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
    tp_symbol = DRY_RUN_SYMBOLS["TAKE_PROFIT_1"]
    sl_symbol = DRY_RUN_SYMBOLS["STOP_LOSS"]
    to_symbol = DRY_RUN_SYMBOLS["TIMEOUT"]

    bars: dict[str, dict[str, float]] = {}
    exit_report: list[dict[str, Any]] = []

    if "TAKE_PROFIT_1" in scenarios:
        # SHORT wins when price falls THROUGH the 10% target.
        prices[tp_symbol] = base_price * 0.89
        result = contour.run_cycle(dict(prices))
        exit_report.append({"scenario": "TAKE_PROFIT_1", **{
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
    snapshot = contour.engine.snapshot(dict(prices))
    ok = sorted(p.exit_reason for p in closed) == sorted(set(scenarios))
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
                "target": round(p.target_price, 6),
                "funding_paid": round(p.funding_paid, 6),
                "net_pnl": round(p.net_pnl, 4),
            }
            for p in sorted(closed, key=lambda p: p.symbol)
        ],
        "exit_cycles": exit_report,
        "snapshot": snapshot,
        "net_pnl": snapshot["net_pnl"],
        "immutability_repairs": getattr(store, "immutability_repairs", 0),
    }
