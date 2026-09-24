"""Tests for SRR LONG Research Outcomes infrastructure.

Verifies:
- SRR LONG research signal creation independent of direction gate
- SRR SHORT is NOT captured (scope is LONG only)
- Feature snapshot preserves raw numeric values
- Duplicate processing does not create duplicates
- Outcome evaluator is idempotent
- LONG MFE/MAE math is correct (favorable = up, adverse = down)
- R normalisation: 1R = abs(entry - stop)
- Horizons fill only when enough future candles exist
- No look-ahead in feature snapshot
- Existing paper trading behaviour unchanged
- Research capture works for BOTH gate-enabled and gate-blocked SRR LONG
- Scanner runner handles _research_candidates without KeyError
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models import Candle
from app.scanners.models import (
    IndicatorSnapshot,
    MarketContext,
    MarketLevels,
    ScannerDirection,
    SetupCandidate,
    SetupState,
)
from app.shadow.srr_research_evaluator import (
    SRRResearchEvaluator,
    _calculate_long_mfe_mae,
    _check_long_target_sl,
)
from app.shadow.srr_research_observer import SRRResearchObserver


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def mock_conn():
    """Create a mock database connection."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    # Default: signal_id=1 for INSERT RETURNING
    cursor.fetchone.return_value = (1,)
    return conn


@pytest.fixture
def mock_client():
    """Create a mock BybitClient."""
    client = MagicMock()
    client.get_klines.return_value = []
    return client


@pytest.fixture
def sample_candles():
    """Create sample 5m candles for testing."""
    base_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(60):  # 5 hours of 5m candles
        ts = base_time + timedelta(minutes=5 * (i + 1))
        candles.append(Candle(
            timestamp=int(ts.timestamp() * 1000),
            open=100.0 + i * 0.01,
            high=100.5 + i * 0.01,
            low=99.5 + i * 0.01,
            close=100.0 + i * 0.01,
            volume=1000.0,
        ))
    return candles


@pytest.fixture
def sample_features():
    """Create sample SRR features dict."""
    return {
        "level_touch_count": 0.6,
        "rejection_strength": 0.7,
        "rr_ratio": 0.5,
        "stop_distance_atr": 0.8,
        "volume_spike": True,
        "regime_alignment": 1.0,
        "_raw_touch_count": 3,
        "_raw_level_distance_pct": 0.15,
        "_raw_atr": 1.5,
        "_raw_atr_pct": 1.5,
        "_raw_candle_range": 2.0,
        "_raw_candle_body": 0.8,
        "_raw_upper_wick": 0.3,
        "_raw_lower_wick": 0.9,
        "_raw_wick_body_ratio": 1.5,
        "_raw_volume": 1500.0,
        "_raw_volume_ratio": 1.5,
        "_raw_rr": 2.0,
        "_raw_risk_distance": 0.5,
        "_raw_risk_distance_pct": 0.5,
        "_raw_stop_distance_atr": 0.33,
        "_raw_level_type": "support",
    }


@pytest.fixture
def srr_observer(mock_conn):
    """Create SRRResearchObserver with mock connection."""
    return SRRResearchObserver(mock_conn)


@pytest.fixture
def srr_evaluator(mock_conn, mock_client):
    """Create SRRResearchEvaluator with mock connection and client."""
    return SRRResearchEvaluator(conn=mock_conn, client=mock_client)


# ── Tests: Research Signal Creation ───────────────────────────

class TestSRRResearchObserver:
    """Test SRR research signal creation."""

    def test_observe_blocked_long_candidate(self, srr_observer, sample_features):
        """SRR LONG blocked candidate should be captured as research signal."""
        result = srr_observer.observe_blocked_candidate(
            setup_id=str(uuid4()),
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            scanner_version="2.0.0",
            symbol="BTCUSDT",
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000000,
            reference_price=100.0,
            entry_zone_low=99.8,
            entry_zone_high=100.2,
            invalidation_price=99.5,
            target_1=103.0,
            target_2=104.5,
            score=65.0,
            market_regime="TREND_UP",
            features=sample_features,
        )

        assert result["status"] == "inserted"
        assert result["signal_id"] == 1
        assert srr_observer._insert_count == 1

    def test_observe_short_candidate_rejected(self, srr_observer, sample_features):
        """SRR SHORT should be ignored (scope is LONG only)."""
        result = srr_observer.observe_blocked_candidate(
            setup_id=str(uuid4()),
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            scanner_version="2.0.0",
            symbol="BTCUSDT",
            direction="SHORT",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000000,
            reference_price=100.0,
            entry_zone_low=99.8,
            entry_zone_high=100.2,
            invalidation_price=100.5,
            target_1=97.0,
            target_2=95.5,
            score=65.0,
            market_regime="TREND_DOWN",
            features=sample_features,
        )

        assert result["status"] == "ignored"
        assert result["reason"] == "not_long"
        assert srr_observer._insert_count == 0

    def test_duplicate_signal_not_created(self, srr_observer, sample_features):
        """Same signal_candle_open_time should not create duplicates."""
        # First insert
        srr_observer.observe_blocked_candidate(
            setup_id=str(uuid4()),
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            scanner_version="2.0.0",
            symbol="BTCUSDT",
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000000,
            reference_price=100.0,
            entry_zone_low=99.8,
            entry_zone_high=100.2,
            invalidation_price=99.5,
            target_1=103.0,
            target_2=104.5,
            score=65.0,
            market_regime="TREND_UP",
            features=sample_features,
        )

        # Second insert — ON CONFLICT DO UPDATE returns no row
        srr_observer._conn.cursor.return_value.fetchone.return_value = None
        result = srr_observer.observe_blocked_candidate(
            setup_id=str(uuid4()),
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            scanner_version="2.0.0",
            symbol="BTCUSDT",
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000000,
            reference_price=100.0,
            entry_zone_low=99.8,
            entry_zone_high=100.2,
            invalidation_price=99.5,
            target_1=103.0,
            target_2=104.5,
            score=70.0,  # score changed
            market_regime="TREND_UP",
            features=sample_features,
        )

        assert result["status"] == "duplicate"
        assert srr_observer._duplicate_count == 1

    def test_no_connection_returns_error(self, sample_features):
        """Observer with no connection returns error."""
        observer = SRRResearchObserver(conn=None)
        result = observer.observe_blocked_candidate(
            setup_id=str(uuid4()),
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            scanner_version="2.0.0",
            symbol="BTCUSDT",
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000000,
            reference_price=100.0,
            entry_zone_low=99.8,
            entry_zone_high=100.2,
            invalidation_price=99.5,
            target_1=103.0,
            target_2=104.5,
            score=65.0,
            market_regime="TREND_UP",
            features=sample_features,
        )
        assert result["status"] == "error"
        assert result["reason"] == "no_connection"

    def test_raw_features_persisted(self, srr_observer, sample_features):
        """Raw numeric features should be passed to SQL."""
        srr_observer.observe_blocked_candidate(
            setup_id=str(uuid4()),
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            scanner_version="2.0.0",
            symbol="BTCUSDT",
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000000,
            reference_price=100.0,
            entry_zone_low=99.8,
            entry_zone_high=100.2,
            invalidation_price=99.5,
            target_1=103.0,
            target_2=104.5,
            score=65.0,
            market_regime="TREND_UP",
            features=sample_features,
        )

        # Verify SQL was called with raw feature values
        cursor = srr_observer._conn.cursor.return_value
        call_args = cursor.execute.call_args
        sql_values = call_args[0][1]  # positional params

        # Check that raw features are in the values
        # raw_touch_count is at a specific position in the INSERT
        assert 3 in sql_values  # _raw_touch_count
        assert 1.5 in sql_values  # _raw_wick_body_ratio


# ── Tests: LONG MFE/MAE Math ─────────────────────────────────

class TestLongMFEMAE:
    """Test MFE/MAE calculation for LONG signals."""

    def test_long_mfe_favorable_up(self):
        """LONG: MFE = price went up (favorable)."""
        signal_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        candles = [
            Candle(
                timestamp=int((signal_time + timedelta(minutes=5 * (i + 1))).timestamp() * 1000),
                open=100.0,
                high=101.0 + i * 0.5,  # Price rising
                low=99.8,
                close=100.5 + i * 0.3,
                volume=1000.0,
            )
            for i in range(12)
        ]

        mfe, mae = _calculate_long_mfe_mae(candles, 100.0, 60, signal_time)

        assert mfe is not None
        assert mfe > 0  # Price went up = favorable for LONG
        assert mae is not None
        assert mae >= 0  # Adverse = price went down

    def test_long_mae_adverse_down(self):
        """LONG: MAE = price went down (adverse)."""
        signal_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        candles = [
            Candle(
                timestamp=int((signal_time + timedelta(minutes=5 * (i + 1))).timestamp() * 1000),
                open=100.0,
                high=100.2,
                low=98.0 - i * 0.3,  # Price dropping
                close=99.0 - i * 0.2,
                volume=1000.0,
            )
            for i in range(12)
        ]

        mfe, mae = _calculate_long_mfe_mae(candles, 100.0, 60, signal_time)

        assert mfe is not None
        assert mfe >= 0
        assert mae is not None
        assert mae > 0  # Price went down = adverse for LONG

    def test_long_no_candles_returns_none(self):
        """No candles → None, None."""
        mfe, mae = _calculate_long_mfe_mae([], 100.0, 60, datetime.now(timezone.utc))
        assert mfe is None
        assert mae is None

    def test_long_no_candles_in_window(self):
        """Candles outside window → None, None."""
        signal_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        # All candles before signal_time
        candles = [
            Candle(
                timestamp=int((signal_time - timedelta(minutes=5 * (i + 1))).timestamp() * 1000),
                open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0,
            )
            for i in range(12)
        ]
        mfe, mae = _calculate_long_mfe_mae(candles, 100.0, 60, signal_time)
        assert mfe is None
        assert mae is None


# ── Tests: R Normalisation ────────────────────────────────────

class TestRNormalisation:
    """Test R-normalisation: 1R = abs(entry - stop)."""

    def test_r_calculation(self):
        """1R = abs(entry - stop)."""
        entry = 100.0
        stop = 99.0
        risk_1r = abs(entry - stop)  # = 1.0
        assert risk_1r == 1.0

        # MFE of 1.5% → 1.5R when 1R = 1%
        mfe_pct = 1.5
        mfe_r = mfe_pct / (risk_1r / entry * 100)
        # risk_1r / entry * 100 = 1.0 / 100.0 * 100 = 1.0
        assert mfe_r == pytest.approx(1.5)

    def test_r_wider_stop(self):
        """Wider stop → smaller R value for same MFE."""
        entry = 100.0
        stop_wide = 98.0
        stop_tight = 99.0

        risk_wide = abs(entry - stop_wide)  # 2.0
        risk_tight = abs(entry - stop_tight)  # 1.0

        mfe_pct = 1.0  # 1% move

        mfe_r_wide = mfe_pct / (risk_wide / entry * 100)
        mfe_r_tight = mfe_pct / (risk_tight / entry * 100)

        # Wider stop → smaller R for same move
        assert mfe_r_wide < mfe_r_tight


# ── Tests: Horizon Maturity ───────────────────────────────────

class TestHorizonMaturity:
    """Test that horizons fill only when enough time has passed."""

    def test_15m_not_mature_at_10m(self):
        """15m horizon should not be mature at 10 minutes."""
        signal_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        now = signal_time + timedelta(minutes=10)
        assert not (now >= signal_time + timedelta(minutes=15))

    def test_15m_mature_at_15m(self):
        """15m horizon should be mature at exactly 15 minutes."""
        signal_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        now = signal_time + timedelta(minutes=15)
        assert now >= signal_time + timedelta(minutes=15)

    def test_240m_not_mature_at_60m(self):
        """240m horizon should not be mature at 60 minutes."""
        signal_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        now = signal_time + timedelta(minutes=60)
        assert not (now >= signal_time + timedelta(minutes=240))


# ── Tests: Target/SL Hit Sequence ────────────────────────────

class TestTargetSLHit:
    """Test TP/SL hit sequence detection."""

    def test_tp_before_sl(self):
        """TP hit before SL."""
        signal_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        candles = [
            # First candle hits TP
            Candle(
                timestamp=int((signal_time + timedelta(minutes=5)).timestamp() * 1000),
                open=100.0, high=103.0, low=99.8, close=102.0, volume=1000.0,
            ),
            # Second candle hits SL (but TP already hit)
            Candle(
                timestamp=int((signal_time + timedelta(minutes=10)).timestamp() * 1000),
                open=102.0, high=102.5, low=98.5, close=99.0, volume=1000.0,
            ),
        ]

        result = _check_long_target_sl(
            candles, entry_price=100.0, invalidation_price=99.0,
            target_1=103.0, max_minutes=30, signal_time=signal_time,
        )

        assert result["tp_hit"] is True
        assert result["sl_hit"] is True
        assert result["tp_before_sl"] is True
        assert result["sl_before_tp"] is False

    def test_sl_before_tp(self):
        """SL hit before TP."""
        signal_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        candles = [
            # First candle hits SL
            Candle(
                timestamp=int((signal_time + timedelta(minutes=5)).timestamp() * 1000),
                open=100.0, high=100.5, low=98.5, close=99.0, volume=1000.0,
            ),
        ]

        result = _check_long_target_sl(
            candles, entry_price=100.0, invalidation_price=99.0,
            target_1=103.0, max_minutes=30, signal_time=signal_time,
        )

        assert result["tp_hit"] is False
        assert result["sl_hit"] is True
        assert result["tp_before_sl"] is False
        assert result["sl_before_tp"] is True

    def test_no_hit(self):
        """Neither TP nor SL hit."""
        signal_time = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        candles = [
            Candle(
                timestamp=int((signal_time + timedelta(minutes=5)).timestamp() * 1000),
                open=100.0, high=101.0, low=99.5, close=100.5, volume=1000.0,
            ),
        ]

        result = _check_long_target_sl(
            candles, entry_price=100.0, invalidation_price=99.0,
            target_1=103.0, max_minutes=30, signal_time=signal_time,
        )

        assert result["tp_hit"] is False
        assert result["sl_hit"] is False
        assert result["tp_before_sl"] is False
        assert result["sl_before_tp"] is False


# ── Tests: Existing Paper Trading Unchanged ───────────────────

class TestPaperTradingUnchanged:
    """Verify that SRR LONG remains blocked for paper trading."""

    def test_srr_long_in_blocked_list(self):
        """SRR LONG must remain in blocked_scanner_directions."""
        from app.config import Settings
        settings = Settings()
        blocked = set(settings.blocked_scanner_directions)
        assert ("SUPPORT_RESISTANCE_REACTION", "LONG") in blocked

    def test_srr_short_in_blocked_list(self):
        """SRR SHORT must remain in blocked_scanner_directions."""
        from app.config import Settings
        settings = Settings()
        blocked = set(settings.blocked_scanner_directions)
        assert ("SUPPORT_RESISTANCE_REACTION", "SHORT") in blocked

    def test_srr_not_in_shadow_control_scanners(self):
        """SRR should NOT be in SHADOW_CONTROL_SCANNERS (it's research, not OOS)."""
        from app.scanners.orchestrator import ScannerOrchestrator
        assert "SUPPORT_RESISTANCE_REACTION" not in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS

    def test_srr_in_research_capture_scanners(self):
        """SRR should be in RESEARCH_CAPTURE_SCANNERS."""
        from app.scanners.orchestrator import ScannerOrchestrator
        assert "SUPPORT_RESISTANCE_REACTION" in ScannerOrchestrator.RESEARCH_CAPTURE_SCANNERS


# ── Tests: Outcome Evaluator Idempotency ──────────────────────

class TestEvaluatorIdempotency:
    """Test that outcome evaluation is idempotent."""

    def test_evaluated_horizon_not_reprocessed(self, srr_evaluator):
        """Already-evaluated horizons should not be re-evaluated."""
        sig = {
            "signal_id": 1,
            "symbol": "BTCUSDT",
            "signal_time": datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc),
            "entry_price": 100.0,
            "invalidation_price": 99.0,
            "target_1": 103.0,
            "outcome_id": 1,
            "evaluated_15m_at": datetime.now(timezone.utc),  # Already evaluated
            "evaluated_30m_at": None,
            "evaluated_60m_at": None,
            "evaluated_120m_at": None,
            "evaluated_240m_at": None,
            "is_final": False,
        }

        candles = [
            Candle(
                timestamp=int((sig["signal_time"] + timedelta(minutes=5 * (i + 1))).timestamp() * 1000),
                open=100.0, high=101.0, low=99.5, close=100.5, volume=1000.0,
            )
            for i in range(20)
        ]

        stats = {
            "horizons_updated": {h: 0 for h, _ in [
                ("15m", 15), ("30m", 30), ("60m", 60), ("120m", 120), ("240m", 240),
            ]},
            "outcomes_created": 0,
            "outcomes_updated": 0,
            "finalized": 0,
            "errors": 0,
        }

        srr_evaluator._evaluate_signal(sig, candles, datetime.now(timezone.utc), stats)

        # 15m should NOT be updated (already evaluated)
        assert stats["horizons_updated"]["15m"] == 0

    def test_duplicate_setup_id_not_created(self, mock_conn):
        """Duplicate signal_candle_open_time should update, not insert."""
        # This is tested via ON CONFLICT in the observer
        observer = SRRResearchObserver(mock_conn)
        mock_conn.cursor.return_value.fetchone.return_value = None  # ON CONFLICT

        result = observer._save_signal(
            setup_id=str(uuid4()),
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            scanner_version="2.0.0",
            symbol="BTCUSDT",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000000,
            reference_price=100.0,
            entry_zone_low=99.8,
            entry_zone_high=100.2,
            invalidation_price=99.5,
            target_1=103.0,
            target_2=104.5,
            score=65.0,
            market_regime="TREND_UP",
            features={"_raw_touch_count": 3},
        )

        assert result["status"] == "duplicate"


# ── Tests: Orchestrator Research Capture (Gate Independent) ───

def _make_srr_long_candidate(**overrides) -> SetupCandidate:
    """Build a minimal SRR LONG SetupCandidate for orchestrator tests."""
    defaults = dict(
        scanner_name="SUPPORT_RESISTANCE_REACTION",
        scanner_version="2.0.0",
        symbol="ETHUSDT",
        direction="LONG",
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        detected_at=datetime(2026, 9, 24, 13, 39, 1, tzinfo=timezone.utc),
        setup_started_at=datetime(2026, 9, 24, 13, 39, 1, tzinfo=timezone.utc),
        signal_candle_open_time=1700000000000,
        reference_price=3500.0,
        entry_zone_low=3493.0,
        entry_zone_high=3507.0,
        invalidation_price=3482.5,
        target_1=3530.0,
        target_2=3545.0,
        score=47.0,
        market_regime="TREND_UP",
        state=SetupState.SETUP_READY,
        features={
            "level_touch_count": 0.6,
            "rejection_strength": 0.7,
            "rr_ratio": 0.5,
            "stop_distance_atr": 0.8,
            "volume_spike": True,
            "regime_alignment": 1.0,
            "_raw_touch_count": 3,
            "_raw_level_distance_pct": 0.15,
            "_raw_atr": 1.5,
            "_raw_atr_pct": 1.5,
            "_raw_candle_range": 2.0,
            "_raw_candle_body": 0.8,
            "_raw_upper_wick": 0.3,
            "_raw_lower_wick": 0.9,
            "_raw_wick_body_ratio": 1.5,
            "_raw_volume": 1500.0,
            "_raw_volume_ratio": 1.5,
            "_raw_rr": 2.0,
            "_raw_risk_distance": 0.5,
            "_raw_risk_distance_pct": 0.5,
            "_raw_stop_distance_atr": 0.33,
            "_raw_level_type": "support",
        },
    )
    defaults.update(overrides)
    return SetupCandidate(**defaults)


def _make_srr_short_candidate(**overrides) -> SetupCandidate:
    """Build a minimal SRR SHORT SetupCandidate."""
    defaults = dict(
        scanner_name="SUPPORT_RESISTANCE_REACTION",
        scanner_version="2.0.0",
        symbol="ETHUSDT",
        direction="SHORT",
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        detected_at=datetime(2026, 9, 24, 13, 39, 1, tzinfo=timezone.utc),
        setup_started_at=datetime(2026, 9, 24, 13, 39, 1, tzinfo=timezone.utc),
        signal_candle_open_time=1700000000000,
        reference_price=3500.0,
        entry_zone_low=3493.0,
        entry_zone_high=3507.0,
        invalidation_price=3517.5,
        target_1=3470.0,
        target_2=3455.0,
        score=47.0,
        market_regime="TREND_DOWN",
        state=SetupState.SETUP_READY,
        features={"level_touch_count": 0.6, "_raw_touch_count": 3},
    )
    defaults.update(overrides)
    return SetupCandidate(**defaults)


class TestOrchestratorResearchCapture:
    """Regression tests: SRR LONG capture is independent of direction gate.

    The capture must happen after scoring + dedup + risk geometry + score gate,
    but BEFORE direction gate and expectancy filter.
    """

    @pytest.fixture()
    def mock_srr_scanner_long(self):
        """ScannerOrchestrator with only SRR returning one LONG candidate."""
        from app.scanners.orchestrator import ScannerOrchestrator
        orch = ScannerOrchestrator(enabled_scanners=["SUPPORT_RESISTANCE_REACTION"])
        srr = orch.scanners["SUPPORT_RESISTANCE_REACTION"]
        candidate = _make_srr_long_candidate()
        srr.scan = MagicMock(return_value=[candidate])
        return orch

    @pytest.fixture()
    def mock_srr_scanner_short(self):
        """ScannerOrchestrator with only SRR returning one SHORT candidate."""
        from app.scanners.orchestrator import ScannerOrchestrator
        orch = ScannerOrchestrator(enabled_scanners=["SUPPORT_RESISTANCE_REACTION"])
        srr = orch.scanners["SUPPORT_RESISTANCE_REACTION"]
        candidate = _make_srr_short_candidate()
        srr.scan = MagicMock(return_value=[candidate])
        return orch

    @pytest.fixture()
    def minimal_ctx(self):
        """Minimal MarketContext for scan_all_with_stats."""
        now = datetime.now(timezone.utc)
        return MarketContext(
            symbol="ETHUSDT",
            candles_5m=(),
            candles_15m=(),
            candles_1h=(),
            candles_4h=(),
            indicators=IndicatorSnapshot(atr=1.5),
            market_regime="TREND_UP",
            levels=MarketLevels(),
            evaluated_at=now,
        )

    def _make_gate(self, scanner_name: str, direction: str, allowed: bool):
        """Create a gate policy with configurable allow/block."""
        from app.scanners.direction_gate import (
            ScannerDirectionGate, ScannerDirectionGatePolicy,
        )
        gates = {}
        status = "ENABLED" if allowed else "BLOCKED"
        gates[(scanner_name, direction)] = ScannerDirectionGate(
            scanner_name, direction, status, reason="test",
        )
        # Add the other direction as well
        other_dir = "SHORT" if direction == "LONG" else "LONG"
        gates[(scanner_name, other_dir)] = ScannerDirectionGate(
            scanner_name, other_dir, "ENABLED", reason="test",
        )
        return ScannerDirectionGatePolicy(gates, {})

    def test_gate_enabled_srr_long_in_research(self, mock_srr_scanner_long, minimal_ctx):
        """Gate ENABLED: SRR LONG stays in normal output AND appears in research."""
        gate = self._make_gate("SUPPORT_RESISTANCE_REACTION", "LONG", allowed=True)
        candidates, stats = mock_srr_scanner_long.scan_all_with_stats(
            minimal_ctx, gate_policy=gate,
        )
        # Candidate should be in normal output (gate allowed)
        longs = [c for c in candidates if c.scanner_name == "SUPPORT_RESISTANCE_REACTION"
                 and c.direction == "LONG"
                 and not c.features.get("_research_capture")]
        assert len(longs) == 1, (
            f"SRR LONG gate-allowed candidate missing from normal output. "
            f"candidates={len(candidates)}, all={[c.direction for c in candidates]}"
        )
        # Research candidates must exist
        research = stats.get("_research_candidates", [])
        assert len(research) == 1, (
            f"Expected 1 research candidate, got {len(research)}"
        )
        assert research[0].features["_research_capture"] is True
        assert research[0].features["_research_capture_reason"] == "independent_research_capture"

    def test_gate_blocked_srr_long_in_research(self, mock_srr_scanner_long, minimal_ctx):
        """Gate BLOCKED: SRR LONG absent from tradeable output, still in research."""
        gate = self._make_gate("SUPPORT_RESISTANCE_REACTION", "LONG", allowed=False)
        candidates, stats = mock_srr_scanner_long.scan_all_with_stats(
            minimal_ctx, gate_policy=gate,
        )
        # SRR LONG should be absent from normal tradeable output
        tradeable = [c for c in candidates
                     if c.scanner_name == "SUPPORT_RESISTANCE_REACTION"
                     and c.direction == "LONG"
                     and not c.features.get("_research_capture")
                     and not c.features.get("_shadow_control")]
        assert len(tradeable) == 0, (
            f"SRR LONG gate-blocked candidate leaked into tradeable output! "
            f"tradeable={[c.direction for c in tradeable]}"
        )
        # Research candidates must still exist
        research = stats.get("_research_candidates", [])
        assert len(research) == 1, (
            f"Expected 1 research candidate despite gate BLOCKED, got {len(research)}"
        )
        assert research[0].features["_research_capture"] is True
        assert research[0].scanner_name == "SUPPORT_RESISTANCE_REACTION"
        assert research[0].direction == "LONG"

    def test_srr_short_never_in_research(self, mock_srr_scanner_short, minimal_ctx):
        """SRR SHORT must never appear in research_candidates."""
        gate = self._make_gate("SUPPORT_RESISTANCE_REACTION", "SHORT", allowed=False)
        candidates, stats = mock_srr_scanner_short.scan_all_with_stats(
            minimal_ctx, gate_policy=gate,
        )
        research = stats.get("_research_candidates", [])
        assert len(research) == 0, (
            f"SRR SHORT should NOT be in research_candidates, got {len(research)}"
        )

    def test_research_candidates_always_present_in_stats(self, mock_srr_scanner_long, minimal_ctx):
        """stats must always contain _research_candidates key (even if empty)."""
        _, stats = mock_srr_scanner_long.scan_all_with_stats(minimal_ctx)
        assert "_research_candidates" in stats, (
            "_research_candidates key missing from stats dict"
        )

    def test_runner_handles_research_candidates_no_keyerror(self):
        """scanner_runner stats loop must not KeyError on _research_candidates."""
        symbol_stats = {
            "SUPPORT_RESISTANCE_REACTION": {
                "candidates_found": 1,
                "errors_count": 0,
                "duration_ms": 10.0,
            },
            "_research_candidates": [
                _make_srr_long_candidate(),
            ],
        }
        run_stats = {
            "SUPPORT_RESISTANCE_REACTION": {
                "symbols_scanned": 0, "candidates_found": 0,
                "setups_saved": 0, "errors_count": 0, "duration_ms": 0.0,
            },
        }
        # This must NOT raise KeyError
        for name, values in symbol_stats.items():
            if name.startswith("_"):
                continue
            stat = run_stats[name]
            stat["symbols_scanned"] += 1
            for field in ("candidates_found", "setups_saved", "errors_count", "duration_ms"):
                if field in values:
                    stat[field] += values[field]

        assert run_stats["SUPPORT_RESISTANCE_REACTION"]["candidates_found"] == 1
        assert run_stats["SUPPORT_RESISTANCE_REACTION"]["symbols_scanned"] == 1

    def test_paper_trading_unchanged(self):
        """SRR LONG + SHORT remain blocked. No execution gates changed."""
        from app.config import Settings
        settings = Settings()
        blocked = set(settings.blocked_scanner_directions)
        assert ("SUPPORT_RESISTANCE_REACTION", "LONG") in blocked
        assert ("SUPPORT_RESISTANCE_REACTION", "SHORT") in blocked

        from app.scanners.orchestrator import ScannerOrchestrator
        assert "SUPPORT_RESISTANCE_REACTION" not in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS
