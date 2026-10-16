"""Runtime opt-in and deployment guards for V3 research capture."""
from __future__ import annotations

import os
from datetime import datetime

from .protocol import (
    APPROVED_FINGERPRINT,
    FROZEN_AT,
    ACTIVATION_AT,
    validate_freeze_activation,
    require_protocol_fingerprint,
)


def capture_enabled() -> bool:
    return os.environ.get("ME_EARLY_MAE_V3_CAPTURE_ENABLED", "").lower() == "true"


def require_capture_opt_in() -> None:
    if not capture_enabled():
        raise RuntimeError("ME_R_EARLY_MAE_V3_CAPTURE_DISABLED")


def require_runtime_configuration(now: datetime) -> None:
    require_capture_opt_in()
    if APPROVED_FINGERPRINT is None:
        raise RuntimeError("ME_R_EARLY_MAE_V3_FINGERPRINT_NOT_APPROVED")
    validate_freeze_activation(FROZEN_AT, ACTIVATION_AT, now)
    require_protocol_fingerprint(FROZEN_AT, ACTIVATION_AT)
