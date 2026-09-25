"""Research adapter for FVG_REACTION_LONG_LOCAL_STRUCT_V1 scanner.

Stateful scanner, LONG only. 3 historical candidates, 0 setups.
Has specialized FVG shadow pipeline running parallel.

Features:
    fvg_created_at         – int, timestamp
    fvg_low                – float
    fvg_high               – float
    fvg_size               – float
    fvg_atr                – float, fvg_size / ATR
    c2_body_ratio          – float, C2 body / range
    c2_body_atr            – float, C2 body / ATR
    bars_to_touch          – int, bars from FVG to first touch
    touch_at               – int, timestamp of first touch
    confirmation_at        – int, timestamp of LOCAL_STRUCT confirmation
    entry_price            – float
    sl_price               – float
    tp_price               – float
    risk                   – float, entry - sl
    risk_pct               – float, risk / entry
    rr                     – float, 3.0

Parameters (frozen — all thresholds are module-level constants):
    MIN_FVG_ATR: 0.05
    MIN_C2_BODY_RATIO: 0.50
    MAX_BARS_TO_TOUCH: 48
    SL_BUFFER_ATR: 0.05
    TARGET_R: 3.0
    LOCAL_STRUCT_LOOKBACK: 20
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "FVG_REACTION_LONG_LOCAL_STRUCT_V1"
EXPERIMENT_ID = "FVG_GENERIC_V1"
PARAMETER_SET_ID = "fvg_1.0.1_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "MIN_FVG_ATR": 0.05,
    "MIN_C2_BODY_RATIO": 0.50,
    "MAX_BARS_TO_TOUCH": 48,
    "SL_BUFFER_ATR": 0.05,
    "TARGET_R": 3.0,
    "LOCAL_STRUCT_LOOKBACK": 20,
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
        "Generic research observation for FVG_REACTION_LONG_LOCAL_STRUCT_V1. "
        "Stateful scanner, LONG only. Runs parallel to FVG shadow pipeline."
    ),
}
