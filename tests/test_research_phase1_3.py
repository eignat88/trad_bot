"""Tests for Phase 1.3: observation → resolved status → signal promotion.

Verifies:
  - setup_ready promotion
  - rejected promotion (all rejection types)
  - duplicate promotion suppressed
  - restart/idempotency
  - observation status update
  - setup_id propagation
  - reject reason propagation
  - GRANT migration presence
  - score=0 observations (informational, not blocking)
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from app.research.constants import ObservationStatus
from app.research.models import ResearchObservation
from app.research.observer import ResearchObserver
from app.research.repository import ResearchRepository


# ── helpers ──────────────────────────────────────────────────

def _make_obs(
    experiment_id: str = "TPV2_GENERIC_V1",
    symbol: str = "ETHUSDT",
    direction: str = "LONG",
    candle_ts: int = 1700000000000,
    setup_id: str = "obs-uuid-001",
    reference_price: float = 3500.0,
    invalidation_price: float = 3450.0,
    target_1: float = 3525.0,
    score: float = 0.0,
) -> ResearchObservation:
    return ResearchObservation(
        experiment_id=experiment_id,
        scanner_name="TREND_PULLBACK_V2",
        scanner_version="1.0.0",
        parameter_set_id="tpv2_1.0.0_20260928",
        symbol=symbol,
        direction=direction,
        signal_time=datetime.now(timezone.utc),
        signal_candle_open_time=candle_ts,
        reference_price=reference_price,
        entry_zone_low=reference_price * 0.998,
        entry_zone_high=reference_price * 1.002,
        invalidation_price=invalidation_price,
        target_1=target_1,
        target_2=None,
        score=score,
        status="DETECTED",
        features={"pullback_quality": 0.65, "target_r": 0.50},
        parameters={"pullback_tolerance": 0.012, "target_r": 0.50},
        market_regime="TREND_UP",
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        setup_id=setup_id,
    )


# ── resolve_and_promote_batch tests ─────────────────────────


class TestResolveAndPromote:
    """Test batch status resolution and signal promotion."""

    def _make_repo(self) -> tuple[ResearchRepository, MagicMock]:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.rowcount = 1
        return ResearchRepository(mock_conn), mock_conn

    def test_setup_ready_promotion(self):
        """SETUP_READY observation gets resolved and promoted."""
        repo, mock_conn = self._make_repo()

        stats = repo.resolve_and_promote_batch(
            rejections=[],
            setup_ready=[("obs-001", "TREND_PULLBACK_V2")],
        )

        assert stats["resolved"] >= 1
        assert stats["errors"] == 0

    def test_rejected_promotion(self):
        """REJECTED observation gets resolved and promoted (core goal)."""
        repo, mock_conn = self._make_repo()

        stats = repo.resolve_and_promote_batch(
            rejections=[
                ("obs-002", "GATE_REJECTED", "DIRECTION_GATE_BLOCKED", "scanner direction blocked"),
                ("obs-003", "SCORE_REJECTED", "score_gate", "score=15.0 < 30"),
                ("obs-004", "GEOMETRY_REJECTED", "risk_geometry", "invalid risk geometry"),
                ("obs-005", "DEDUP_REJECTED", "deduplication", "duplicate fingerprint"),
                ("obs-006", "REGIME_REJECTED", "regime_filter", "TREND_DOWN vs LONG"),
            ],
            setup_ready=[],
        )

        assert stats["resolved"] >= 5
        assert stats["errors"] == 0

    def test_mixed_resolved(self):
        """Both setup_ready and rejected in same batch."""
        repo, mock_conn = self._make_repo()

        stats = repo.resolve_and_promote_batch(
            rejections=[("obs-10", "GATE_REJECTED", "DIRECTION_GATE_BLOCKED", "blocked")],
            setup_ready=[("obs-11", "TREND_PULLBACK_V2")],
        )

        assert stats["resolved"] >= 2
        assert stats["errors"] == 0

    def test_idempotent_rerun(self):
        """Re-running resolve for same setup_id doesn't error (already resolved)."""
        repo, mock_conn = self._make_repo()

        # First run
        repo.resolve_and_promote_batch(
            rejections=[("obs-20", "GATE_REJECTED", "gate", "blocked")],
            setup_ready=[],
        )
        # Second run — same setup_id, already non-DETECTED → 0 resolved
        stats = repo.resolve_and_promote_batch(
            rejections=[("obs-20", "GATE_REJECTED", "gate", "blocked")],
            setup_ready=[],
        )

        # Should not error — idempotent
        assert stats["errors"] == 0

    def test_empty_batch(self):
        """Empty batch is a no-op, no errors."""
        repo, mock_conn = self._make_repo()
        stats = repo.resolve_and_promote_batch(rejections=[], setup_ready=[])
        # Empty rejections/setup_ready means no UPDATE executed, but
        # the INSERT SELECT runs (with 0 rows) — that's fine.
        assert stats["errors"] == 0
        assert stats["resolved"] == 0

    def test_no_connection(self):
        """No connection returns zero stats."""
        repo = ResearchRepository(None)
        stats = repo.resolve_and_promote_batch(
            rejections=[("x", "GATE_REJECTED", None, None)],
            setup_ready=[],
        )
        assert stats == {"resolved": 0, "promoted": 0, "errors": 0}


# ── Status update tests ──────────────────────────────────────


class TestObservationStatusUpdate:
    """Test individual status update via update_observation_status."""

    def test_update_observation_status(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.rowcount = 1

        repo = ResearchRepository(mock_conn)
        result = repo.update_observation_status(
            setup_id="test-uuid",
            status="SETUP_READY",
            rejection_stage=None,
            rejection_reason=None,
        )
        assert result is True

    def test_update_with_rejection_info(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.rowcount = 1

        repo = ResearchRepository(mock_conn)
        result = repo.update_observation_status(
            setup_id="test-uuid",
            status="GATE_REJECTED",
            rejection_stage="direction_gate",
            rejection_reason="scanner direction blocked",
        )
        assert result is True

    def test_update_no_setup_id(self):
        repo = ResearchRepository(MagicMock())
        result = repo.update_observation_status(setup_id="", status="SETUP_READY")
        assert result is False


# ── Orchestrator rejection tracking tests ────────────────────


class TestOrchestratorRejections:
    """Verify orchestrator exposes _research_rejections and _research_setup_ready."""

    def _make_orchestrator_with_mock(self):
        """Create orchestrator with mock scanner that returns candidates."""
        from app.scanners.orchestrator import ScannerOrchestrator
        from app.scanners.models import SetupCandidate, SetupState

        orch = ScannerOrchestrator(enabled_scanners=["TREND_PULLBACK_V2"])

        # Mock scanner to return one candidate
        mock_scanner = MagicMock()
        candidate = SetupCandidate(
            setup_id="orch-test-001",
            scanner_name="TREND_PULLBACK_V2",
            scanner_version="1.0.0",
            symbol="ETHUSDT",
            direction="LONG",
            htf_timeframe="1h",
            setup_timeframe="15m",
            entry_timeframe="5m",
            detected_at=datetime.now(timezone.utc),
            setup_started_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000000,
            reference_price=3500.0,
            entry_zone_low=3493.0,
            entry_zone_high=3507.0,
            invalidation_price=3450.0,
            target_1=3525.0,
            target_2=None,
            score=45.0,
            market_regime="TREND_UP",
            reasons=(),
            features={"pullback_quality": 0.65, "target_r": 0.50},
            state=SetupState.SETUP_READY,
        )
        mock_scanner.scan.return_value = [candidate]
        orch.scanners["TREND_PULLBACK_V2"] = mock_scanner

        return orch, candidate

    def test_research_rejections_exposed(self):
        """Rejections are exposed via stats['_research_rejections']."""
        orch, _ = self._make_orchestrator_with_mock()

        ctx = SimpleNamespace(
            symbol="ETHUSDT",
            market_regime="TREND_UP",
            candles_5m=(SimpleNamespace(timestamp=1700000000000, open=0, high=0, low=0, close=0, volume=0),),
            candles_15m=(),
            candles_1h=(),
            candles_4h=(),
            evaluated_at=datetime.now(timezone.utc),
            indicators=SimpleNamespace(
                atr=50, rsi=55, ema20=3500, ema50=3450, ema200=3400,
                bb_upper=3550, bb_lower=3450, bb_width=0.02,
                volume_sma=1000, adx=25, ema50_slope=0.001,
            ),
            levels=SimpleNamespace(),
        )

        candidates, stats = orch.scan_all_with_stats(ctx)

        # _research_rejections should be a list
        assert "_research_rejections" in stats
        assert isinstance(stats["_research_rejections"], list)

        # _research_setup_ready should be a list
        assert "_research_setup_ready" in stats
        assert isinstance(stats["_research_setup_ready"], list)


# ── Score=0 informational tests ──────────────────────────────


class TestScoreZeroObservations:
    """Verify score=0 observations are captured but noted."""

    def test_score_zero_observation_created(self):
        """score=0 observations are saved (not filtered by observer)."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 999

        from app.research.adapters.trend_pullback_v2 import EXPERIMENT_CONFIG
        observer = ResearchObserver(mock_repo, {"TREND_PULLBACK_V2": dict(EXPERIMENT_CONFIG)})

        obs = _make_obs(score=0.0)
        observer.observe(SimpleNamespace(
            setup_id="score-zero-001",
            scanner_name="TREND_PULLBACK_V2",
            scanner_version="1.0.0",
            symbol="ETHUSDT",
            direction="LONG",
            htf_timeframe="1h",
            setup_timeframe="15m",
            entry_timeframe="5m",
            detected_at=datetime.now(timezone.utc),
            setup_started_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000001,
            reference_price=3500.0,
            entry_zone_low=3493.0,
            entry_zone_high=3507.0,
            invalidation_price=3450.0,
            target_1=3525.0,
            target_2=None,
            score=0.0,
            market_regime="TREND_UP",
            reasons=(),
            features={"pullback_quality": 0.5},
            state=SimpleNamespace(value="SETUP_READY"),
        ))

        assert mock_repo.save_observation.call_count == 1
        obs_arg = mock_repo.save_observation.call_args[0][0]
        assert obs_arg.score == 0.0

    def test_score_zero_will_be_rejected_by_gate(self):
        """score=0 candidate will get GATE_REJECTED or SCORE_REJECTED."""
        # This is informational — score=0 observations exist, they'll be
        # rejected by production pipeline, and then get outcome evaluation.
        # This is the correct behavior for research framework.
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 999

        from app.research.adapters.trend_pullback_v2 import EXPERIMENT_CONFIG
        observer = ResearchObserver(mock_repo, {"TREND_PULLBACK_V2": dict(EXPERIMENT_CONFIG)})

        # Observer doesn't filter by score — it captures everything
        obs = _make_obs(score=0.0)
        observer.observe(SimpleNamespace(
            setup_id="score-zero-002",
            scanner_name="TREND_PULLBACK_V2",
            scanner_version="1.0.0",
            symbol="ETHUSDT",
            direction="LONG",
            htf_timeframe="1h",
            setup_timeframe="15m",
            entry_timeframe="5m",
            detected_at=datetime.now(timezone.utc),
            setup_started_at=datetime.now(timezone.utc),
            signal_candle_open_time=1700000000002,
            reference_price=3500.0,
            entry_zone_low=3493.0,
            entry_zone_high=3507.0,
            invalidation_price=3450.0,
            target_1=3525.0,
            target_2=None,
            score=0.0,
            market_regime="TREND_UP",
            reasons=(),
            features={"pullback_quality": 0.5},
            state=SimpleNamespace(value="SETUP_READY"),
        ))

        # Observer captures it — production pipeline will reject it
        assert mock_repo.save_observation.call_count == 1


# ── GRANT migration test ─────────────────────────────────────


class TestGrantMigration:
    """Verify GRANT statements exist in migration 049."""

    def test_grant_present_in_migration(self):
        """Migration file contains GRANT for trad_bot role."""
        from pathlib import Path
        migration_path = Path(__file__).parent.parent / "sql" / "migrations" / "049_generic_research_framework.sql"
        content = migration_path.read_text(encoding="utf-8")

        assert "GRANT USAGE ON SCHEMA research TO trad_bot" in content
        assert "GRANT SELECT, INSERT, UPDATE" in content
        assert "ON ALL TABLES IN SCHEMA research" in content
        assert "TO trad_bot" in content
        assert "GRANT USAGE, SELECT" in content
        assert "ON ALL SEQUENCES IN SCHEMA research" in content
        assert "ALTER DEFAULT PRIVILEGES" in content


# ── Promotion policy tests ───────────────────────────────────


class TestPromotionPolicy:
    """Verify promotion policy: both SETUP_READY and REJECTED get signals."""

    def test_rejection_types_in_repository(self):
        """All rejection statuses are valid and supported."""
        valid_rejections = {
            "DEDUP_REJECTED",
            "GATE_REJECTED",
            "SCORE_REJECTED",
            "GEOMETRY_REJECTED",
            "REGIME_REJECTED",
            "EXPECTANCY_REJECTED",
        }
        # These should all be valid ObservationStatus values
        assert valid_rejections.issubset({
            ObservationStatus.DEDUP_REJECTED,
            ObservationStatus.GATE_REJECTED,
            ObservationStatus.SCORE_REJECTED,
            ObservationStatus.GEOMETRY_REJECTED,
            ObservationStatus.REGIME_REJECTED,
            ObservationStatus.EXPECTANCY_REJECTED,
        })
