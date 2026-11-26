"""Scanner AC7 Recovery V1: offline scheduling decision.

No Bybit calls, database writes, or limiter modifications.
Recovery must be a NEW cycle with a NEW run_id.
Historical incomplete cycles are never rewritten.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RecoveryDecision:
    expedite: bool
    delay_seconds: float
    reason: str


def decide_recovery(
    *,
    enabled: bool,
    expected_symbols: int,
    processed_symbols: int,
    limiter_pre_send_skips: int,
    previous_recovery_attempted: bool,
    normal_delay_seconds: float,
    recovery_delay_seconds: float = 130.0,
) -> RecoveryDecision:

    if (
        not math.isfinite(normal_delay_seconds)
        or normal_delay_seconds < 0
        or not math.isfinite(recovery_delay_seconds)
        or recovery_delay_seconds <= 0
    ):
        raise ValueError("INVALID_DELAY")

    if expected_symbols < 0:
        raise ValueError("INVALID_EXPECTED")

    if not 0 <= processed_symbols <= expected_symbols:
        raise ValueError("INVALID_PROCESSED")

    missing = expected_symbols - processed_symbols

    if not 0 <= limiter_pre_send_skips <= missing:
        raise ValueError("INVALID_SKIPS")

    normal = RecoveryDecision(
        expedite=False,
        delay_seconds=normal_delay_seconds,
        reason="NORMAL_SCHEDULE",
    )

    if not enabled:
        return normal

    if previous_recovery_attempted:
        return RecoveryDecision(
            expedite=False,
            delay_seconds=normal_delay_seconds,
            reason="RECOVERY_ALREADY_ATTEMPTED",
        )

    if limiter_pre_send_skips == 0:
        return normal

    if missing == 0:
        return normal

    delay = min(
        normal_delay_seconds,
        recovery_delay_seconds,
    )

    if delay >= normal_delay_seconds:
        return normal

    return RecoveryDecision(
        expedite=True,
        delay_seconds=delay,
        reason="LIMITER_PRE_SEND_SKIP_NEW_CYCLE",
    )
