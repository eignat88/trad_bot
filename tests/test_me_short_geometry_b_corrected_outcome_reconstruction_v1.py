"""Test coverage for ME SHORT B historical semantics and DB snapshot sensitivity V1."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models import Candle
from app.research.prospective_evaluator import _check_tp_sl
from tools.research import me_short_geometry_b_corrected_outcome_reconstruction_v1 as analysis

FIX_TS = datetime(2026, 10, 7, 13, 4, 55, tzinfo=timezone.utc)


def _candle(offset_minutes: int, high: float, low: float, close: float, open_: float | None = None) -> Candle:
    timestamp = int((FIX_TS + timedelta(minutes=offset_minutes)).timestamp() * 1000)
    return Candle(timestamp=timestamp, open=open_ or high, high=high, low=low, close=close, volume=1.0)


def _observation(**overrides):
    row = {
        "experiment_id": analysis.B_EXPERIMENT,
        "source_signal_id": 1,
        "observation_id": 1,
        "symbol": "TESTUSDT",
        "direction": "SHORT",
        "signal_time": FIX_TS,
        "created_at": FIX_TS,
        "reference_price": 100.0,
        "invalidation_price": 100.2,
        "target_1": 95.0,
        "variant_entry": 100.0,
        "variant_stop": 102.0,
        "variant_target": 95.0,
        "score": 1.0,
        "rule_passed": True,
        "filter_reason": None,
        "market_regime": None,
        "features": {},
        "parameters": {},
    }
    row.update(overrides)
    return row


def _stored_outcome(**overrides):
    fields = {field: None for field in analysis.RELEVANT_FIELDS}
    fields.update({
        "tp_hit": False,
        "sl_hit": True,
        "tp_before_sl": False,
        "sl_before_tp": True,
        "ambiguous_intrabar": False,
        "time_to_tp": None,
        "time_to_sl": 30.0,
    })
    fields.update(overrides)
    return fields


def test_naive_exported_timestamp_is_utc():
    assert analysis._parse_ts("2026-10-04 22:55:00") == datetime(
        2026, 10, 4, 22, 55, 0, tzinfo=timezone.utc
    )


def test_known_klac_candle_does_not_shift_by_local_timezone():
    path = Path("audit_db/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007/candles.tsv")
    row = next(
        row for row in analysis._read_tsv(path)
        if row[2] == "KLACUSDT" and row[4] == "2026-10-04 22:55:00"
    )
    assert analysis._parse_ts(row[4]) == datetime(2026, 10, 4, 22, 55, tzinfo=timezone.utc)
    assert analysis._parse_ts(row[4]).timestamp() == 1791154500.0


def test_outcomes_tsv_exact_47_column_mapping():
    path = Path("audit_db/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007/outcomes.tsv")
    outcomes = analysis.load_outcomes(path)
    first = next(iter(outcomes.values()))
    assert len(first) == 47
    assert first["observation_id"] == int(analysis._read_tsv(path)[0][0])
    assert first["experiment_id"] == analysis._read_tsv(path)[0][1]
    assert first["is_final"] is True


def test_time_to_sl_index_increments_before_evaluated_15m_at():
    path = Path("audit_db/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007/outcomes.tsv")
    row = next(row for row in analysis._read_tsv(path) if row[38] not in {"", "\\N"})
    outcome = analysis.load_outcomes(path)[int(row[0])]
    assert outcome["time_to_sl"] == float(row[38])
    assert outcome["evaluated_15m_at"] == analysis._parse_ts(row[39])


def test_persisted_complete_variant_tuple():
    assert analysis.persisted_variant_tuple(_observation()) == (100.0, 102.0, 95.0)


def test_persisted_partial_variant_tuple():
    partial = _observation(variant_entry=99.0, variant_stop=None, variant_target=95.0)
    assert analysis.persisted_variant_tuple(partial) == (99.0, None, 95.0)


def test_complete_fixed_evaluator_tuple_uses_all_variant_fields_atomically():
    assert analysis.complete_variant_or_base_tuple(_observation()) == (100.0, 102.0, 95.0)


def test_partial_current_variant_tuple_falls_back_to_base():
    partial = _observation(variant_entry=99.0, variant_stop=None, variant_target=95.0)
    assert analysis.complete_variant_or_base_tuple(partial) == (100.0, 100.2, 95.0)


def test_h1_h2_comparison_orientation():
    assert analysis.HYPOTHESIS_H1 == "COMPLETE_PERSISTED_VARIANT"
    assert analysis.HYPOTHESIS_H2 == "VARIANT_ENTRY_PLUS_BASE_STOP_TARGET"
    assert analysis.entry_plus_base_hypothesis_tuple(_observation()) == (100.0, 100.2, 95.0)


def test_synthetic_h1_matches_stored_and_h2_does_not():
    candles = [_candle(30, 102.0, 101.0, 101.0)]
    outcome = analysis.capture_evaluation(_observation(), candles).updates
    comparison = analysis.compare_historical_hypotheses(_observation(), outcome, candles)
    assert comparison["H1"]["exact"] is True
    assert comparison["H2"]["exact"] is False
    assert comparison["preferred_hypothesis"] == analysis.HYPOTHESIS_H1


def test_strict_signal_cohort_boundary():
    assert analysis.classify_cohorts(
        [{"observation_id": 1, "signal_time": FIX_TS}], {}
    )["by_observation"][1] == "PRE_FIX_SIGNAL_PRE_FIX_EVALUATION"
    assert analysis.classify_cohorts(
        [{"observation_id": 2, "signal_time": FIX_TS + timedelta(microseconds=1)}], {}
    )["by_observation"][2] == "POST_FIX_SIGNAL_PRE_FIX_EVALUATION"


def test_horizon_specific_coverage_uses_60m_not_240m():
    candles = [_candle(1 + i * 5, 101.0, 100.5, 100.8) for i in range(12)]
    coverage = analysis.coverage_for("TESTUSDT", FIX_TS, candles)
    assert coverage["60m"] == analysis.EXPECTED_COVERAGE["60m"]
    assert coverage["240m"] < analysis.EXPECTED_COVERAGE["240m"]


def test_deterministic_bootstrap():
    first = analysis.bootstrap_pair_mean([1.0, -1.0, 0.5], iterations=100, seed=7)
    second = analysis.bootstrap_pair_mean([1.0, -1.0, 0.5], iterations=100, seed=7)
    assert first == second


def test_b_minus_a_delta_orientation():
    summary = analysis.paired_effect([{"B-A": 2.5}])
    assert summary["positive"] == 1
    assert analysis._summary([10.0 - 7.5])["positive"] == 1


def test_no_db_writes_from_analysis_module():
    source = Path(analysis.__file__).read_text(encoding="utf-8")
    forbidden = (
        "INSERT INTO", "UPDATE research.", "DELETE FROM research.",
        "psycopg2.connect", "pg8000.connect",
        "ResearchRepository(", "BybitClient(", ".get_klines(",
    )
    assert all(token not in source for token in forbidden)


def test_verdict_guard_db_snapshot_cannot_establish_execution():
    history = analysis.historical_semantics_validation(
        [{
            "preferred_hypothesis": analysis.HYPOTHESIS_H1,
            "h1_exact": True,
            "h2_exact": False,
            "h1_mismatch_count": 0,
            "h2_mismatch_count": 1,
        }],
        1,
    )
    sensitivity = {
        "delta_metrics": {
            "mfe_60m": {
                "n": 1,
                "bootstrap": {"ci_95": [0.1, 0.2]},
            }
        }
    }
    verdicts = analysis.build_final_verdicts(history, sensitivity)
    assert verdicts["executable_edge"] == "NOT_ESTABLISHED"
    assert verdicts["lifecycle_recommendation"] == "KEEP_BLOCKED"
    assert verdicts["production_outcomes_rewritten"] is False


def test_verdict_guard_path_nonreproducibility_forces_no_rewrite():
    with pytest.raises(AssertionError):
        analysis.build_final_verdicts(
            analysis.historical_semantics_validation([], 0),
            {"delta_metrics": {"mfe_60m": {"n": 0, "bootstrap": {"ci_95": [None, None]}}}},
            path_status="EXACT",
        )


def test_old_hypothesis_not_called_historical_actual():
    source = Path(analysis.__file__).read_text(encoding="utf-8")
    assert "historical_actual" not in source
    assert analysis.entry_plus_base_hypothesis_tuple(_observation())[1] == 100.2


def test_ambiguous_candle_policy_is_authoritative():
    result = _check_tp_sl(
        [_candle(5, 111.0, 89.0, 100.0)],
        100.0, 90.0, 110.0, 240, FIX_TS, False, "STOP_FIRST",
    )
    assert result["ambiguous_intrabar"] is True
    assert result["sl_before_tp"] is True
    assert result["tp_before_sl"] is False


def test_capture_evaluation_preserves_corrected_parameter_mapping():
    captured = analysis.capture_evaluation(_observation(), [_candle(5, 105.0, 95.0, 100.0)])
    assert captured.updates["observation_id"] == 1
    assert captured.updates["experiment_id"] == analysis.B_EXPERIMENT
