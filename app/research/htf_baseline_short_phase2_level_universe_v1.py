"""Issue #176 Phase-2 unified causal resistance level universe (Blocker B3).

Implements the single frozen-contract candidate for SHORT key-resistance level
formation, identity and touch semantics. Selection semantics deliberately mirror
the audited research adapter (strict 4/4 swing, contiguous tied-plateau collapse
P2-07, independent same-price swings P2-11, touch count strictly before
formation) so that no behavior change is hidden; the following audit defects are
fixed here without touching the frozen Point-B module:

* D1 — strict 5m series validation at the universe boundary;
* D2 — explicit 1H coverage accounting instead of silent hour drops;
* R3 — touch-count tolerance is ATR-normalized at the DECISION bar
  (identical to the adapter contract; the fallback-ATR call pattern of the old
  observer is not reproduced);
* symbol-scoped level identity (a level on two symbols can never collide).

Level identity is content-anchored and independent of any rolling window:
sha256(symbol|direction|formation_close_ms|available_ms|price). The legacy
adapter-compatible id is retained as ``legacy_level_id`` for parity auditing.

Pure functions only: no I/O, no network, no database.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Sequence

from app.models import Candle
from app.research.htf_keylevel_point_b import (
    KEY_LEVEL_MIN_TOUCHES,
    KEY_LEVEL_TOUCH_TOLERANCE_ATR,
    LEVEL_TOUCH_COUNT_TOLERANCE_ATR,
    _causal_atr_series,
)
from app.research.htf_baseline_short_phase2_contracts_v1 import (
    HTF_MS,
    REASON_INCOMPLETE_1H_COVERAGE,
    ContractViolation,
    HtfCoverage,
    aggregate_closed_htf,
    htf_coverage_from_htf_series,
    validate_execution_series,
    validate_htf_series,
)

SWING_LEFT = 4
SWING_RIGHT = 4


@dataclass(frozen=True)
class LevelRecord:
    level_id: str
    legacy_level_id: str
    symbol: str
    direction: str
    price: float
    formation_close_ms: int
    available_ms: int
    touch_count: int
    eligible_at_decision: bool


@dataclass(frozen=True)
class UniverseDiagnostics:
    decision_asof_ms: int
    reference_price: float
    decision_atr: float
    htf_source: str
    coverage_clean: bool
    partial_hours: tuple[int, ...]
    missing_hours: tuple[int, ...]
    formed_levels: int
    eligible_levels: int


def level_id_for(
    symbol: str, direction: str, formation_close_ms: int, available_ms: int, price: float
) -> str:
    """Content-anchored, window-independent level identity."""
    raw = f"{symbol}|{direction}|{formation_close_ms}|{available_ms}|{price:.12g}"
    return sha256(raw.encode("utf-8")).hexdigest()


def legacy_level_id_for(direction: str, formation_close_ms: int, available_ms: int, price: float) -> str:
    """Adapter-compatible id (no symbol), kept for parity auditing only."""
    raw = f"{direction}|{formation_close_ms}|{available_ms}|{price:.12g}"
    return sha256(raw.encode("utf-8")).hexdigest()


def is_touch(bar: Candle, price: float, available_ms: int, atr: float) -> bool:
    """Frozen touch predicate (KEY_LEVEL_TOUCH_TOLERANCE_ATR, availability gate)."""
    if atr <= 0 or int(bar.timestamp) < available_ms:
        return False
    return (
        float(bar.high) >= price
        and min(
            abs((float(bar.low) - price) / atr),
            abs((price - float(bar.high)) / atr),
        )
        <= KEY_LEVEL_TOUCH_TOLERANCE_ATR
    )


def build_resistance_universe(
    htf_closed: Sequence[Candle],
    exec_closed: Sequence[Candle],
    asof_ms: int,
    *,
    symbol: str,
    direction: str = "SHORT",
    require_clean_coverage: bool = True,
    htf_source_label: str | None = None,
) -> tuple[tuple[LevelRecord, ...], UniverseDiagnostics]:
    """Build the causal resistance universe for one decision time.

    Fail-closed on any 5m contract violation (D1). 1H coverage is accounted for
    explicitly and surfaced in the diagnostics (D2); hours that are partial or
    missing are never used to form levels and never silently ignored.

    ``require_clean_coverage`` (default True) makes any partial/missing hour in
    the decision window a hard ``ContractViolation(REASON_INCOMPLETE_1H_COVERAGE)``
    — swing geometry must never silently stretch across a gap. Callers that
    intentionally evaluate gap diagnostics pass False and handle coverage
    themselves.
    """
    if direction != "SHORT":
        raise ValueError("phase-2 universe is SHORT-only")
    validate_execution_series(exec_closed, asof_ms)
    htf_source = htf_source_label or "AGGREGATED_5M"
    coverage = HtfCoverage()
    if htf_closed:
        validate_htf_series(htf_closed, asof_ms)
        if htf_source_label is None:
            htf_source = "PROVIDER_1H"
        coverage = htf_coverage_from_htf_series(htf_closed, asof_ms)
    if require_clean_coverage and (coverage.partial_hours or coverage.missing_hours):
        raise ContractViolation(
            REASON_INCOMPLETE_1H_COVERAGE,
            f"partial={len(coverage.partial_hours)} missing={len(coverage.missing_hours)}",
        )
    if not htf_closed or len(htf_closed) < SWING_LEFT + SWING_RIGHT + 1:
        raise ValueError("insufficient closed 1h history")

    reference = float(exec_closed[-1].close)
    atr = _causal_atr_series(exec_closed)[-1]

    eligible: list[tuple[float, int, LevelRecord]] = []
    cur_run_price: float | None = None
    run_repped = False
    for i in range(SWING_RIGHT, len(htf_closed) - SWING_RIGHT):
        candle = htf_closed[i]
        price = float(candle.high)
        # P2-11: only a genuine tied swing-high plateau collapses to one
        # representative; independent same-price swings stay separate.
        if price != cur_run_price:
            cur_run_price = price
            run_repped = False
        if not all(htf_closed[i - j].high <= price for j in range(1, SWING_LEFT + 1)):
            continue
        if not all(htf_closed[i + j].high <= price for j in range(1, SWING_RIGHT + 1)):
            continue
        if not any(htf_closed[i - j].high < price for j in range(1, SWING_LEFT + 1)):
            continue
        if not any(htf_closed[i + j].high < price for j in range(1, SWING_RIGHT + 1)):
            continue
        if run_repped:
            continue
        available = int(htf_closed[i + SWING_RIGHT].timestamp) + HTF_MS
        if available > asof_ms or price <= reference:
            continue
        tol = max(price * 0.003, atr * LEVEL_TOUCH_COUNT_TOLERANCE_ATR)
        count = sum(
            abs(float(x.high) - price) <= tol or abs(float(x.low) - price) <= tol
            for x in htf_closed[:i]
        )
        if count < KEY_LEVEL_MIN_TOUCHES:
            continue
        run_repped = True
        formation = int(candle.timestamp)
        eligible.append((
            price,
            formation,
            LevelRecord(
                level_id=level_id_for(symbol, direction, formation, available, price),
                legacy_level_id=legacy_level_id_for(direction, formation, available, price),
                symbol=symbol,
                direction=direction,
                price=price,
                formation_close_ms=formation,
                available_ms=available,
                touch_count=count,
                eligible_at_decision=True,
            ),
        ))
    records = tuple(x[2] for x in sorted(eligible, key=lambda x: (x[0], x[1], x[2].level_id)))
    diagnostics = UniverseDiagnostics(
        decision_asof_ms=asof_ms,
        reference_price=reference,
        decision_atr=atr,
        htf_source=htf_source,
        coverage_clean=coverage.clean,
        partial_hours=coverage.partial_hours,
        missing_hours=coverage.missing_hours,
        formed_levels=len(records),
        eligible_levels=len(records),
    )
    return records, diagnostics


def decision_htf(
    exec_closed: Sequence[Candle], asof_ms: int
) -> tuple[list[Candle], HtfCoverage]:
    """Closed 1H view of a validated 5m prefix with explicit coverage."""
    validate_execution_series(exec_closed, asof_ms)
    return aggregate_closed_htf(exec_closed, asof_ms)
