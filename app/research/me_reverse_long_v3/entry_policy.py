"""Frozen NEXT_OPEN_5M entry-time policy for research V3."""
from datetime import datetime, timedelta, timezone

STEP_SECONDS = 300


def next_executable_entry_at(detected_at: datetime) -> datetime:
    if (
        not isinstance(detected_at, datetime)
        or detected_at.tzinfo is None
        or detected_at.utcoffset() is None
    ):
        raise ValueError("INVALID_DETECTION_TIME")

    detected = detected_at.astimezone(timezone.utc)
    current = detected.timestamp()

    # Strictly later than detection, including exact 5m boundaries.
    next_boundary = (
        int(current // STEP_SECONDS) + 1
    ) * STEP_SECONDS

    return datetime.fromtimestamp(next_boundary, timezone.utc)


def execution_geometry(entry_price: float) -> tuple[float, float, float]:
    import math

    price = float(entry_price)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("INVALID_ENTRY_PRICE")

    return price, price * 0.975, price * 1.03
