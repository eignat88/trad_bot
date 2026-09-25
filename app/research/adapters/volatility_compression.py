"""Research adapter for VOLATILITY_COMPRESSION scanner.

232 historical candidates (LONG 104 + SHORT 128), 3 executed.
Both directions supported.

Features:
    squeeze_duration      – float [0,1], bars in squeeze normalised to 20
    expansion_ratio       – float [0,1], recent range / previous range
    bb_width_percentile   – float [0,1], BB width percentile (tighter = higher)
    volume_ratio          – float [0,1], volume / average normalised
    rr_ratio              – float [0,1], reward-to-risk normalised
    stop_distance_atr     – float [0,1], stop in ATR (inverted)

Parameters (frozen):
    atr_period: 14
    squeeze_lookback: 20
    bb_squeeze_threshold: 0.02
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "VOLATILITY_COMPRESSION"
EXPERIMENT_ID = "VC_GENERIC_V1"
PARAMETER_SET_ID = "vc_2.0.0_20260928"

FROZEN_PARAMETERS: dict[str, Any] = {
    "atr_period": 14,
    "squeeze_lookback": 20,
    "bb_squeeze_threshold": 0.02,
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
        "Generic research observation for VOLATILITY_COMPRESSION. "
        "Captures all LONG and SHORT candidates for outcome evaluation."
    ),
}
