"""Research adapter for TREND_PULLBACK_V2 scanner.

439 candidates total, 0 setups — best candidate for rejected candidate research.

Features (from scanner SetupCandidate.features):
    htf_context              – bool, HTF trend alignment confirmed
    trend_alignment          – bool, EMA trend aligned
    pullback_to_ema          – bool, price near EMA
    pullback_quality         – float [0,1], proximity to EMA normalised
    rsi_cool                 – bool, RSI below cooldown threshold
    rsi_confirmation         – float [0,1], RSI proximity to 50
    stop_distance_ok         – bool, invalidation geometry valid
    target_r                 – float, target risk-reward ratio (0.50)
    risk_r                   – float, absolute risk in price units
    recommended_expiry_policy – str, "BREAKEVEN"

Parameters (frozen):
    pullback_tolerance: 0.012
    rsi_cool_threshold: 55
    target_r: 0.50
    stop_buffer: 0.002
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "TREND_PULLBACK_V2"
EXPERIMENT_ID = "TPV2_GENERIC_V1"
PARAMETER_SET_ID = "tpv2_1.0.0_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "pullback_tolerance": 0.012,
    "rsi_cool_threshold": 55,
    "target_r": 0.50,
    "stop_buffer": 0.002,
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
        "Generic research observation for TREND_PULLBACK_V2. "
        "Captures all LONG candidates for outcome evaluation "
        "and offline parameter optimization."
    ),
}
