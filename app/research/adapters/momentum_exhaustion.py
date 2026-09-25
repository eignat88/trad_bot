"""Research adapter for MOMENTUM_EXHAUSTION scanner.

77 historical candidates (SHORT 57 + LONG 20), 8 executed.
Both directions supported.

Features:
    exhaustion_magnitude  – float [0,1], how far past prior swing
    body_ratio            – float [0,1], body / range of exhaustion candle
    rsi_confirmation      – float [0,1], RSI proximity to overbought/oversold
    volume_ratio          – float [0,1], volume / average
    rr_ratio              – float [0,1], reward-to-risk normalised
    stop_distance_atr     – float [0,1], stop in ATR (inverted)

Parameters (frozen):
    swing_lookback: 5
    exhaustion_threshold: 0.003
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "MOMENTUM_EXHAUSTION"
EXPERIMENT_ID = "ME_GENERIC_V1"
PARAMETER_SET_ID = "me_1.0.0_20260928"

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
        "Generic research observation for MOMENTUM_EXHAUSTION. "
        "Captures all LONG and SHORT candidates for outcome evaluation."
    ),
}
