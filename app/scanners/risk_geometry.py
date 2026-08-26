from __future__ import annotations

from app.scanners.models import SetupCandidate

INVALID_RISK_GEOMETRY = "INVALID_RISK_GEOMETRY"


def validate_risk_geometry(candidate: SetupCandidate) -> tuple[bool, str | None]:
    """Validate that entry, stop and first target form a tradable setup."""
    if candidate.entry_zone_low <= 0 or candidate.entry_zone_high <= 0:
        return False, "ENTRY_ZONE_MISSING"
    if candidate.entry_zone_low > candidate.entry_zone_high:
        return False, "ENTRY_ZONE_INVERTED"
    if candidate.invalidation_price <= 0:
        return False, "STOP_MISSING"
    if candidate.target_1 is None or candidate.target_1 <= 0:
        return False, "TARGET_1_MISSING"

    if candidate.direction == "LONG":
        if candidate.invalidation_price >= candidate.entry_zone_low:
            return False, INVALID_RISK_GEOMETRY
        if candidate.target_1 <= candidate.entry_zone_high:
            return False, INVALID_RISK_GEOMETRY
    elif candidate.direction == "SHORT":
        if candidate.invalidation_price <= candidate.entry_zone_high:
            return False, INVALID_RISK_GEOMETRY
        if candidate.target_1 >= candidate.entry_zone_low:
            return False, INVALID_RISK_GEOMETRY
    else:
        return False, "UNKNOWN_DIRECTION"

    return True, None


def has_valid_risk_geometry(candidate: SetupCandidate) -> bool:
    valid, _ = validate_risk_geometry(candidate)
    return valid
