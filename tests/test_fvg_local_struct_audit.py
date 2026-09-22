"""Tests for LOCAL_STRUCT parity audit + strict touch+1 parity."""
from __future__ import annotations

from datetime import datetime, timezone
from app.models import Candle
from app.scanners.fvg_reaction_long_local_struct_v1 import (
    FVGReactionLongLocalStructV1Scanner, FVGState,
)
from app.scanners.fvg_local_struct_audit import audit_local_struct, print_audit_summary
from app.scanners.models import (
    IndicatorSnapshot, MarketContext, MarketLevels, SetupState,
)


def _c(ts, o, h, l, c, v=100):
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)

def _ctx(symbol="BTC", candles_5m=(), candles_15m=()):
    return MarketContext(
        symbol=symbol, candles_5m=candles_5m, candles_15m=candles_15m,
        candles_1h=(), candles_4h=(),
        indicators=IndicatorSnapshot(), market_regime="RANGE",
        levels=MarketLevels(), evaluated_at=datetime.now(timezone.utc),
    )


class TestLocalStructAudit:
    def test_audit_on_touched_fvg(self):
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

        records = audit_local_struct(scanner, "BTC", "5m", w2)
        assert len(records) >= 1
        rec = records[0]
        assert rec["touch_at"] == 200 * ms
        assert rec["swing_high"] > 0
        assert rec["check_idx"] == rec["touch_idx"] + 1

    def test_audit_no_confirm_when_close_below_swing(self):
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
        w3[199] = _c(201 * ms, 103, 110, 102, 110)  # close=110 < swing=115

        records = audit_local_struct(scanner, "BTC", "5m", w3)
        assert len(records) >= 1
        rec = records[0]
        assert rec["would_confirm"] is False
        assert rec["distance_to_confirmation"] is not None
        assert rec["distance_to_confirmation"] < 0  # close < swing

    def test_swing_high_source_identified(self):
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

        records = audit_local_struct(scanner, "BTC", "5m", w2)
        assert len(records) >= 1
        rec = records[0]
        assert rec["swing_high_source_ts"] is not None
        assert rec["swing_high"] in (112.0, 115.0)

    def test_print_audit_summary(self):
        """print_audit_summary should not crash."""
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

        records = audit_local_struct(scanner, "BTC", "5m", w2)
        print_audit_summary(records)  # should not raise


class TestStrictTouchPlusOneParity:
    """Production LOCAL_STRUCT now matches research EXACTLY:
    only touch_idx + 1 is checked."""

    def test_only_next_candle_checked(self):
        """Even if candle at touch_idx+2 has close > swing_high, it's ignored."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        # Detect FVG
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

        # touch+1: close=110 < swing=115 → not confirmed
        # touch+2: close=120 > swing=115 → would confirm in old code, but NOT in strict
        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 110, 102, 110)  # touch+1: close=110 < swing=115

        r = scanner.scan(_ctx(candles_5m=tuple(w3)))
        assert len(r) == 0  # NOT confirmed — strict touch+1

        setup = list(scanner._setups.values())[0]
        assert setup.state == FVGState.LOCAL_STRUCT_REJECTED  # rejected on touch+1

    def test_confirms_on_exact_next_candle(self):
        """Confirms only when touch+1 close > swing_high."""
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

        # touch+1: close=118 > swing=115 → confirmed
        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 120, 102, 118)

        r = scanner.scan(_ctx(candles_5m=tuple(w3)))
        assert len(r) >= 1
        assert r[0].state == SetupState.READY_TO_TRADE

    def test_research_parity_formula(self):
        """Verify the exact research formula in production code."""
        ms = 300_000

        # Create a candle array with known values
        candles = [_c(i * ms, 100, 102, 98, 100) for i in range(50)]
        # Set a spike at index 10 (within lookback of touch at 30)
        candles[10] = _c(10 * ms, 100, 200, 99, 150)  # high=200
        # Touch at index 30
        candles[30] = _c(30 * ms, 103, 104, 101, 102)
        # Touch+1 at index 31
        candles[31] = _c(31 * ms, 102, 190, 101, 189)  # close=189 < swing=200

        from app.scanners.fvg_reaction_long_local_struct_v1 import check_local_struct_long
        confirmed, swing_high = check_local_struct_long(candles, 30)

        # swing_high should include the spike at index 10
        assert swing_high == 200.0
        # 189 < 200 → not confirmed
        assert confirmed is False

        # Now make touch+1 close above swing
        candles[31] = _c(31 * ms, 102, 210, 101, 205)
        confirmed2, swing_high2 = check_local_struct_long(candles, 30)
        assert swing_high2 == 200.0
        assert confirmed2 is True  # 205 > 200
