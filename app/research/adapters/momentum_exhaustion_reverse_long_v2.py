"""Research adapter for MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 scanner.

Reversal scanner: bearish exhaustion → LONG setup. LONG only.

Features:
    exhaustion_magnitude       – float [0,1], overshoot past swing
    body_ratio                 – float [0,1], inverted body/range
    rsi_confirmation           – float [0,1], RSI proximity to overbought
    volume_ratio               – float [0,1], volume / average
    rr_ratio                   – float [0,1], reward-to-risk
    stop_distance_atr          – float [0,1], stop in ATR (inverted)
    rsi_14                     – float, current RSI(14) value
    rsi_14_3bars_ago           – float, RSI(14) from 3 bars ago
    rsi_delta_3                – float, rsi_now - rsi_prev (must be > 0)
    rsi_period                 – int, 14
    rsi_timeframe              – str, "5m"
    entry_confirmation_rsi_rising – bool, True when rsi_delta_3 > 0
    stop_loss_pct              – float, 2.5
    take_profit_pct            – float, 3.0
    hold_minutes               – int, 240
    source_scanner             – str, "MOMENTUM_EXHAUSTION"
    source_direction           – str, "SHORT"

Parameters (frozen):
    swing_lookback: 5
    exhaustion_threshold: 0.003
    rsi_period: 14
    rsi_delta_lookback: 3
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
EXPERIMENT_ID = "ME_RL_V2_GENERIC_V1"
PARAMETER_SET_ID = "me_rlv2_1.0.0_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "swing_lookback": 5,
    "exhaustion_threshold": 0.003,
    "rsi_period": 14,
    "rsi_delta_lookback": 3,
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
        "Generic research observation for MOMENTUM_EXHAUSTION_REVERSE_LONG_V2. "
        "Reversal scanner: bearish exhaustion → LONG. Includes RSI delta features."
    ),
}
