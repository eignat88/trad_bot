"""Research adapter for BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1.

Captures BREAKOUT_RETEST LONG candidates that are rejected by the expectancy filter
(profit_factor < 1.20) to evaluate if new signals have positive edge despite
historical performance.

Features (from scanner SetupCandidate.features):
    volume_ratio       – float [0,1], breakout volume / average volume
    retest_distance    – float [0,1], proximity to retest zone center
    rr_ratio           – float [0,1], reward-to-risk normalised
    stop_distance_atr  – float [0,1], stop distance in ATR (inverted)
    regime_alignment   – float, 1.0 if aligned, 0.3 otherwise

Historical baseline:
    samples: 63
    entries: 56
    avg_r_after_costs: 0.0037
    profit_factor: 1.0976
    win_rate: 0.1607
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "BREAKOUT_RETEST"
EXPERIMENT_ID = "BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1"
PARAMETER_SET_ID = "br_2.0.0_20260928_expectancy_oos"

FROZEN_PARAMETERS: dict[str, Any] = {
    "swing_lookback": 5,
    "breakout_margin": 0.001,
    "retest_margin": 0.003,
    "expectancy_threshold": 1.20,
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
        "Prospective OOS capture for BREAKOUT_RETEST LONG candidates "
        "rejected by expectancy filter (profit_factor < 1.20). "
        "Goal: check if new signals have positive edge despite historical PF=1.0976."
    ),
}
