"""Targeted prospective validation tests for SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1.

The tests are deliberately read-only/unit-level. They verify the frozen observer
geometry, strict contamination boundary, shared evaluator first-hit semantics,
and the single execution-R normalization used by the analysis helper.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.research.prospective_evaluator import _check_tp_sl
from app.research.prospective_execution_analysis import (
    ELEVATED_ROUND_TRIP,
    NORMAL_ROUND_TRIP,
    analyze_observations,
    derive_cost_r,
    derive_execution_r,
    derive_gross_r,
    derive_realized_path_class,
    derive_timeout_close,
    derive_timeout_gross_r,
)
from app.research.prospective_observer import ProspectiveOOSObserver

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_ID = "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
FREEZE_TS = "2026-10-07T00:00:00Z"


def _registry_entry(**overrides):
    entry = {
        "experiment_id": EXPERIMENT_ID,
        "scanner_name": "SUPPORT_RESISTANCE_REACTION",
        "direction": "SHORT",
        "status": "READY_TO_START",
        "freeze_ts": FREEZE_TS,
        "max_hold_minutes": 120,
        "frozen_sl_r": 2.0,
        "frozen_tp_r": 1.5,
    }
    entry.update(overrides)
    return entry


class _FakeCursor:
    def __init__(self, lifecycle_rows=None, insert_row=(1,)):
        self.execute_calls = []
        self.lifecycle_rows = lifecycle_rows or [("RUNNING",), ("RUNNING", FREEZE_TS)]
        self.insert_row = insert_row

    def execute(self, sql, params=None):
        self.execute_calls.append((sql, params))

    def fetchone(self):
        if self.lifecycle_rows:
            return self.lifecycle_rows.pop(0)
        return self.insert_row

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass


def _observe(
    *,
    registry_entry=None,
    direction="SHORT",
    signal_time="2026-10-07T00:00:01Z",
    reference_price=100.0,
    invalidation_price=100.5,
    insert_row=(1,),
    lifecycle_rows=None,
):
    registry_entry = registry_entry or _registry_entry()
    cursor = _FakeCursor(lifecycle_rows=lifecycle_rows, insert_row=insert_row)
    conn = _FakeConn(cursor)
    observer = ProspectiveOOSObserver(conn, {EXPERIMENT_ID: registry_entry})
    result = observer.observe(
        scanner_name="SUPPORT_RESISTANCE_REACTION",
        direction=direction,
        symbol="BTCUSDT",
        signal_time=signal_time,
        reference_price=reference_price,
        invalidation_price=invalidation_price,
        target_1=99.0,
        target_2=98.0,
        score=75.0,
        features={"level_touch_count": 0.6},
        parameters={},
        market_regime="TREND_DOWN",
    )
    return result, cursor


def _insert_calls(cursor):
    return [
        call for call in cursor.execute_calls
        if "INSERT INTO research.prospective_observation" in call[0]
    ]


class TestFrozenGeometry:
    def test_registry_entry_frozen_protocol(self):
        registry = json.loads(
            (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
        )
        experiment = next(
            item for item in registry["experiments"]
            if item["experiment_id"] == EXPERIMENT_ID
        )
        assert experiment["scanner_name"] == "SUPPORT_RESISTANCE_REACTION"
        assert experiment["direction"] == "SHORT"
        assert experiment["experiment_type"] == "OOS_EXECUTION_VALIDATION"
        assert experiment["parent_discovery"] == "SRR_SHORT_EXECUTION_R_EXPANSION_COUNTERFACTUAL_V1"
        assert experiment["parent_closed_experiment"] == (
            "SRR_OOS_SCANNER_V1_PROSPECTIVE / SHORT / CLOSED_NEGATIVE"
        )
        assert experiment["max_hold_minutes"] == 120
        assert experiment["intrabar_policy"] == "STOP_FIRST"
        assert experiment["freeze_ts"] is None
        assert experiment["started_at"] is None
        assert experiment["status"] == "READY_TO_START"
        assert experiment["minimum_n"] == 300
        assert experiment["minimum_symbols"] == 30
        assert experiment["minimum_oos_days"] == 14
        assert experiment["cost_model"]["normal_round_trip_pct"] == NORMAL_ROUND_TRIP
        assert experiment["cost_model"]["elevated_round_trip_pct"] == ELEVATED_ROUND_TRIP

    def test_short_frozen_sl_tp_and_execution_r(self):
        result, cursor = _observe()
        assert result == [1]
        calls = _insert_calls(cursor)
        assert len(calls) == 1
        params = calls[0][1]
        assert params[0] == EXPERIMENT_ID
        assert params[4] == "SHORT"
        # entry / invalidation / variant geometry
        assert params[6] == 100.0
        assert params[7] == 100.5
        assert params[12] == 100.0
        assert params[13] == 101.0
        assert params[14] == 99.25
        features = json.loads(params[15])
        assert features["_structural_r"] == 0.5
        assert features["_frozen_sl_r"] == 2.0
        assert features["_frozen_tp_r"] == 1.5
        assert features["_frozen_max_hold"] == 120
        assert features["_frozen_intrabar_policy"] == "STOP_FIRST"
        assert features["_execution_r_abs"] == 1.0
        assert features["_execution_r_definition"] == "2.00 * structural_R"
        assert features["_frozen_freeze_ts"] == FREEZE_TS

    def test_geometry_helper_matches_task_example(self):
        assert derive_execution_r(100.0, 100.5) == 1.0
        assert derive_gross_r(entry=100.0, invalidation_price=100.5, path_class="TP_FIRST") == 0.75
        assert derive_gross_r(entry=100.0, invalidation_price=100.5, path_class="SL_FIRST") == -1.0
        assert derive_gross_r(entry=100.0, invalidation_price=100.5, path_class="AMBIGUOUS") == -1.0


class TestValidGeometryRejection:
    @pytest.mark.parametrize("kwargs", [
        {"reference_price": 0.0},
        {"reference_price": -1.0},
        {"invalidation_price": None},
        {"invalidation_price": -1.0},
        {"invalidation_price": 100.0},
        {"direction": "LONG"},
    ])
    def test_invalid_candidate_rejected(self, kwargs):
        result, cursor = _observe(**kwargs)
        assert result == []
        assert _insert_calls(cursor) == []


class TestFreezeBoundary:
    def test_strict_greater_than_boundary(self):
        before, before_cursor = _observe(signal_time="2026-10-06T23:59:59Z")
        assert before == []
        assert _insert_calls(before_cursor) == []

        equal, equal_cursor = _observe(signal_time=FREEZE_TS)
        assert equal == []
        assert _insert_calls(equal_cursor) == []

        after, after_cursor = _observe(signal_time="2026-10-07T00:00:01Z")
        assert after == [1]
        assert len(_insert_calls(after_cursor)) == 1

    def test_db_ready_to_start_blocks_capture(self):
        result, cursor = _observe(
            lifecycle_rows=[
                ("RUNNING",),
                ("READY_TO_START", FREEZE_TS),
            ]
        )
        assert result == []
        assert _insert_calls(cursor) == []

    def test_db_started_at_mismatch_blocks_capture(self):
        result, cursor = _observe(
            lifecycle_rows=[
                ("RUNNING",),
                ("RUNNING", "2026-10-07T00:00:02Z"),
            ]
        )
        assert result == []
        assert _insert_calls(cursor) == []

    def test_missing_registry_freeze_blocks_capture(self):
        entry = _registry_entry(freeze_ts=None)
        result, cursor = _observe(registry_entry=entry)
        assert result == []
        assert _insert_calls(cursor) == []


class TestNoHistoricalBackfill:
    def test_pre_freeze_signal_never_inserted(self):
        result, cursor = _observe(signal_time="2026-09-01T00:00:00Z")
        assert result == []
        assert _insert_calls(cursor) == []


class TestDuplicateProtection:
    def test_source_key_is_deduplicated_by_database_unique_constraint(self):
        observer = ProspectiveOOSObserver(conn=None, registry={})
        key1 = observer._make_source_key(
            "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", FREEZE_TS,
        )
        key2 = observer._make_source_key(
            "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", FREEZE_TS,
        )
        assert key1 == key2
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert "ON CONFLICT (experiment_id, source_signal_id) DO NOTHING" in source


class TestSameCandleCollision:
    def test_stop_first_collision_flags_and_execution_r(self):
        signal_time = datetime(2026, 10, 7, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)
        candle = SimpleNamespace(
            timestamp=signal_ts + 5 * 60_000,
            high=101.0,
            low=99.0,
            close=100.0,
        )
        result = _check_tp_sl(
            [candle],
            entry_price=100.0,
            stop_price=101.0,
            target_price=99.25,
            max_minutes=120,
            signal_time=signal_time,
            is_short=True,
            intrabar_policy="STOP_FIRST",
        )
        assert result["ambiguous_intrabar"] is True
        assert result["sl_before_tp"] is True
        assert result["tp_before_sl"] is False
        assert derive_realized_path_class(
            ambiguous_intrabar=True, tp_before_sl=False, sl_before_tp=True,
        ) == "AMBIGUOUS"
        assert derive_gross_r(
            entry=100.0, invalidation_price=100.5, path_class="AMBIGUOUS",
        ) == -1.0


class TestOrderedPaths:
    @staticmethod
    def _short_path(tp_min=None, sl_min=None):
        signal_time = datetime(2026, 10, 7, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)
        candles = []
        for minute in range(5, 125, 5):
            high, low = 100.4, 99.6
            if tp_min == minute:
                low = 99.2
            if sl_min == minute:
                high = 101.1
            candles.append(SimpleNamespace(
                timestamp=signal_ts + minute * 60_000,
                high=high,
                low=low,
            ))
        return _check_tp_sl(
            candles,
            entry_price=100.0,
            stop_price=101.0,
            target_price=99.25,
            max_minutes=120,
            signal_time=signal_time,
            is_short=True,
            intrabar_policy="STOP_FIRST",
        )

    def test_ordered_tp_then_sl_is_tp_first_not_ambiguous(self):
        result = self._short_path(tp_min=10, sl_min=60)
        assert result["tp_before_sl"] is True
        assert result["sl_before_tp"] is False
        assert result["ambiguous_intrabar"] is False
        assert derive_realized_path_class(
            ambiguous_intrabar=False,
            tp_before_sl=result["tp_before_sl"],
            sl_before_tp=result["sl_before_tp"],
        ) == "TP_FIRST"

    def test_ordered_sl_then_tp_is_sl_first_not_ambiguous(self):
        result = self._short_path(tp_min=60, sl_min=10)
        assert result["sl_before_tp"] is True
        assert result["tp_before_sl"] is False
        assert result["ambiguous_intrabar"] is False
        assert derive_realized_path_class(
            ambiguous_intrabar=False,
            tp_before_sl=result["tp_before_sl"],
            sl_before_tp=result["sl_before_tp"],
        ) == "SL_FIRST"


class TestTimeoutNormalization:
    def test_timeout_gross_r_uses_execution_r_and_short_sign(self):
        # return_at_120m = +2.0% means close is above entry for LONG semantics.
        # For SHORT, this is a loss of 2% of entry.
        gross_r = derive_timeout_gross_r(
            entry=100.0,
            invalidation_price=100.5,
            return_at_120m=2.0,
        )
        assert gross_r == -2.0

    def test_timeout_return_equivalence_to_direct_close(self):
        entry = 100.0
        invalidation = 100.5
        timeout_close = 102.0
        return_at_120m = (timeout_close - entry) / entry * 100.0
        direct = (entry - timeout_close) / derive_execution_r(entry, invalidation)
        derived = derive_timeout_gross_r(
            entry=entry,
            invalidation_price=invalidation,
            return_at_120m=return_at_120m,
            timeout_close=timeout_close,
        )
        assert derived == pytest.approx(direct)
        assert derive_timeout_close(entry, return_at_120m) == pytest.approx(timeout_close)


class TestCostAndNetR:
    def test_normal_and_elevated_cost_r(self):
        entry = 100.0
        invalidation = 100.5
        assert derive_cost_r(entry, invalidation, round_trip=NORMAL_ROUND_TRIP) == 0.21
        assert derive_cost_r(entry, invalidation, round_trip=ELEVATED_ROUND_TRIP) == 0.31

    def test_tp_and_sl_net_r(self):
        entry = 100.0
        invalidation = 100.5
        gross_tp = derive_gross_r(
            entry=entry, invalidation_price=invalidation, path_class="TP_FIRST",
        )
        gross_sl = derive_gross_r(
            entry=entry, invalidation_price=invalidation, path_class="SL_FIRST",
        )
        normal_cost = derive_cost_r(entry, invalidation, round_trip=NORMAL_ROUND_TRIP)
        elevated_cost = derive_cost_r(entry, invalidation, round_trip=ELEVATED_ROUND_TRIP)
        assert gross_tp - normal_cost == 0.54
        assert gross_tp - elevated_cost == 0.44
        assert gross_sl - normal_cost == -1.21
        assert gross_sl - elevated_cost == -1.31


class TestAnalysisClassificationAndMetrics:
    def _row(self, observation_id, *, ambiguous=False, tp=False, sl=False,
             return_120m=0.0):
        return {
            "observation_id": observation_id,
            "symbol": f"SYM{observation_id}",
            "signal_time": datetime(2026, 10, 7, tzinfo=timezone.utc),
            "day": "2026-10-07",
            "variant_entry": 100.0,
            "invalidation_price": 100.5,
            "outcome": {
                "ambiguous_intrabar": ambiguous,
                "tp_before_sl": tp,
                "sl_before_tp": sl,
                "return_at_120m": return_120m,
            },
        }

    def test_analysis_uses_first_hit_semantics_and_no_historical_shortcut(self):
        rows = [
            self._row(1, tp=True, sl=False, ambiguous=False),
            self._row(2, sl=True, tp=False, ambiguous=False),
            self._row(3, ambiguous=True),
            self._row(4, return_120m=1.0),
        ]
        result = analyze_observations(rows, bootstrap_n=100, seed=1)
        assert result["eligible_n"] == 4
        assert result["normal"]["tp_first"] == 1
        assert result["normal"]["sl_first"] == 1
        assert result["normal"]["ambiguous"] == 1
        assert result["normal"]["timeout"] == 1
        assert result["normal"]["gross_er"] is not None
        assert result["normal"]["bootstrap"]["ci_95_low"] is not None
        assert result["elevated"]["net_er"] is not None

    def test_forbidden_legacy_both_hit_ambiguous_classification(self):
        assert derive_realized_path_class(
            ambiguous_intrabar=False,
            tp_before_sl=True,
            sl_before_tp=False,
        ) == "TP_FIRST"
        assert derive_realized_path_class(
            ambiguous_intrabar=False,
            tp_before_sl=False,
            sl_before_tp=True,
        ) == "SL_FIRST"


class TestTerminalLifecycleRegression:
    def test_terminal_db_state_blocks_new_capture(self):
        result, cursor = _observe(lifecycle_rows=[("COMPLETED",)])
        assert result == []
        assert _insert_calls(cursor) == []

    def test_existing_terminal_protection_remains_in_shared_observer(self):
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert '{"CANCELLED", "COMPLETED", "CLOSED_NEGATIVE"}' in source
        assert "DELETE FROM research.prospective_observation" not in source
        assert "UPDATE research.prospective_observation" not in source


class TestExistingExperimentRegression:
    def test_existing_experiments_unchanged_by_new_branch(self):
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert "VC_SHORT_EXECUTION_V1" in source
        assert "SRR_OOS_SCANNER_V1_PROSPECTIVE" in source
        assert EXPERIMENT_ID in source

    def test_evaluator_frozen_window_and_stop_first_semantics_remain_shared(self):
        source = (PROJECT_ROOT / "app" / "research" / "prospective_evaluator.py").read_text()
        assert "_get_frozen_max_hold" in source
        assert "_get_frozen_intrabar_policy" in source
        assert "_check_tp_sl" in source
        assert "c.timestamp <= signal_ts" in source
        assert "c.timestamp >= cutoff_ts" in source
        assert 'intrabar_policy == "STOP_FIRST"' in source


class TestMigration:
    def test_registration_migration_is_inactive_and_idempotent(self):
        migration = (
            PROJECT_ROOT / "sql" / "migrations" /
            "060_srr_short_execution_r_expansion_prospective_registration.sql"
        ).read_text()
        assert "INSERT INTO research.prospective_experiment" in migration
        assert "'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1', 1," in migration
        assert "'READY_TO_START', NULL" in migration
        assert "ON CONFLICT (experiment_id) DO NOTHING" in migration
        assert "UPDATE research.prospective_experiment" not in migration
        assert "DELETE FROM research.prospective_experiment" not in migration
        assert "INSERT INTO research.prospective_observation" not in migration
        assert "INSERT INTO research.prospective_outcome" not in migration
        assert "DISCOVERY ONLY" in migration
