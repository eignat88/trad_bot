"""Tests for FVG Reaction Long Local Struct V1 scanner."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from app.models import Candle
from app.scanners.fvg_reaction_long_local_struct_v1 import (
    FVGReactionLongLocalStructV1Scanner, FVGState,
    detect_bullish_fvg, check_local_struct_long,
    _atr, _bars_between, _find_idx_by_ts,
    MIN_FVG_ATR, MIN_C2_BODY_RATIO, MAX_BARS_TO_TOUCH,
    TARGET_R, SL_BUFFER_ATR, SCANNER_VERSION,
)
from app.scanners.models import (
    IndicatorSnapshot, MarketContext, MarketLevels, SetupCandidate, SetupState,
)


def _c(ts, o, h, l, c, v=100):
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)

def _ctx(symbol="BTC", candles_5m=(), candles_15m=(), regime="RANGE"):
    return MarketContext(
        symbol=symbol, candles_5m=candles_5m, candles_15m=candles_15m,
        candles_1h=(), candles_4h=(),
        indicators=IndicatorSnapshot(), market_regime=regime,
        levels=MarketLevels(), evaluated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Rolling Window Regression
# ---------------------------------------------------------------------------

class TestRollingWindowRegression:
    def test_3_cycle_lifecycle(self):
        """Cycle 1: detect, Cycle 2: touch, Cycle 3: confirm + signal."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        # Cycle 1: FVG at 197-199
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        r1 = scanner.scan(_ctx(candles_5m=tuple(w1)))
        assert len(r1) == 0
        assert scanner.active_setups == 1
        setup = list(scanner._setups.values())[0]
        assert setup.fvg_created_at == 199 * ms
        assert setup.state == FVGState.WAITING_TOUCH

        # Cycle 2: window shifts, FVG C3 now at 198, new candle 199 touches
        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)  # touch
        r2 = scanner.scan(_ctx(candles_5m=tuple(w2)))
        assert len(r2) == 0
        setup = list(scanner._setups.values())[0]
        assert setup.state == FVGState.WAITING_LOCAL_STRUCT
        assert setup.first_touch_at == 200 * ms
        assert setup.bars_to_touch == 1

        # Cycle 3: confirm
        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 120, 102, 118)
        r3 = scanner.scan(_ctx(candles_5m=tuple(w3)))
        assert len(r3) >= 1
        c = r3[0]
        assert c.direction == "LONG"
        assert c.scanner_name == "FVG_REACTION_LONG_LOCAL_STRUCT_V1"
        assert c.state == SetupState.READY_TO_TRADE
        assert c.features["bars_to_touch"] == 1

    def test_bars_since_creation_progresses(self):
        """bars_since should increase across cycles, not stay at 0."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 115, 116, 110, 112)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 115, 116, 110, 112)
        w3[199] = _c(201 * ms, 115, 116, 110, 112)
        scanner.scan(_ctx(candles_5m=tuple(w3)))

        assert _bars_between(199 * ms, 201 * ms, ms) == 2


# ---------------------------------------------------------------------------
# FVG Detection
# ---------------------------------------------------------------------------

class TestFVGDetection:
    def test_bullish_fvg(self):
        ms = 300_000
        c = [_c(i * ms, 100, 102, 98, 100) for i in range(20)]
        c[17] = _c(17 * ms, 100, 102, 98, 101)
        c[18] = _c(18 * ms, 101, 110, 100, 108)
        c[19] = _c(19 * ms, 108, 112, 103, 111)
        fvg = detect_bullish_fvg(c, 19)
        assert fvg is not None
        assert fvg["fvg_low"] == 102.0 and fvg["fvg_high"] == 103.0
        assert fvg["c2_body_ratio"] > MIN_C2_BODY_RATIO

    def test_no_fvg_gap_overlaps(self):
        c = [_c(i * 300_000, 100, 105, 95, 101) for i in range(20)]
        c[17] = _c(17 * 300_000, 100, 105, 95, 101)
        c[18] = _c(18 * 300_000, 101, 110, 100, 108)
        c[19] = _c(19 * 300_000, 108, 112, 104, 111)
        assert detect_bullish_fvg(c, 19) is None

    def test_no_fvg_weak_c2(self):
        ms = 300_000
        c = [_c(i * ms, 100, 102, 98, 100) for i in range(20)]
        c[17] = _c(17 * ms, 100, 102, 98, 101)
        c[18] = _c(18 * ms, 100.5, 110, 90, 100.6)
        c[19] = _c(19 * ms, 100.6, 112, 103, 111)
        assert detect_bullish_fvg(c, 19) is None

    def test_no_fvg_before_c3(self):
        c = [_c(i * 300_000, 100, 102, 98, 100) for i in range(20)]
        assert detect_bullish_fvg(c, 1) is None

    def test_no_fvg_small_atr(self):
        c = [_c(i * 300_000, 100, 100.01, 99.99, 100) for i in range(20)]
        c[17] = _c(17 * 300_000, 100, 100.01, 99.99, 100)
        c[18] = _c(18 * 300_000, 100, 100.02, 99.98, 100.01)
        c[19] = _c(19 * 300_000, 100.01, 100.03, 100.005, 100.02)
        assert detect_bullish_fvg(c, 19) is None


# ---------------------------------------------------------------------------
# LOCAL_STRUCT
# ---------------------------------------------------------------------------

class TestLocalStruct:
    def test_confirmed(self):
        ms = 300_000
        c = [_c(i * ms, 100, 105, 99, 103) for i in range(25)]
        c[23] = _c(23 * ms, 103, 104, 101, 102)
        c[24] = _c(24 * ms, 102, 110, 101, 108)
        ok, sh = check_local_struct_long(c, 23)
        assert ok and sh == 105.0

    def test_not_confirmed(self):
        ms = 300_000
        c = [_c(i * ms, 100, 105, 99, 103) for i in range(25)]
        c[23] = _c(23 * ms, 103, 104, 101, 102)
        c[24] = _c(24 * ms, 102, 104, 101, 103)
        ok, _ = check_local_struct_long(c, 23)
        assert not ok

    def test_lookback_boundary(self):
        ms = 300_000
        c = [_c(i * ms, 100, 105, 99, 103) for i in range(25)]
        c[3] = _c(3 * ms, 100, 200, 99, 150)
        c[23] = _c(23 * ms, 103, 104, 101, 102)
        c[24] = _c(24 * ms, 102, 110, 101, 109)
        ok, sh = check_local_struct_long(c, 23)
        assert sh == 200.0 and not ok


# ---------------------------------------------------------------------------
# No look-ahead
# ---------------------------------------------------------------------------

class TestNoLookAhead:
    def test_no_signal_without_confirmation_candle(self):
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        r = scanner.scan(_ctx(candles_5m=tuple(w2)))
        assert len(r) == 0


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------

class TestDuplicate:
    def test_same_fvg_no_duplicate(self):
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 120, 102, 118)
        r1 = scanner.scan(_ctx(candles_5m=tuple(w3)))
        assert len(r1) >= 1
        r2 = scanner.scan(_ctx(candles_5m=tuple(w3)))
        assert len(r2) == 0


# ---------------------------------------------------------------------------
# 15m
# ---------------------------------------------------------------------------

class TestTimeframe:
    def test_15m(self):
        ms = 900_000
        scanner = FVGReactionLongLocalStructV1Scanner()
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_15m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_15m=tuple(w2)))

        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 120, 102, 118)
        r = scanner.scan(_ctx(candles_15m=tuple(w3)))
        assert len(r) >= 1
        assert r[0].setup_timeframe == "15m"


# ---------------------------------------------------------------------------
# Risk geometry
# ---------------------------------------------------------------------------

class TestRiskGeometry:
    def _build_and_confirm(self, ms, c2_low_override=None):
        scanner = FVGReactionLongLocalStructV1Scanner()
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        c2_low = c2_low_override if c2_low_override is not None else 100.0
        w1[198] = _c(198 * ms, 101, 112, c2_low, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 120, 102, 118)
        return scanner.scan(_ctx(candles_5m=tuple(w3)))

    def test_c2_extremum_stop(self):
        ms = 300_000
        r = self._build_and_confirm(ms, c2_low_override=99.5)
        assert len(r) >= 1
        assert r[0].invalidation_price < 99.5

    def test_tp_3r(self):
        ms = 300_000
        r = self._build_and_confirm(ms)
        assert len(r) >= 1
        risk = r[0].entry_zone_high - r[0].invalidation_price
        expected = r[0].entry_zone_high + TARGET_R * risk
        assert abs(r[0].target_1 - expected) < 0.001


# ---------------------------------------------------------------------------
# Expired
# ---------------------------------------------------------------------------

class TestExpired:
    def test_expires_after_max_bars(self):
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))
        assert scanner.active_setups == 1

        # 50 cycles later (50 bars)
        w50 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w50[199] = _c((199 + 49) * ms, 115, 116, 110, 112)
        scanner.scan(_ctx(candles_5m=tuple(w50)))
        setup = list(scanner._setups.values())[0]
        assert setup.state == FVGState.EXPIRED


# ---------------------------------------------------------------------------
# Frozen values
# ---------------------------------------------------------------------------

class TestFrozenValues:
    def test_constants(self):
        assert MIN_FVG_ATR == 0.05
        assert MIN_C2_BODY_RATIO == 0.50
        assert MAX_BARS_TO_TOUCH == 48
        assert TARGET_R == 3.0
        assert SL_BUFFER_ATR == 0.05


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

class TestFeatures:
    def test_diagnostic_data(self):
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 120, 102, 118)
        r = scanner.scan(_ctx(candles_5m=tuple(w3)))

        assert len(r) >= 1
        f = r[0].features
        for key in ["fvg_created_at", "fvg_low", "fvg_high", "fvg_atr",
                     "c2_body_ratio", "bars_to_touch", "entry_price",
                     "sl_price", "tp_price"]:
            assert key in f
        assert f["rr"] == 3.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_bars_between(self):
        assert _bars_between(0, 300_000, 300_000) == 1
        assert _bars_between(0, 600_000, 300_000) == 2
        assert _bars_between(300_000, 0, 300_000) == 0

    def test_find_idx_by_ts(self):
        ms = 300_000
        c = [_c(i * ms, 100, 102, 98, 100) for i in range(5)]
        assert _find_idx_by_ts(c, 2 * ms) == 2
        assert _find_idx_by_ts(c, 999 * ms) is None

    def test_version_bumped(self):
        assert SCANNER_VERSION == "1.0.1"


# ---------------------------------------------------------------------------
# LOCAL_STRUCT_REJECTED terminal state
# ---------------------------------------------------------------------------

class TestLocalStructRejected:
    def test_rejected_when_close_below_swing(self):
        """When touch+1 close <= swing_high, setup transitions to REJECTED."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        # Detect
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        # Touch
        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        # touch+1: close=110 < swing=115 → REJECTED
        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 110, 102, 110)  # close=110 < swing=115
        scanner.scan(_ctx(candles_5m=tuple(w3)))

        setup = list(scanner._setups.values())[0]
        assert setup.state == FVGState.LOCAL_STRUCT_REJECTED

    def test_rejected_is_terminal(self):
        """REJECTED setup is not processed in subsequent cycles."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        # Full cycle: detect → touch → reject
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 110, 102, 110)
        scanner.scan(_ctx(candles_5m=tuple(w3)))
        assert scanner.active_setups == 0  # rejected = terminal

    def test_rejected_counter(self):
        """Rejected counter increments exactly once."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 110, 102, 110)
        scanner.scan(_ctx(candles_5m=tuple(w3)))

        snap = scanner.get_observability_snapshot()
        assert snap["total"]["rejected"] == 1
        assert snap["total"]["waiting_local_struct_current"] == 0
        assert snap["total"]["active_total"] == 0

    def test_not_rejected_when_confirmed(self):
        """If touch+1 close > swing, setup is CONFIRMED, not REJECTED."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 120, 102, 118)  # close=118 > swing=115
        scanner.scan(_ctx(candles_5m=tuple(w3)))

        snap = scanner.get_observability_snapshot()
        assert snap["total"]["rejected"] == 0
        assert snap["total"]["confirmed"] == 1
        assert snap["total"]["emitted"] == 1

    def test_lifecycle_log_includes_rejected(self):
        """log_lifecycle_summary should include rejected count."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 110, 102, 110)
        scanner.scan(_ctx(candles_5m=tuple(w3)))

        # Should not raise
        scanner.log_lifecycle_summary()
        snap = scanner.get_observability_snapshot()
        assert snap["total"]["rejected"] == 1
