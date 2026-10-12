"""Frozen seven-variant MAE evaluation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .candles import ClosedCandle, validate_window
from .outcomes import Outcome, first_terminal_exit, outcome_from_exit
from .protocol import VariantDef


@dataclass(frozen=True)
class VariantResult:
    variant_status: str
    mae_r: float | None
    mfe_r: float | None
    window_low: float | None
    window_high: float | None
    trigger_result: str
    variant_exit_at: datetime | None
    variant_exit_price: float | None
    variant_exit_reason: str | None
    variant_gross_r: float | None
    variant_net_r: float | None
    delta_net_r: float | None
    ambiguous_ordering: bool
    coverage_reason: str | None


def evaluate_variant(
    variant: VariantDef,
    candles: list[ClosedCandle],
    entry_at: datetime,
    entry_price: float,
    stop_price: float,
    target_price: float,
    baseline: Outcome,
) -> VariantResult:
    """Evaluate one frozen horizon without extending it to wall-clock time."""
    horizon_end = entry_at + timedelta(minutes=variant.horizon_minutes)
    if baseline.exit_at < horizon_end:
        return VariantResult(
            variant_status="BASELINE_EXITED_EARLY",
            mae_r=None, mfe_r=None, window_low=None, window_high=None,
            trigger_result="BASELINE_EXITED_EARLY",
            variant_exit_at=baseline.exit_at,
            variant_exit_price=baseline.exit_price,
            variant_exit_reason=baseline.exit_reason,
            variant_gross_r=baseline.gross_r,
            variant_net_r=baseline.net_r,
            delta_net_r=0.0,
            ambiguous_ordering=baseline.ambiguous,
            coverage_reason=None,
        )

    window, error = validate_window(candles, entry_at, horizon_end)
    if error:
        return _incomplete(error)

    terminal = first_terminal_exit(window, entry_price, stop_price, target_price)
    if terminal is not None and terminal[0].close_at <= baseline.exit_at:
        candle, reason, ambiguous, exit_price = terminal
        exit_outcome = outcome_from_exit(
            entry_price, stop_price, candle.close_at, exit_price, reason, ambiguous,
        )
        return VariantResult(
            variant_status="AMBIGUOUS" if ambiguous else "BASELINE_EXITED_EARLY",
            mae_r=None, mfe_r=None, window_low=None, window_high=None,
            trigger_result="BASELINE_EXITED_EARLY",
            variant_exit_at=exit_outcome.exit_at,
            variant_exit_price=exit_outcome.exit_price,
            variant_exit_reason=exit_outcome.exit_reason,
            variant_gross_r=exit_outcome.gross_r,
            variant_net_r=exit_outcome.net_r,
            delta_net_r=0.0,
            ambiguous_ordering=ambiguous,
            coverage_reason=None,
        )

    risk = entry_price - stop_price
    window_low = min(c.low for c in window)
    window_high = max(c.high for c in window)
    mae_r = (entry_price - window_low) / risk if risk > 0 else 0.0
    mfe_r = (window_high - entry_price) / risk if risk > 0 else 0.0
    triggered = mae_r >= variant.threshold_mae_r
    terminal = first_terminal_exit(window, entry_price, stop_price, target_price)

    if triggered and terminal is None:
        boundary = next((c for c in window if c.close_at >= horizon_end), None)
        if boundary is None:
            result = _incomplete("MISSING_HORIZON_CLOSE")
            return VariantResult(
                result.variant_status, round(mae_r, 8), round(mfe_r, 8),
                window_low, window_high, result.trigger_result,
                result.variant_exit_at, result.variant_exit_price,
                result.variant_exit_reason, result.variant_gross_r,
                result.variant_net_r, result.delta_net_r,
                result.ambiguous_ordering, result.coverage_reason,
            )
        exit_outcome = outcome_from_exit(
            entry_price, stop_price, boundary.close_at, boundary.close, "EARLY_MAE",
        )
    elif terminal is not None:
        candle, reason, ambiguous, exit_price = terminal
        exit_outcome = outcome_from_exit(
            entry_price, stop_price, candle.close_at, exit_price, reason, ambiguous,
        )
    else:
        return VariantResult(
            variant_status="EVALUATED_NO_TRIGGER",
            mae_r=round(mae_r, 8), mfe_r=round(mfe_r, 8),
            window_low=window_low, window_high=window_high,
            trigger_result="NO_TRIGGER",
            variant_exit_at=baseline.exit_at,
            variant_exit_price=baseline.exit_price,
            variant_exit_reason=baseline.exit_reason,
            variant_gross_r=baseline.gross_r,
            variant_net_r=baseline.net_r,
            delta_net_r=0.0,
            ambiguous_ordering=baseline.ambiguous,
            coverage_reason=None,
        )

    return VariantResult(
        variant_status=(
            "AMBIGUOUS" if exit_outcome.ambiguous
            else "EVALUATED_TRIGGERED" if triggered
            else "EVALUATED_NO_TRIGGER"
        ),
        mae_r=round(mae_r, 8),
        mfe_r=round(mfe_r, 8),
        window_low=window_low,
        window_high=window_high,
        trigger_result="TRIGGERED" if triggered else "NO_TRIGGER",
        variant_exit_at=exit_outcome.exit_at,
        variant_exit_price=exit_outcome.exit_price,
        variant_exit_reason=exit_outcome.exit_reason,
        variant_gross_r=exit_outcome.gross_r,
        variant_net_r=exit_outcome.net_r,
        delta_net_r=round(exit_outcome.net_r - baseline.net_r, 8),
        ambiguous_ordering=exit_outcome.ambiguous,
        coverage_reason=None,
    )


def _incomplete(reason: str) -> VariantResult:
    return VariantResult(
        variant_status="INCOMPLETE_COVERAGE",
        mae_r=None, mfe_r=None, window_low=None, window_high=None,
        trigger_result="INCOMPLETE_COVERAGE",
        variant_exit_at=None, variant_exit_price=None, variant_exit_reason=None,
        variant_gross_r=None, variant_net_r=None, delta_net_r=None,
        ambiguous_ordering=False, coverage_reason=reason,
    )
