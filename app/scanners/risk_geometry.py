from __future__ import annotations

from app.scanners.models import SetupCandidate

# Legacy catch-all code kept for backward compatibility with log parsers.
INVALID_RISK_GEOMETRY = "INVALID_RISK_GEOMETRY"

# Detailed reason codes — use these instead of the catch-all.
REASON_STOP_INSIDE_ENTRY_ZONE = "STOP_INSIDE_ENTRY_ZONE"
REASON_STOP_ABOVE_ENTRY_ZONE = "STOP_ABOVE_ENTRY_ZONE"
REASON_STOP_BELOW_ENTRY_ZONE = "STOP_BELOW_ENTRY_ZONE"
REASON_TARGET_INSIDE_ENTRY_ZONE = "TARGET_INSIDE_ENTRY_ZONE"
REASON_TARGET_ABOVE_ENTRY_ZONE = "TARGET_ABOVE_ENTRY_ZONE"
REASON_TARGET_BELOW_ENTRY_ZONE = "TARGET_BELOW_ENTRY_ZONE"
REASON_NON_POSITIVE_RISK = "NON_POSITIVE_RISK"
REASON_NON_POSITIVE_REWARD = "NON_POSITIVE_REWARD"


def validate_risk_geometry(candidate: SetupCandidate) -> tuple[bool, str | None]:
    """Validate that entry, stop and first target form a tradable setup.

    Returns (True, None) on success or (False, reason_code) on failure.
    Reason codes are specific enough to distinguish the violation class:
      ENTRY_ZONE_MISSING / ENTRY_ZONE_INVERTED / STOP_MISSING / TARGET_1_MISSING /
      STOP_INSIDE_ENTRY_ZONE / STOP_ABOVE_ENTRY_ZONE / STOP_BELOW_ENTRY_ZONE /
      TARGET_INSIDE_ENTRY_ZONE / TARGET_ABOVE_ENTRY_ZONE / TARGET_BELOW_ENTRY_ZONE /
      NON_POSITIVE_RISK / NON_POSITIVE_REWARD / UNKNOWN_DIRECTION
    """
    if candidate.entry_zone_low <= 0 or candidate.entry_zone_high <= 0:
        return False, "ENTRY_ZONE_MISSING"
    if candidate.entry_zone_low > candidate.entry_zone_high:
        return False, "ENTRY_ZONE_INVERTED"
    if candidate.invalidation_price <= 0:
        return False, "STOP_MISSING"
    if candidate.target_1 is None or candidate.target_1 <= 0:
        return False, "TARGET_1_MISSING"

    # Compute risk distance once for positive-distance checks.
    risk = abs(candidate.entry_zone_high - candidate.invalidation_price)
    reward = abs(candidate.target_1 - candidate.entry_zone_high)

    if candidate.direction == "LONG":
        # Stop must be below the entire entry zone.
        if candidate.invalidation_price >= candidate.entry_zone_low:
            if candidate.invalidation_price <= candidate.entry_zone_high:
                return False, REASON_STOP_INSIDE_ENTRY_ZONE
            return False, REASON_STOP_ABOVE_ENTRY_ZONE
        # Target must be above the entire entry zone.
        if candidate.target_1 <= candidate.entry_zone_high:
            if candidate.target_1 >= candidate.entry_zone_low:
                return False, REASON_TARGET_INSIDE_ENTRY_ZONE
            return False, REASON_TARGET_BELOW_ENTRY_ZONE
    elif candidate.direction == "SHORT":
        # Stop must be above the entire entry zone.
        if candidate.invalidation_price <= candidate.entry_zone_high:
            if candidate.invalidation_price >= candidate.entry_zone_low:
                return False, REASON_STOP_INSIDE_ENTRY_ZONE
            return False, REASON_STOP_BELOW_ENTRY_ZONE
        # Target must be below the entire entry zone.
        if candidate.target_1 >= candidate.entry_zone_low:
            if candidate.target_1 <= candidate.entry_zone_high:
                return False, REASON_TARGET_INSIDE_ENTRY_ZONE
            return False, REASON_TARGET_ABOVE_ENTRY_ZONE
    else:
        return False, "UNKNOWN_DIRECTION"

    return True, None


def has_valid_risk_geometry(candidate: SetupCandidate) -> bool:
    valid, _ = validate_risk_geometry(candidate)
    return valid
