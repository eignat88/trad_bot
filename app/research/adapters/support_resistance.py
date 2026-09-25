"""Research adapter for SUPPORT_RESISTANCE_REACTION scanner.

91 historical candidates (LONG 49 + SHORT 42), 2 executed.
Both directions supported. Has specialized SRR_LONG pipeline running parallel.

Features (normalised):
    level_touch_count     – float [0,1], touches normalised to 5
    rejection_strength    – float [0,1], wick/body ratio
    rr_ratio              – float [0,1], reward-to-risk
    stop_distance_atr     – float [0,1], stop in ATR (inverted)
    volume_spike          – bool, volume > 1.3x average
    regime_alignment      – float [0,1], regime match

Features (raw, for parameter analysis):
    _raw_touch_count, _raw_level_distance_pct, _raw_atr, _raw_atr_pct,
    _raw_candle_range, _raw_candle_body, _raw_upper_wick, _raw_lower_wick,
    _raw_wick_body_ratio, _raw_volume, _raw_volume_ratio, _raw_rr,
    _raw_risk_distance, _raw_risk_distance_pct, _raw_stop_distance_atr,
    _raw_level_type

Parameters (frozen):
    swing_lookback: 5
    level_proximity_pct: 0.003
    min_touches: 3
    min_rejection_body_ratio: 0.5
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "SUPPORT_RESISTANCE_REACTION"
EXPERIMENT_ID = "SRR_GENERIC_V1"
PARAMETER_SET_ID = "srr_2.0.0_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "swing_lookback": 5,
    "level_proximity_pct": 0.003,
    "min_touches": 3,
    "min_rejection_body_ratio": 0.5,
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
        "Generic research observation for SUPPORT_RESISTANCE_REACTION. "
        "Captures all LONG and SHORT candidates. Runs parallel to "
        "specialized SRR_LONG_OUTCOME_V1 pipeline."
    ),
}
