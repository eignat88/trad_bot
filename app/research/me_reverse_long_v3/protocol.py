"""ME Reverse Long V3 prospective shadow protocol.

Research only. No paper/live execution.

The 0.4 threshold is a candidate research split, NOT an
independently validated V3 trading gate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

EXPERIMENT_ID = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS"
SOURCE_SCANNER = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"

CANDIDATE_THRESHOLD = 0.4
MAX_HOLD_MINUTES = 240

EXPECTED_STOP_PCT = 0.025
EXPECTED_TARGET_PCT = 0.03

FEE_PER_SIDE = 0.00055
NORMAL_SLIPPAGE_PER_SIDE = 0.0005
ELEVATED_SLIPPAGE_PER_SIDE = 0.001


@dataclass(frozen=True)
class V3Classification:
    group: str
    exhaustion_magnitude: float | None
    reason: str


@dataclass(frozen=True)
class V3Geometry:
    entry: float
    stop: float
    target: float


def classify_exhaustion(value: Any) -> V3Classification:
    if value is None:
        return V3Classification(
            "MISSING_FEATURE", None, "exhaustion_magnitude_missing"
        )

    try:
        magnitude = float(value)
    except (TypeError, ValueError, OverflowError):
        return V3Classification(
            "MISSING_FEATURE", None, "exhaustion_magnitude_invalid"
        )

    if not math.isfinite(magnitude):
        return V3Classification(
            "MISSING_FEATURE", None, "exhaustion_magnitude_nonfinite"
        )

    if magnitude <= CANDIDATE_THRESHOLD:
        return V3Classification(
            "PASS", magnitude, "candidate_threshold_pass"
        )

    return V3Classification(
        "REJECT", magnitude, "candidate_threshold_reject"
    )


def reconstruct_geometry(
    entry_zone_low: float | None,
    invalidation_price: float | None,
    target_1: float | None,
) -> V3Geometry:
    """Reconstruct the V2 current-price geometry.

    V2 entry_zone_low = current_price * 0.998.
    V2 reference_price must NOT be used as entry.
    """
    if any(x is None for x in (
        entry_zone_low, invalidation_price, target_1
    )):
        raise ValueError("MISSING_SOURCE_GEOMETRY")

    low = float(entry_zone_low)
    stop = float(invalidation_price)
    target = float(target_1)

    if not all(math.isfinite(x) for x in (low, stop, target)):
        raise ValueError("NONFINITE_SOURCE_GEOMETRY")

    entry = low / 0.998

    if not (0 < stop < entry < target):
        raise ValueError("INVALID_LONG_GEOMETRY")

    sl_pct = (entry - stop) / entry
    tp_pct = (target - entry) / entry

    if (
        abs(sl_pct - EXPECTED_STOP_PCT) > 1e-6
        or abs(tp_pct - EXPECTED_TARGET_PCT) > 1e-6
    ):
        raise ValueError("SOURCE_GEOMETRY_MISMATCH")

    return V3Geometry(entry, stop, target)


def is_post_activation(
    signal_time: datetime,
    activation_time: datetime | None,
) -> bool:
    """Strict prospective boundary. Missing/invalid freeze fails closed."""
    if activation_time is None:
        return False

    if signal_time.tzinfo is None or activation_time.tzinfo is None:
        return False

    signal_utc = signal_time.astimezone(timezone.utc)
    activation_utc = activation_time.astimezone(timezone.utc)

    return signal_utc > activation_utc


def is_v3_source(scanner_name: str, direction: str) -> bool:
    return scanner_name == SOURCE_SCANNER and direction == "LONG"
