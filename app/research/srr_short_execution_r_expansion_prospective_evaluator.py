"""Isolated prospective evaluator route for frozen SRR SHORT execution-R paths.

This adapter is the single routing point for exactly:

``SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1``

It reuses ``SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1`` as the sole candle-path,
STOP_FIRST, uncertainty, coverage, and economics implementation. It does not
duplicate that policy. All other prospective experiments continue through
``ProspectiveOOSEvaluator`` unchanged.

The route requires an explicit historical candle-history provider. The provider
must request a bounded historical interval, not merely latest-N candles, and
must distinguish source fetch errors from incomplete coverage. The route never
writes outcomes when source semantics or frozen geometry are unverified.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.models import Candle
from app.research.policies.srr_short_timeout_candle_policy_v1 import (
    CANDLE_MS,
    REASON_INVALID_DIRECTION,
    REASON_INVALID_EXPERIMENT,
    FROZEN_FREEZE_TS,
    SRR_DIRECTION,
    SRR_EXPERIMENT_ID,
    SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
    evaluate_srr_short_timeout_candle_path,
)

logger = logging.getLogger(__name__)

SRR_ADAPTER_ID = "SRR_SHORT_PROSPECTIVE_EVALUATOR_POLICY_ROUTING_V1"
SOURCE_FETCH_ERROR = "SOURCE_FETCH_ERROR"
SOURCE_INCOMPLETE_COVERAGE = "SOURCE_INCOMPLETE_COVERAGE"
SOURCE_TIMESTAMP_INVALID = "SOURCE_TIMESTAMP_INVALID"
SOURCE_CONFLICTING_CANDLES = "SOURCE_CONFLICTING_CANDLES"
SOURCE_VALIDATED = "SOURCE_VALIDATED"

FROZEN_SL_R = 2.0
FROZEN_TP_R = 1.5
FROZEN_MAX_HOLD_MINUTES = 120
FROZEN_INTRABAR_POLICY = "STOP_FIRST"
NORMAL_ROUND_TRIP_COST = 0.0021
ELEVATED_ROUND_TRIP_COST = 0.0031

FEATURE_EXPECTED = (
    "_structural_r",
    "_frozen_sl_r",
    "_frozen_tp_r",
    "_frozen_max_hold",
    "_frozen_intrabar_policy",
    "_execution_r_abs",
    "_execution_r_definition",
    "_frozen_freeze_ts",
)


class HistoricalCandleSource(Protocol):
    """Explicit source contract for historical 5-minute candle retrieval."""

    def fetch_5m_candles(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        evaluation_asof_ms: int | None = None,
    ) -> "HistoricalCandleSourceResult": ...


@dataclass(frozen=True)
class HistoricalCandleSourceResult:
    status: str
    candles: tuple[Candle, ...] = ()
    reason_code: str = ""
    diagnostics: Mapping[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == SOURCE_VALIDATED


@dataclass(frozen=True)
class SrrFrozenGeometry:
    entry: float
    structural_r: float
    execution_r: float
    stop: float
    target: float
    max_hold_minutes: int
    intrabar_policy: str
    freeze_ts: str


@dataclass(frozen=True)
class SrrRouteResult:
    experiment_id: str
    observation_id: Any
    status: str
    reason_code: str
    path_class: str | None
    finalization_eligible: bool
    source_status: str
    geometry_valid: bool
    eligible_candle_count: int
    gross_r: float | None
    cost_r_normal: float | None
    cost_r_elevated: float | None
    net_r_normal: float | None
    net_r_elevated: float | None
    policy_result: Mapping[str, Any] | None
    source_diagnostics: Mapping[str, Any]
    route_diagnostics: Mapping[str, Any]


def parse_utc_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError("fractional millisecond timestamp")
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def load_features(features: Any) -> dict[str, Any]:
    if features is None:
        return {}
    if isinstance(features, str):
        try:
            loaded = json.loads(features)
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return dict(features) if isinstance(features, Mapping) else {}


def is_srr_short_experiment(experiment_id: Any) -> bool:
    return experiment_id == SRR_EXPERIMENT_ID


def _close(value: Any, name: str) -> float:
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{name} is not finite")
    return number


def extract_frozen_geometry(obs: Mapping[str, Any]) -> tuple[SrrFrozenGeometry | None, str, dict[str, Any]]:
    """Validate frozen variant geometry and registry-compatible features.

    Missing or malformed frozen data can never silently fall back to mutable
    scanner geometry. Both the variant tuple and the frozen feature contract
    are mandatory for this route.
    """
    diagnostics: dict[str, Any] = {
        "experiment_id": obs.get("experiment_id"),
        "observation_id": obs.get("observation_id"),
        "direction": obs.get("direction"),
        "variant_entry_present": obs.get("variant_entry") is not None,
        "variant_stop_present": obs.get("variant_stop") is not None,
        "variant_target_present": obs.get("variant_target") is not None,
    }
    if obs.get("experiment_id") != SRR_EXPERIMENT_ID:
        return None, REASON_INVALID_EXPERIMENT, diagnostics
    if obs.get("direction") != SRR_DIRECTION:
        return None, REASON_INVALID_DIRECTION, diagnostics

    missing_variant = [
        key for key in ("variant_entry", "variant_stop", "variant_target")
        if obs.get(key) is None
    ]
    if missing_variant:
        return None, "FROZEN_VARIANT_TUPLE_MISSING", {**diagnostics, "missing_variant_fields": missing_variant}

    features = load_features(obs.get("features"))
    missing_features = [key for key in FEATURE_EXPECTED if key not in features]
    if missing_features:
        return None, "FROZEN_FEATURES_MISSING", {**diagnostics, "missing_features": missing_features}
    diagnostics["frozen_features"] = {key: features.get(key) for key in FEATURE_EXPECTED}

    try:
        entry = _close(obs.get("reference_price"), "reference_price")
        variant_entry = _close(obs.get("variant_entry"), "variant_entry")
        invalidation = _close(obs.get("invalidation_price"), "invalidation_price")
        stop = _close(obs.get("variant_stop"), "variant_stop")
        target = _close(obs.get("variant_target"), "variant_target")
        structural_r = _close(features.get("_structural_r"), "_structural_r")
        execution_r = _close(features.get("_execution_r_abs"), "_execution_r_abs")
        max_hold = int(features.get("_frozen_max_hold"))
        freeze_ts = str(features.get("_frozen_freeze_ts")).strip()
        structural_abs = abs(entry - invalidation)
        expected_stop = entry + FROZEN_SL_R * structural_r
        expected_target = entry - FROZEN_TP_R * structural_r
        expected_execution_r = FROZEN_SL_R * structural_r
        checks = {
            "entry_matches_reference": abs(variant_entry - entry) <= 1e-12,
            "structural_r_matches_reference": abs(structural_r - structural_abs) <= 1e-12,
            "structural_r_positive": structural_r > 0,
            "invalidation_positive": invalidation > 0,
            "invalidation_above_entry": invalidation > entry,
            "stop_matches_frozen": abs(stop - expected_stop) <= 1e-12,
            "target_matches_frozen": abs(target - expected_target) <= 1e-12,
            "stop_above_entry": stop > entry,
            "target_below_entry": target < entry,
            "execution_r_matches_frozen": abs(execution_r - expected_execution_r) <= 1e-12,
            "max_hold_matches_frozen": max_hold == FROZEN_MAX_HOLD_MINUTES,
            "intrabar_policy_matches_frozen": features.get("_frozen_intrabar_policy") == FROZEN_INTRABAR_POLICY,
            "freeze_ts_matches_frozen": freeze_ts == FROZEN_FREEZE_TS,
        }
    except (TypeError, ValueError, OverflowError) as exc:
        diagnostics["geometry_error"] = str(exc)
        return None, "FROZEN_GEOMETRY_MALFORMED", diagnostics

    diagnostics["geometry_checks"] = checks
    failed = [key for key, passed in checks.items() if not passed]
    if failed:
        return None, "FROZEN_GEOMETRY_VALIDATION_FAILED", {**diagnostics, "failed_checks": failed}

    geometry = SrrFrozenGeometry(
        entry=entry,
        structural_r=structural_r,
        execution_r=execution_r,
        stop=stop,
        target=target,
        max_hold_minutes=max_hold,
        intrabar_policy=str(features.get("_frozen_intrabar_policy")),
        freeze_ts=freeze_ts,
    )
    return geometry, "FROZEN_GEOMETRY_VALIDATED", diagnostics


def validate_source_result(
    result: Any,
    *,
    symbol: str,
    requested_start_ms: int,
    requested_end_ms: int,
    required_close_ms: int,
    evaluation_asof_ms: int | None,
) -> HistoricalCandleSourceResult:
    """Normalize and independently validate a historical source response.

    A successful HTTP or provider call is not sufficient. The adapter checks
    alignment, chronology, duplicates/conflicts, availability, and interval
    coverage before the policy is allowed to interpret any candle.
    """
    if not isinstance(result, HistoricalCandleSourceResult):
        return HistoricalCandleSourceResult(
            status=SOURCE_FETCH_ERROR,
            reason_code="SOURCE_RESULT_TYPE_INVALID",
            diagnostics={"symbol": symbol, "actual_type": type(result).__name__},
        )
    if result.status != SOURCE_VALIDATED:
        return result

    diagnostics = dict(result.diagnostics or {})
    diagnostics.update(
        {
            "symbol": symbol,
            "requested_start_ms": requested_start_ms,
            "requested_end_ms": requested_end_ms,
            "required_close_ms": required_close_ms,
        }
    )

    normalized: list[Candle] = []
    by_open: dict[int, Candle] = {}
    alignment_errors: list[int] = []
    ohlc_errors: list[int] = []
    availability_errors: list[int] = []
    for candle in result.candles:
        try:
            open_ms = int(candle.timestamp)
            high = _close(candle.high, "high")
            low = _close(candle.low, "low")
            close = _close(candle.close, "close")
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            return HistoricalCandleSourceResult(
                status=SOURCE_TIMESTAMP_INVALID,
                reason_code="SOURCE_CANDLE_INVALID",
                diagnostics={**diagnostics, "error": str(exc)},
            )
        if open_ms % CANDLE_MS != 0:
            alignment_errors.append(open_ms)
        if open_ms < requested_start_ms or open_ms > requested_end_ms:
            availability_errors.append(open_ms)
        # Never interpret OHLC for a candle that was still open at ASOF.
        # Providers must not supply future candles to the frozen path.
        if evaluation_asof_ms is not None and open_ms + CANDLE_MS > evaluation_asof_ms:
            availability_errors.append(open_ms)
        if high < max(candle.open, close) or low > min(candle.open, close) or low > high:
            ohlc_errors.append(open_ms)
        if open_ms in by_open:
            previous = by_open[open_ms]
            if (previous.open, previous.high, previous.low, previous.close, previous.volume) != (
                candle.open, high, low, close, candle.volume,
            ):
                return HistoricalCandleSourceResult(
                    status=SOURCE_CONFLICTING_CANDLES,
                    reason_code="SOURCE_CONFLICTING_CANDLES",
                    diagnostics={**diagnostics, "conflicting_open_time_ms": open_ms},
                )
            continue
        by_open[open_ms] = candle

    if alignment_errors:
        return HistoricalCandleSourceResult(
            status=SOURCE_TIMESTAMP_INVALID,
            reason_code="SOURCE_TIMESTAMP_INVALID",
            diagnostics={**diagnostics, "non_5m_open_time_ms": alignment_errors},
        )
    if ohlc_errors:
        return HistoricalCandleSourceResult(
            status=SOURCE_TIMESTAMP_INVALID,
            reason_code="SOURCE_OHLC_INVALID",
            diagnostics={**diagnostics, "invalid_ohlc_open_time_ms": ohlc_errors},
        )
    if availability_errors:
        return HistoricalCandleSourceResult(
            status=SOURCE_INCOMPLETE_COVERAGE,
            reason_code="SOURCE_CANDLE_NOT_AVAILABLE_AT_ASOF",
            diagnostics={**diagnostics, "unavailable_open_time_ms": availability_errors},
        )

    ordered = sorted(by_open.values(), key=lambda candle: candle.timestamp)
    if any(ordered[index - 1].timestamp >= ordered[index].timestamp for index in range(1, len(ordered))):
        return HistoricalCandleSourceResult(
            status=SOURCE_TIMESTAMP_INVALID,
            reason_code="SOURCE_CANDLES_NOT_SORTED",
            diagnostics={**diagnostics, "input_order_preserved": False},
        )

    latest_closed_open_ms = (
        requested_end_ms if evaluation_asof_ms is None
        else min(requested_end_ms, (evaluation_asof_ms // CANDLE_MS - 1) * CANDLE_MS)
    )
    expected_opens = list(range(requested_start_ms, latest_closed_open_ms + 1, CANDLE_MS))
    present = {candle.timestamp for candle in ordered}
    missing = [open_ms for open_ms in expected_opens if open_ms not in present]
    if missing:
        return HistoricalCandleSourceResult(
            status=SOURCE_INCOMPLETE_COVERAGE,
            reason_code="SOURCE_INTERVAL_INCOMPLETE",
            diagnostics={**diagnostics, "missing_open_time_ms": missing, "present_count": len(present)},
        )

    diagnostics.update(
        {
            "validated_open_count": len(ordered),
            "first_open_ms": ordered[0].timestamp if ordered else None,
            "last_open_ms": ordered[-1].timestamp if ordered else None,
        }
    )
    return HistoricalCandleSourceResult(
        status=SOURCE_VALIDATED,
        candles=tuple(ordered),
        reason_code="SOURCE_VALIDATED",
        diagnostics=diagnostics,
    )


class BybitHistoricalCandleSource:
    """Bounded historical Bybit adapter used only by the SRR-specific route.

    It calls the existing public Bybit client but adds explicit start/end
    pagination. It does not change production endpoints, authentication, or
    the generic evaluator's latest-N behavior.
    """

    def __init__(self, client: Any, *, page_limit: int = 200) -> None:
        if client is None:
            raise ValueError("Bybit historical source requires a client")
        self._client = client
        self._page_limit = max(1, min(int(page_limit), 200))

    def fetch_5m_candles(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        evaluation_asof_ms: int | None = None,
    ) -> HistoricalCandleSourceResult:
        if end_ms < start_ms:
            return HistoricalCandleSourceResult(
                status=SOURCE_FETCH_ERROR,
                reason_code="SOURCE_INTERVAL_REVERSED",
                diagnostics={"symbol": symbol, "start_ms": start_ms, "end_ms": end_ms},
            )
        collected: list[Candle] = []
        page_end = end_ms if evaluation_asof_ms is None else min(
            end_ms, (evaluation_asof_ms // CANDLE_MS - 1) * CANDLE_MS
        )
        pages = 0
        max_pages = 100
        try:
            while page_end >= start_ms and pages < max_pages:
                pages += 1
                payload = self._client._public_get(
                "/v5/market/kline",
                category="linear",
                symbol=symbol,
                interval="5",
                limit=self._page_limit,
                end=page_end,
                )
                candles = [
                Candle(int(r[0]), *(float(value) for value in r[1:6]))
                for r in payload["result"]["list"]
                ]
                page = [
                    candle for candle in candles
                    if start_ms <= int(candle.timestamp) <= page_end
                ]
                if not page:
                    break
                page = sorted(page, key=lambda candle: int(candle.timestamp))
                collected = page + collected
                first_open = int(page[0].timestamp)
                if first_open <= start_ms:
                    break
                page_end = first_open - CANDLE_MS
            if pages >= max_pages and page_end >= start_ms:
                return HistoricalCandleSourceResult(
                    status=SOURCE_INCOMPLETE_COVERAGE,
                    reason_code="SOURCE_PAGINATION_PAGE_LIMIT",
                    diagnostics={"symbol": symbol, "pages": pages, "remaining_page_end_ms": page_end},
                )
        except Exception as exc:
            logger.exception("SRR historical source fetch failed for %s", symbol)
            return HistoricalCandleSourceResult(
                status=SOURCE_FETCH_ERROR,
                reason_code="SOURCE_FETCH_EXCEPTION",
                diagnostics={"symbol": symbol, "error": str(exc)},
            )

        return validate_source_result(
            HistoricalCandleSourceResult(
                status=SOURCE_VALIDATED,
                candles=tuple(collected),
                diagnostics={"pages": pages, "provider": "BYBIT_V5_KLINE_EXPLICIT_START_END"},
            ),
            symbol=symbol,
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
            required_close_ms=end_ms,
            evaluation_asof_ms=evaluation_asof_ms,
        )


class HistoricalArchiveCandleSource:
    """Offline fixture source used for read-only dry-runs and unit tests."""

    def __init__(self, candles_by_symbol: Mapping[str, Sequence[Any]], *, archive_id: str = "") -> None:
        self._candles_by_symbol = candles_by_symbol
        self._archive_id = archive_id

    def fetch_5m_candles(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        evaluation_asof_ms: int | None = None,
    ) -> HistoricalCandleSourceResult:
        rows = self._candles_by_symbol.get(symbol)
        if rows is None:
            return HistoricalCandleSourceResult(
                status=SOURCE_FETCH_ERROR,
                reason_code="SOURCE_SYMBOL_NOT_FOUND",
                diagnostics={"symbol": symbol, "archive_id": self._archive_id},
            )
        normalized: list[Candle] = []
        for row in rows:
            try:
                if isinstance(row, Candle):
                    normalized.append(row)
                elif hasattr(row, "open_time_ms"):
                    normalized.append(Candle(
                        int(row.open_time_ms),
                        float(row.open),
                        float(row.high),
                        float(row.low),
                        float(row.close),
                        float(getattr(row, "volume", 0.0)),
                    ))
                elif hasattr(row, "timestamp"):
                    normalized.append(Candle(
                        int(row.timestamp),
                        float(row.open),
                        float(row.high),
                        float(row.low),
                        float(row.close),
                        float(getattr(row, "volume", 0.0)),
                    ))
                else:
                    normalized.append(Candle(
                        int(row.get("open_time_ms", row.get("timestamp"))),
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("volume", 0.0)),
                    ))
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
                return HistoricalCandleSourceResult(
                    status=SOURCE_FETCH_ERROR,
                    reason_code="SOURCE_ARCHIVE_ROW_INVALID",
                    diagnostics={"symbol": symbol, "error": str(exc)},
                )
        available = tuple(
            candle for candle in normalized
            if start_ms <= candle.timestamp <= end_ms
            and (evaluation_asof_ms is None or candle.timestamp + CANDLE_MS <= evaluation_asof_ms)
        )
        return validate_source_result(
            HistoricalCandleSourceResult(
                status=SOURCE_VALIDATED,
                candles=available,
                diagnostics={"archive_id": self._archive_id, "provider": "OFFLINE_ARCHIVE"},
            ),
            symbol=symbol,
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
            required_close_ms=end_ms,
            evaluation_asof_ms=evaluation_asof_ms,
        )


class SrrShortExecutionRExpansionProspectiveEvaluator:
    """Route only the frozen SRR experiment to the verified policy."""

    def __init__(
        self,
        conn: Any,
        candle_source: HistoricalCandleSource,
        *,
        dry_run: bool = True,
        write_outcomes: Callable[..., None] | None = None,
        activation_boundary: Any = None,
    ) -> None:
        self._conn = conn
        self._candle_source = candle_source
        self._dry_run = bool(dry_run)
        self._activation_boundary = activation_boundary
        if not self._dry_run and write_outcomes is None:
            raise ValueError(
                "SRR persistence requires write_outcomes when dry_run=False"
            )
        self._write_outcomes = write_outcomes

    @property
    def experiment_id(self) -> str:
        return SRR_EXPERIMENT_ID

    @property
    def adapter_id(self) -> str:
        return SRR_ADAPTER_ID

    def route(self, obs: Mapping[str, Any], evaluation_asof_ms: int) -> SrrRouteResult:
        geometry, geometry_reason, geometry_diagnostics = extract_frozen_geometry(obs)
        if geometry is None:
            return SrrRouteResult(
                experiment_id=str(obs.get("experiment_id")),
                observation_id=obs.get("observation_id"),
                status="INVALID_INPUT",
                reason_code=geometry_reason,
                path_class=None,
                finalization_eligible=False,
                source_status="NOT_FETCHED",
                geometry_valid=False,
                eligible_candle_count=0,
                gross_r=None,
                cost_r_normal=None,
                cost_r_elevated=None,
                net_r_normal=None,
                net_r_elevated=None,
                policy_result=None,
                source_diagnostics={},
                route_diagnostics=geometry_diagnostics,
            )

        assert geometry is not None
        signal_ms = parse_utc_ms(obs.get("signal_time"))
        cutoff_ms = signal_ms + geometry.max_hold_minutes * 60_000
        if signal_ms <= parse_utc_ms(FROZEN_FREEZE_TS):
            return SrrRouteResult(
                experiment_id=SRR_EXPERIMENT_ID,
                observation_id=obs.get("observation_id"),
                status="INVALID_INPUT",
                reason_code="SIGNAL_NOT_AFTER_FREEZE",
                path_class=None,
                finalization_eligible=False,
                source_status="NOT_FETCHED",
                geometry_valid=True,
                eligible_candle_count=0,
                gross_r=None,
                cost_r_normal=None,
                cost_r_elevated=None,
                net_r_normal=None,
                net_r_elevated=None,
                policy_result=None,
                source_diagnostics={},
                route_diagnostics={**geometry_diagnostics, "signal_ms": signal_ms, "freeze_ts": geometry.freeze_ts},
            )

        # The source request must include the first fully eligible 5m candle
        # and every eligible candle through the frozen cutoff.
        first_eligible_open_ms = ((signal_ms + CANDLE_MS - 1) // CANDLE_MS) * CANDLE_MS
        requested_start_ms = (signal_ms // CANDLE_MS) * CANDLE_MS
        requested_end_ms = ((cutoff_ms - CANDLE_MS) // CANDLE_MS) * CANDLE_MS
        source_result = validate_source_result(
            self._candle_source.fetch_5m_candles(
                str(obs.get("symbol")),
                requested_start_ms,
                requested_end_ms,
                evaluation_asof_ms,
            ),
            symbol=str(obs.get("symbol")),
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            required_close_ms=cutoff_ms,
            evaluation_asof_ms=evaluation_asof_ms,
        )
        source_diagnostics = dict(source_result.diagnostics or {})

        if source_result.status == SOURCE_FETCH_ERROR:
            return SrrRouteResult(
                experiment_id=SRR_EXPERIMENT_ID,
                observation_id=obs.get("observation_id"),
                status="SOURCE_UNVERIFIABLE",
                reason_code=source_result.reason_code or SOURCE_FETCH_ERROR,
                path_class=None,
                finalization_eligible=False,
                source_status=SOURCE_FETCH_ERROR,
                geometry_valid=True,
                eligible_candle_count=0,
                gross_r=None,
                cost_r_normal=None,
                cost_r_elevated=None,
                net_r_normal=None,
                net_r_elevated=None,
                policy_result=None,
                source_diagnostics=source_diagnostics,
                route_diagnostics=geometry_diagnostics,
            )
        if source_result.status == SOURCE_INCOMPLETE_COVERAGE:
            return SrrRouteResult(
                experiment_id=SRR_EXPERIMENT_ID,
                observation_id=obs.get("observation_id"),
                status="INCOMPLETE_COVERAGE",
                reason_code=source_result.reason_code or SOURCE_INCOMPLETE_COVERAGE,
                path_class=None,
                finalization_eligible=False,
                source_status=SOURCE_INCOMPLETE_COVERAGE,
                geometry_valid=True,
                eligible_candle_count=0,
                gross_r=None,
                cost_r_normal=None,
                cost_r_elevated=None,
                net_r_normal=None,
                net_r_elevated=None,
                policy_result=None,
                source_diagnostics=source_diagnostics,
                route_diagnostics=geometry_diagnostics,
            )
        if source_result.status != SOURCE_VALIDATED:
            return SrrRouteResult(
                experiment_id=SRR_EXPERIMENT_ID,
                observation_id=obs.get("observation_id"),
                status="INVALID_INPUT",
                reason_code=source_result.reason_code or SOURCE_TIMESTAMP_INVALID,
                path_class=None,
                finalization_eligible=False,
                source_status=source_result.status,
                geometry_valid=True,
                eligible_candle_count=0,
                gross_r=None,
                cost_r_normal=None,
                cost_r_elevated=None,
                net_r_normal=None,
                net_r_elevated=None,
                policy_result=None,
                source_diagnostics=source_diagnostics,
                route_diagnostics=geometry_diagnostics,
            )

        policy = evaluate_srr_short_timeout_candle_path(
            signal_time_ms=signal_ms,
            entry=geometry.entry,
            stop=geometry.stop,
            target=geometry.target,
            structural_r=geometry.structural_r,
            execution_r=geometry.execution_r,
            max_hold_minutes=geometry.max_hold_minutes,
            candles=source_result.candles,
            evaluation_asof_ms=evaluation_asof_ms,
            experiment_id=SRR_EXPERIMENT_ID,
            direction=SRR_DIRECTION,
            intrabar_policy=geometry.intrabar_policy,
            source_provenance={
                "source_kind": source_diagnostics.get("provider", "EXPLICIT_HISTORICAL_SOURCE"),
                "source_id": str(source_diagnostics.get("archive_id", f"{SRR_ADAPTER_ID}:{obs.get('symbol')}")),
                "retrieval_method": source_diagnostics.get("provider"),
                "notes": "validated historical interval; production source equivalence not established",
            },
            cutoff_time_ms=cutoff_ms,
        )
        result_diagnostics = dict(geometry_diagnostics)
        result_diagnostics.update(
            {
                "adapter_id": SRR_ADAPTER_ID,
                "policy_version": SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
                "dry_run": self._dry_run,
                "source_status": source_result.status,
                "signal_ms": signal_ms,
                "cutoff_ms": cutoff_ms,
                "evaluation_asof_ms": evaluation_asof_ms,
            }
        )
        return SrrRouteResult(
            experiment_id=SRR_EXPERIMENT_ID,
            observation_id=obs.get("observation_id"),
            status=policy.status,
            reason_code=policy.reason_code,
            path_class=policy.path_class,
            finalization_eligible=bool(policy.finalization_eligible),
            source_status=SOURCE_VALIDATED,
            geometry_valid=True,
            eligible_candle_count=policy.eligible_candle_count,
            gross_r=policy.gross_r,
            cost_r_normal=policy.cost_r_normal,
            cost_r_elevated=policy.cost_r_elevated,
            net_r_normal=policy.net_r_normal,
            net_r_elevated=policy.net_r_elevated,
            policy_result=policy.as_dict(),
            source_diagnostics=source_diagnostics,
            route_diagnostics=result_diagnostics,
        )

    def run_evaluation_cycle(self, experiment_id: str) -> dict[str, Any]:
        """Evaluate the SRR experiment without writing outcomes in dry-run mode."""
        stats = {
            "experiment_id": experiment_id,
            "adapter_id": SRR_ADAPTER_ID,
            "signals_checked": 0,
            "finalized": 0,
            "uncertain": 0,
            "source_errors": 0,
            "policy_mismatches": 0,
            "errors": 0,
            "observations": [],
        }
        if experiment_id != SRR_EXPERIMENT_ID:
            stats["errors"] += 1
            logger.error("SRR adapter received unrelated experiment %s", experiment_id)
            return stats

        cursor = self._conn.cursor()
        try:
            # The SRR-specific lifecycle never depends on the generic
            # prospective_outcome table. When an activation boundary is
            # configured, the strict eligibility filter is applied here in
            # SQL so that pre-activation observations never reach the queue,
            # never trigger candle fetches, and never invoke the frozen
            # policy. The boundary filter is intentionally stricter than the
            # generic prospective evaluator path.
            activation_filter = ""
            activation_params: tuple[Any, ...] = ()
            if self._activation_boundary is not None:
                from app.research.srr_short_writer_activation_boundary_v1 import (
                    build_activation_sql_filters,
                )

                activation_filter, activation_params = build_activation_sql_filters(
                    self._activation_boundary
                )
                activation_filter = f"AND {activation_filter}"

            cursor.execute(
                f"""
                SELECT o.observation_id, o.symbol, o.signal_time, o.experiment_id,
                       o.direction, o.reference_price, o.invalidation_price,
                       o.variant_entry, o.variant_stop, o.variant_target, o.features,
                       r.is_final, r.observation_id
                FROM research.prospective_observation o
                JOIN research.prospective_experiment e
                  ON e.experiment_id = o.experiment_id
                LEFT JOIN research.srr_short_execution_prospective_outcome r
                  ON r.observation_id = o.observation_id
                LEFT JOIN research.prospective_experiment_direction_state d
                  ON d.experiment_id = o.experiment_id
                 AND d.direction = o.direction
                WHERE o.experiment_id = %s
                  AND e.started_at IS NOT NULL
                  AND o.signal_time >= e.started_at
                  AND o.signal_time > %s
                  AND o.direction = 'SHORT'
                  AND (r.observation_id IS NULL OR r.is_final = FALSE)
                  AND (
                      d.status IS NULL
                      OR d.status = 'RUNNING'
                      OR r.observation_id IS NOT NULL
                  )
                  {activation_filter}
                ORDER BY o.observation_id
                """,
                (SRR_EXPERIMENT_ID, FROZEN_FREEZE_TS, *activation_params),
            )
            rows = cursor.fetchall()
        except Exception:
            logger.exception("SRR adapter failed to load observations")
            stats["errors"] += 1
            rows = []
        finally:
            cursor.close()

        asof_ms = self._current_asof_ms()
        for row in rows:
            obs = {
                "observation_id": row[0],
                "symbol": row[1],
                "signal_time": row[2],
                "experiment_id": row[3],
                "direction": row[4],
                "reference_price": row[5],
                "invalidation_price": row[6],
                "variant_entry": row[7],
                "variant_stop": row[8],
                "variant_target": row[9],
                "features": row[10],
                "is_final": row[11],
                "outcome_exists": row[12] is not None,
            }
            try:
                result = self.route(obs, asof_ms)
            except Exception as exc:
                logger.exception("SRR adapter route failed for observation %s", obs.get("observation_id"))
                stats["errors"] += 1
                stats["observations"].append({
                    "observation_id": obs.get("observation_id"),
                    "status": "INVALID_INPUT",
                    "reason_code": "ROUTE_EXCEPTION",
                    "finalization_eligible": False,
                    "error": str(exc),
                })
                continue
            stats["signals_checked"] += 1
            record = {
                "observation_id": result.observation_id,
                "symbol": obs.get("symbol"),
                "status": result.status,
                "reason_code": result.reason_code,
                "path_class": result.path_class,
                "finalization_eligible": result.finalization_eligible,
                "source_status": result.source_status,
                "geometry_valid": result.geometry_valid,
                "eligible_candle_count": result.eligible_candle_count,
                "gross_r": result.gross_r,
                "cost_r_normal": result.cost_r_normal,
                "cost_r_elevated": result.cost_r_elevated,
                "net_r_normal": result.net_r_normal,
                "net_r_elevated": result.net_r_elevated,
            }
            stats["observations"].append(record)
            if result.finalization_eligible:
                if self._dry_run:
                    stats["finalized"] += 1

            if not self._dry_run:
                try:
                    # Defense-in-depth: re-check the activation boundary
                    # immediately before persistence. This protects against
                    # upstream routing regressions that could otherwise leak a
                    # pre-activation observation into the writer path.
                    if self._activation_boundary is not None:
                        from app.research.srr_short_writer_activation_boundary_v1 import (
                            SrrPreActivationObservation,
                        )

                        try:
                            self._activation_boundary.require_eligible(
                                experiment_id=obs.get("experiment_id"),
                                direction=obs.get("direction"),
                                signal_time=obs.get("signal_time"),
                                observation_id=obs.get("observation_id"),
                            )
                        except SrrPreActivationObservation:
                            record["write_action"] = "SKIPPED_PRE_ACTIVATION"
                            if result.source_status in {
                                SOURCE_FETCH_ERROR,
                                SOURCE_INCOMPLETE_COVERAGE,
                                SOURCE_TIMESTAMP_INVALID,
                                SOURCE_CONFLICTING_CANDLES,
                            }:
                                stats["source_errors"] += 1
                            continue

                    from app.research.srr_short_timeout_persistence_gate import (
                        prepare_srr_persistence_result,
                    )

                    persistence_result = prepare_srr_persistence_result(
                        result,
                        evaluation_asof_ms=asof_ms,
                    )
                    record["persistence_finalization_eligible"] = (
                        persistence_result["finalization_eligible"]
                    )
                    record["persistence_reason_code"] = (
                        persistence_result["reason_code"]
                    )

                    write_result = self._write_outcomes(
                        observation_id=obs["observation_id"],
                        experiment_id=SRR_EXPERIMENT_ID,
                        observation=obs,
                        result=persistence_result,
                    )
                    record["write_action"] = write_result.action

                    if persistence_result["finalization_eligible"] and write_result.is_final:
                        if write_result.action == "FINALIZED" or write_result.action == "INSERTED":
                            stats["finalized"] += 1

                except Exception:
                    stats["errors"] += 1
                    logger.exception(
                        "SRR outcome persistence failed for observation %s",
                        obs["observation_id"],
                    )
                    record["write_action"] = "WRITE_FAILED"

            if not result.finalization_eligible and (
                result.status == "SOURCE_UNVERIFIABLE" or result.source_status in {
                SOURCE_FETCH_ERROR,
                SOURCE_INCOMPLETE_COVERAGE,
                SOURCE_TIMESTAMP_INVALID,
                SOURCE_CONFLICTING_CANDLES,
            }
            ):
                if record.get("write_action") != "SKIPPED_PRE_ACTIVATION":
                    stats["source_errors"] += 1
            elif not result.finalization_eligible and result.status in {
                "FIRST_CANDLE_BOUNDARY_UNCERTAIN",
                "CUTOFF_BOUNDARY_UNCERTAIN",
            }:
                stats["uncertain"] += 1
        return stats

    @staticmethod
    def _current_asof_ms() -> int:
        return int(datetime.now(timezone.utc).timestamp() * 1000)
