"""Research adapter for TREND_PULLBACK_V3_HIGH_VOL_OOS_V1.

Captures TREND_PULLBACK_V3 LONG candidates that pass all filters except
regime_filter (actual=HIGH_VOLATILITY, required=TREND_UP) to check if
signals would have been profitable in current regime.

Features (from scanner SetupCandidate.features):
    htf_context          – bool, HTF context available
    trend_alignment      – bool, EMA20 > EMA50 and price > EMA200
    pullback_to_ema      – bool, price near EMA20 or EMA50
    pullback_quality     – float [0,1], quality of pullback
    rsi_momentum         – bool, RSI > threshold
    rsi_confirmation     – float [0,1], RSI confirmation strength
    adx_trend_strength   – bool, ADX > threshold
    adx_value            – float, ADX value
    ema50_slope_positive – bool, EMA50 slope > 0
    ema50_slope_value    – float, EMA50 slope value
    hour_filter          – bool, within allowed trading hours
    stop_distance_ok     – bool, stop distance valid
    target_r             – float, target R:R ratio (0.50)
    risk_r               – float, risk distance
    signal_timeframe     – str, signal timeframe (15m)
    recommended_expiry_bars – int, recommended expiry bars
    recommended_expiry_policy – str, expiry policy (BREAKEVEN)

Historical baseline:
    No historical data (scanner is new)
"""
from __future__ import annotations

from typing import Any


SCANNER_NAME = "TREND_PULLBACK_V3"
EXPERIMENT_ID = "TREND_PULLBACK_V3_HIGH_VOL_OOS_V1"
PARAMETER_SET_ID = "tpv3_1.0.0_20260928_high_vol_oos"

FROZEN_PARAMETERS: dict[str, Any] = {
    "pullback_tolerance": 0.012,
    "rsi_threshold": 60.0,
    "adx_threshold": 35.0,
    "ema50_slope_min": 0.0,
    "allowed_regimes": ["TREND_UP"],
    "counterfactual_regimes": ["HIGH_VOLATILITY", "SIDEWAYS", "TREND_DOWN"],
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
        "Counterfactual OOS capture for TREND_PULLBACK_V3 LONG candidates "
        "rejected by regime filter (actual=HIGH_VOLATILITY, required=TREND_UP). "
        "Goal: check if signals would have been profitable in current regime."
    ),
}
