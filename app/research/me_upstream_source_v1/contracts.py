"""Issue #305 — upstream source event contracts (research only).

One ``UpstreamSourceEvent`` is the immutable, verbatim capture of ONE
original ``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` / ``LONG`` detector event
as it exists IMMEDIATELY after ``scanner.scan(ctx)`` — before scoring,
deduplication, risk geometry, direction gate, regime filter and expectancy
filter.

Contract highlights (requirement 4.2 / 4.5):

* ``source_event_key`` — deterministic, restart-stable, independent of the
  V3 / OOS-2 protocol version (see :mod:`.identity`);
* the ORIGINAL feature values are stored verbatim — never recalculated,
  never coerced;
* ``lineage`` mirrors the V3 lineage key naming for the ORIGINAL source
  facts so the read-only reconciler can compare the two ledgers on a frozen
  comparable subset (:data:`COMPARABLE_LINEAGE_KEYS` /
  :data:`COMPARABLE_FEATURE_KEYS`), while capture-side metadata
  (``capture_*``, ``*_protocol_*``) is excluded from every comparison;
* an event with incomplete lineage / required features is a deterministic
  rejection (:class:`SourceValidationError`) — it never enters the eligible
  ``source_event`` cohort (criterion 12) and is recorded in the durable
  rejection ledger with a ``reason_code``.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from app.scanners.models import SetupCandidate

from .identity import (
    normalized_identity,
    signal_candle_open_ms,
    source_event_key,
)
from .protocol import (
    IDENTITY_NAMESPACE,
    PROTOCOL_VERSION,
    ROUTE_PRE_SCORING_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    protocol_fingerprint,
)


class SourceValidationError(ValueError):
    """The source event must not enter the eligible upstream cohort.

    Deterministic: ``str(exc)`` equals its ``reason_code``.
    """

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class SourceIntegrityError(RuntimeError):
    """A durable immutable-lineage contract was violated.

    Deterministic: the capture contour fails closed on it (BLOCKED state).
    """


# ---------------------------------------------------------------------------
# Deterministic rejection reason codes.
# ---------------------------------------------------------------------------
REASON_INVALID_SOURCE_SCANNER = "INVALID_SOURCE_SCANNER"
REASON_INVALID_SOURCE_DIRECTION = "INVALID_SOURCE_DIRECTION"
REASON_MISSING_SYMBOL = "MISSING_SYMBOL"
REASON_MISSING_SIGNAL_TIME = "MISSING_SIGNAL_TIME"
REASON_NAIVE_SIGNAL_TIME = "NAIVE_SIGNAL_TIME"
REASON_INVALID_CANDLE_IDENTITY = "INVALID_CANDLE_IDENTITY"
REASON_INVALID_ENTRY_TIMEFRAME = "INVALID_ENTRY_TIMEFRAME"
REASON_INCOMPLETE_LINEAGE = "INCOMPLETE_LINEAGE"
REASON_INCOMPLETE_FEATURES = "INCOMPLETE_FEATURES"
REASON_INVALID_FEATURES = "INVALID_FEATURES"
REASON_FEATURE_RSI14_INVALID_TYPE = "FEATURE_RSI14_INVALID_TYPE"
REASON_FEATURE_RSI14_NOT_FINITE = "FEATURE_RSI14_NOT_FINITE"
REASON_FEATURE_RSI14_OUT_OF_RANGE = "FEATURE_RSI14_OUT_OF_RANGE"
REASON_FEATURE_RSI_DELTA_3_INVALID_TYPE = "FEATURE_RSI_DELTA_3_INVALID_TYPE"
REASON_FEATURE_RSI_DELTA_3_NOT_FINITE = "FEATURE_RSI_DELTA_3_NOT_FINITE"
REASON_FEATURE_CONFIRMATION_FLAG_INVALID_TYPE = (
    "FEATURE_CONFIRMATION_FLAG_INVALID_TYPE"
)
REASON_INVALID_LINEAGE_JSON = "INVALID_LINEAGE_JSON"
REASON_MISSING_PIPELINE_FINGERPRINT = "MISSING_PIPELINE_FINGERPRINT"
REASON_EVENT_KEY_LINEAGE_CONFLICT = "EVENT_KEY_LINEAGE_CONFLICT"

#: RSI(14) is bounded by its definition.
RSI_MIN = 0.0
RSI_MAX = 100.0

#: Entry-quality features the eligible cohort must carry (type/value
#: validated; values preserved verbatim).
REQUIRED_FEATURES = frozenset({
    "rsi_14",
    "rsi_delta_3",
    "entry_confirmation_rsi_rising",
})

#: Mandatory lineage keys of the ORIGINAL source facts.
REQUIRED_LINEAGE_KEYS = frozenset({
    "source_identity",
    "source_signal_time",
    "source_signal_candle_open_time",
    "source_scanner",
    "source_direction",
    "source_features",
    "source_pipeline_fingerprint",
})

#: Detector feature keys captured verbatim (mirrors the V3 lineage feature
#: subset so cross-ledger comparison is well-defined).
LINEAGE_FEATURE_KEYS = (
    "exhaustion_magnitude", "body_ratio", "rsi_confirmation", "volume_ratio",
    "rr_ratio", "stop_distance_atr", "rsi_14", "rsi_delta_3",
    "entry_confirmation_rsi_rising",
)

#: Lineage keys compared across the upstream and V3 ledgers AND used for
#: immutable-lineage conflict detection.  Only ORIGINAL detector facts that
#: are stable across re-detections of the same signal qualify:
#:
#: * ``source_setup_id`` and ``source_signal_time`` are EXCLUDED — every
#:   re-detection of the same signal candle produces a NEW setup_id and a
#:   new detected_at, so including them would make every idempotent
#:   redelivery an "integrity conflict" (criterion 5 forbids that);
#: * ``source_score``, ``source_reasons``, ``source_state`` and
#:   ``source_market_regime`` are excluded — the later scoring/attach passes
#:   may rewrite them;
#: * capture-side metadata (``capture_*``, ``identity_namespace``) is
#:   excluded — it describes this capture, not the source signal.
COMPARABLE_LINEAGE_KEYS = (
    "source_identity",
    "source_scanner",
    "source_scanner_version",
    "source_direction",
    "source_signal_candle_open_time",
    "source_entry_timeframe",
    "source_reference_price",
    "source_invalidation_price",
    "source_target_price",
    "source_target_2",
    "source_entry_zone",
    "source_pipeline_fingerprint",
)

#: Feature keys compared across ledgers (intersection of both captures).
COMPARABLE_FEATURE_KEYS = LINEAGE_FEATURE_KEYS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise SourceValidationError(REASON_MISSING_SIGNAL_TIME)
    if value.tzinfo is None or value.utcoffset() is None:
        raise SourceValidationError(REASON_NAIVE_SIGNAL_TIME)
    return value.astimezone(timezone.utc)


def _json_safe(value: Any, depth: int = 0) -> Any:
    """JSON-safe COPY of a value — the original object is never mutated."""
    if depth > 6:
        return str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    return str(value)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _is_finite_number(value: Any) -> bool:
    """True only for real, finite numbers — never bool/str/None/NaN/Inf."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return False


def _require_finite_number(
    value: Any,
    *,
    invalid_type_code: str,
    not_finite_code: str,
) -> float:
    if value is None:
        raise SourceValidationError(not_finite_code)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise SourceValidationError(invalid_type_code)
    if not _is_finite_number(value):
        raise SourceValidationError(not_finite_code)
    return float(value)


def _validate_required_features(features: Mapping[str, Any]) -> None:
    """Type/value validation of the entry-quality features.

    Original values are checked only — never recalculated or replaced.
    """
    number = _require_finite_number(
        features["rsi_14"],
        invalid_type_code=REASON_FEATURE_RSI14_INVALID_TYPE,
        not_finite_code=REASON_FEATURE_RSI14_NOT_FINITE,
    )
    if not RSI_MIN <= number <= RSI_MAX:
        raise SourceValidationError(REASON_FEATURE_RSI14_OUT_OF_RANGE)

    _require_finite_number(
        features["rsi_delta_3"],
        invalid_type_code=REASON_FEATURE_RSI_DELTA_3_INVALID_TYPE,
        not_finite_code=REASON_FEATURE_RSI_DELTA_3_NOT_FINITE,
    )

    if not isinstance(features["entry_confirmation_rsi_rising"], bool):
        raise SourceValidationError(
            REASON_FEATURE_CONFIRMATION_FLAG_INVALID_TYPE
        )


def lineage_hash(lineage: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 of the original JSON lineage."""
    try:
        payload = json.dumps(
            lineage,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise SourceValidationError(REASON_INVALID_LINEAGE_JSON) from None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def comparable_lineage_view(lineage: Mapping[str, Any]) -> dict[str, Any]:
    """Frozen immutable/comparable subset of a lineage (ORIGINAL facts).

    This subset is BOTH the immutable-lineage conflict set (a re-delivery
    differing on it is an ``INTEGRITY_CONFLICT``) AND the cross-ledger
    comparison set (upstream vs V3).
    """
    view: dict[str, Any] = {
        key: _json_safe(lineage.get(key))
        for key in COMPARABLE_LINEAGE_KEYS
    }
    features = lineage.get("source_features")
    view["source_features"] = {
        key: _json_safe(features.get(key))
        for key in COMPARABLE_FEATURE_KEYS
        if isinstance(features, Mapping) and key in features
    }
    return view


def comparable_lineage_hash(lineage: Mapping[str, Any]) -> str:
    """Deterministic hash of the immutable/comparable lineage subset.

    Used for immutable-lineage conflict detection (``EVENT_KEY_LINEAGE_
    CONFLICT``) and as the cross-ledger lineage fingerprint.
    """
    return lineage_hash(comparable_lineage_view(lineage))


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UpstreamSourceEvent:
    source_event_key: str
    source_identity: str
    symbol: str
    signal_time: datetime
    captured_at: datetime
    activation_freeze_ts: datetime
    source_setup_id: str
    source_scanner: str
    source_scanner_version: str
    source_direction: str
    entry_timeframe: str
    signal_candle_open_time: int
    source_reference_price: float | None
    source_invalidation_price: float | None
    source_target_price: float | None
    source_target_2: float | None
    entry_zone_low: float | None
    entry_zone_high: float | None
    source_score: float | None
    market_regime: str | None
    source_fingerprint: str
    capture_route: str
    features: dict[str, Any]
    lineage: dict[str, Any]
    lineage_hash: str
    comparable_lineage_hash: str = field(default="")


def build_source_event(
    candidate: SetupCandidate,
    *,
    activation_freeze_ts: datetime,
    captured_at: datetime | None = None,
) -> UpstreamSourceEvent:
    """Build the immutable upstream event for ONE detector candidate.

    Pure (no I/O); raises :class:`SourceValidationError` with a deterministic
    ``reason_code`` for anything that cannot enter the eligible cohort.
    """
    freeze = as_utc(activation_freeze_ts)

    if candidate.scanner_name != SOURCE_SCANNER:
        raise SourceValidationError(REASON_INVALID_SOURCE_SCANNER)
    if candidate.direction != SOURCE_DIRECTION:
        raise SourceValidationError(REASON_INVALID_SOURCE_DIRECTION)
    if not candidate.symbol:
        raise SourceValidationError(REASON_MISSING_SYMBOL)

    detected_at = candidate.detected_at
    if not isinstance(detected_at, datetime):
        raise SourceValidationError(REASON_MISSING_SIGNAL_TIME)
    signal_time = as_utc(detected_at)

    try:
        candle_ms = signal_candle_open_ms(candidate)
    except ValueError:
        raise SourceValidationError(REASON_INVALID_CANDLE_IDENTITY) from None
    if candle_ms <= 0:
        raise SourceValidationError(REASON_INVALID_CANDLE_IDENTITY)

    entry_timeframe = str(candidate.entry_timeframe or "").strip()
    if not entry_timeframe:
        raise SourceValidationError(REASON_INVALID_ENTRY_TIMEFRAME)

    now = captured_at if captured_at is not None else datetime.now(timezone.utc)
    captured = as_utc(now)

    fingerprint = str(candidate.fingerprint or "").strip()
    if not fingerprint:
        raise SourceValidationError(REASON_MISSING_PIPELINE_FINGERPRINT)

    identity = normalized_identity(candidate)
    key = source_event_key(candidate)

    features = dict(candidate.features or {})

    # Verify the entry-quality features on the RAW values BEFORE anything is
    # admitted (criterion 12: incomplete lineage/features never enter the
    # cohort) and before any JSON normalization — so NaN/Inf/None/str get
    # their exact deterministic reason codes.
    if not REQUIRED_FEATURES.issubset(features):
        raise SourceValidationError(REASON_INCOMPLETE_FEATURES)
    _validate_required_features(features)
    source_features = _json_safe(features)

    lineage: dict[str, Any] = {
        "source_identity": identity,
        "source_setup_id": str(candidate.setup_id) if candidate.setup_id else "",
        "source_scanner": candidate.scanner_name,
        "source_scanner_version": str(candidate.scanner_version or ""),
        "source_direction": candidate.direction,
        "source_signal_time": signal_time.isoformat(),
        "source_signal_candle_open_time": int(candle_ms),
        "source_entry_timeframe": entry_timeframe,
        "source_state": str(getattr(candidate.state, "value", candidate.state)),
        "source_score": _opt_float(candidate.score),
        "source_market_regime": candidate.market_regime,
        "source_reference_price": _opt_float(candidate.reference_price),
        "source_invalidation_price": _opt_float(candidate.invalidation_price),
        "source_target_price": _opt_float(candidate.target_1),
        "source_target_2": _opt_float(candidate.target_2),
        "source_entry_zone": [
            _opt_float(candidate.entry_zone_low),
            _opt_float(candidate.entry_zone_high),
        ],
        "source_reasons": _json_safe(list(candidate.reasons or ())),
        "source_features": source_features,
        "source_pipeline_fingerprint": fingerprint,
        # Capture-side metadata — never part of identity or comparison.
        "capture_route": ROUTE_PRE_SCORING_SOURCE,
        "capture_independent_of": [
            "scoring", "deduplication", "risk_geometry",
            "direction_gate", "regime_filter", "expectancy_filter",
        ],
        "capture_protocol_version": PROTOCOL_VERSION,
        "capture_protocol_fingerprint": protocol_fingerprint(),
        "identity_namespace": IDENTITY_NAMESPACE,
    }

    if not REQUIRED_LINEAGE_KEYS.issubset(lineage):
        raise SourceValidationError(REASON_INCOMPLETE_LINEAGE)

    return UpstreamSourceEvent(
        source_event_key=key,
        source_identity=identity,
        symbol=str(candidate.symbol),
        signal_time=signal_time,
        captured_at=captured,
        activation_freeze_ts=freeze,
        source_setup_id=str(candidate.setup_id) if candidate.setup_id else "",
        source_scanner=candidate.scanner_name,
        source_scanner_version=str(candidate.scanner_version or ""),
        source_direction=candidate.direction,
        entry_timeframe=entry_timeframe,
        signal_candle_open_time=int(candle_ms),
        source_reference_price=_opt_float(candidate.reference_price),
        source_invalidation_price=_opt_float(candidate.invalidation_price),
        source_target_price=_opt_float(candidate.target_1),
        source_target_2=_opt_float(candidate.target_2),
        entry_zone_low=_opt_float(candidate.entry_zone_low),
        entry_zone_high=_opt_float(candidate.entry_zone_high),
        source_score=_opt_float(candidate.score),
        market_regime=candidate.market_regime,
        source_fingerprint=fingerprint,
        capture_route=ROUTE_PRE_SCORING_SOURCE,
        features=source_features,
        lineage=lineage,
        lineage_hash=lineage_hash(lineage),
        comparable_lineage_hash=comparable_lineage_hash(lineage),
    )


def normalize_reason_code(exc: BaseException) -> str:
    """Deterministic reason_code for any validation-path exception."""
    if isinstance(exc, SourceValidationError):
        return exc.reason_code
    if isinstance(exc, KeyError):
        return f"MISSING_FIELD:{exc.args[0]}"
    if isinstance(exc, (TypeError, ValueError)):
        return "INVALID_FIELD_TYPE"
    return "UNEXPECTED_VALIDATION_ERROR"
