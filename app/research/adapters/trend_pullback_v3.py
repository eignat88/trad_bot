"""Research adapter for TREND_PULLBACK_V3 scanner.

Edge-optimized variant with regime filters. LONG only.

Features:
    htf_context            – bool, HTF trend confirmed
    trend_alignment        – bool, EMA trend aligned
    pullback_to_ema        – bool, price near EMA
    pullback_quality       – float [0,1], proximity to EMA
    rsi_momentum           – bool, RSI > 60
    rsi_confirmation       – float [0,1], RSI proximity to 70
    adx_trend_strength     – bool, ADX > 35
    adx_value              – float, raw ADX value
    ema50_slope_positive   – bool, EMA50 slope > 0
    ema50_slope_value      – float, raw EMA50 slope
    hour_filter            – bool, within allowed hours
    stop_distance_ok       – bool, geometry valid
    target_r               – float, target risk-reward (0.50)
    risk_r                 – float, absolute risk
    signal_timeframe       – str, "15m"
    recommended_expiry_bars – int, 144
    recommended_expiry_policy – str, "BREAKEVEN"

Parameters (frozen):
    pullback_tolerance: 0.012
    rsi_threshold: 60.0
    adx_threshold: 35.0
    ema50_slope_min: 0.0
    target_r: 0.50
    stop_buffer: 0.002
    max_pullback_quality: 0.75
    hour_start: 6
    hour_end: 23
    excluded_hours: [10, 15, 19]
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "TREND_PULLBACK_V3"
EXPERIMENT_ID = "TPV3_GENERIC_V1"
PARAMETER_SET_ID = "tpv3_1.0.0_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "pullback_tolerance": 0.012,
    "rsi_threshold": 60.0,
    "adx_threshold": 35.0,
    "ema50_slope_min": 0.0,
    "target_r": 0.50,
    "stop_buffer": 0.002,
    "max_pullback_quality": 0.75,
    "hour_start": 6,
    "hour_end": 23,
    "excluded_hours": [10, 15, 19],
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
        "Generic research observation for TREND_PULLBACK_V3. "
        "Edge-optimized variant with regime filters. LONG only."
    ),
}
