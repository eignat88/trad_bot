"""ATR Wick Filter OOS V2_D — Prospective StochRSI Filter Experiment.

Applies a StochRSI filter to ATR Wick SHORT raw candidates:

  Filter: 0.20 <= StochRSI < 0.60
  Direction: SHORT (always)
  Volume filter: NOT applied

Architecture:
  - Uses the same raw candidate detection as V1
  - Applies V2_D-specific filter on top
  - Records PASS candidates as separate prospective cohort
  - Does NOT modify V1 scanner logic or V1 data

Prospective rules:
  - Only new signals from deploy time forward
  - No backfill of historical V1 observations
  - Each PASS candidate stored with experiment_id = ATR_WICK_FILTER_OOS_V2_D

Author: MiMo-v2.5
Date: 2026-09-26
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "ATR_WICK_FILTER_OOS_V2_D"
SCANNER_NAME = "ATR_WICK_REJECTION_SHORT_V1"  # source scanner

# --- V2_D Filter Thresholds (FROZEN — do not change after deployment) ---
STOCH_RSI_MIN = 0.20   # inclusive lower bound
STOCH_RSI_MAX = 0.60   # exclusive upper bound


@dataclass(frozen=True)
class V2DFilterResult:
    """Result of applying the V2_D filter to a raw candidate."""
    passed: bool
    reason: str
    stoch_rsi: float | None


def apply_v2d_filter(
    stoch_rsi: float | None,
    direction: str,
) -> V2DFilterResult:
    """Apply the V2_D filter to a raw ATR Wick candidate.

    Filter rules:
      1. direction must be SHORT
      2. 0.20 <= StochRSI < 0.60
      3. StochRSI must not be None

    No volume filter is applied.

    Returns V2DFilterResult with passed=True/False and reason.
    """
    # Rule 1: direction must be SHORT
    if direction != "SHORT":
        return V2DFilterResult(
            passed=False,
            reason="DIRECTION_NOT_SHORT",
            stoch_rsi=stoch_rsi,
        )

    # Rule 2: StochRSI must be available
    if stoch_rsi is None:
        return V2DFilterResult(
            passed=False,
            reason="STOCH_RSI_NULL",
            stoch_rsi=None,
        )

    # Rule 3: 0.20 <= StochRSI < 0.60
    if stoch_rsi < STOCH_RSI_MIN:
        return V2DFilterResult(
            passed=False,
            reason=f"STOCH_RSI_BELOW_MIN ({stoch_rsi:.4f} < {STOCH_RSI_MIN})",
            stoch_rsi=stoch_rsi,
        )

    if stoch_rsi >= STOCH_RSI_MAX:
        return V2DFilterResult(
            passed=False,
            reason=f"STOCH_RSI_ABOVE_MAX ({stoch_rsi:.4f} >= {STOCH_RSI_MAX})",
            stoch_rsi=stoch_rsi,
        )

    return V2DFilterResult(
        passed=True,
        reason="PASS_ALL_FILTERS",
        stoch_rsi=stoch_rsi,
    )
