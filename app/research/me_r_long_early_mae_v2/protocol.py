"""Frozen protocol constants for ME_R_LONG early MAE exit V2.

The protocol is intentionally explicit and immutable after the activation
boundary. It does not import runtime paper settings, balances, direction
gates or production exit handlers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

EXPERIMENT_ID = "ME_R_LONG_EARLY_MAE_EXIT_OOS_V2"
PROTOCOL_VERSION = "2.1.0"
SCANNER_NAME = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"
SCANNER_VERSION = "1.0.0"
SOURCE_DIRECTION = "LONG"
SOURCE_SCHEMA = "research_me_early_mae_v2"

# Operator approval is required for both immutable boundaries. Both remain
# intentionally unset until a reviewed freeze decision assigns UTC values.
def _read_utc_boundary(name: str) -> datetime | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"ME V2 INVALID_UTC_BOUNDARY: {name}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError(f"ME V2 INVALID_UTC_BOUNDARY: {name}")
    if value.utcoffset().total_seconds() != 0:
        raise RuntimeError(f"ME V2 NON_UTC_BOUNDARY: {name}")
    return value.astimezone(timezone.utc)


FROZEN_AT: datetime | None = _read_utc_boundary("ME_EARLY_MAE_V2_FROZEN_AT")
ACTIVATION_AT: datetime | None = _read_utc_boundary("ME_EARLY_MAE_V2_ACTIVATION_AT")

ENTRY_EXPIRY_MINUTES = 240
BASELINE_MAX_HOLD_MINUTES = 240
BASELINE_POLICY = "FIXED_TP_SL_HORIZON_V1"
TAKER_FEE_PER_SIDE = 0.00055
SLIPPAGE_PER_SIDE = 0.0005
INTRABAR_POLICY = "STOP_FIRST"
CANDLE_SOURCE_VERSION = "MARKET_CANDLE_CLOSED_5M_V1"


@dataclass(frozen=True)
class VariantDef:
    variant_id: str
    horizon_minutes: int
    threshold_mae_r: float
    is_primary: bool = False


VARIANTS: tuple[VariantDef, ...] = (
    VariantDef("MAE_10m_040", 10, 0.40),
    VariantDef("MAE_10m_050", 10, 0.50),
    VariantDef("MAE_15m_040", 15, 0.40),
    VariantDef("MAE_15m_050", 15, 0.50, is_primary=True),
    VariantDef("MAE_15m_060", 15, 0.60),
    VariantDef("MAE_15m_075", 15, 0.75),
    VariantDef("MAE_30m_050", 30, 0.50),
)




def validate_activation_freeze(
    frozen_at: datetime | None,
    activation_at: datetime | None,
    now: datetime,
) -> None:
    """Refuse capture unless immutable UTC freeze boundaries are valid."""
    if frozen_at is None or activation_at is None:
        raise RuntimeError("ME V2 FREEZE_NOT_CONFIGURED")

    if any(
        dt.tzinfo is None or dt.utcoffset() is None
        for dt in (frozen_at, activation_at, now)
    ):
        raise RuntimeError("ME V2 FREEZE_TIMEZONE_INVALID")

    if frozen_at >= activation_at:
        raise RuntimeError("ME V2 FREEZE_ORDER_INVALID")

    if now < frozen_at:
        raise RuntimeError("ME V2 FREEZE_NOT_YET_EFFECTIVE")


def require_activation_at(
    activation_at: datetime | None = None,
) -> datetime:
    """Return the approved activation boundary or fail closed."""
    boundary = ACTIVATION_AT if activation_at is None else activation_at
    if boundary is None:
        raise RuntimeError("ME V2 FREEZE_NOT_CONFIGURED")
    if boundary.tzinfo is None or boundary.utcoffset() is None:
        raise RuntimeError("ME V2 FREEZE_TIMEZONE_INVALID")
    return boundary


def calculate_protocol_fingerprint(payload: dict[str, object]) -> str:
    """Deterministic SHA256 of the complete frozen protocol."""
    import hashlib
    import json

    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_source_geometry(
    entry_zone_low: float,
    entry_zone_high: float,
    invalidation_price: float,
    target_price: float | None,
) -> tuple[bool, str | None]:
    """Validate the frozen LONG geometry independently of scanner orchestration."""
    if entry_zone_low <= 0 or entry_zone_high <= 0:
        return False, "ENTRY_ZONE_MISSING"
    if entry_zone_low > entry_zone_high:
        return False, "ENTRY_ZONE_INVERTED"
    if invalidation_price <= 0:
        return False, "STOP_MISSING"
    if invalidation_price >= entry_zone_low:
        return False, "STOP_NOT_BELOW_ENTRY_ZONE"
    if target_price is None or target_price <= entry_zone_high:
        return False, "TARGET_NOT_ABOVE_ENTRY_ZONE"
    return True, None


def round_trip_cost_r(entry_price: float, exit_price: float, risk_distance: float) -> float:
    """Return the frozen fee + normal-slippage cost in R units."""
    if entry_price <= 0 or exit_price <= 0:
        return 0.0
    if risk_distance <= 0:
        raise ValueError("risk_distance must be positive")
    side_cost = TAKER_FEE_PER_SIDE + SLIPPAGE_PER_SIDE
    return side_cost * (entry_price + exit_price) / risk_distance


def protocol_payload(
    frozen_at: datetime | None = None,
    activation_at: datetime | None = None,
) -> dict[str, object]:
    """Return the frozen protocol payload for durable registration.

    Both boundaries must already be operator-approved and timezone-aware.
    An unfrozen payload is intentionally unusable for persistence.
    """
    if frozen_at is None:
        frozen_at = FROZEN_AT
    if activation_at is None:
        activation_at = ACTIVATION_AT
    if frozen_at is None or activation_at is None:
        raise RuntimeError("ME V2 FREEZE_NOT_CONFIGURED")
    validate_activation_freeze(
        frozen_at,
        activation_at,
        datetime.now(timezone.utc),
    )
    return {
        "experiment_id": EXPERIMENT_ID,
        "protocol_version": PROTOCOL_VERSION,
        "scanner_name": SCANNER_NAME,
        "scanner_version": SCANNER_VERSION,
        "direction": SOURCE_DIRECTION,
        "frozen_at": _iso_utc(frozen_at),
        "activation_at": _iso_utc(activation_at),
        "entry_rule": (
            "First completed 5m candle after source availability whose low <= "
            "entry_zone_high and high >= entry_zone_low; fill at entry_zone_high."
        ),
        "entry_expiry_minutes": ENTRY_EXPIRY_MINUTES,
        "baseline_policy": BASELINE_POLICY,
        "baseline_max_hold_minutes": BASELINE_MAX_HOLD_MINUTES,
        "taker_fee_per_side": TAKER_FEE_PER_SIDE,
        "slippage_per_side": SLIPPAGE_PER_SIDE,
        "intrabar_policy": INTRABAR_POLICY,
        "variants": [
            {
                "variant_id": v.variant_id,
                "horizon_minutes": v.horizon_minutes,
                "threshold_mae_r": v.threshold_mae_r,
                "is_primary": v.is_primary,
            }
            for v in VARIANTS
        ],
        "paper_live_side_effects": "FORBIDDEN",
        "historical_backfill": "FORBIDDEN",
        "reference_price_as_fill": "FORBIDDEN",
        "old_table_reuse": "FORBIDDEN",
    }


PROTOCOL_FINGERPRINT: str | None = (
    os.environ.get("ME_EARLY_MAE_V2_PROTOCOL_FINGERPRINT", "").strip().lower()
    or None
)


def _iso_utc(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def require_protocol_fingerprint(
    frozen_at: datetime | None = None,
    activation_at: datetime | None = None,
) -> str:
    """Fail closed unless an approved protocol can produce a SHA256."""
    import hmac

    calculated = calculate_protocol_fingerprint(
        protocol_payload(frozen_at, activation_at)
    )
    approved = PROTOCOL_FINGERPRINT

    if approved is None:
        raise RuntimeError("ME V2 FINGERPRINT_NOT_APPROVED")
    if len(approved) != 64 or any(c not in "0123456789abcdef" for c in approved):
        raise RuntimeError("ME V2 FINGERPRINT_INVALID")
    if not hmac.compare_digest(calculated, approved):
        raise RuntimeError("ME V2 FINGERPRINT_MISMATCH")

    return calculated
