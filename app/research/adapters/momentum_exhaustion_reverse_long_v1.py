"""Research adapter for MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 scanner.

BLOCKED in production direction gates — but candidates still generated
and captured by research observer BEFORE gates. This is exactly the
use case: studying rejected/blocked candidates.

Features: same structure as V2 but without RSI delta features.
    exhaustion_magnitude  – float [0,1]
    body_ratio            – float [0,1]
    rsi_confirmation      – float [0,1]
    volume_ratio          – float [0,1]
    rr_ratio              – float [0,1]
    stop_distance_atr     – float [0,1]

Parameters (frozen):
    swing_lookback: 5
    exhaustion_threshold: 0.003
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"
EXPERIMENT_ID = "ME_RL_V1_GENERIC_V1"
PARAMETER_SET_ID = "me_rlv1_1.0.0_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "swing_lookback": 5,
    "exhaustion_threshold": 0.003,
}

EXPERIMENT_CONFIG: dict[str, Any] = {
    "experiment_id": EXPERIMENT_ID,
    "scanner_name": SCANNER_NAME,
    "scanner_version": "1.0.0",
    "parameter_set_id": PARAMETER_SET_ID,
    "parameters": FROZEN_PARAMETERS,
    "htf_timeframe": "1h",
    "setup_timeframe": "15m",
    "entry_timeframe": "5m",
    "description": (
        "Generic research observation for MOMENTUM_EXHAUSTION_REVERSE_LONG_V1. "
        "BLOCKED in production — captures blocked candidates for research."
    ),
}
