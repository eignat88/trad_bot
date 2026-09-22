"""FVG Reaction Long Local Struct V1 — Production Scanner.

Implements a stateful scanner that detects bullish Fair Value Gaps,
waits for price retracement (first touch), then requires LOCAL_STRUCT
confirmation before emitting a LONG signal.

Frozen configuration from V2 validation (OOS ROBUST CANDIDATE):
  Direction:      LONG
  Entry:          FIRST_TOUCH
  Confirmation:   LOCAL_STRUCT
  SL:             C2_EXTREMUM (C2.low - buffer)
  TP:             3.0R
  MIN_FVG_ATR:    0.05
  MIN_C2_BODY_RATIO: 0.50
  MAX_BARS_TO_TOUCH: 48

LOCAL_STRUCT formula (from research backtest_engine.py):
  On candle AFTER touch:
    swing_high = max(c.high for c in candles[touch_idx-20 : touch_idx])
    confirmation = candle.close > swing_high

References:
  FVG_REACTION_LONG_LOCAL_STRUCT_V2_VALIDATION
  FVG_REACTION_LONG_LOCAL_STRUCT_V2_SPECIFICATION.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.scanners.models import (
    MarketContext,
    ScannerDirection,
    SetupCandidate,
    SetupState,
)

logger = logging.getLogger(__name__)

SCANNER_NAME = "FVG_REACTION_LONG_LOCAL_STRUCT_V1"
SCANNER_VERSION = "1.0.0"

# --- Frozen eligibility from V2 validation ---
MIN_FVG_ATR = 0.05
MIN_C2_BODY_RATIO = 0.50
MAX_BARS_TO_TOUCH = 48

# --- SL/TP ---
SL_BUFFER_ATR = 0.05  # SL = C2.low - atr * SL_BUFFER_ATR
TARGET_R = 3.0

# --- LOCAL_STRUCT lookback ---
LOCAL_STRUCT_LOOKBACK = 20


# ---------------------------------------------------------------------------
# FVG detection (3-candle model)
# ---------------------------------------------------------------------------

def _atr(candles: list, period: int = 14) -> float:
    """Wilder ATR from a list of Candle objects."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for prev, cur in zip(candles, candles[1:]):
        tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def _volume_sma(candles: list, period: int = 20) -> float:
    """Simple moving average of volume."""
    if not candles:
        return 1.0
    n = min(period, len(candles))
    return sum(c.volume for c in candles[-n:]) / n


def detect_bullish_fvg(candles: list, index: int) -> dict[str, Any] | None:
    """Detect a bullish FVG at position `index` (which is C3).

    Returns FVG dict if detected, None otherwise.
    FVG is formed only after C3 closes — no look-ahead.
    """
    if index < 2:
        return None

    c1 = candles[index - 2]
    c2 = candles[index - 1]
    c3 = candles[index]

    # Bullish FVG: C3.low > C1.high
    if c3.low <= c1.high:
        return None

    fvg_low = c1.high
    fvg_high = c3.low
    fvg_size = fvg_high - fvg_low

    # Compute ATR at C3
    atr_val = _atr(candles[: index + 1])

    # Compute C2 metrics
    c2_body = abs(c2.close - c2.open)
    c2_range = c2.high - c2.low
    c2_body_ratio = c2_body / c2_range if c2_range > 0 else 0.0
    c2_body_atr = c2_body / atr_val if atr_val > 0 else 0.0

    # Eligibility
    if atr_val <= 0 or fvg_size / atr_val < MIN_FVG_ATR:
        return None
    if c2_body_ratio < MIN_C2_BODY_RATIO:
        return None

    return {
        "c1_timestamp": c1.timestamp,
        "c2_timestamp": c2.timestamp,
        "c3_timestamp": c3.timestamp,
        "fvg_created_at": c3.timestamp,
        "fvg_low": fvg_low,
        "fvg_high": fvg_high,
        "fvg_size": fvg_size,
        "fvg_atr": fvg_size / atr_val if atr_val > 0 else 0.0,
        "c2_high": c2.high,
        "c2_low": c2.low,
        "c2_body_ratio": c2_body_ratio,
        "c2_body_atr": c2_body_atr,
        "atr": atr_val,
        "c3_index": index,
    }


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class FVGState(str, Enum):
    DETECTED = "DETECTED"
    WAITING_TOUCH = "WAITING_TOUCH"
    TOUCHED = "TOUCHED"
    WAITING_LOCAL_STRUCT = "WAITING_LOCAL_STRUCT"
    CONFIRMED = "CONFIRMED"
    SIGNAL_EMITTED = "SIGNAL_EMITTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


@dataclass
class FVGSetup:
    """Tracks a single FVG through its lifecycle."""
    symbol: str
    timeframe: str
    state: FVGState = FVGState.DETECTED

    # FVG formation data
    fvg_created_at: int = 0
    fvg_low: float = 0.0
    fvg_high: float = 0.0
    fvg_size: float = 0.0
    fvg_atr: float = 0.0

    # C2 data
    c2_timestamp: int = 0
    c2_high: float = 0.0
    c2_low: float = 0.0
    c2_body_ratio: float = 0.0
    c2_body_atr: float = 0.0
    c2_index: int = 0

    # ATR
    atr: float = 0.0

    # Index tracking
    fvg_c3_index: int = 0  # index of C3 in candle array
    bars_since_creation: int = 0

    # Touch data
    first_touch_at: int | None = None
    bars_to_touch: int | None = None
    touch_price: float | None = None
    touch_candle_index: int | None = None

    # Confirmation data
    confirmation_at: int | None = None
    confirmation_candle_index: int | None = None

    # Signal data (when confirmed)
    entry_price: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    risk: float = 0.0

    # Metadata
    market_regime: str | None = None


# ---------------------------------------------------------------------------
# LOCAL_STRUCT confirmation
# ---------------------------------------------------------------------------

def check_local_struct_long(candles: list, touch_candle_idx: int) -> tuple[bool, float]:
    """Check LOCAL_STRUCT confirmation for LONG.

    Formula (from research backtest_engine.py):
      swing_high = max(c.high for c in candles[touch_idx-20 : touch_idx])
      On next candle: confirmed if candle.close > swing_high

    Args:
        candles: Full candle array.
        touch_candle_idx: Index of the candle that touched the FVG.

    Returns:
        (is_confirmed, swing_high_value)
    """
    check_idx = touch_candle_idx + 1
    if check_idx >= len(candles):
        return False, 0.0

    start = max(0, touch_candle_idx - LOCAL_STRUCT_LOOKBACK)
    if touch_candle_idx <= start:
        return False, 0.0

    swing_high = max(c.high for c in candles[start:touch_candle_idx])
    confirmed = candles[check_idx].close > swing_high

    return confirmed, swing_high


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class FVGReactionLongLocalStructV1Scanner:
    """Stateful scanner: detects bullish FVGs, waits for touch + LOCAL_STRUCT."""

    name = SCANNER_NAME
    version = SCANNER_VERSION

    def __init__(self) -> None:
        # Active FVG setups keyed by (symbol, timeframe, fvg_created_at)
        self._setups: dict[tuple[str, str, int], FVGSetup] = {}
        # Emitted signal fingerprints to prevent duplicates
        self._emitted: set[str] = set()

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        """Scan for FVG setups. Called each cycle per symbol."""
        candidates: list[SetupCandidate] = []

        for tf_label, candles in [
            ("5m", ctx.candles_5m),
            ("15m", ctx.candles_15m),
        ]:
            if len(candles) < 3:
                continue

            # Process existing setups first (may emit signals)
            setup_candidates = self._update_setups(ctx.symbol, tf_label, candles, ctx)
            candidates.extend(setup_candidates)

            # Detect new FVGs on the latest candle
            new_candidates = self._detect_and_track(ctx.symbol, tf_label, candles, ctx)
            candidates.extend(new_candidates)

            # If new FVGs were just created, immediately check touch/confirmation
            # on the current candle data (handles both incremental and batch modes)
            if new_candidates or any(
                s.state == FVGState.WAITING_TOUCH
                for s in self._setups.values()
                if s.symbol == ctx.symbol and s.timeframe == tf_label
            ):
                more = self._update_setups(ctx.symbol, tf_label, candles, ctx)
                candidates.extend(more)

        return candidates

    def _detect_and_track(
        self,
        symbol: str,
        timeframe: str,
        candles: list,
        ctx: MarketContext,
    ) -> list[SetupCandidate]:
        """Detect new FVGs and start tracking them."""
        candidates: list[SetupCandidate] = []
        last_idx = len(candles) - 1

        # Detect FVG on the most recent candle (C3 = last candle)
        fvg = detect_bullish_fvg(candles, last_idx)
        if fvg is None:
            return candidates

        # Duplicate check: don't create if same FVG already tracked
        key = (symbol, timeframe, fvg["fvg_created_at"])
        if key in self._setups:
            return candidates

        # Create new FVG setup
        setup = FVGSetup(
            symbol=symbol,
            timeframe=timeframe,
            state=FVGState.WAITING_TOUCH,
            fvg_created_at=fvg["fvg_created_at"],
            fvg_low=fvg["fvg_low"],
            fvg_high=fvg["fvg_high"],
            fvg_size=fvg["fvg_size"],
            fvg_atr=fvg["fvg_atr"],
            c2_timestamp=fvg["c2_timestamp"],
            c2_high=fvg["c2_high"],
            c2_low=fvg["c2_low"],
            c2_body_ratio=fvg["c2_body_ratio"],
            c2_body_atr=fvg["c2_body_atr"],
            c2_index=fvg["c3_index"] - 1,
            atr=fvg["atr"],
            fvg_c3_index=fvg["c3_index"],
            market_regime=ctx.market_regime,
        )

        self._setups[key] = setup
        logger.debug(
            "%s %s: FVG detected fvg_low=%.4f fvg_high=%.4f fvg_atr=%.4f c2_body_ratio=%.2f",
            symbol, timeframe, setup.fvg_low, setup.fvg_high,
            setup.fvg_atr, setup.c2_body_ratio,
        )

        return candidates

    def _update_setups(
        self,
        symbol: str,
        timeframe: str,
        candles: list,
        ctx: MarketContext,
    ) -> list[SetupCandidate]:
        """Update all active setups for this symbol/timeframe."""
        candidates: list[SetupCandidate] = []
        last_idx = len(candles) - 1
        last_candle = candles[last_idx]

        to_expire: list[tuple[str, str, int]] = []
        to_confirm: list[tuple[str, str, int]] = []

        for key, setup in self._setups.items():
            if setup.symbol != symbol or setup.timeframe != timeframe:
                continue
            if setup.state in (FVGState.EXPIRED, FVGState.INVALIDATED, FVGState.SIGNAL_EMITTED):
                continue

            setup.bars_since_creation = last_idx - setup.fvg_c3_index

            # --- State: WAITING_TOUCH ---
            if setup.state == FVGState.WAITING_TOUCH:
                # Check expiration
                if setup.bars_since_creation > MAX_BARS_TO_TOUCH:
                    setup.state = FVGState.EXPIRED
                    logger.debug("%s %s: FVG expired (bars=%d)", symbol, timeframe, setup.bars_since_creation)
                    continue

                # Check touch: price enters FVG zone (high >= fvg_low for bullish)
                # Only check candles AFTER the FVG was formed (fvg_c3_index + 1)
                if last_idx > setup.fvg_c3_index and last_candle.low <= setup.fvg_high:
                    setup.state = FVGState.TOUCHED
                    setup.first_touch_at = last_candle.timestamp
                    setup.bars_to_touch = setup.bars_since_creation
                    setup.touch_price = min(last_candle.low, setup.fvg_high)
                    setup.touch_candle_index = last_idx

                    # Immediately check LOCAL_STRUCT on next candle
                    setup.state = FVGState.WAITING_LOCAL_STRUCT

            # --- State: WAITING_LOCAL_STRUCT ---
            elif setup.state == FVGState.WAITING_LOCAL_STRUCT:
                # Check expiration
                if setup.bars_since_creation > MAX_BARS_TO_TOUCH:
                    setup.state = FVGState.EXPIRED
                    continue

                # LOCAL_STRUCT check: on the candle AFTER touch
                if setup.touch_candle_index is not None:
                    confirmed, swing_high = check_local_struct_long(
                        candles, setup.touch_candle_index,
                    )
                    if confirmed:
                        setup.state = FVGState.CONFIRMED
                        setup.confirmation_at = last_candle.timestamp
                        setup.confirmation_candle_index = last_idx

                        # Compute entry, SL, TP
                        # Entry: at the close of confirmation candle (next candle after touch)
                        entry_price = last_candle.close
                        sl_price = setup.c2_low - setup.atr * SL_BUFFER_ATR
                        risk = entry_price - sl_price

                        if risk <= 0 or entry_price <= sl_price:
                            setup.state = FVGState.INVALIDATED
                            logger.debug(
                                "%s %s: FVG invalidated (bad geometry entry=%.4f sl=%.4f)",
                                symbol, timeframe, entry_price, sl_price,
                            )
                            continue

                        tp_price = entry_price + TARGET_R * risk

                        setup.entry_price = entry_price
                        setup.sl_price = sl_price
                        setup.tp_price = tp_price
                        setup.risk = risk

                        # Create candidate
                        candidate = self._make_candidate(setup, ctx)
                        if candidate is not None:
                            candidates.append(candidate)
                            setup.state = FVGState.SIGNAL_EMITTED

        # Cleanup expired/invalidated
        for key in to_expire:
            if key in self._setups:
                del self._setups[key]

        return candidates

    def _make_candidate(self, setup: FVGSetup, ctx: MarketContext) -> SetupCandidate | None:
        """Convert a confirmed FVGSetup into a SetupCandidate."""
        if setup.entry_price is None or setup.sl_price is None or setup.tp_price is None:
            return None

        # Duplicate fingerprint check
        fp = f"{SCANNER_NAME}|{setup.symbol}|LONG|{setup.timeframe}|{setup.fvg_created_at}"
        if fp in self._emitted:
            return None
        self._emitted.add(fp)

        now = datetime.now(timezone.utc)

        return SetupCandidate(
            scanner_name=SCANNER_NAME,
            scanner_version=SCANNER_VERSION,
            symbol=setup.symbol,
            direction="LONG",
            htf_timeframe="1h",
            setup_timeframe=setup.timeframe,
            entry_timeframe=setup.timeframe,
            detected_at=now,
            setup_started_at=now,
            signal_candle_open_time=setup.fvg_created_at,
            reference_price=setup.entry_price,
            entry_zone_low=setup.entry_price,  # single price entry
            entry_zone_high=setup.entry_price,
            invalidation_price=setup.sl_price,
            target_1=setup.tp_price,
            target_2=None,
            score=80.0,  # base score
            market_regime=setup.market_regime,
            reasons=(
                "FVG_REACTION",
                f"FVG_ATR={setup.fvg_atr:.3f}",
                f"C2_BODY_RATIO={setup.c2_body_ratio:.2f}",
                f"BARS_TO_TOUCH={setup.bars_to_touch}",
            ),
            features={
                "fvg_created_at": setup.fvg_created_at,
                "fvg_low": setup.fvg_low,
                "fvg_high": setup.fvg_high,
                "fvg_size": setup.fvg_size,
                "fvg_atr": setup.fvg_atr,
                "c2_body_ratio": setup.c2_body_ratio,
                "c2_body_atr": setup.c2_body_atr,
                "bars_to_touch": setup.bars_to_touch,
                "touch_at": setup.first_touch_at,
                "confirmation_at": setup.confirmation_at,
                "entry_price": setup.entry_price,
                "sl_price": setup.sl_price,
                "tp_price": setup.tp_price,
                "risk": setup.risk,
                "risk_pct": setup.risk / setup.entry_price if setup.entry_price else 0,
                "rr": TARGET_R,
            },
            state=SetupState.READY_TO_TRADE,
        )

    def cleanup_expired(self) -> int:
        """Remove old expired/invalidated setups. Returns count removed."""
        to_remove = [
            key for key, setup in self._setups.items()
            if setup.state in (FVGState.EXPIRED, FVGState.INVALIDATED, FVGState.SIGNAL_EMITTED)
        ]
        for key in to_remove:
            del self._setups[key]
        return len(to_remove)

    @property
    def active_setups(self) -> int:
        """Number of currently active (non-terminal) setups."""
        return sum(
            1 for s in self._setups.values()
            if s.state not in (FVGState.EXPIRED, FVGState.INVALIDATED, FVGState.SIGNAL_EMITTED)
        )
