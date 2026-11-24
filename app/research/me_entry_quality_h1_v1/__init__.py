"""Issue #297 — ME Inverse SHORT V3 / H1 HTF trend alignment (research-only).

Hypothesis H1 (parent #295 ``ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1``): part of
the losing ``ME_LONG_V2_INVERSE_SHORT_V3`` SHORT signals arise while the
higher-timeframe structure is still bullish.  This package classifies the 4H
and 1D market structure CAUSALLY at the original signal time so that OOS
analysis can compare SHORT outcomes under ``BULLISH`` / ``BEARISH`` /
``NEUTRAL`` HTF structure.

Scope guarantees (Issue #297 §7):

* pure functions only — no network, no database, no clock reads, no I/O;
* nothing in this package is wired into any scanner, orchestrator, trading
  gate, systemd unit or migration;
* HTF classification NEVER changes a trading decision and NEVER blocks a
  signal — it is an observation feature for offline OOS analysis;
* the module must not convert missing data into a ``NEUTRAL`` market mode:
  data problems fail closed with ``INVALID_SOURCE`` and an explicit reason
  code.

Modules:

* ``htf_candles``  — H1-A.1 HTF candle validation (4H / 1D, causal ASOF);
* ``structure``    — H1-A.2 causal swing structure, HH/HL/LH/LL/EQ, BOS;
* ``classifier``   — H1-A.3 / H1-A.4 regime classification and the combined
  4H/1D contract.
"""
from __future__ import annotations

from .classifier import (
    CANONICAL_LOOKBACK_CANDLES,
    COMBINED_REGIME_SEPARATOR,
    H1_VERSION,
    SWING_LOOKBACK_DEFAULT,
    VALID_COMBINED_REGIMES,
    classify_combined,
    classify_timeframe,
)
from .htf_candles import (
    TIMEFRAME_1D,
    TIMEFRAME_4H,
    TIMEFRAME_MS,
    CandleValidationResult,
    validate_htf_candles,
)
from .structure import (
    BosEvent,
    Swing,
    detect_bos_events,
    detect_swings,
    pair_relation,
    usable_swings,
)

__all__ = [
    "BosEvent",
    "CANONICAL_LOOKBACK_CANDLES",
    "CandleValidationResult",
    "COMBINED_REGIME_SEPARATOR",
    "H1_VERSION",
    "SWING_LOOKBACK_DEFAULT",
    "Swing",
    "TIMEFRAME_1D",
    "TIMEFRAME_4H",
    "TIMEFRAME_MS",
    "VALID_COMBINED_REGIMES",
    "classify_combined",
    "classify_timeframe",
    "detect_bos_events",
    "detect_swings",
    "pair_relation",
    "usable_swings",
    "validate_htf_candles",
]
