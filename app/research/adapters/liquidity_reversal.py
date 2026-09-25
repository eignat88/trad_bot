"""Research adapter for LIQUIDITY_REVERSAL scanner.

40 historical candidates (LONG 33 + SHORT 7), 2 executed.
Both directions supported.

Features:
    sweep_depth           – float [0,1], how far past level
    rejection_strength    – float [0,1], wick/body ratio
    rr_ratio              – float [0,1], reward-to-risk
    stop_distance_atr     – float [0,1], stop in ATR (inverted)
    volume_spike          – bool, volume > 1.5x average
    regime_alignment      – float [0,1], regime match

Parameters (frozen):
    swing_lookback: 5
    sweep_margin: 0.001
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "LIQUIDITY_REVERSAL"
EXPERIMENT_ID = "LR_GENERIC_V1"
PARAMETER_SET_ID = "lr_2.0.0_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "swing_lookback": 5,
    "sweep_margin": 0.001,
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
        "Generic research observation for LIQUIDITY_REVERSAL. "
        "Captures all LONG and SHORT candidates for outcome evaluation."
    ),
}
