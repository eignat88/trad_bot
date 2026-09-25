"""Research adapter for LIQUIDITY_SWEEP_CHOCH_OB scanner.

5 historical candidates (LONG 2 + SHORT 3), 1 executed.
Both directions supported.

Features:
    sweep_depth           – float [0,1], how far past level
    displacement_strength – float [0,1], body/range of displacement candle
    ob_distance           – float [0,1], proximity to OB center
    rr_ratio              – float [0,1], reward-to-risk
    stop_distance_atr     – float [0,1], stop in ATR (inverted)
    volume_spike          – bool, volume > 1.3x average
    rsi_confirmation      – float, neutral placeholder (0.5)

Parameters (frozen):
    ob_lookback: 5
    swing_lookback: 5
    sweep_margin: 0.001
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "LIQUIDITY_SWEEP_CHOCH_OB"
EXPERIMENT_ID = "LSCO_GENERIC_V1"
PARAMETER_SET_ID = "lsco_2.0.0_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "ob_lookback": 5,
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
        "Generic research observation for LIQUIDITY_SWEEP_CHOCH_OB. "
        "Captures all LONG and SHORT candidates for outcome evaluation."
    ),
}
