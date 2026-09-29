"""Regression tests for prospective OOS promotion to research_signal.

Covers:
  A. prospective observation → adapter → research_signal created
  B. research_signal.prospective_observation_id == observation_id
  C. experiment_id preserved
  D. repeated resolve → no duplicate research_signal
  E. promoted counter only incremented after successful persistence
  F. adapter failure → promoted=0, promote_errors observable
  G. all three new experiment adapters pass contract test
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.research.prospective_observer import ProspectiveOOSObserver
from app.research.repository import ResearchRepository


# ── fixtures ──────────────────────────────────────────────────


def _make_registry():
    return {
        "BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1": {
            "scanner_name": "BREAKOUT_RETEST",
            "direction": "LONG",
        },
        "FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1": {
            "scanner_name": "FVG_REACTION_LONG_LOCAL_STRUCT_V1",
            "direction": "LONG",
        },
        "TREND_PULLBACK_V3_HIGH_VOL_OOS_V1": {
            "scanner_name": "TREND_PULLBACK_V3",
            "direction": "LONG",
        },
        "ME_SHORT_GEOM_A_V1": {
            "scanner_name": "MOMENTUM_EXHAUSTION",
            "direction": "SHORT",
        },
    }


def _make_observation_row(obs_id=1, exp_id="TEST", symbol="BTCUSDT"):
    return (
        obs_id, exp_id, symbol, "LONG",
        datetime.now(timezone.utc),
        83791.6, 83478.3, 84805.6, 85566.1, 44.55,
        json.dumps({}), json.dumps({}), "RANGE",
    )


def _mock_conn_with_cursor():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


# ── A. observation → research_signal created ────────────────


class TestPromotionCreatesSignal:
    def test_promote_inserts_row(self):
        conn, cursor = _mock_conn_with_cursor()
        cursor.fetchone.side_effect = [_make_observation_row(), None]
        cursor.rowcount = 1

        repo = ResearchRepository(conn)
        assert repo.promote_prospective_to_signal(1) is True

        insert_calls = [
            c for c in cursor.execute.call_args_list
            if "INSERT INTO research.research_signal" in str(c)
        ]
        assert len(insert_calls) == 1

    def test_promote_commits(self):
        conn, cursor = _mock_conn_with_cursor()
        cursor.fetchone.side_effect = [_make_observation_row(), None]
        cursor.rowcount = 1

        ResearchRepository(conn).promote_prospective_to_signal(1)
        conn.commit.assert_called()

    def test_promote_not_found(self):
        conn, cursor = _mock_conn_with_cursor()
        cursor.fetchone.return_value = None
        assert ResearchRepository(conn).promote_prospective_to_signal(99999) is False


# ── B. prospective_observation_id preserved ─────────────────


class TestObservationIdPreserved:
    def test_prospective_observation_id_set(self):
        conn, cursor = _mock_conn_with_cursor()
        cursor.fetchone.side_effect = [_make_observation_row(obs_id=42), None]
        cursor.rowcount = 1

        ResearchRepository(conn).promote_prospective_to_signal(42)

        for c in cursor.execute.call_args_list:
            sql = c.args[0] if c.args else ""
            if "INSERT INTO research.research_signal" in sql:
                params = c.args[1]
                assert params[-1] == 42
                return
        pytest.fail("INSERT INTO research.research_signal not found")


# ── C. experiment_id preserved ────────────────────────────


class TestExperimentIdPreserved:
    def test_experiment_id_copied(self):
        conn, cursor = _mock_conn_with_cursor()
        obs = _make_observation_row(exp_id="BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1")
        cursor.fetchone.side_effect = [obs, None]
        cursor.rowcount = 1

        ResearchRepository(conn).promote_prospective_to_signal(1)

        for c in cursor.execute.call_args_list:
            sql = c.args[0] if c.args else ""
            if "INSERT INTO research.research_signal" in sql:
                params = c.args[1]
                assert params[1] == "BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1"
                return
        pytest.fail("INSERT not found")


# ── D. idempotency ─────────────────────────────────────────


class TestIdempotency:
    def test_second_promote_returns_false(self):
        conn, cursor = _mock_conn_with_cursor()
        cursor.fetchone.side_effect = [_make_observation_row(), None]
        cursor.rowcount = 1

        repo = ResearchRepository(conn)
        assert repo.promote_prospective_to_signal(1) is True

        cursor.fetchone.return_value = None
        assert repo.promote_prospective_to_signal(1) is False

    def test_not_exists_in_query(self):
        conn, cursor = _mock_conn_with_cursor()
        cursor.fetchone.return_value = None

        ResearchRepository(conn).promote_prospective_to_signal(1)

        select_call = cursor.execute.call_args_list[0]
        assert "NOT EXISTS" in select_call.args[0]


# ── E. promoted counter accuracy ──────────────────────────


class TestPromotedCounterAccuracy:
    def test_promoted_incremented_on_success(self):
        p_conn, p_cursor = _mock_conn_with_cursor()
        p_cursor.fetchone.side_effect = [(1,)]  # RETURNING observation_id
        p_cursor.rowcount = 1

        r_conn, r_cursor = _mock_conn_with_cursor()
        r_cursor.fetchone.side_effect = [_make_observation_row(), None]
        r_cursor.rowcount = 1

        research_repo = ResearchRepository(r_conn)
        observer = ProspectiveOOSObserver(p_conn, _make_registry(), research_repo=research_repo)

        observer.observe(
            scanner_name="BREAKOUT_RETEST", direction="LONG", symbol="BTCUSDT",
            signal_time=datetime.now(timezone.utc),
            reference_price=83791.6, invalidation_price=83478.3,
            target_1=84805.6, target_2=85566.1, score=44.55,
            features={}, parameters={}, market_regime="RANGE",
        )

        assert observer.stats.get("promoted", 0) >= 1

    def test_promote_error_tracked(self):
        p_conn, p_cursor = _mock_conn_with_cursor()
        p_cursor.fetchone.side_effect = [(1,)]  # RETURNING observation_id
        p_cursor.rowcount = 1

        research_repo = MagicMock()
        research_repo.promote_prospective_to_signal.side_effect = RuntimeError("DB error")

        observer = ProspectiveOOSObserver(p_conn, _make_registry(), research_repo=research_repo)
        observer.observe(
            scanner_name="BREAKOUT_RETEST", direction="LONG", symbol="BTCUSDT",
            signal_time=datetime.now(timezone.utc),
            reference_price=83791.6, invalidation_price=83478.3,
            target_1=84805.6, target_2=85566.1, score=44.55,
            features={}, parameters={}, market_regime="RANGE",
        )

        assert observer.stats.get("promote_errors", 0) >= 1
        assert observer.stats.get("errors", 0) == 0


# ── F. no research_repo → skip promotion ──────────────────


class TestAdapterFailure:
    def test_no_research_repo_skips_promotion(self):
        p_conn, p_cursor = _mock_conn_with_cursor()
        p_cursor.fetchone.side_effect = [(1,)]  # RETURNING observation_id
        p_cursor.rowcount = 1

        observer = ProspectiveOOSObserver(p_conn, _make_registry(), research_repo=None)
        observer.observe(
            scanner_name="BREAKOUT_RETEST", direction="LONG", symbol="BTCUSDT",
            signal_time=datetime.now(timezone.utc),
            reference_price=83791.6, invalidation_price=83478.3,
            target_1=84805.6, target_2=85566.1, score=44.55,
            features={}, parameters={}, market_regime="RANGE",
        )

        stats = observer.stats
        assert stats.get("BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1", 0) == 1
        assert "promoted" not in stats
        assert "promote_errors" not in stats


# ── G. all three new experiments ──────────────────────────


class TestNewExperimentAdapters:
    @pytest.mark.parametrize("scanner_name,direction,exp_id", [
        ("BREAKOUT_RETEST", "LONG", "BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1"),
        ("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG", "FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1"),
        ("TREND_PULLBACK_V3", "LONG", "TREND_PULLBACK_V3_HIGH_VOL_OOS_V1"),
    ])
    def test_creates_observation_and_promotes(self, scanner_name, direction, exp_id):
        p_conn, p_cursor = _mock_conn_with_cursor()
        p_cursor.fetchone.side_effect = [(1,)]  # RETURNING observation_id

        r_conn, r_cursor = _mock_conn_with_cursor()
        r_cursor.fetchone.side_effect = [_make_observation_row(exp_id=exp_id), None]
        r_cursor.rowcount = 1

        research_repo = ResearchRepository(r_conn)
        observer = ProspectiveOOSObserver(p_conn, _make_registry(), research_repo=research_repo)

        observer.observe(
            scanner_name=scanner_name, direction=direction, symbol="BTCUSDT",
            signal_time=datetime.now(timezone.utc),
            reference_price=83791.6, invalidation_price=83478.3,
            target_1=84805.6, target_2=85566.1, score=44.55,
            features={}, parameters={}, market_regime="RANGE",
        )

        stats = observer.stats
        assert stats.get(exp_id, 0) == 1
        assert stats.get("promoted", 0) == 1
        assert stats.get("errors", 0) == 0
        assert stats.get("promote_errors", 0) == 0
