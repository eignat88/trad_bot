"""Research adapter for MOMENTUM_EXHAUSTION_R scanner.

Pilot scanner for the generic research framework.
65 candidates, 0 executions — ideal for rejected candidate research.

Features (all normalised to [0, 1]):
    exhaustion_magnitude  – how far past the prior swing, normalised
    body_ratio            – body / range ratio of the exhaustion candle
    rsi_confirmation      – RSI proximity to overbought/oversold zone
    volume_ratio          – volume / average volume, normalised
    rr_ratio              – reward-to-risk normalised
    stop_distance_atr     – stop distance in ATR (inverted: tighter = higher)

Parameters (frozen):
    swing_lookback: 5
    exhaustion_threshold: 0.003
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "MOMENTUM_EXHAUSTION_R"
EXPERIMENT_ID = "MER_GENERIC_V1"
PARAMETER_SET_ID = "mer_1.0.0_20260928"

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
        "Generic research observation for MOMENTUM_EXHAUSTION_R. "
        "Captures all LONG and SHORT candidates for outcome evaluation "
        "and offline parameter optimization."
    ),
}
