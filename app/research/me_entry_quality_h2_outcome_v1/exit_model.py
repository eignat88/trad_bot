"""Issue #298 / #312 — paired-leg outcome simulation (D-5, D-6, D-9).

Research-only, pure: no I/O, no network, no database, no clock reads.

* D-5 SL/TP geometry: source-verbatim / re-based at entry / re-derived
  from EXPLICIT policy distances — every mode is explicit, nothing is
  inferred from outcomes;
* D-6 outcome horizon & exit rule with a DOCUMENTED stop+target same-candle
  tie-break (the tie-break is a required policy parameter, never a default);
* D-9 MAE/MFE: close-only AND wick path sampling, both supported and
  explicitly labeled on every result; normalization (percent of entry price
  or R multiples of the stop distance) is labeled too;
* closed-candle-only path evaluation, ASOF strict at µs precision — the
  entry-containing candle participates in the wick path only when the entry
  happened at (or before) its open (pre-entry intra-candle action is not
  separable in OHLCV data).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.research.me_entry_quality_h2_outcome_v1.costs import CostModel
from app.research.me_entry_quality_h2_outcome_v1.entry_model import (
    EntryResolution,
    INTRACANDLE_TIME_BOUND_POLICY,
)
from app.research.me_entry_quality_h2_outcome_v1.policy import (
    EXIT_RULE_HORIZON_ONLY,
    EXIT_RULE_STOP_OR_TARGET,
    GEOMETRY_RE_DERIVED,
    GEOMETRY_REBASED_AT_ENTRY,
    GEOMETRY_SOURCE_VERBATIM,
    NORM_PCT_ENTRY,
    PATH_CLOSE_ONLY,
    PATH_WICK,
    TIE_BREAK_AVERAGE,
    TIE_BREAK_STOP_FIRST,
    TIE_BREAK_TARGET_FIRST,
    ExitRule,
    MaeMfeSpec,
    OutcomePolicy,
    SlTpGeometryPolicy,
)
from app.research.me_entry_quality_h2_outcome_v1.types import (
    ENTRY_ENTERED,
    ENTRY_INVALID,
    ENTRY_PRICE_UNDETERMINED,
    EXIT_HORIZON,
    EXIT_NONE,
    EXIT_PRICE_POLICY,
    EXIT_STOP,
    EXIT_TARGET,
    CandleBar,
    LegOutcome,
    SourceEventInput,
)

EXIT_MODEL_VERSION = "H2_OUTCOME_EXIT_MODEL_1.0.0"

#: stop+target touched inside ONE candle and averaged by the tie-break
EXIT_TIE_AVERAGE = "EXIT_STOP_TARGET_TIE_AVERAGE"

REASON_UNRESOLVED_NO_TOUCH = "UNRESOLVED_NO_TOUCH_WITHIN_HORIZON"
REASON_GEOMETRY_INVALID = "GEOMETRY_INVALID"
REASON_INSUFFICIENT_GEOMETRY = "INSUFFICIENT_GEOMETRY_DATA"
REASON_HORIZON_EARLY_EXIT = "HORIZON_EXIT_AT_FIRST_AVAILABLE_CLOSE"


class GeometryError(ValueError):
    """Invalid / insufficient SL-TP geometry (fail closed)."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class Geometry:
    """Concrete stop/target levels for ONE leg."""

    mode: str
    stop_price: float
    target_price: float
    stop_distance: float
    target_distance: float
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "stop_distance": self.stop_distance,
            "target_distance": self.target_distance,
            "note": self.note,
        }


def _positive(value: float | None, name: str) -> float:
    if value is None:
        raise GeometryError(REASON_INSUFFICIENT_GEOMETRY)
    number = float(value)
    if not (number > 0.0) or number != number:
        raise GeometryError(f"{name}: not a positive finite distance")
    return number


def build_geometry(
    *,
    direction: str,
    entry_price: float,
    original_price: float | None,
    source_stop_price: float | None,
    source_target_price: float | None,
    geometry_policy: SlTpGeometryPolicy,
) -> Geometry:
    """Build the leg's SL/TP geometry per D-5 (explicit mode required)."""
    if direction not in ("SHORT", "LONG"):
        raise GeometryError("GEOMETRY_INVALID_DIRECTION")
    is_short = direction == "SHORT"

    if geometry_policy.mode == GEOMETRY_SOURCE_VERBATIM:
        if source_stop_price is None or source_target_price is None:
            raise GeometryError(REASON_INSUFFICIENT_GEOMETRY)
        stop = float(source_stop_price)
        target = float(source_target_price)
        stop_distance = (stop - entry_price) if is_short else (entry_price - stop)
        target_distance = (
            (entry_price - target) if is_short else (target - entry_price)
        )
        if not (stop_distance > 0.0):
            raise GeometryError("SOURCE_VERBATIM_STOP_NOT_BEYOND_ENTRY")
        if not (target_distance > 0.0):
            raise GeometryError("SOURCE_VERBATIM_TARGET_NOT_BEYOND_ENTRY")
        return Geometry(
            mode=geometry_policy.mode,
            stop_price=stop,
            target_price=target,
            stop_distance=stop_distance,
            target_distance=target_distance,
            note="SOURCE_LEVELS_VERBATIM",
        )

    if geometry_policy.mode == GEOMETRY_REBASED_AT_ENTRY:
        if (
            source_stop_price is None
            or source_target_price is None
            or original_price is None
        ):
            raise GeometryError(REASON_INSUFFICIENT_GEOMETRY)
        stop_distance = _positive(
            abs(float(source_stop_price) - float(original_price)),
            "source_stop_distance",
        )
        target_distance = _positive(
            abs(float(original_price) - float(source_target_price)),
            "source_target_distance",
        )
    else:
        assert geometry_policy.mode == GEOMETRY_RE_DERIVED
        stop_distance = _positive(geometry_policy.stop_distance, "stop_distance")
        target_distance = _positive(
            geometry_policy.target_distance, "target_distance"
        )

    stop = entry_price + stop_distance if is_short else entry_price - stop_distance
    target = (
        entry_price - target_distance
        if is_short
        else entry_price + target_distance
    )
    return Geometry(
        mode=geometry_policy.mode,
        stop_price=stop,
        target_price=target,
        stop_distance=stop_distance,
        target_distance=target_distance,
        note="DISTANCES_REAPPLIED_AT_LEG_ENTRY_PRICE",
    )


def _gross_returns(
    *, direction: str, entry_price: float, exit_price: float, stop_distance: float
) -> tuple[float, float]:
    is_short = direction == "SHORT"
    delta = (entry_price - exit_price) if is_short else (exit_price - entry_price)
    return delta / entry_price, delta / stop_distance


def _mae_mfe(
    *,
    direction: str,
    entry_price: float,
    stop_distance: float,
    points: Sequence[float],
) -> tuple[float, float, float, float]:
    is_short = direction == "SHORT"
    highest = max(points)
    lowest = min(points)
    if is_short:
        mae = highest - entry_price
        mfe = entry_price - lowest
    else:
        mae = entry_price - lowest
        mfe = highest - entry_price
    return (
        mae / entry_price,
        mfe / entry_price,
        mae / stop_distance,
        mfe / stop_distance,
    )


def simulate_leg(
    *,
    event: SourceEventInput,
    leg_name: str,
    entry: EntryResolution,
    geometry: Geometry,
    closed: Sequence[CandleBar],
    policy: OutcomePolicy,
    evaluation_asof_us: int,
) -> LegOutcome:
    """Simulate ONE entered leg's outcome path (closed candles only)."""
    exit_rule: ExitRule = policy.decision("d_6_exit_rule")
    mae_mfe_spec: MaeMfeSpec = policy.decision("d_9_mae_mfe_conventions")
    cost_model: CostModel = policy.decision("d_3_cost_model")
    direction = event.direction
    entry_price = float(entry.entry_price)  # entered legs only
    entry_time_us = int(entry.entry_time_us)
    horizon_end_us = entry_time_us + exit_rule.horizon_us

    path = [
        bar
        for bar in closed
        if bar.close_time_us > entry_time_us and bar.open_time_us < horizon_end_us
    ]
    path.sort(key=lambda bar: bar.open_time_us)

    is_short = direction == "SHORT"

    def stop_touched(bar: CandleBar) -> bool:
        return (bar.high >= geometry.stop_price) if is_short else (
            bar.low <= geometry.stop_price
        )

    def target_touched(bar: CandleBar) -> bool:
        return (bar.low <= geometry.target_price) if is_short else (
            bar.high >= geometry.target_price
        )

    exit_status = EXIT_NONE
    exit_price: float | None = None
    exit_time_us: int | None = None
    exit_reason = ""
    stopped_at_bar: CandleBar | None = None

    for bar in path:
        if exit_rule.exit_rule == EXIT_RULE_HORIZON_ONLY:
            break  # no touch exits at all under this rule
        wick_eligible = entry_time_us <= bar.open_time_us
        if mae_mfe_spec.path_sampling == PATH_WICK:
            if not wick_eligible:
                continue  # pre-entry intra-candle action is unseparable
            stop_hit = stop_touched(bar)
            target_hit = target_touched(bar)
            if stop_hit and target_hit:
                tie = exit_rule.same_candle_tie_break
                if tie == TIE_BREAK_STOP_FIRST:
                    exit_status, exit_price = EXIT_STOP, geometry.stop_price
                elif tie == TIE_BREAK_TARGET_FIRST:
                    exit_status, exit_price = EXIT_TARGET, geometry.target_price
                else:
                    assert tie == TIE_BREAK_AVERAGE
                    exit_status = EXIT_TIE_AVERAGE
                    exit_price = (geometry.stop_price + geometry.target_price) / 2.0
                exit_time_us = bar.open_time_us
                stopped_at_bar = bar
                break
            if stop_hit:
                exit_status, exit_price = EXIT_STOP, geometry.stop_price
                exit_time_us = bar.open_time_us
                stopped_at_bar = bar
                break
            if target_hit:
                exit_status, exit_price = EXIT_TARGET, geometry.target_price
                exit_time_us = bar.open_time_us
                stopped_at_bar = bar
                break
        else:
            assert mae_mfe_spec.path_sampling == PATH_CLOSE_ONLY
            close = bar.close
            if (
                (close >= geometry.stop_price)
                if is_short
                else (close <= geometry.stop_price)
            ):
                exit_status, exit_price = EXIT_STOP, geometry.stop_price
                exit_time_us = bar.open_time_us
                stopped_at_bar = bar
                break
            if (
                (close <= geometry.target_price)
                if is_short
                else (close >= geometry.target_price)
            ):
                exit_status, exit_price = EXIT_TARGET, geometry.target_price
                exit_time_us = bar.open_time_us
                stopped_at_bar = bar
                break

    if exit_status == EXIT_NONE:
        if exit_rule.exit_rule == EXIT_RULE_STOP_OR_TARGET:
            # explicit D-6 mode: only stop/target exits count — an untouched
            # horizon stays UNRESOLVED (never a fabricated return)
            return LegOutcome(
                leg_name=leg_name,
                entry_status=ENTRY_ENTERED,
                entry_reason=entry.reason,
                entry_time_us=entry_time_us,
                entry_price=entry_price,
                entry_price_label=entry.entry_price_label,
                exit_status=EXIT_NONE,
                exit_price=None,
                exit_time_us=None,
                held_ms=0,
                gross_return_pct=None,
                gross_return_r=None,
                net_return_pct=None,
                net_return_r=None,
                mae_pct=None,
                mfe_pct=None,
                mae_r=None,
                mfe_r=None,
                mae_mfe_path_sampling=mae_mfe_spec.path_sampling,
                mae_mfe_normalization=mae_mfe_spec.normalization,
                cost_breakdown=None,
            )
        # horizon resolution: the last close at or before the horizon end
        horizon_bars = [
            bar for bar in path if bar.close_time_us <= horizon_end_us
        ]
        if horizon_bars:
            last = horizon_bars[-1]
            exit_price = last.close
            exit_time_us = last.close_time_us
            exit_reason = EXIT_PRICE_POLICY
        elif path:
            first = path[0]
            exit_price = first.close
            exit_time_us = first.close_time_us
            exit_reason = REASON_HORIZON_EARLY_EXIT
        else:
            return LegOutcome(
                leg_name=leg_name,
                entry_status=ENTRY_ENTERED,
                entry_reason=entry.reason,
                entry_time_us=entry_time_us,
                entry_price=entry_price,
                entry_price_label=entry.entry_price_label,
                exit_status=EXIT_NONE,
                exit_price=None,
                exit_time_us=None,
                held_ms=0,
                gross_return_pct=None,
                gross_return_r=None,
                net_return_pct=None,
                net_return_r=None,
                mae_pct=None,
                mfe_pct=None,
                mae_r=None,
                mfe_r=None,
                mae_mfe_path_sampling=mae_mfe_spec.path_sampling,
                mae_mfe_normalization=mae_mfe_spec.normalization,
                cost_breakdown=None,
            )
        exit_status = EXIT_HORIZON

    assert exit_price is not None and exit_time_us is not None
    held_ms = max(0, (exit_time_us - entry_time_us) // 1000)
    gross_pct, gross_r = _gross_returns(
        direction=direction,
        entry_price=entry_price,
        exit_price=float(exit_price),
        stop_distance=geometry.stop_distance,
    )
    net_pct = cost_model.net_return_pct(gross_pct, held_ms)
    net_r = cost_model.net_return_r(
        gross_r,
        held_ms,
        entry_price=entry_price,
        stop_distance=geometry.stop_distance,
    )

    # ── D-9: MAE/MFE over the sampled path (entry price + path extremes) ────
    sample_bars = list(path)
    if stopped_at_bar is not None and stopped_at_bar in sample_bars:
        sample_bars = sample_bars[: sample_bars.index(stopped_at_bar) + 1]
    points: list[float] = [entry_price]
    for bar in sample_bars:
        if mae_mfe_spec.path_sampling == PATH_WICK:
            if entry_time_us <= bar.open_time_us:
                points.append(bar.high)
                points.append(bar.low)
        else:
            points.append(bar.close)
    mae_pct, mfe_pct, mae_r, mfe_r = _mae_mfe(
        direction=direction,
        entry_price=entry_price,
        stop_distance=geometry.stop_distance,
        points=points,
    )

    return LegOutcome(
        leg_name=leg_name,
        entry_status=ENTRY_ENTERED,
        entry_reason=entry.reason,
        entry_time_us=entry_time_us,
        entry_price=entry_price,
        entry_price_label=entry.entry_price_label,
        exit_status=exit_status,
        exit_price=float(exit_price),
        exit_time_us=int(exit_time_us),
        held_ms=held_ms,
        gross_return_pct=gross_pct,
        gross_return_r=gross_r,
        net_return_pct=net_pct,
        net_return_r=net_r,
        mae_pct=mae_pct,
        mfe_pct=mfe_pct,
        mae_r=mae_r,
        mfe_r=mfe_r,
        mae_mfe_path_sampling=mae_mfe_spec.path_sampling,
        mae_mfe_normalization=mae_mfe_spec.normalization,
        cost_breakdown={
            **cost_model.breakdown(held_ms),
            "exit_reason": exit_reason or exit_status,
            "exit_price_policy": EXIT_PRICE_POLICY,
            "intracandle_time_bound_policy": INTRACANDLE_TIME_BOUND_POLICY,
            "geometry": geometry.to_dict(),
        },
    )


def non_entry_leg(
    *,
    leg_name: str,
    entry_status: str,
    entry_reason: str,
    policy: OutcomePolicy,
) -> LegOutcome:
    """A leg that never entered: NO return numbers, explicit status/reason
    (it still stays in the denominator accounting, D-8)."""
    mae_mfe_spec: MaeMfeSpec = policy.decision("d_9_mae_mfe_conventions")
    return LegOutcome(
        leg_name=leg_name,
        entry_status=entry_status,
        entry_reason=entry_reason,
        entry_time_us=None,
        entry_price=None,
        entry_price_label=ENTRY_PRICE_UNDETERMINED,
        exit_status=EXIT_NONE,
        exit_price=None,
        exit_time_us=None,
        held_ms=0,
        gross_return_pct=None,
        gross_return_r=None,
        net_return_pct=None,
        net_return_r=None,
        mae_pct=None,
        mfe_pct=None,
        mae_r=None,
        mfe_r=None,
        mae_mfe_path_sampling=mae_mfe_spec.path_sampling,
        mae_mfe_normalization=mae_mfe_spec.normalization,
        cost_breakdown=None,
    )


def invalid_leg(*, leg_name: str, reason: str) -> LegOutcome:
    """A leg that cannot be evaluated at all (invalid/insufficient input)."""
    return LegOutcome(
        leg_name=leg_name,
        entry_status=ENTRY_INVALID,
        entry_reason=reason,
        entry_time_us=None,
        entry_price=None,
        entry_price_label=ENTRY_PRICE_UNDETERMINED,
        exit_status=EXIT_NONE,
        exit_price=None,
        exit_time_us=None,
        held_ms=0,
        gross_return_pct=None,
        gross_return_r=None,
        net_return_pct=None,
        net_return_r=None,
        mae_pct=None,
        mfe_pct=None,
        mae_r=None,
        mfe_r=None,
        mae_mfe_path_sampling="",
        mae_mfe_normalization="",
        cost_breakdown=None,
    )
