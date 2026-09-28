"""Research adapter for FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1.

Captures FVG_REACTION_LONG_LOCAL_STRUCT_V1 candidates that are rejected by the
expectancy filter (negative historical performance) to check if there's a subset
with positive edge within the bad overall sample.

Features (from scanner SetupCandidate.features):
    fvg_created_at      – int, timestamp of FVG creation
    fvg_low             – float, FVG lower boundary
    fvg_high            – float, FVG upper boundary
    fvg_size            – float, FVG size in price
    fvg_atr             – float, FVG size in ATR units
    c2_body_ratio       – float, C2 candle body ratio
    c2_body_atr         – float, C2 body in ATR units
    bars_to_touch       – int, bars from FVG to first touch
    touch_at            – int, timestamp of first touch
    confirmation_at     – int, timestamp of LOCAL_STRUCT confirmation
    entry_price         – float, entry price
    sl_price            – float, stop loss price
    tp_price            – float, take profit price
    risk                – float, risk distance
    risk_pct            – float, risk as % of entry
    rr                  – float, reward:risk ratio (3.0)

Historical baseline:
    samples: 78
    entries: 78
    avg_r_after_costs: -0.4200
    profit_factor: 0.4593
    win_rate: 0.1667
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "FVG_REACTION_LONG_LOCAL_STRUCT_V1"
EXPERIMENT_ID = "FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1"
PARAMETER_SET_ID = "fvg_1.0.1_20260928_expectancy_oos"

FROZEN_PARAMETERS: dict[str, Any] = {
    "min_fvg_atr": 0.05,
    "min_c2_body_ratio": 0.50,
    "max_bars_to_touch": 48,
    "sl_buffer_atr": 0.05,
    "target_r": 3.0,
    "expectancy_threshold": 0.0,
}

EXPERIMENT_CONFIG: dict[str, Any] = {
    "experiment_id": EXPERIMENT_ID,
    "scanner_name": SCANNER_NAME,
    "scanner_version": "1.0.1",
    "parameter_set_id": PARAMETER_SET_ID,
    "parameters": FROZEN_PARAMETERS,
    "htf_timeframe": "1h",
    "setup_timeframe": "15m",
    "entry_timeframe": "5m",
    "description": (
        "Prospective OOS capture for FVG_REACTION_LONG_LOCAL_STRUCT_V1 candidates "
        "rejected by expectancy filter (negative historical performance). "
        "Goal: check if there's a subset with positive edge within the bad overall sample."
    ),
}
