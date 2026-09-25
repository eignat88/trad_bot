"""Research adapter for BREAKOUT_RETEST scanner.

185 candidates total, 16 executed, 0 currently active — good candidate
for research on filter quality and parameter optimization.

Features (from scanner SetupCandidate.features):
    volume_ratio       – float [0,1], breakout volume / average volume
    retest_distance    – float [0,1], proximity to retest zone center
    rr_ratio           – float [0,1], reward-to-risk normalised
    stop_distance_atr  – float [0,1], stop distance in ATR (inverted)
    regime_alignment   – float, 1.0 if aligned, 0.3 otherwise

Parameters (frozen):
    swing_lookback: 5
    breakout_margin: 0.001
    retest_margin: 0.003
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "BREAKOUT_RETEST"
EXPERIMENT_ID = "BR_GENERIC_V1"
PARAMETER_SET_ID = "br_2.0.0_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "swing_lookback": 5,
    "breakout_margin": 0.001,
    "retest_margin": 0.003,
}

EXPERIMENT_CONFIG: dict[str, Any] = {
    "experiment_id": EXPERIMENT_ID,
    "scanner_name": SCANNER_NAME,
    "scanner_version": "2.0.0",
    "parameter_set_id": PARAMETER_SET_ID,
    "parameters": FROZEN_PARAMETERS,
    "htf_timeframe": "1h",
    "setup_timeframe": "15m",
    "entry_timeframe": "5m",
    "description": (
        "Generic research observation for BREAKOUT_RETEST. "
        "Captures all LONG and SHORT candidates for outcome evaluation "
        "and offline parameter optimization."
    ),
}
