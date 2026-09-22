"""Tests for FVG Reaction Long Local Struct V1 scanner."""
from __future__ import annotations

import pytest
from app.models import Candle
from app.scanners.fvg_reaction_long_local_struct_v1 import (
    FVGReactionLongLocalStructV1Scanner, FVGState,
    detect_bullish_fvg, check_local_struct_long,
    MIN_FVG_ATR, MIN_C2_BODY_RATIO, MAX_BARS_TO_TOUCH,
    TARGET_R, SL_BUFFER_ATR,
)
from app.scanners.models import (
    IndicatorSnapshot, MarketContext, MarketLevels,
    SetupCandidate, SetupState,
)
from app.scanners.models import ScannerDirection
from datetime import datetime, timezone


def _c(ts, o, h, l, c, v=100):
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)

def _ctx(symbol="BTC", candles_5m=(), candles_15m=(), regime="RANGE"):
    return MarketContext(
        symbol=symbol, candles_5m=candles_5m, candles_15m=candles_15m,
        candles_1h=(), candles_4h=(),
        indicators=IndicatorSnapshot(), market_regime=regime,
        levels=MarketLevels(), evaluated_at=datetime.now(timezone.utc),
    )


def _fvg_candles():
    """FVG at 20-22, touch at 23, confirm at 24.

    C2 high=112, C3 high=115 → swing_high (within 20 lookback of touch at 23) = 115.
    Confirmation candle must close > 115.
    """
    ms = 300_000
    c = [_c(i * ms, 100, 102, 98, 100) for i in range(50)]
    c[20] = _c(20*ms, 100, 102, 98, 101)    # C1: high=102
    c[21] = _c(21*ms, 101, 112, 100, 110)   # C2: high=112
    c[22] = _c(22*ms, 110, 115, 103, 114)   # C3: high=115, FVG [102, 103]
    c[23] = _c(23*ms, 114, 116, 102, 103)   # touch (low=102 <= fvg_high=103)
    c[24] = _c(24*ms, 103, 120, 102, 118)   # confirm: close=118 > swing_high=115
    return c


def _run(candles):
    """3-step incremental: detect -> touch -> confirm."""
    s = FVGReactionLongLocalStructV1Scanner()
    s.scan(_ctx(candles_5m=tuple(candles[:23])))  # detect
    s.scan(_ctx(candles_5m=tuple(candles[:24])))  # touch
    return s.scan(_ctx(candles_5m=tuple(candles[:25])))  # confirm


# --- FVG Detection ---

class TestFVGDetection:
    def test_bullish_fvg(self):
        ms = 300_000
        c = [_c(i*ms, 100, 102, 98, 100) for i in range(20)]
        c[17] = _c(17*ms, 100, 102, 98, 101)
        c[18] = _c(18*ms, 101, 110, 100, 108)
        c[19] = _c(19*ms, 108, 112, 103, 111)
        fvg = detect_bullish_fvg(c, 19)
        assert fvg is not None
        assert fvg["fvg_low"] == 102.0
        assert fvg["fvg_high"] == 103.0
        assert fvg["c2_body_ratio"] > MIN_C2_BODY_RATIO

    def test_no_fvg_gap_overlaps(self):
        c = [_c(i*300_000, 100, 105, 95, 101) for i in range(20)]
        c[17] = _c(17*300_000, 100, 105, 95, 101)
        c[18] = _c(18*300_000, 101, 110, 100, 108)
        c[19] = _c(19*300_000, 108, 112, 104, 111)  # low=104 < C1.high=105
        assert detect_bullish_fvg(c, 19) is None

    def test_no_fvg_weak_c2(self):
        ms = 300_000
        c = [_c(i*ms, 100, 102, 98, 100) for i in range(20)]
        c[17] = _c(17*ms, 100, 102, 98, 101)
        c[18] = _c(18*ms, 100.5, 110, 90, 100.6)  # tiny body
        c[19] = _c(19*ms, 100.6, 112, 103, 111)
        assert detect_bullish_fvg(c, 19) is None

    def test_no_fvg_before_c3(self):
        c = [_c(i*300_000, 100, 102, 98, 100) for i in range(20)]
        assert detect_bullish_fvg(c, 1) is None

    def test_no_fvg_small_atr(self):
        c = [_c(i*300_000, 100, 100.01, 99.99, 100) for i in range(20)]
        c[17] = _c(17*300_000, 100, 100.01, 99.99, 100)
        c[18] = _c(18*300_000, 100, 100.02, 99.98, 100.01)
        c[19] = _c(19*300_000, 100.01, 100.03, 100.005, 100.02)
        assert detect_bullish_fvg(c, 19) is None


# --- LOCAL_STRUCT ---

class TestLocalStruct:
    def test_confirmed(self):
        ms = 300_000
        c = [_c(i*ms, 100, 105, 99, 103) for i in range(25)]
        c[23] = _c(23*ms, 103, 104, 101, 102)
        c[24] = _c(24*ms, 102, 110, 101, 108)
        ok, sh = check_local_struct_long(c, 23)
        assert ok and sh == 105.0

    def test_not_confirmed(self):
        ms = 300_000
        c = [_c(i*ms, 100, 105, 99, 103) for i in range(25)]
        c[23] = _c(23*ms, 103, 104, 101, 102)
        c[24] = _c(24*ms, 102, 104, 101, 103)
        ok, _ = check_local_struct_long(c, 23)
        assert not ok

    def test_lookback_boundary(self):
        ms = 300_000
        c = [_c(i*ms, 100, 105, 99, 103) for i in range(25)]
        c[3] = _c(3*ms, 100, 200, 99, 150)  # far back, within lookback
        c[23] = _c(23*ms, 103, 104, 101, 102)
        c[24] = _c(24*ms, 102, 110, 101, 109)
        ok, sh = check_local_struct_long(c, 23)
        assert sh == 200.0
        assert not ok  # 109 < 200


# --- State Machine ---

class TestStateMachine:
    def test_full_lifecycle(self):
        c = _fvg_candles()
        s = FVGReactionLongLocalStructV1Scanner()
        # detect
        r1 = s.scan(_ctx(candles_5m=tuple(c[:23])))
        assert len(r1) == 0 and s.active_setups >= 1
        # touch
        r2 = s.scan(_ctx(candles_5m=tuple(c[:24])))
        assert len(r2) == 0
        # confirm
        r3 = s.scan(_ctx(candles_5m=tuple(c[:25])))
        assert len(r3) >= 1
        assert r3[0].direction == "LONG"
        assert r3[0].scanner_name == "FVG_REACTION_LONG_LOCAL_STRUCT_V1"
        assert r3[0].state == SetupState.READY_TO_TRADE

    def test_expired(self):
        ms = 300_000
        c = [_c(i*ms, 100, 102, 98, 100) for i in range(100)]
        c[20] = _c(20*ms, 100, 102, 98, 101)
        c[21] = _c(21*ms, 101, 112, 100, 110)
        c[22] = _c(22*ms, 110, 115, 103, 114)
        s = FVGReactionLongLocalStructV1Scanner()
        s.scan(_ctx(candles_5m=tuple(c[:23])))
        assert s.active_setups >= 1
        s.scan(_ctx(candles_5m=tuple(c[:80])))
        active = [x for x in s._setups.values()
                  if x.state not in (FVGState.EXPIRED, FVGState.INVALIDATED)]
        assert len(active) == 0

    def test_no_look_ahead(self):
        ms = 300_000
        c = [_c(i*ms, 100, 102, 98, 100) for i in range(50)]
        c[20] = _c(20*ms, 100, 102, 98, 101)
        c[21] = _c(21*ms, 101, 112, 100, 110)
        c[22] = _c(22*ms, 110, 115, 103, 114)
        c[23] = _c(23*ms, 114, 116, 102, 103)  # touch
        s = FVGReactionLongLocalStructV1Scanner()
        s.scan(_ctx(candles_5m=tuple(c[:23])))
        r = s.scan(_ctx(candles_5m=tuple(c[:24])))
        assert len(r) == 0  # no confirm candle yet

    def test_duplicate(self):
        c = _fvg_candles()
        s = FVGReactionLongLocalStructV1Scanner()
        s.scan(_ctx(candles_5m=tuple(c[:23])))  # detect
        s.scan(_ctx(candles_5m=tuple(c[:24])))  # touch
        r1 = s.scan(_ctx(candles_5m=tuple(c[:25])))  # confirm
        assert len(r1) >= 1
        r2 = s.scan(_ctx(candles_5m=tuple(c[:25])))  # same data
        assert len(r2) == 0

    def test_15m(self):
        ms = 900_000
        c = [_c(i*ms, 100, 102, 98, 100) for i in range(50)]
        c[20] = _c(20*ms, 100, 102, 98, 101)
        c[21] = _c(21*ms, 101, 112, 100, 110)
        c[22] = _c(22*ms, 110, 115, 103, 114)
        c[23] = _c(23*ms, 114, 116, 102, 103)  # touch
        c[24] = _c(24*ms, 103, 120, 102, 118)  # confirm: close > swing
        s = FVGReactionLongLocalStructV1Scanner()
        s.scan(_ctx(candles_15m=tuple(c[:23])))
        s.scan(_ctx(candles_15m=tuple(c[:24])))
        r = s.scan(_ctx(candles_15m=tuple(c[:25])))
        assert len(r) >= 1
        assert r[0].setup_timeframe == "15m"


# --- Risk Geometry ---

class TestRiskGeometry:
    def test_c2_extremum_stop(self):
        ms = 300_000
        c = [_c(i*ms, 100, 102, 98, 100) for i in range(50)]
        c[20] = _c(20*ms, 100, 102, 98, 101)
        c[21] = _c(21*ms, 101, 112, 99.5, 110)  # C2.low = 99.5
        c[22] = _c(22*ms, 110, 115, 103, 114)
        c[23] = _c(23*ms, 114, 116, 102, 103)
        c[24] = _c(24*ms, 103, 120, 102, 118)  # confirm: close > swing
        r = _run(c)
        assert len(r) >= 1
        assert r[0].invalidation_price < 99.5

    def test_tp_3r(self):
        ms = 300_000
        c = [_c(i*ms, 100, 102, 98, 100) for i in range(50)]
        c[20] = _c(20*ms, 100, 102, 98, 101)
        c[21] = _c(21*ms, 101, 112, 100, 110)
        c[22] = _c(22*ms, 110, 115, 103, 114)
        c[23] = _c(23*ms, 114, 116, 102, 103)
        c[24] = _c(24*ms, 103, 120, 102, 118)  # confirm
        r = _run(c)
        assert len(r) >= 1
        risk = r[0].entry_zone_high - r[0].invalidation_price
        expected = r[0].entry_zone_high + TARGET_R * risk
        assert abs(r[0].target_1 - expected) < 0.001


# --- Frozen Values ---

class TestFrozenValues:
    def test_constants(self):
        assert MIN_FVG_ATR == 0.05
        assert MIN_C2_BODY_RATIO == 0.50
        assert MAX_BARS_TO_TOUCH == 48
        assert TARGET_R == 3.0
        assert SL_BUFFER_ATR == 0.05


# --- Features ---

class TestFeatures:
    def test_diagnostic_data(self):
        c = _fvg_candles()
        r = _run(c)
        assert len(r) >= 1
        f = r[0].features
        for key in ["fvg_created_at", "fvg_low", "fvg_high", "fvg_atr",
                     "c2_body_ratio", "bars_to_touch", "entry_price",
                     "sl_price", "tp_price"]:
            assert key in f
        assert f["rr"] == 3.0
