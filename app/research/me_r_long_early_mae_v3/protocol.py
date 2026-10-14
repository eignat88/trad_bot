"""Immutable design constants and fail-closed guards for MAE V3.

This module is deliberately independent of frozen V2 storage, production
scanner configuration, paper/live engines, balances, and direction gates.
Design values remain unfrozen until a separately approved deployment task.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EXPERIMENT_ID = "ME_R_LONG_EARLY_MAE_EXIT_OOS_V3"
PROTOCOL_VERSION = "3.0.0-DRAFT"
SCANNER_NAME = "ME_R_LONG_CLOSED_V1"
SCANNER_VERSION = "1.0.0-DRAFT"
SOURCE_DIRECTION = "LONG"
SOURCE_SCHEMA = "research_me_early_mae_v3"
SOURCE_CANDLE_VERSION = "MARKET_CANDLE_CLOSED_5M_V1"
RULESET = "FROZEN_V1_RULES_ON_COMPLETED_CANDLES"
BASELINE_POLICY = "FIXED_TP_SL_HORIZON_V3"
ENTRY_MODEL = "NEXT_OPEN_5M"
INTRABAR_POLICY = "STOP_FIRST"
TIMEOUT_POLICY = "FIRST_COMPLETED_5M_CLOSE_AT_OR_AFTER_DEADLINE"
MAE_EXIT_POLICY = "HORIZON_END_CONDITIONAL_MAE_EXIT"
MAX_SOURCE_DELAY_SECONDS = 300
ENTRY_EXPIRY_MINUTES = 240
BASELINE_MAX_HOLD_MINUTES = 240
STOP_PCT = 0.025
TARGET_PCT = 0.03
TAKER_FEE_PER_SIDE = 0.00055
NORMAL_SLIPPAGE_PER_SIDE = 0.0005
ELEVATED_SLIPPAGE_PER_SIDE = 0.001
REQUIRED_TIMEFRAMES = ("5m", "15m", "1h")
PAPER_LIVE_SIDE_EFFECTS = "FORBIDDEN"
HISTORICAL_BACKFILL = "FORBIDDEN"


@dataclass(frozen=True)
class VariantDef:
    variant_id: str
    horizon_minutes: int
    threshold_mae_r: float
    role: str = "SECONDARY"
    is_primary: bool = False


VARIANTS: tuple[VariantDef, ...] = (
    VariantDef("MAE_10m_040", 10, 0.40, "SECONDARY"),
    VariantDef("MAE_10m_050", 10, 0.50, "SECONDARY"),
    VariantDef("MAE_15m_040", 15, 0.40, "SECONDARY"),
    VariantDef("MAE_15m_050", 15, 0.50, "PRIMARY", True),
    VariantDef("MAE_15m_060", 15, 0.60, "SECONDARY"),
    VariantDef("MAE_15m_075", 15, 0.75, "SECONDARY"),
    VariantDef("MAE_30m_050", 30, 0.50, "SECONDARY"),
)


def parse_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("NAIVE_UTC_TIMESTAMP")
    return value.astimezone(timezone.utc)


def _read_utc_boundary(name: str) -> datetime | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    value = parse_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    if value.utcoffset().total_seconds() != 0:
        raise RuntimeError(f"ME_R_EARLY_MAE_V3_NON_UTC_BOUNDARY:{name}")
    return value


FROZEN_AT = _read_utc_boundary("ME_EARLY_MAE_V3_FROZEN_AT")
ACTIVATION_AT = _read_utc_boundary("ME_EARLY_MAE_V3_ACTIVATION_AT")
APPROVED_FINGERPRINT = os.environ.get(
    "ME_EARLY_MAE_V3_PROTOCOL_FINGERPRINT", ""
).strip().lower() or None


def validate_freeze_activation(frozen_at, activation_at, now) -> None:
    if frozen_at is None or activation_at is None:
        raise RuntimeError("ME_R_EARLY_MAE_V3_FREEZE_NOT_CONFIGURED")
    frozen = parse_utc(frozen_at)
    active = parse_utc(activation_at)
    observed = parse_utc(now)
    if frozen >= active:
        raise RuntimeError("ME_R_EARLY_MAE_V3_FREEZE_ORDER_INVALID")
    if observed < frozen:
        raise RuntimeError("ME_R_EARLY_MAE_V3_FREEZE_NOT_EFFECTIVE")


def require_activation_at(activation_at: datetime | None = None) -> datetime:
    value = ACTIVATION_AT if activation_at is None else activation_at
    if value is None:
        raise RuntimeError("ME_R_EARLY_MAE_V3_FREEZE_NOT_CONFIGURED")
    return parse_utc(value)


def validate_source_availability(source_candle_close_at, detected_at) -> None:
    close_at = parse_utc(source_candle_close_at)
    detected = parse_utc(detected_at)
    if detected < close_at:
        raise ValueError("SOURCE_CANDLE_NOT_CLOSED_BEFORE_DETECTION")
    if (detected - close_at).total_seconds() > MAX_SOURCE_DELAY_SECONDS:
        raise ValueError("SOURCE_DELAY_EXCEEDS_300_SECONDS")


def validate_measured_source_availability(
    source_candle_close_at: datetime,
    source_available_at: datetime,
    detected_at: datetime,
) -> None:
    close_at = parse_utc(source_candle_close_at)
    available = parse_utc(source_available_at)
    detected = parse_utc(detected_at)
    if available < close_at:
        raise ValueError("SOURCE_AVAILABILITY_BEFORE_CLOSE")
    if available > detected:
        raise ValueError("SOURCE_AVAILABILITY_AFTER_DETECTION")
    if (available - close_at).total_seconds() > MAX_SOURCE_DELAY_SECONDS:
        raise ValueError("SOURCE_AVAILABILITY_EXCEEDS_300_SECONDS")
    if (detected - close_at).total_seconds() > MAX_SOURCE_DELAY_SECONDS:
        raise ValueError("SOURCE_DELAY_EXCEEDS_300_SECONDS")


def entry_eligible_at(source_available_at_value, detected_at, activation_at):
    return max(
        parse_utc(source_available_at_value),
        parse_utc(detected_at),
        parse_utc(activation_at),
    )


def execution_geometry(entry_price: float) -> tuple[float, float, float]:
    price = float(entry_price)
    if price <= 0:
        raise ValueError("INVALID_ENTRY_PRICE")
    return price, price * (1 - STOP_PCT), price * (1 + TARGET_PCT)


def round_trip_cost_r(
    entry_price: float,
    exit_price: float,
    risk_distance: float,
    slippage_per_side: float = NORMAL_SLIPPAGE_PER_SIDE,
) -> float:
    if entry_price <= 0 or exit_price <= 0:
        raise ValueError("INVALID_EXECUTION_PRICE")
    if risk_distance <= 0:
        raise ValueError("INVALID_RISK_DISTANCE")
    if slippage_per_side not in (
        NORMAL_SLIPPAGE_PER_SIDE,
        ELEVATED_SLIPPAGE_PER_SIDE,
    ):
        raise ValueError("UNAPPROVED_SLIPPAGE_MODEL")
    return (
        (TAKER_FEE_PER_SIDE + slippage_per_side)
        * (entry_price + exit_price)
        / risk_distance
    )


def protocol_payload(frozen_at, activation_at) -> dict[str, object]:
    frozen = parse_utc(frozen_at)
    active = parse_utc(activation_at)
    return {
        "draft": False,
        "experiment_id": EXPERIMENT_ID,
        "protocol_version": PROTOCOL_VERSION,
        "source": {
            "scanner_name": SCANNER_NAME,
            "scanner_version": SCANNER_VERSION,
            "direction": SOURCE_DIRECTION,
            "ruleset": RULESET,
            "source_candle_version": SOURCE_CANDLE_VERSION,
            "required_timeframes": list(REQUIRED_TIMEFRAMES),
            "max_source_delay_seconds": MAX_SOURCE_DELAY_SECONDS,
        },
        "entry": {
            "primary_model": ENTRY_MODEL,
            "expiry_minutes": ENTRY_EXPIRY_MINUTES,
        },
        "baseline": {
            "policy": BASELINE_POLICY,
            "stop_pct": STOP_PCT,
            "target_pct": TARGET_PCT,
            "max_hold_minutes": BASELINE_MAX_HOLD_MINUTES,
            "intrabar_policy": INTRABAR_POLICY,
            "timeout_policy": TIMEOUT_POLICY,
            "taker_fee_per_side": TAKER_FEE_PER_SIDE,
            "normal_slippage_per_side": NORMAL_SLIPPAGE_PER_SIDE,
            "elevated_slippage_per_side": ELEVATED_SLIPPAGE_PER_SIDE,
            "funding": "NOT_MODELED",
        },
        "mae_exit": {
            "policy": MAE_EXIT_POLICY,
            "baseline_sl_tp_precedence": True,
            "immediate_first_touch_exit": "FORBIDDEN",
            "research_execution_proxy": "COMPLETED_5M_CLOSE_AT_HORIZON_BOUNDARY",
        },
        "variants": [
            {
                "variant_id": v.variant_id,
                "horizon_minutes": v.horizon_minutes,
                "threshold_mae_r": v.threshold_mae_r,
                "role": v.role,
                "is_primary": v.is_primary,
            }
            for v in VARIANTS
        ],
        "frozen_at": _iso_utc(frozen),
        "activation_at": _iso_utc(active),
        "paper_live_side_effects": PAPER_LIVE_SIDE_EFFECTS,
        "historical_backfill": HISTORICAL_BACKFILL,
    }


def calculate_protocol_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_protocol_fingerprint(
    frozen_at: datetime | None = None,
    activation_at: datetime | None = None,
) -> str:
    frozen = FROZEN_AT if frozen_at is None else parse_utc(frozen_at)
    active = ACTIVATION_AT if activation_at is None else parse_utc(activation_at)
    validate_freeze_activation(frozen, active, datetime.now(timezone.utc))
    calculated = calculate_protocol_fingerprint(
        protocol_payload(frozen, active)
    )
    if APPROVED_FINGERPRINT is None:
        raise RuntimeError("ME_R_EARLY_MAE_V3_FINGERPRINT_NOT_APPROVED")
    if not hmac.compare_digest(APPROVED_FINGERPRINT, calculated):
        raise RuntimeError("ME_R_EARLY_MAE_V3_FINGERPRINT_MISMATCH")
    return calculated


def _iso_utc(value: datetime) -> str:
    return parse_utc(value).isoformat().replace("+00:00", "Z")
