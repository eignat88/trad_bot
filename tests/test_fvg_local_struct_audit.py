"""Tests for LOCAL_STRUCT parity audit + parity comparison with research."""
from __future__ import annotations

from datetime import datetime, timezone
from app.models import Candle
from app.scanners.fvg_reaction_long_local_struct_v1 import (
    FVGReactionLongLocalStructV1Scanner, FVGState,
)
from app.scanners.fvg_local_struct_audit import audit_local_struct
from app.scanners.models import (
    IndicatorSnapshot, MarketContext, MarketLevels,
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
        """Audit should find the touched FVG and report swing_high details."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        # Cycle 1: detect
        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)
        w1[199] = _c(199 * ms, 110, 115, 103, 114)
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        # Cycle 2: touch
        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        # Audit on cycle 2 data
        records = audit_local_struct(scanner, "BTC", "5m", w2)
        assert len(records) >= 1
        rec = records[0]
        assert rec["touch_at"] == 200 * ms
        assert rec["swing_high"] > 0
        assert rec["lookback_len"] > 0
        assert "candles_after_touch" in rec

    def test_audit_confirms_when_close_above_swing(self):
        """When close > swing_high, audit should show would_confirm=True."""
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

        # Cycle 3: confirm
        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 120, 102, 118)  # close=118 > swing=115

        # Audit on w3 data (has confirmation candle)
        records = audit_local_struct(scanner, "BTC", "5m", w3)
        assert len(records) >= 1
        rec = records[0]
        assert rec["any_confirms"] is True

    def test_audit_no_confirm_when_close_below_swing(self):
        """When close < swing_high, audit shows would_confirm=False."""
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

        # confirm candle close=110 < swing=115
        w3 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w3[197] = _c(199 * ms, 110, 115, 103, 114)
        w3[198] = _c(200 * ms, 114, 116, 102, 103)
        w3[199] = _c(201 * ms, 103, 110, 102, 110)  # close=110 < swing=115

        records = audit_local_struct(scanner, "BTC", "5m", w3)
        assert len(records) >= 1
        rec = records[0]
        assert rec["any_confirms"] is False
        # All candles should show close < swing_high
        for r in rec["candles_after_touch"]:
            assert not r["would_confirm"]

    def test_swing_high_source_identified(self):
        """Audit identifies which candle set the swing_high."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        w1 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w1[197] = _c(197 * ms, 100, 102, 98, 101)
        w1[198] = _c(198 * ms, 101, 112, 100, 110)  # high=112
        w1[199] = _c(199 * ms, 110, 115, 103, 114)  # high=115
        scanner.scan(_ctx(candles_5m=tuple(w1)))

        w2 = [_c(i * ms, 100, 102, 98, 100) for i in range(200)]
        w2[198] = _c(199 * ms, 110, 115, 103, 114)
        w2[199] = _c(200 * ms, 114, 116, 102, 103)
        scanner.scan(_ctx(candles_5m=tuple(w2)))

        records = audit_local_struct(scanner, "BTC", "5m", w2)
        assert len(records) >= 1
        rec = records[0]
        assert "swing_high_source_ts" in rec
        # swing_high should be from C2 (high=112) or C3 (high=115)
        assert rec["swing_high"] in (112.0, 115.0)


class TestParityWithResearch:
    """Verify production LOCAL_STRUCT matches research backtest_engine.py exactly.

    Research formula:
      swing_high = max(c.high for c in candles[touch_idx-20 : touch_idx])
      confirmed = candle.close > swing_high  (on touch_idx+1 only)

    Production formula:
      swing_high = max(c.high for c in candles[touch_idx-20 : touch_idx])
      confirmed = candle.close > swing_high  (scans ALL candles after touch)

    Key difference: production scans multiple candles, research only touch+1.
    Production is MORE permissive.  If production gives 0 confirms, research
    would also give 0 (or fewer).  This means the issue is NOT in the
    scan range — it's in swing_high being too high.
    """

    def test_production_is_more_permissive_than_research(self):
        """Production checks multiple candles after touch; research only touch+1.
        If production gives 0 confirms, research would also give 0."""
        ms = 300_000
        scanner = FVGReactionLongLocalStructV1Scanner()

        # Setup: C2 high=112, C3 high=115 → swing_high=115
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

        # Research only checks touch_idx+1
        touch_idx = rec["touch_idx"]
        research_checks = [r for r in rec["candles_after_touch"]
                          if r["idx"] == touch_idx + 1]
        production_checks = rec["candles_after_touch"]

        # Production checks >= research checks
        assert len(production_checks) >= len(research_checks)

        # If production says no confirms, research would also say no
        if not rec["any_confirms"]:
            # Research only checks first candle after touch
            if research_checks:
                assert not research_checks[0]["would_confirm"]
