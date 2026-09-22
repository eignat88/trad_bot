"""FVG Reaction Long Local Struct V1 — Production Scanner.

Implements a stateful scanner that detects bullish Fair Value Gaps,
waits for price retracement (first touch), then requires LOCAL_STRUCT
confirmation before emitting a LONG signal.

STATE INDEX FIX (2026-09-22):
  All state is tracked by timestamp, NOT by candle array index.
  Each scan() call receives a fresh 200-candle rolling window where
  absolute indices shift between cycles.  We resolve timestamps to
  current indices on every cycle to avoid the "frozen index" bug.

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
  swing_high = max(c.high for c in candles[touch_idx-20:touch_idx])
  confirmed = next_candle.close > swing_high

References:
  FVG_REACTION_LONG_LOCAL_STRUCT_V2_VALIDATION
  FVG_REACTION_LONG_LOCAL_STRUCT_V2_SPECIFICATION.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.scanners.models import (
    MarketContext,
    SetupCandidate,
    SetupState,
)

logger = logging.getLogger(__name__)

SCANNER_NAME = "FVG_REACTION_LONG_LOCAL_STRUCT_V1"
SCANNER_VERSION = "1.0.1"  # bump for rolling-window fix

# --- Frozen eligibility from V2 validation ---
MIN_FVG_ATR = 0.05
MIN_C2_BODY_RATIO = 0.50
MAX_BARS_TO_TOUCH = 48

# --- SL/TP ---
SL_BUFFER_ATR = 0.05
TARGET_R = 3.0

# --- LOCAL_STRUCT lookback ---
LOCAL_STRUCT_LOOKBACK = 20

# Interval lookup for timestamp-based bar counting
_INTERVAL_MS: dict[str, int] = {
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_idx_by_ts(candles: list, ts: int) -> int | None:
    """Find index of candle with matching timestamp. O(n) but n<=200."""
    for i, c in enumerate(candles):
        if c.timestamp == ts:
            return i
    return None


def _bars_between(ts1: int, ts2: int, interval_ms: int) -> int:
    """Number of bars between two timestamps."""
    if interval_ms <= 0:
        return 0
    return max(0, (ts2 - ts1) // interval_ms)


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


# ---------------------------------------------------------------------------
# FVG detection
# ---------------------------------------------------------------------------

def detect_bullish_fvg(candles: list, index: int) -> dict[str, Any] | None:
    """Detect a bullish FVG at position `index` (C3).

    FVG formed only after C3 closes — no look-ahead.
    """
    if index < 2:
        return None

    c1 = candles[index - 2]
    c2 = candles[index - 1]
    c3 = candles[index]

    if c3.low <= c1.high:
        return None

    fvg_low = c1.high
    fvg_high = c3.low
    fvg_size = fvg_high - fvg_low
    atr_val = _atr(candles[: index + 1])

    c2_body = abs(c2.close - c2.open)
    c2_range = c2.high - c2.low
    c2_body_ratio = c2_body / c2_range if c2_range > 0 else 0.0
    c2_body_atr = c2_body / atr_val if atr_val > 0 else 0.0

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
    """Tracks a single FVG through its lifecycle.

    All temporal state is stored as timestamps, never as candle array indices.
    """
    symbol: str
    timeframe: str
    state: FVGState = FVGState.DETECTED

    # FVG formation data (immutable after creation)
    fvg_created_at: int = 0
    fvg_low: float = 0.0
    fvg_high: float = 0.0
    fvg_size: float = 0.0
    fvg_atr: float = 0.0

    # C2 data (immutable)
    c2_timestamp: int = 0
    c2_high: float = 0.0
    c2_low: float = 0.0
    c2_body_ratio: float = 0.0
    c2_body_atr: float = 0.0

    # ATR (immutable)
    atr: float = 0.0

    # Touch data (timestamps, not indices)
    first_touch_at: int | None = None
    bars_to_touch: int | None = None
    touch_price: float | None = None

    # Confirmation data
    confirmation_at: int | None = None

    # Signal data
    entry_price: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    risk: float = 0.0

    # Metadata
    market_regime: str | None = None


# ---------------------------------------------------------------------------
# LOCAL_STRUCT confirmation
# ---------------------------------------------------------------------------

def check_local_struct_long(candles: list, touch_idx: int) -> tuple[bool, float]:
    """Check LOCAL_STRUCT confirmation for LONG.

    Formula:
      swing_high = max(c.high for c in candles[touch_idx-20:touch_idx])
      confirmed = candles[touch_idx+1].close > swing_high
    """
    check_idx = touch_idx + 1
    if check_idx >= len(candles):
        return False, 0.0

    start = max(0, touch_idx - LOCAL_STRUCT_LOOKBACK)
    if touch_idx <= start:
        return False, 0.0

    swing_high = max(c.high for c in candles[start:touch_idx])
    confirmed = candles[check_idx].close > swing_high
    return confirmed, swing_high


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class FVGReactionLongLocalStructV1Scanner:
    """Stateful scanner: detects bullish FVGs, waits for touch + LOCAL_STRUCT.

    State is tracked by timestamps.  On each scan() cycle, we resolve
    timestamps to current candle indices to handle the rolling 200-candle
    window correctly.
    """

    name = SCANNER_NAME
    version = SCANNER_VERSION

    def __init__(self) -> None:
        self._setups: dict[tuple[str, str, int], FVGSetup] = {}
        self._emitted: set[str] = set()
        # --- Observability counters (cumulative) ---
        self._counters: dict[str, int] = {
            "detected": 0, "touched": 0, "confirmed": 0,
            "expired": 0, "invalidated": 0, "emitted": 0,
        }
        self._tf_counters: dict[str, dict[str, int]] = {
            "5m": {k: 0 for k in self._counters},
            "15m": {k: 0 for k in self._counters},
        }

    def _bump(self, tf: str, counter: str) -> None:
        """Increment a lifecycle counter (total + per-timeframe)."""
        self._counters[counter] = self._counters.get(counter, 0) + 1
        if tf in self._tf_counters:
            self._tf_counters[tf][counter] = self._tf_counters[tf].get(counter, 0) + 1

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        candidates: list[SetupCandidate] = []

        for tf_label, candles in [
            ("5m", ctx.candles_5m),
            ("15m", ctx.candles_15m),
        ]:
            if len(candles) < 3:
                continue

            interval_ms = _INTERVAL_MS.get(tf_label, 300_000)
            last_candle = candles[-1]

            # 1) Update existing setups using current candle window
            setup_candidates = self._update_setups(
                ctx.symbol, tf_label, candles, ctx, interval_ms,
            )
            candidates.extend(setup_candidates)

            # 2) Detect new FVGs on the latest candle
            self._detect_and_track(ctx.symbol, tf_label, candles, ctx)

            # 3) Re-process if new FVGs or WAITING_TOUCH setups exist
            if any(
                s.state in (FVGState.WAITING_TOUCH, FVGState.WAITING_LOCAL_STRUCT)
                for s in self._setups.values()
                if s.symbol == ctx.symbol and s.timeframe == tf_label
            ):
                more = self._update_setups(
                    ctx.symbol, tf_label, candles, ctx, interval_ms,
                )
                candidates.extend(more)

        return candidates

    def _detect_and_track(
        self,
        symbol: str,
        timeframe: str,
        candles: list,
        ctx: MarketContext,
    ) -> None:
        """Detect new FVG on the latest candle and add to tracking."""
        last_idx = len(candles) - 1
        fvg = detect_bullish_fvg(candles, last_idx)
        if fvg is None:
            return

        key = (symbol, timeframe, fvg["fvg_created_at"])
        if key in self._setups:
            return

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
            atr=fvg["atr"],
            market_regime=ctx.market_regime,
        )
        self._setups[key] = setup
        self._bump(timeframe, "detected")
        logger.debug(
            "scanner=%s symbol=%s tf=%s event=FVG_DETECTED fvg_created_at=%d "
            "fvg_low=%.4f fvg_high=%.4f fvg_atr=%.4f c2_body_ratio=%.2f",
            SCANNER_NAME, symbol, timeframe, setup.fvg_created_at,
            setup.fvg_low, setup.fvg_high, setup.fvg_atr, setup.c2_body_ratio,
        )

    def _update_setups(
        self,
        symbol: str,
        timeframe: str,
        candles: list,
        ctx: MarketContext,
        interval_ms: int,
    ) -> list[SetupCandidate]:
        """Update all active setups using timestamp-based state resolution."""
        candidates: list[SetupCandidate] = []
        last_candle = candles[-1]
        last_ts = last_candle.timestamp

        for key, setup in self._setups.items():
            if setup.symbol != symbol or setup.timeframe != timeframe:
                continue
            if setup.state in (FVGState.EXPIRED, FVGState.INVALIDATED, FVGState.SIGNAL_EMITTED):
                continue

            # --- bars_since_creation via timestamps ---
            bars_since = _bars_between(setup.fvg_created_at, last_ts, interval_ms)

            # --- WAITING_TOUCH ---
            if setup.state == FVGState.WAITING_TOUCH:
                if bars_since > MAX_BARS_TO_TOUCH:
                    setup.state = FVGState.EXPIRED
                    self._bump(timeframe, "expired")
                    logger.debug(
                        "scanner=%s symbol=%s tf=%s event=FVG_EXPIRED "
                        "fvg_created_at=%d bars_since=%d",
                        SCANNER_NAME, symbol, timeframe,
                        setup.fvg_created_at, bars_since,
                    )
                    continue

                # Scan ALL candles after FVG creation for touch
                if self._check_touch(setup, candles, interval_ms, bars_since):
                    setup.state = FVGState.WAITING_LOCAL_STRUCT
                    self._bump(timeframe, "touched")
                    logger.debug(
                        "scanner=%s symbol=%s tf=%s event=FVG_TOUCHED "
                        "fvg_created_at=%d touch_at=%d bars_to_touch=%d",
                        SCANNER_NAME, symbol, timeframe,
                        setup.fvg_created_at, setup.first_touch_at or 0,
                        setup.bars_to_touch or 0,
                    )

            # --- WAITING_LOCAL_STRUCT ---
            elif setup.state == FVGState.WAITING_LOCAL_STRUCT:
                if bars_since > MAX_BARS_TO_TOUCH:
                    setup.state = FVGState.EXPIRED
                    self._bump(timeframe, "expired")
                    logger.debug(
                        "scanner=%s symbol=%s tf=%s event=FVG_EXPIRED "
                        "fvg_created_at=%d bars_since=%d",
                        SCANNER_NAME, symbol, timeframe,
                        setup.fvg_created_at, bars_since,
                    )
                    continue

                candidate = self._check_local_struct_and_emit(
                    setup, candles, ctx, interval_ms, bars_since, timeframe,
                )
                if candidate is not None:
                    candidates.append(candidate)

        return candidates

    def _check_touch(
        self,
        setup: FVGSetup,
        candles: list,
        interval_ms: int,
        bars_since: int,
    ) -> bool:
        """Check all candles after FVG creation for first touch.

        Returns True if touch was found (setup state updated).
        """
        # Find the candle index of FVG creation in the current window
        fvg_idx = _find_idx_by_ts(candles, setup.fvg_created_at)
        if fvg_idx is None:
            # FVG created candle not in current window — too old
            if bars_since > MAX_BARS_TO_TOUCH:
                setup.state = FVGState.EXPIRED
            return False

        # Scan candles AFTER FVG creation
        for idx in range(fvg_idx + 1, len(candles)):
            candle = candles[idx]
            if candle.low <= setup.fvg_high:
                bars_to_touch = _bars_between(setup.fvg_created_at, candle.timestamp, interval_ms)
                setup.first_touch_at = candle.timestamp
                setup.bars_to_touch = bars_to_touch
                setup.touch_price = min(candle.low, setup.fvg_high)
                return True

        return False

    def _check_local_struct_and_emit(
        self,
        setup: FVGSetup,
        candles: list,
        ctx: MarketContext,
        interval_ms: int,
        bars_since: int,
        timeframe: str,
    ) -> SetupCandidate | None:
        """Check LOCAL_STRUCT confirmation and emit signal if confirmed.

        Looks at ALL candles after first_touch_at for the confirmation pattern.
        """
        if setup.first_touch_at is None:
            return None

        touch_idx = _find_idx_by_ts(candles, setup.first_touch_at)
        if touch_idx is None:
            # Touch candle not in current window — check if we can still confirm
            # using the latest available candles
            return None

        # Check LOCAL_STRUCT on candles AFTER touch
        for check_idx in range(touch_idx + 1, len(candles)):
            check_candle = candles[check_idx]

            # Compute swing_high from the 20 candles before touch
            swing_start = max(0, touch_idx - LOCAL_STRUCT_LOOKBACK)
            if touch_idx <= swing_start:
                continue
            swing_high = max(c.high for c in candles[swing_start:touch_idx])

            if check_candle.close > swing_high:
                # CONFIRMED!
                setup.state = FVGState.CONFIRMED
                setup.confirmation_at = check_candle.timestamp
                self._bump(timeframe, "confirmed")
                logger.debug(
                    "scanner=%s symbol=%s tf=%s event=LOCAL_STRUCT_CONFIRMED "
                    "fvg_created_at=%d confirmation_at=%d swing_high=%.4f",
                    SCANNER_NAME, setup.symbol, timeframe,
                    setup.fvg_created_at, check_candle.timestamp, swing_high,
                )

                entry_price = check_candle.close
                sl_price = setup.c2_low - setup.atr * SL_BUFFER_ATR
                risk = entry_price - sl_price

                if risk <= 0 or entry_price <= sl_price:
                    setup.state = FVGState.INVALIDATED
                    self._bump(timeframe, "invalidated")
                    logger.debug(
                        "scanner=%s symbol=%s tf=%s event=INVALIDATED "
                        "fvg_created_at=%d reason=bad_risk_geometry",
                        SCANNER_NAME, setup.symbol, timeframe, setup.fvg_created_at,
                    )
                    return None

                tp_price = entry_price + TARGET_R * risk
                setup.entry_price = entry_price
                setup.sl_price = sl_price
                setup.tp_price = tp_price
                setup.risk = risk

                candidate = self._make_candidate(setup, ctx)
                if candidate is not None:
                    setup.state = FVGState.SIGNAL_EMITTED
                    self._bump(timeframe, "emitted")
                    logger.debug(
                        "scanner=%s symbol=%s tf=%s event=SIGNAL_EMITTED "
                        "fvg_created_at=%d entry=%.4f sl=%.4f tp=%.4f",
                        SCANNER_NAME, setup.symbol, timeframe,
                        setup.fvg_created_at, setup.entry_price or 0,
                        setup.sl_price or 0, setup.tp_price or 0,
                    )
                return candidate

        return None

    def _make_candidate(self, setup: FVGSetup, ctx: MarketContext) -> SetupCandidate | None:
        if setup.entry_price is None or setup.sl_price is None or setup.tp_price is None:
            return None

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
            entry_zone_low=setup.entry_price,
            entry_zone_high=setup.entry_price,
            invalidation_price=setup.sl_price,
            target_1=setup.tp_price,
            target_2=None,
            score=80.0,
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
        to_remove = [
            key for key, s in self._setups.items()
            if s.state in (FVGState.EXPIRED, FVGState.INVALIDATED, FVGState.SIGNAL_EMITTED)
        ]
        for key in to_remove:
            del self._setups[key]
        return len(to_remove)

    @property
    def active_setups(self) -> int:
        return sum(
            1 for s in self._setups.values()
            if s.state not in (FVGState.EXPIRED, FVGState.INVALIDATED, FVGState.SIGNAL_EMITTED)
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def get_observability_snapshot(self) -> dict[str, dict[str, int]]:
        """Return lifecycle counters without modifying state.

        Returns dict with keys: 'total', '5m', '15m', 'active'.
        Each contains: detected, touched, confirmed, expired, invalidated,
        emitted (cumulative) plus waiting_touch, waiting_local_struct (current).
        """
        # Current state gauges
        wt = sum(1 for s in self._setups.values()
                 if s.state == FVGState.WAITING_TOUCH)
        wl = sum(1 for s in self._setups.values()
                 if s.state == FVGState.WAITING_LOCAL_STRUCT)

        snapshot: dict[str, dict[str, int]] = {}

        # Total cumulative + active
        snapshot["total"] = {
            **self._counters,
            "waiting_touch_current": wt,
            "waiting_local_struct_current": wl,
            "active_total": wt + wl,
        }

        # Per-timeframe
        for tf in ("5m", "15m"):
            tf_wt = sum(1 for s in self._setups.values()
                        if s.timeframe == tf and s.state == FVGState.WAITING_TOUCH)
            tf_wl = sum(1 for s in self._setups.values()
                        if s.timeframe == tf and s.state == FVGState.WAITING_LOCAL_STRUCT)
            snapshot[tf] = {
                **self._tf_counters.get(tf, {}),
                "waiting_touch_current": tf_wt,
                "waiting_local_struct_current": tf_wl,
                "active_total": tf_wt + tf_wl,
            }

        return snapshot

    def log_lifecycle_summary(self) -> None:
        """Emit one INFO log line with aggregate lifecycle counters.

        Call once per scanner cycle (after all symbols processed),
        not per-symbol.
        """
        snap = self.get_observability_snapshot()
        total = snap.get("total", {})

        parts = []
        for tf in ("5m", "15m"):
            tf_data = snap.get(tf, {})
            parts.append(
                f"{tf} detected={tf_data.get('detected', 0)} "
                f"waiting_touch={tf_data.get('waiting_touch_current', 0)} "
                f"touched={tf_data.get('touched', 0)} "
                f"waiting_struct={tf_data.get('waiting_local_struct_current', 0)} "
                f"confirmed={tf_data.get('confirmed', 0)} "
                f"expired={tf_data.get('expired', 0)} "
                f"invalidated={tf_data.get('invalidated', 0)} "
                f"emitted={tf_data.get('emitted', 0)}"
            )

        logger.info(
            "FVG lifecycle: %s | total detected=%d active=%d touched=%d "
            "confirmed=%d expired=%d invalidated=%d emitted=%d",
            " | ".join(parts),
            total.get("detected", 0),
            total.get("active_total", 0),
            total.get("touched", 0),
            total.get("confirmed", 0),
            total.get("expired", 0),
            total.get("invalidated", 0),
            total.get("emitted", 0),
        )
