"""Approved seven-variant horizon-end conditional MAE evaluation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .candles import ClosedCandle, validate_window
from .outcomes import BaselineOutcome, first_terminal, outcome
from .protocol import (
    ELEVATED_SLIPPAGE_PER_SIDE,
    NORMAL_SLIPPAGE_PER_SIDE,
    VariantDef,
    VARIANTS,
    round_trip_cost_r,
)


@dataclass(frozen=True)
class VariantOutcome:
    variant_status: str
    variant_id: str
    mae_r: float | None
    mfe_r: float | None
    window_low: float | None
    window_high: float | None
    trigger_result: str
    horizon_end_at: datetime | None
    variant_exit_at: datetime | None
    variant_exit_price: float | None
    variant_exit_reason: str | None
    variant_gross_r: float | None
    net_r_normal: float | None
    net_r_elevated: float | None
    delta_net_r_normal: float | None
    delta_net_r_elevated: float | None
    ambiguous_ordering: bool
    coverage_reason: str | None
    execution_price_semantics: str | None


def evaluate_variant(
    variant: VariantDef,
    candles: list[ClosedCandle],
    entry_at: datetime,
    entry_price: float,
    stop_price: float,
    target_price: float,
    baseline: BaselineOutcome,
) -> VariantOutcome:
    horizon_end = entry_at + timedelta(minutes=variant.horizon_minutes)
    if baseline.exit_at is not None and baseline.exit_at < horizon_end:
        inherited = _baseline_inherit(variant, baseline)
        if baseline.exit_reason in ("STOP_LOSS", "TAKE_PROFIT", "AMBIGUOUS_STOP_FIRST"):
            inherited = VariantOutcome(
                variant_status=inherited.variant_status,
                variant_id=inherited.variant_id,
                mae_r=inherited.mae_r,
                mfe_r=inherited.mfe_r,
                window_low=inherited.window_low,
                window_high=inherited.window_high,
                trigger_result="BASELINE_SL_TP_PRECEDENCE",
                horizon_end_at=inherited.horizon_end_at,
                variant_exit_at=inherited.variant_exit_at,
                variant_exit_price=inherited.variant_exit_price,
                variant_exit_reason=inherited.variant_exit_reason,
                variant_gross_r=inherited.variant_gross_r,
                net_r_normal=inherited.net_r_normal,
                net_r_elevated=inherited.net_r_elevated,
                delta_net_r_normal=inherited.delta_net_r_normal,
                delta_net_r_elevated=inherited.delta_net_r_elevated,
                ambiguous_ordering=inherited.ambiguous_ordering,
                coverage_reason=inherited.coverage_reason,
                execution_price_semantics=inherited.execution_price_semantics,
            )
        return inherited
    window, error = validate_window(
        candles, entry_at, horizon_end, variant.horizon_minutes
    )
    if error:
        return _incomplete(variant, horizon_end, error)
    terminal = first_terminal(window, entry_price, stop_price, target_price)
    if terminal is not None:
        candle, reason, ambiguous, exit_price = terminal
        if candle.close_at <= horizon_end:
            return _from_execution(
                variant,
                outcome(entry_price, stop_price, candle.close_at, exit_price, reason, ambiguous),
                baseline,
                trigger_result="BASELINE_SL_TP_PRECEDENCE",
                coverage="COMPLETE",
                horizon_end=horizon_end,
                window=window,
            )
    window_low = min(c.low for c in window)
    window_high = max(c.high for c in window)
    risk = entry_price - stop_price
    mae_r = (entry_price - window_low) / risk
    mfe_r = (window_high - entry_price) / risk
    triggered = mae_r >= variant.threshold_mae_r
    boundary = window[-1]
    if boundary.close_at != horizon_end:
        return _incomplete(variant, horizon_end, "MISSING_HORIZON_BOUNDARY_CLOSE")
    if triggered:
        return _from_execution(
            variant,
            outcome(
                entry_price, stop_price, boundary.close_at, boundary.close,
                "HORIZON_END_CONDITIONAL_MAE_EXIT",
            ),
            baseline,
            trigger_result="TRIGGERED",
            coverage="COMPLETE",
            horizon_end=horizon_end,
            window=window,
            mae_r=mae_r,
            mfe_r=mfe_r,
            proxy=True,
        )
    return _from_execution(
        variant,
        baseline,
        baseline,
        trigger_result="NO_TRIGGER",
        coverage="COMPLETE",
        horizon_end=horizon_end,
        window=window,
        mae_r=mae_r,
        mfe_r=mfe_r,
    )


def _baseline_inherit(variant: VariantDef, baseline: BaselineOutcome) -> VariantOutcome:
    return _from_execution(
        variant, baseline, baseline, "BASELINE_EXITED_EARLY", "COMPLETE", None, [],
    )


def _from_execution(
    variant: VariantDef,
    execution: BaselineOutcome,
    baseline: BaselineOutcome,
    trigger_result: str,
    coverage: str,
    horizon_end: datetime | None,
    window: list[ClosedCandle],
    mae_r: float | None = None,
    mfe_r: float | None = None,
    proxy: bool = False,
) -> VariantOutcome:
    delta_normal = (
        execution.net_r_normal - baseline.net_r_normal
        if execution.net_r_normal is not None and baseline.net_r_normal is not None
        else None
    )
    delta_elevated = (
        execution.net_r_elevated - baseline.net_r_elevated
        if execution.net_r_elevated is not None and baseline.net_r_elevated is not None
        else None
    )
    return VariantOutcome(
        variant_status="AMBIGUOUS" if execution.ambiguous else "EVALUATED",
        variant_id=variant.variant_id,
        mae_r=round(mae_r, 8) if mae_r is not None else None,
        mfe_r=round(mfe_r, 8) if mfe_r is not None else None,
        window_low=min((c.low for c in window), default=None),
        window_high=max((c.high for c in window), default=None),
        trigger_result=trigger_result,
        horizon_end_at=horizon_end,
        variant_exit_at=execution.exit_at,
        variant_exit_price=execution.exit_price,
        variant_exit_reason=execution.exit_reason,
        variant_gross_r=execution.gross_r,
        net_r_normal=execution.net_r_normal,
        net_r_elevated=execution.net_r_elevated,
        delta_net_r_normal=round(delta_normal, 8) if delta_normal is not None else None,
        delta_net_r_elevated=round(delta_elevated, 8) if delta_elevated is not None else None,
        ambiguous_ordering=execution.ambiguous,
        coverage_reason=None if coverage == "COMPLETE" else coverage,
        execution_price_semantics=(
            "RESEARCH_PRICE_PROXY_NOT_VERIFIED_EXECUTABLE" if proxy else "BASELINE_EXECUTION_PROXY"
        ),
    )


def evaluate_all(candles, entry_at, entry_price, stop_price, target_price, baseline) -> list[VariantOutcome]:
    return [
        evaluate_variant(v, candles, entry_at, entry_price, stop_price, target_price, baseline)
        for v in VARIANTS
    ]


def _incomplete(variant: VariantDef, horizon_end: datetime, reason: str) -> VariantOutcome:
    return VariantOutcome(
        variant_status="INCOMPLETE_COVERAGE", variant_id=variant.variant_id,
        mae_r=None, mfe_r=None, window_low=None, window_high=None,
        trigger_result="INCOMPLETE_COVERAGE", horizon_end_at=horizon_end,
        variant_exit_at=None, variant_exit_price=None, variant_exit_reason=None,
        variant_gross_r=None, net_r_normal=None, net_r_elevated=None,
        delta_net_r_normal=None, delta_net_r_elevated=None,
        ambiguous_ordering=False, coverage_reason=reason,
        execution_price_semantics=None,
    )
