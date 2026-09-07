from datetime import datetime, timezone

from app.models import Candle
from app.scanners.expectancy_filter import (
    DEFAULT_MIN_SAMPLES,
    ExpectancyFilter,
    ExpectancyRecord,
    filter_candidates,
)
from app.scanners.models import (
    IndicatorSnapshot,
    MarketContext,
    MarketLevels,
    SetupCandidate,
)
from app.scanners.orchestrator import ScannerOrchestrator


def candidate(**overrides):
    base = SetupCandidate(
        scanner_name="TREND_PULLBACK",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        signal_candle_open_time=1_777_294_700_000,
        reference_price=79_203.10,
        entry_zone_low=79_000,
        entry_zone_high=79_300,
        invalidation_price=78_500,
        target_1=80_000,
        score=30,
    )
    return SetupCandidate(**{**base.__dict__, **overrides})


def test_filter_allows_no_history_combinations():
    f = ExpectancyFilter()
    # LIVE mode: no history → blocked
    assert f.is_profitable("UNKNOWN_SCANNER", "LONG", trading_mode="live") is False
    assert f.reason_for("UNKNOWN_SCANNER", "LONG") == "INSUFFICIENT_DATA(0)"


def test_filter_allows_insufficient_samples():
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=5, avg_r_after_costs=-0.5, win_rate=0.0,
        ),
    })
    # LIVE mode: insufficient samples → blocked
    assert f.is_profitable("TREND_PULLBACK", "LONG", trading_mode="live") is False
    assert f.reason_for("TREND_PULLBACK", "LONG").startswith("INSUFFICIENT_DATA")


def test_filter_rejects_negative_expectancy():
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=30, avg_r_after_costs=-0.2, win_rate=0.1,
            profit_factor=1.3, net_pnl=1.0,
        ),
    })
    assert f.is_profitable("TREND_PULLBACK", "LONG", min_avg_r=0.0) is False
    assert f.is_profitable("TREND_PULLBACK", "LONG", min_avg_r=-0.5) is True


def test_filter_allows_positive_expectancy():
    f = ExpectancyFilter(records={
        ("SUPPORT_RESISTANCE_REACTION", "LONG"): ExpectancyRecord(
            scanner_name="SUPPORT_RESISTANCE_REACTION", direction="LONG",
            samples=30, avg_r_after_costs=0.3, win_rate=0.4,
            profit_factor=1.2, net_pnl=1.0,
        ),
    })
    assert f.is_profitable("SUPPORT_RESISTANCE_REACTION", "LONG") is True


def test_filter_candidates_returns_accepted_and_rejected():
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=20, avg_r_after_costs=-0.2, win_rate=0.1,
        ),
        ("BREAKOUT_RETEST", "SHORT"): ExpectancyRecord(
            scanner_name="BREAKOUT_RETEST", direction="SHORT",
            samples=20, avg_r_after_costs=-0.1, win_rate=0.1,
        ),
    })
    candidates = [
        candidate(scanner_name="TREND_PULLBACK", direction="LONG"),
        candidate(scanner_name="BREAKOUT_RETEST", direction="SHORT"),
        candidate(scanner_name="VOLATILITY_COMPRESSION", direction="SHORT"),  # no history
    ]
    accepted, rejected = filter_candidates(candidates, f, trading_mode="live")
    assert rejected == 3
    assert accepted == []


def test_filter_rejects_manual_block_without_history():
    candidates = [candidate(scanner_name="BREAKOUT_RETEST", direction="SHORT")]

    accepted, rejected = filter_candidates(
        candidates,
        ExpectancyFilter(),
        blocked_combinations=frozenset({("BREAKOUT_RETEST", "SHORT")}),
    )

    assert accepted == []
    assert rejected == 1


def test_orchestrator_applies_expectancy_filter():
    """Expectancy filter is applied in scan_all_with_stats."""

    def fake_candidate(**overrides):
        base = candidate(
            entry_zone_low=95, entry_zone_high=100,
            invalidation_price=90, target_1=110,
            reference_price=100,
            score=50, reasons=("TEST",),
            features={"trend_alignment": True, "htf_context": True},
        )
        return SetupCandidate(**{**base.__dict__, **overrides})

    class FakeScanner:
        def scan(self, ctx):
            return [fake_candidate()]

    candle = Candle(1_777_294_700_000, 100, 102, 99, 101, 10)
    ctx = MarketContext(
        symbol="BTCUSDT", candles_5m=(candle,), candles_15m=(),
        candles_1h=(), candles_4h=(), indicators=IndicatorSnapshot(),
        market_regime="TREND_UP", levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )
    orchestrator = ScannerOrchestrator(enabled_scanners=[])
    orchestrator.scanners = {"TREND_PULLBACK": FakeScanner()}

    # With negative expectancy: should be filtered out
    negative_filter = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=20, avg_r_after_costs=-0.2, win_rate=0.1,
        ),
    })
    candidates, stats = orchestrator.scan_all_with_stats(
        ctx, expectancy_filter=negative_filter,
    )
    assert candidates == []
    assert stats["TREND_PULLBACK"]["setups_saved"] == 0

    # Without filter: should pass (fresh orchestrator + different candle)
    candle2 = Candle(1_777_295_000_000, 100, 102, 99, 101, 10)
    ctx2 = MarketContext(
        symbol="BTCUSDT", candles_5m=(candle2,), candles_15m=(),
        candles_1h=(), candles_4h=(), indicators=IndicatorSnapshot(),
        market_regime="TREND_UP", levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )
    orchestrator2 = ScannerOrchestrator(enabled_scanners=[])
    orchestrator2.scanners = {"TREND_PULLBACK": FakeScanner()}
    candidates2, stats2 = orchestrator2.scan_all_with_stats(ctx2)
    assert len(candidates2) == 1
    assert stats2["TREND_PULLBACK"]["setups_saved"] == 1


def test_expectancy_requires_30_samples_pf_and_avg_r():
    """Gate: entries >= 30, avg_r_after_costs > 0, PF >= 1.2.

    net_pnl is intentionally excluded from the gate — it is an absolute
    position-size-dependent metric and does not reflect normalized expectancy.
    """
    def allowed(samples=30, avg_r=0.1, pf=1.2, net=1.0):
        f = ExpectancyFilter({
            ("TEST", "LONG"): ExpectancyRecord(
                "TEST", "LONG", samples, avg_r, 0.5, pf, net,
            )
        })
        return f.is_profitable("TEST", "LONG", trading_mode="live")

    assert not allowed(samples=10)
    assert not allowed(samples=29)
    assert allowed(samples=30)
    assert not allowed(pf=1.19)
    assert not allowed(avg_r=0.0)
    # net_pnl no longer affects the gate — even zero is allowed
    assert allowed(net=0.0)


def test_net_pnl_does_not_affect_gate():
    """net_pnl is logging-only; negative net_pnl does not block."""
    f = ExpectancyFilter(records={
        ("TEST", "LONG"): ExpectancyRecord(
            scanner_name="TEST", direction="LONG",
            samples=40, avg_r_after_costs=0.15, win_rate=0.4,
            profit_factor=1.3, net_pnl=-100.0,
        ),
    })
    # Despite large negative net_pnl, positive avg_r + PF ≥ 1.2 → ALLOW
    assert f.is_profitable("TEST", "LONG", trading_mode="live") is True


def test_negative_avg_r_with_high_pf_still_blocks():
    """PF alone is not enough — avg_r must be positive."""
    f = ExpectancyFilter(records={
        ("TEST", "SHORT"): ExpectancyRecord(
            scanner_name="TEST", direction="SHORT",
            samples=35, avg_r_after_costs=-0.05, win_rate=0.2,
            profit_factor=2.0, net_pnl=50.0,
        ),
    })
    assert f.is_profitable("TEST", "SHORT", trading_mode="live") is False


# ---------------------------------------------------------------------------
# Paper bootstrap tests
# ---------------------------------------------------------------------------

def test_paper_allows_insufficient_samples_bootstrap():
    """Test 1: PAPER + closed < min_samples → allowed (bootstrap)."""
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=10, avg_r_after_costs=0.0, win_rate=0.0,
        ),
    })
    assert f.is_profitable("TREND_PULLBACK", "LONG", trading_mode="paper") is True

    # filter_candidates should also accept the candidate
    cands = [candidate()]
    accepted, rejected = filter_candidates(
        cands, f, min_samples=30, trading_mode="paper",
    )
    assert len(accepted) == 1
    assert rejected == 0


def test_live_blocks_insufficient_samples():
    """Test 2: LIVE + closed < min_samples → blocked."""
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=10, avg_r_after_costs=0.0, win_rate=0.0,
        ),
    })
    assert f.is_profitable("TREND_PULLBACK", "LONG", trading_mode="live") is False

    cands = [candidate()]
    accepted, rejected = filter_candidates(
        cands, f, min_samples=30, trading_mode="live",
    )
    assert len(accepted) == 0
    assert rejected == 1


def test_paper_manual_block_overrides_bootstrap():
    """Test 3: PAPER + manual block → blocked regardless of samples."""
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=5, avg_r_after_costs=0.5, win_rate=0.5,
        ),
    })
    cands = [candidate()]
    accepted, rejected = filter_candidates(
        cands, f,
        blocked_combinations=frozenset({("TREND_PULLBACK", "LONG")}),
        trading_mode="paper",
    )
    assert len(accepted) == 0
    assert rejected == 1


def test_paper_blocks_bad_expectancy_after_min_samples():
    """Test 4: PAPER + samples >= min + bad expectancy → blocked."""
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=30, avg_r_after_costs=-0.2, win_rate=0.1,
            profit_factor=1.3, net_pnl=1.0,
        ),
    })
    # avg_r is below default min_avg_r (0.0), so should be rejected
    assert f.is_profitable("TREND_PULLBACK", "LONG", trading_mode="paper") is False

    cands = [candidate()]
    accepted, rejected = filter_candidates(cands, f, trading_mode="paper")
    assert len(accepted) == 0
    assert rejected == 1


def test_paper_allows_good_expectancy_after_min_samples():
    """Test 5: PAPER + samples >= min + good expectancy → allowed."""
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=35, avg_r_after_costs=0.3, win_rate=0.4,
            profit_factor=1.5, net_pnl=5.0,
        ),
    })
    assert f.is_profitable("TREND_PULLBACK", "LONG", trading_mode="paper") is True

    cands = [candidate()]
    accepted, rejected = filter_candidates(cands, f, trading_mode="paper")
    assert len(accepted) == 1
    assert rejected == 0


def test_live_safety_gate_not_weakened_by_paper_bootstrap():
    """Test 6: LIVE mode never benefits from paper bootstrap bypass."""
    # Case A: insufficient samples → blocked in LIVE
    f_empty = ExpectancyFilter()
    assert f_empty.is_profitable("ANY", "LONG", trading_mode="live") is False

    # Case B: bad expectancy → blocked in LIVE
    f_bad = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=50, avg_r_after_costs=-1.0, win_rate=0.1,
            profit_factor=0.5, net_pnl=-50.0,
        ),
    })
    assert f_bad.is_profitable("TREND_PULLBACK", "LONG", trading_mode="live") is False

    # Case C: filter_candidates blocks in LIVE
    cands = [candidate()]
    accepted, rejected = filter_candidates(cands, f_bad, trading_mode="live")
    assert len(accepted) == 0
    assert rejected == 1


# ---------------------------------------------------------------------------
# Momentum Exhaustion SHORT regression test
# ---------------------------------------------------------------------------

def test_momentum_exhaustion_short_with_scanner_expectancy_source():
    """Regression: MOMENTUM_EXHAUSTION SHORT should use signal_outcome data.

    Before the fix, the filter read dds.paper_trade_stats which included
    NO_ENTRY / EXPIRED artifacts, yielding avg_r=-0.2895, PF=0.516.

    After the fix, the filter reads dds.scanner_expectancy which uses only
    entry_touched signals: entries=17, avg_r_after_costs=+0.0594,
    PF=1.2336.  With 17 entries (< 30 min_samples) the filter should
    ALLOW in paper mode (bootstrap) and block in live mode (insufficient data).
    """
    f = ExpectancyFilter(records={
        ("MOMENTUM_EXHAUSTION", "SHORT"): ExpectancyRecord(
            scanner_name="MOMENTUM_EXHAUSTION", direction="SHORT",
            samples=17,
            avg_r_after_costs=0.0594,
            win_rate=0.1765,
            profit_factor=1.2336,
        ),
    })
    # Paper mode: insufficient entries → bootstrap ALLOW
    assert f.is_profitable("MOMENTUM_EXHAUSTION", "SHORT", trading_mode="paper") is True
    # Live mode: insufficient entries → BLOCK
    assert f.is_profitable("MOMENTUM_EXHAUSTION", "SHORT", trading_mode="live") is False
    # reason_for should show INSUFFICIENT_DATA
    reason = f.reason_for("MOMENTUM_EXHAUSTION", "SHORT")
    assert "INSUFFICIENT_DATA" in reason
    assert "17" in reason


def test_momentum_exhaustion_short_passes_with_enough_entries():
    """If MOMENTUM_EXHAUSTION SHORT accumulates ≥ 30 entries with the same
    positive expectancy, it should be allowed in live mode too.
    """
    f = ExpectancyFilter(records={
        ("MOMENTUM_EXHAUSTION", "SHORT"): ExpectancyRecord(
            scanner_name="MOMENTUM_EXHAUSTION", direction="SHORT",
            samples=35,
            avg_r_after_costs=0.0594,
            win_rate=0.1765,
            profit_factor=1.2336,
        ),
    })
    assert f.is_profitable("MOMENTUM_EXHAUSTION", "SHORT", trading_mode="live") is True


# ---------------------------------------------------------------------------
# Logging format tests
# ---------------------------------------------------------------------------

def test_reason_for_shows_avg_r_after_costs_not_raw_avg_r():
    """reason_for should use avg_r_after_costs label."""
    f = ExpectancyFilter(records={
        ("TEST", "LONG"): ExpectancyRecord(
            scanner_name="TEST", direction="LONG",
            samples=40, avg_r_after_costs=0.1234, win_rate=0.5,
            profit_factor=1.5, net_pnl=42.0,
        ),
    })
    reason = f.reason_for("TEST", "LONG")
    assert "AVG_R_AFTER_COSTS=0.1234" in reason
    assert "PF=1.5000" in reason
    assert "NET_PNL=42.00" in reason
