"""SRR-specific full-candle eligibility policy for frozen SHORT execution paths.

This module is an isolated, deterministic research policy for experiment
``SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1``.  It does not
access PostgreSQL, the network, systemd, scanner, paper, or live execution.
It never mutates the shared prospective evaluator or historical outcomes.

Frozen window semantics
-----------------------
A 5-minute candle is eligible for TP/SL or TIMEOUT resolution only when it is
fully contained in the approved evaluation interval:

    cutoff_ms = signal_time_ms + max_hold_minutes * 60_000

    candle.open_time_ms >= signal_time_ms
    and candle.open_time_ms + CANDLE_MS <= cutoff_ms

A candle whose open time is before the signal and a candle whose close time
extends beyond cutoff are excluded.  If ``evaluation_asof_ms`` is supplied, an
otherwise eligible candle is unavailable until its close time is available:

    candle.open_time_ms + CANDLE_MS <= evaluation_asof_ms

Outcome rules
-------------
* A fully eligible candle touching SHORT TP resolves ``TP_FIRST``.
* A fully eligible candle touching SHORT SL resolves ``SL_FIRST``.
* Both levels touched in the same fully eligible candle resolve ``SL_FIRST``
  under the frozen ``STOP_FIRST`` intrabar policy.
* A signal-spanning candle that could touch either level yields
  ``FIRST_CANDLE_BOUNDARY_UNCERTAIN``.
* A cutoff-spanning candle that could touch either level yields
  ``CUTOFF_BOUNDARY_UNCERTAIN`` when no earlier eligible candle resolved the
  path.
* A clean eligible path uses the close of the last fully eligible, available
  candle as the approved TIMEOUT reference.
* Missing or unproven full coverage never fabricates a TIMEOUT.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

CANDLE_MS = 300_000
CANDLE_INTERVAL_MINUTES = 5
SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1 = "SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1"
SRR_EXPERIMENT_ID = "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
SRR_DIRECTION = "SHORT"
SRR_DEFAULT_MAX_HOLD_MINUTES = 120
SRR_DEFAULT_INTRABAR_POLICY = "STOP_FIRST"
FROZEN_FREEZE_TS = "2026-10-07T08:17:50Z"

RESOLVED = "RESOLVED"
FIRST_CANDLE_BOUNDARY_UNCERTAIN = "FIRST_CANDLE_BOUNDARY_UNCERTAIN"
CUTOFF_BOUNDARY_UNCERTAIN = "CUTOFF_BOUNDARY_UNCERTAIN"
INCOMPLETE_COVERAGE = "INCOMPLETE_COVERAGE"
SOURCE_UNVERIFIABLE = "SOURCE_UNVERIFIABLE"
INVALID_INPUT = "INVALID_INPUT"

TP_FIRST = "TP_FIRST"
SL_FIRST = "SL_FIRST"
TIMEOUT = "TIMEOUT"
AMBIGUOUS_INTRABAR_STOP_FIRST = "AMBIGUOUS_INTRABAR_STOP_FIRST"

REASON_OK = "SRR_SHORT_POLICY_OK"
REASON_INVALID_EXPERIMENT = "EXPERIMENT_ID_MISMATCH"
REASON_INVALID_DIRECTION = "DIRECTION_NOT_SHORT"
REASON_INVALID_GEOMETRY = "INVALID_FROZEN_SHORT_GEOMETRY"
REASON_INVALID_CANDLE = "INVALID_CANDLE_DATA"
REASON_DUPLICATE_CANDLE = "DUPLICATE_CANDLE_TIMESTAMP"
REASON_UNORDERED_CANDLES = "CANDLES_NOT_SORTED"
REASON_WRONG_INTERVAL = "NON_5M_CANDLE_INTERVAL"
REASON_UNAVAILABLE_CANDLE = "CANDLE_NOT_YET_AVAILABLE_AT_EVALUATION_ASOF"
REASON_MISSING_COVERAGE = "INCOMPLETE_ELIGIBLE_CANDLE_COVERAGE"
REASON_SOURCE_PROVENANCE = "CANDLE_SOURCE_PROVENANCE_MISSING_OR_UNPROVEN"
REASON_SIGNAL_BOUNDARY = "FIRST_CANDLE_BOUNDARY_UNCERTAIN"
REASON_CUTOFF_BOUNDARY = "CUTOFF_BOUNDARY_UNCERTAIN"
REASON_STOP_FIRST = "TP_SL_SAME_CANDLE_STOP_FIRST"

_VALID_STATUSES = {
    RESOLVED,
    FIRST_CANDLE_BOUNDARY_UNCERTAIN,
    CUTOFF_BOUNDARY_UNCERTAIN,
    INCOMPLETE_COVERAGE,
    SOURCE_UNVERIFIABLE,
    INVALID_INPUT,
}
_VALID_PATH_CLASSES = {TP_FIRST, SL_FIRST, TIMEOUT, AMBIGUOUS_INTRABAR_STOP_FIRST}


@dataclass(frozen=True)
class PolicyCandle:
    """Normalized 5-minute candle with UTC millisecond open timestamp."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def close_time_ms(self) -> int:
        return int(self.open_time_ms) + CANDLE_MS


@dataclass(frozen=True)
class CandleSourceProvenance:
    """Explicit provenance for an archived candle input source."""

    source_kind: str
    source_id: str
    retrieval_ts_ms: int | None = None
    retrieval_method: str | None = None
    archive_path: str | None = None
    response_sha256: str | None = None
    notes: str | None = None

    @property
    def is_unproven(self) -> bool:
        return not str(self.source_id or "").strip()


@dataclass(frozen=True)
class SRRShortTimeoutCandleEvaluation:
    """Deterministic output of the isolated SRR SHORT timeout policy."""

    policy_version: str
    experiment_id: str
    direction: str
    status: str
    path_class: str | None
    reason_code: str
    entry: float | None
    stop: float | None
    target: float | None
    structural_r: float | None
    execution_r: float | None
    max_hold_minutes: int
    signal_time_ms: int | None
    cutoff_time_ms: int | None
    evaluation_asof_ms: int | None
    eligible_candle_count: int
    first_eligible_candle_ms: int | None
    last_eligible_candle_ms: int | None
    selected_timeout_candle_ms: int | None
    selected_timeout_close: float | None
    selected_timeout_close_ms: int | None
    signal_boundary_uncertain: bool
    cutoff_boundary_uncertain: bool
    coverage_complete: bool
    net_r: float | None
    return_at_120m: float | None
    gross_r: float | None
    cost_r_normal: float | None
    cost_r_elevated: float | None
    net_r_normal: float | None
    net_r_elevated: float | None
    finalization_eligible: bool
    source_confidence: str
    source_provenance: dict[str, Any]
    data_quality: dict[str, Any]
    diagnostics: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError("fractional millisecond timestamp")
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    text = str(value).strip().replace("Z", "+00:00")
    if text.endswith("+00:00"):
        text = text[:-6] + "+0000"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _utc_iso_ms(value: int | None) -> str | None:
    if value is None:
        return None
    dt = datetime.fromtimestamp(value / 1000, timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _finite_float(value: Any) -> float:
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("non-finite float")
    return number


def _normalize_candle(value: Any) -> PolicyCandle:
    if isinstance(value, PolicyCandle):
        return value
    if isinstance(value, Mapping):
        open_ms = value.get("open_time_ms", value.get("timestamp", value.get("open_ts")))
        if open_ms is None:
            raise ValueError("candle open timestamp missing")
        return PolicyCandle(
            open_time_ms=_utc_ms(open_ms),
            open=_finite_float(value["open"]),
            high=_finite_float(value["high"]),
            low=_finite_float(value["low"]),
            close=_finite_float(value["close"]),
            volume=_finite_float(value.get("volume", 0.0)),
        )
    return PolicyCandle(
        open_time_ms=_utc_ms(getattr(value, "open_time_ms", getattr(value, "timestamp"))),
        open=_finite_float(getattr(value, "open")),
        high=_finite_float(getattr(value, "high")),
        low=_finite_float(getattr(value, "low")),
        close=_finite_float(getattr(value, "close")),
        volume=_finite_float(getattr(value, "volume", 0.0)),
    )


def _normalize_provenance(value: Any) -> CandleSourceProvenance | None:
    if value is None:
        return None
    if isinstance(value, CandleSourceProvenance):
        return value
    if isinstance(value, str):
        value = {"source_id": value, "source_kind": "ARCHIVE_OR_CALLER_SUPPLIED"}
    if not isinstance(value, Mapping):
        raise ValueError("source provenance must be a mapping or string")
    retrieval_ts = value.get("retrieval_ts_ms")
    return CandleSourceProvenance(
        source_kind=str(value.get("source_kind", "") or ""),
        source_id=str(value.get("source_id", "") or ""),
        retrieval_ts_ms=None if retrieval_ts is None else _utc_ms(retrieval_ts),
        retrieval_method=value.get("retrieval_method"),
        archive_path=value.get("archive_path"),
        response_sha256=value.get("response_sha256"),
        notes=value.get("notes"),
    )


def _error_result(
    *,
    reason_code: str,
    data_quality: dict[str, Any] | None = None,
    source_provenance: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    signal_time_ms: int | None = None,
    cutoff_time_ms: int | None = None,
    evaluation_asof_ms: int | None = None,
) -> SRRShortTimeoutCandleEvaluation:
    return SRRShortTimeoutCandleEvaluation(
        policy_version=SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
        experiment_id=SRR_EXPERIMENT_ID,
        direction=SRR_DIRECTION,
        status=INVALID_INPUT,
        path_class=None,
        reason_code=reason_code,
        entry=None,
        stop=None,
        target=None,
        structural_r=None,
        execution_r=None,
        max_hold_minutes=SRR_DEFAULT_MAX_HOLD_MINUTES,
        signal_time_ms=signal_time_ms,
        cutoff_time_ms=cutoff_time_ms,
        evaluation_asof_ms=evaluation_asof_ms,
        eligible_candle_count=0,
        first_eligible_candle_ms=None,
        last_eligible_candle_ms=None,
        selected_timeout_candle_ms=None,
        selected_timeout_close=None,
        selected_timeout_close_ms=None,
        signal_boundary_uncertain=False,
        cutoff_boundary_uncertain=False,
        coverage_complete=False,
        net_r=None,
        return_at_120m=None,
        gross_r=None,
        cost_r_normal=None,
        cost_r_elevated=None,
        net_r_normal=None,
        net_r_elevated=None,
        finalization_eligible=False,
        source_confidence="UNPROVEN",
        source_provenance=source_provenance or {},
        data_quality=data_quality or {},
        diagnostics=diagnostics or {},
    )


class SrrShortTimeoutCandlePolicy:
    """Isolated deterministic policy for frozen SRR SHORT 5m candle paths."""

    def evaluate(
        self,
        *,
        signal_time_ms: int,
        entry: float,
        stop: float,
        target: float,
        structural_r: float,
        execution_r: float,
        max_hold_minutes: int = SRR_DEFAULT_MAX_HOLD_MINUTES,
        candles: Sequence[Any],
        evaluation_asof_ms: int | None = None,
        experiment_id: str = SRR_EXPERIMENT_ID,
        direction: str = SRR_DIRECTION,
        intrabar_policy: str = SRR_DEFAULT_INTRABAR_POLICY,
        source_provenance: Any = None,
        cutoff_time_ms: int | None = None,
    ) -> SRRShortTimeoutCandleEvaluation:
        provenance_obj = _normalize_provenance(source_provenance)
        provenance = asdict(provenance_obj) if provenance_obj is not None else {
            "source_kind": "",
            "source_id": "",
            "retrieval_ts_ms": None,
            "retrieval_method": None,
            "archive_path": None,
            "response_sha256": None,
            "notes": None,
        }

        if experiment_id != SRR_EXPERIMENT_ID:
            return _error_result(
                reason_code=REASON_INVALID_EXPERIMENT,
                source_provenance=provenance,
                diagnostics={"experiment_id": experiment_id},
            )
        if direction != SRR_DIRECTION:
            return _error_result(
                reason_code=REASON_INVALID_DIRECTION,
                source_provenance=provenance,
                diagnostics={"direction": direction},
            )
        if intrabar_policy != SRR_DEFAULT_INTRABAR_POLICY:
            return _error_result(
                reason_code=REASON_INVALID_GEOMETRY,
                source_provenance=provenance,
                diagnostics={"intrabar_policy": intrabar_policy},
            )

        try:
            signal_ms = _utc_ms(signal_time_ms)
            entry_n = _finite_float(entry)
            stop_n = _finite_float(stop)
            target_n = _finite_float(target)
            structural_n = _finite_float(structural_r)
            execution_n = _finite_float(execution_r)
            max_hold_n = int(max_hold_minutes)
            asof_ms = None if evaluation_asof_ms is None else _utc_ms(evaluation_asof_ms)
            derived_cutoff_ms = signal_ms + max_hold_n * 60_000
            cutoff_ms = derived_cutoff_ms if cutoff_time_ms is None else _utc_ms(cutoff_time_ms)
            if cutoff_ms != derived_cutoff_ms:
                raise ValueError("cutoff_time_ms does not match frozen max hold")
            if (
                max_hold_n <= 0
                or entry_n <= 0
                or structural_n <= 0
                or execution_n <= 0
                or stop_n <= entry_n
                or target_n >= entry_n
            ):
                raise ValueError("invalid frozen SHORT geometry")
        except (TypeError, ValueError, OverflowError) as exc:
            return _error_result(
                reason_code=REASON_INVALID_GEOMETRY,
                source_provenance=provenance,
                diagnostics={"error": str(exc)},
            )

        normalized: list[PolicyCandle] = []
        try:
            for item in candles or []:
                normalized.append(_normalize_candle(item))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            return _error_result(
                reason_code=REASON_INVALID_CANDLE,
                source_provenance=provenance,
                signal_time_ms=signal_ms,
                cutoff_time_ms=cutoff_ms,
                evaluation_asof_ms=asof_ms,
                diagnostics={"error": str(exc)},
            )

        opens = [candle.open_time_ms for candle in normalized]
        duplicate_opens = sorted({value for value in opens if opens.count(value) > 1})
        if duplicate_opens:
            return _error_result(
                reason_code=REASON_DUPLICATE_CANDLE,
                source_provenance=provenance,
                signal_time_ms=signal_ms,
                cutoff_time_ms=cutoff_ms,
                evaluation_asof_ms=asof_ms,
                data_quality={
                    "input_candle_count": len(normalized),
                    "duplicate_open_time_ms": duplicate_opens,
                },
            )

        sorted_candles = sorted(normalized, key=lambda candle: candle.open_time_ms)
        if [candle.open_time_ms for candle in sorted_candles] != opens:
            return _error_result(
                reason_code=REASON_UNORDERED_CANDLES,
                source_provenance=provenance,
                signal_time_ms=signal_ms,
                cutoff_time_ms=cutoff_ms,
                evaluation_asof_ms=asof_ms,
                data_quality={
                    "input_candle_count": len(normalized),
                    "input_open_times_ms": opens,
                },
            )

        wrong_intervals = [
            candle.open_time_ms
            for candle in sorted_candles
            if candle.open_time_ms % CANDLE_MS != 0
        ]
        if wrong_intervals:
            return _error_result(
                reason_code=REASON_WRONG_INTERVAL,
                source_provenance=provenance,
                signal_time_ms=signal_ms,
                cutoff_time_ms=cutoff_ms,
                evaluation_asof_ms=asof_ms,
                data_quality={
                    "input_candle_count": len(sorted_candles),
                    "non_aligned_open_time_ms": wrong_intervals,
                },
            )

        invalid_ohlc = [
            candle.open_time_ms
            for candle in sorted_candles
            if (
                candle.open < 0 or candle.high < 0 or candle.low < 0 or candle.close < 0
                or candle.high < max(candle.open, candle.close)
                or candle.low > min(candle.open, candle.close)
                or candle.low > candle.high
            )
        ]
        if invalid_ohlc:
            return _error_result(
                reason_code=REASON_INVALID_CANDLE,
                source_provenance=provenance,
                signal_time_ms=signal_ms,
                cutoff_time_ms=cutoff_ms,
                evaluation_asof_ms=asof_ms,
                data_quality={
                    "input_candle_count": len(sorted_candles),
                    "invalid_ohlc_open_time_ms": invalid_ohlc,
                },
            )

        window = [
            candle for candle in sorted_candles
            if candle.open_time_ms + CANDLE_MS > signal_ms
            and candle.open_time_ms < cutoff_ms
        ]
        first_window = window[0] if window else None
        last_window = window[-1] if window else None

        def touches_level(candle: PolicyCandle) -> bool:
            return candle.low <= target_n or candle.high >= stop_n

        signal_boundary_uncertain = bool(
            first_window
            and first_window.open_time_ms < signal_ms < first_window.close_time_ms
            and touches_level(first_window)
        )
        cutoff_boundary_uncertain = bool(
            last_window
            and last_window.open_time_ms < cutoff_ms < last_window.close_time_ms
            and touches_level(last_window)
        )

        unavailable_opens = [
            candle.open_time_ms
            for candle in window
            if candle.open_time_ms + CANDLE_MS > signal_ms
            and candle.open_time_ms + CANDLE_MS <= cutoff_ms
            and asof_ms is not None
            and candle.close_time_ms > asof_ms
        ]
        if unavailable_opens:
            return SRRShortTimeoutCandleEvaluation(
                policy_version=SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
                experiment_id=experiment_id,
                direction=direction,
                status=INCOMPLETE_COVERAGE,
                path_class=None,
                reason_code=REASON_UNAVAILABLE_CANDLE,
                entry=entry_n,
                stop=stop_n,
                target=target_n,
                structural_r=structural_n,
                execution_r=execution_n,
                max_hold_minutes=max_hold_n,
                signal_time_ms=signal_ms,
                cutoff_time_ms=cutoff_ms,
                evaluation_asof_ms=asof_ms,
                eligible_candle_count=0,
                first_eligible_candle_ms=None,
                last_eligible_candle_ms=None,
                selected_timeout_candle_ms=None,
                selected_timeout_close=None,
                selected_timeout_close_ms=None,
                signal_boundary_uncertain=signal_boundary_uncertain,
                cutoff_boundary_uncertain=cutoff_boundary_uncertain,
                coverage_complete=False,
                net_r=None,
                return_at_120m=None,
                gross_r=None,
                cost_r_normal=None,
                cost_r_elevated=None,
                net_r_normal=None,
                net_r_elevated=None,
                finalization_eligible=False,
                source_confidence="UNPROVEN" if provenance_obj is None or provenance_obj.is_unproven else "ARCHIVE_VALIDATED_LOCAL",
                source_provenance=provenance,
                data_quality={
                    "input_candle_count": len(sorted_candles),
                    "window_candle_count": len(window),
                    "unavailable_open_time_ms": unavailable_opens,
                },
                diagnostics={"window_closes_ms": [candle.close_time_ms for candle in window]},
            )

        eligible = [
            candle for candle in window
            if candle.open_time_ms >= signal_ms
            and candle.close_time_ms <= cutoff_ms
        ]
        signal_boundary_touched = signal_boundary_uncertain
        cutoff_boundary_touched = cutoff_boundary_uncertain

        if signal_boundary_touched:
            return SRRShortTimeoutCandleEvaluation(
                policy_version=SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
                experiment_id=experiment_id,
                direction=direction,
                status=FIRST_CANDLE_BOUNDARY_UNCERTAIN,
                path_class=FIRST_CANDLE_BOUNDARY_UNCERTAIN,
                reason_code=REASON_SIGNAL_BOUNDARY,
                entry=entry_n,
                stop=stop_n,
                target=target_n,
                structural_r=structural_n,
                execution_r=execution_n,
                max_hold_minutes=max_hold_n,
                signal_time_ms=signal_ms,
                cutoff_time_ms=cutoff_ms,
                evaluation_asof_ms=asof_ms,
                eligible_candle_count=len(eligible),
                first_eligible_candle_ms=eligible[0].open_time_ms if eligible else None,
                last_eligible_candle_ms=eligible[-1].open_time_ms if eligible else None,
                selected_timeout_candle_ms=None,
                selected_timeout_close=None,
                selected_timeout_close_ms=None,
                signal_boundary_uncertain=True,
                cutoff_boundary_uncertain=cutoff_boundary_touched,
                coverage_complete=False,
                net_r=None,
                return_at_120m=None,
                gross_r=None,
                cost_r_normal=None,
                cost_r_elevated=None,
                net_r_normal=None,
                net_r_elevated=None,
                finalization_eligible=False,
                source_confidence="UNPROVEN" if provenance_obj is None or provenance_obj.is_unproven else "ARCHIVE_VALIDATED_LOCAL",
                source_provenance=provenance,
                data_quality={
                    "input_candle_count": len(sorted_candles),
                    "window_candle_count": len(window),
                    "eligible_candle_count": len(eligible),
                    "signal_boundary_open_time_ms": first_window.open_time_ms if first_window else None,
                },
                diagnostics={
                    "eligible_open_times_ms": [candle.open_time_ms for candle in eligible],
                    "eligible_close_times_ms": [candle.close_time_ms for candle in eligible],
                },
            )

        path_class = None
        reason_code = REASON_OK
        selected_candle: PolicyCandle | None = None
        gross_r: float | None = None
        return_120m: float | None = None
        selected_open_ms = selected_close_ms = None

        for candle in eligible:
            tp_now = candle.low <= target_n
            sl_now = candle.high >= stop_n
            if tp_now and sl_now:
                path_class = SL_FIRST
                reason_code = REASON_STOP_FIRST
                gross_r = -1.0
                break
            if sl_now:
                path_class = SL_FIRST
                gross_r = -1.0
                break
            if tp_now:
                path_class = TP_FIRST
                gross_r = 0.75
                break

        # A TP/SL hit is valid only with continuous eligible coverage
        # from the first fully eligible 5m candle through the hit candle.
        if path_class in (TP_FIRST, SL_FIRST):
            first_eligible_open_ms = (
                (signal_ms + CANDLE_MS - 1) // CANDLE_MS
            ) * CANDLE_MS
            expected_eligible = list(
                range(first_eligible_open_ms, candle.open_time_ms + CANDLE_MS, CANDLE_MS)
            )
            actual_eligible = [c.open_time_ms for c in eligible
                               if c.open_time_ms <= candle.open_time_ms]
            actual_set = set(actual_eligible)
            missing = [ms for ms in expected_eligible if ms not in actual_set]
            if missing:
                return SRRShortTimeoutCandleEvaluation(
                    policy_version=SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
                    experiment_id=experiment_id,
                    direction=direction,
                    status=INCOMPLETE_COVERAGE,
                    path_class=None,
                    reason_code=REASON_MISSING_COVERAGE,
                    entry=entry_n,
                    stop=stop_n,
                    target=target_n,
                    structural_r=structural_n,
                    execution_r=execution_n,
                    max_hold_minutes=max_hold_n,
                    signal_time_ms=signal_ms,
                    cutoff_time_ms=cutoff_ms,
                    evaluation_asof_ms=asof_ms,
                    eligible_candle_count=len(eligible),
                    first_eligible_candle_ms=eligible[0].open_time_ms if eligible else None,
                    last_eligible_candle_ms=eligible[-1].open_time_ms if eligible else None,
                    selected_timeout_candle_ms=None,
                    selected_timeout_close=None,
                    selected_timeout_close_ms=None,
                    signal_boundary_uncertain=False,
                    cutoff_boundary_uncertain=False,
                    coverage_complete=False,
                    net_r=None,
                    return_at_120m=None,
                    gross_r=None,
                    cost_r_normal=None,
                    cost_r_elevated=None,
                    net_r_normal=None,
                    net_r_elevated=None,
                    finalization_eligible=False,
                    source_confidence="UNPROVEN" if provenance_obj is None or provenance_obj.is_unproven else "ARCHIVE_VALIDATED_LOCAL",
                    source_provenance=provenance,
                    data_quality={
                        "input_candle_count": len(sorted_candles),
                        "window_candle_count": len(window),
                        "expected_eligible_open_times_ms": expected_eligible,
                        "actual_eligible_open_times_ms": actual_eligible,
                        "missing_eligible_open_times_ms": missing,
                    },
                    diagnostics={"coverage_complete": False},
                )


        if path_class is None:
            if cutoff_boundary_touched:
                return SRRShortTimeoutCandleEvaluation(
                    policy_version=SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
                    experiment_id=experiment_id,
                    direction=direction,
                    status=CUTOFF_BOUNDARY_UNCERTAIN,
                    path_class=CUTOFF_BOUNDARY_UNCERTAIN,
                    reason_code=REASON_CUTOFF_BOUNDARY,
                    entry=entry_n,
                    stop=stop_n,
                    target=target_n,
                    structural_r=structural_n,
                    execution_r=execution_n,
                    max_hold_minutes=max_hold_n,
                    signal_time_ms=signal_ms,
                    cutoff_time_ms=cutoff_ms,
                    evaluation_asof_ms=asof_ms,
                    eligible_candle_count=len(eligible),
                    first_eligible_candle_ms=eligible[0].open_time_ms if eligible else None,
                    last_eligible_candle_ms=eligible[-1].open_time_ms if eligible else None,
                    selected_timeout_candle_ms=None,
                    selected_timeout_close=None,
                    selected_timeout_close_ms=None,
                    signal_boundary_uncertain=False,
                    cutoff_boundary_uncertain=True,
                    coverage_complete=False,
                    net_r=None,
                    return_at_120m=None,
                    gross_r=None,
                    cost_r_normal=None,
                    cost_r_elevated=None,
                    net_r_normal=None,
                    net_r_elevated=None,
                    finalization_eligible=False,
                    source_confidence="UNPROVEN" if provenance_obj is None or provenance_obj.is_unproven else "ARCHIVE_VALIDATED_LOCAL",
                    source_provenance=provenance,
                    data_quality={
                        "input_candle_count": len(sorted_candles),
                        "window_candle_count": len(window),
                        "eligible_candle_count": len(eligible),
                        "cutoff_boundary_open_time_ms": last_window.open_time_ms if last_window else None,
                    },
                    diagnostics={
                        "eligible_open_times_ms": [candle.open_time_ms for candle in eligible],
                        "eligible_close_times_ms": [candle.close_time_ms for candle in eligible],
                    },
                )

            first_eligible_open_ms = (
                (signal_ms + CANDLE_MS - 1) // CANDLE_MS
            ) * CANDLE_MS
            expected_eligible = [
                open_ms
                for open_ms in range(first_eligible_open_ms, cutoff_ms, CANDLE_MS)
                if open_ms + CANDLE_MS <= cutoff_ms
            ]
            actual_eligible = [candle.open_time_ms for candle in eligible]
            missing = [open_ms for open_ms in expected_eligible if open_ms not in actual_eligible]
            if missing:
                return SRRShortTimeoutCandleEvaluation(
                    policy_version=SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
                    experiment_id=experiment_id,
                    direction=direction,
                    status=INCOMPLETE_COVERAGE,
                    path_class=None,
                    reason_code=REASON_MISSING_COVERAGE,
                    entry=entry_n,
                    stop=stop_n,
                    target=target_n,
                    structural_r=structural_n,
                    execution_r=execution_n,
                    max_hold_minutes=max_hold_n,
                    signal_time_ms=signal_ms,
                    cutoff_time_ms=cutoff_ms,
                    evaluation_asof_ms=asof_ms,
                    eligible_candle_count=len(eligible),
                    first_eligible_candle_ms=eligible[0].open_time_ms if eligible else None,
                    last_eligible_candle_ms=eligible[-1].open_time_ms if eligible else None,
                    selected_timeout_candle_ms=None,
                    selected_timeout_close=None,
                    selected_timeout_close_ms=None,
                    signal_boundary_uncertain=False,
                    cutoff_boundary_uncertain=False,
                    coverage_complete=False,
                    net_r=None,
                    return_at_120m=None,
                    gross_r=None,
                    cost_r_normal=None,
                    cost_r_elevated=None,
                    net_r_normal=None,
                    net_r_elevated=None,
                    finalization_eligible=False,
                    source_confidence="UNPROVEN" if provenance_obj is None or provenance_obj.is_unproven else "ARCHIVE_VALIDATED_LOCAL",
                    source_provenance=provenance,
                    data_quality={
                        "input_candle_count": len(sorted_candles),
                        "window_candle_count": len(window),
                        "expected_eligible_open_times_ms": expected_eligible,
                        "actual_eligible_open_times_ms": actual_eligible,
                        "missing_eligible_open_times_ms": missing,
                    },
                    diagnostics={"coverage_complete": False},
                )

            selected_candle = eligible[-1]
            selected_open_ms = selected_candle.open_time_ms
            selected_close_ms = selected_candle.close_time_ms
            path_class = TIMEOUT
            reason_code = REASON_OK
            return_120m = (selected_candle.close - entry_n) / entry_n * 100.0
            gross_r = -(return_120m / 100.0) * entry_n / execution_n

        assert selected_candle is None or selected_open_ms is not None
        cost_r_normal = entry_n * 0.0021 / execution_n
        cost_r_elevated = entry_n * 0.0031 / execution_n
        net_r_normal = gross_r - cost_r_normal
        net_r_elevated = gross_r - cost_r_elevated
        return SRRShortTimeoutCandleEvaluation(
            policy_version=SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
            experiment_id=experiment_id,
            direction=direction,
            status=RESOLVED,
            path_class=path_class,
            reason_code=reason_code,
            entry=entry_n,
            stop=stop_n,
            target=target_n,
            structural_r=structural_n,
            execution_r=execution_n,
            max_hold_minutes=max_hold_n,
            signal_time_ms=signal_ms,
            cutoff_time_ms=cutoff_ms,
            evaluation_asof_ms=asof_ms,
            eligible_candle_count=len(eligible),
            first_eligible_candle_ms=eligible[0].open_time_ms if eligible else None,
            last_eligible_candle_ms=eligible[-1].open_time_ms if eligible else None,
            selected_timeout_candle_ms=selected_open_ms,
            selected_timeout_close=None if selected_candle is None else selected_candle.close,
            selected_timeout_close_ms=selected_close_ms,
            signal_boundary_uncertain=False,
            cutoff_boundary_uncertain=False,
            coverage_complete=True,
            net_r=net_r_normal,
            return_at_120m=return_120m,
            gross_r=gross_r,
            cost_r_normal=cost_r_normal,
            cost_r_elevated=cost_r_elevated,
            net_r_normal=net_r_normal,
            net_r_elevated=net_r_elevated,
            finalization_eligible=True,
            source_confidence="UNPROVEN" if provenance_obj is None or provenance_obj.is_unproven else "ARCHIVE_VALIDATED_LOCAL",
            source_provenance=provenance,
            data_quality={
                "input_candle_count": len(sorted_candles),
                "window_candle_count": len(window),
                "eligible_candle_count": len(eligible),
                "expected_eligible_open_times_ms": [
                    open_ms
                    for open_ms in range(
                        ((signal_ms + CANDLE_MS - 1) // CANDLE_MS) * CANDLE_MS,
                        cutoff_ms,
                        CANDLE_MS,
                    )
                    if open_ms + CANDLE_MS <= cutoff_ms
                ],
                "actual_eligible_open_times_ms": [candle.open_time_ms for candle in eligible],
            },
            diagnostics={
                "eligible_open_times_ms": [candle.open_time_ms for candle in eligible],
                "eligible_close_times_ms": [candle.close_time_ms for candle in eligible],
                "selected_timeout_open_utc": _utc_iso_ms(selected_open_ms),
                "selected_timeout_close_utc": _utc_iso_ms(selected_close_ms),
                "last_eligible_close_utc": _utc_iso_ms(eligible[-1].close_time_ms),
            },
        )


def evaluate_srr_short_timeout_candle_path(
    *,
    signal_time_ms: int,
    entry: float,
    stop: float,
    target: float,
    structural_r: float,
    execution_r: float,
    max_hold_minutes: int = SRR_DEFAULT_MAX_HOLD_MINUTES,
    candles: Sequence[Any],
    evaluation_asof_ms: int | None = None,
    experiment_id: str = SRR_EXPERIMENT_ID,
    direction: str = SRR_DIRECTION,
    intrabar_policy: str = SRR_DEFAULT_INTRABAR_POLICY,
    source_provenance: Any = None,
    cutoff_time_ms: int | None = None,
) -> SRRShortTimeoutCandleEvaluation:
    """Evaluate a frozen SRR SHORT candle path under policy v1."""
    return SrrShortTimeoutCandlePolicy().evaluate(
        signal_time_ms=signal_time_ms,
        entry=entry,
        stop=stop,
        target=target,
        structural_r=structural_r,
        execution_r=execution_r,
        max_hold_minutes=max_hold_minutes,
        candles=candles,
        evaluation_asof_ms=evaluation_asof_ms,
        experiment_id=experiment_id,
        direction=direction,
        intrabar_policy=intrabar_policy,
        source_provenance=source_provenance,
        cutoff_time_ms=cutoff_time_ms,
    )
