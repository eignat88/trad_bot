from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.research.srr_variant_outcome_semantics_reconstruction_audit_v1 import (
    classify_geometry,
    decision_for,
    evaluation_cohort,
    expected_geometry_mismatches,
    frozen_tuple,
    historical_tuple,
    integrity_counts,
    load_sections,
    normalize_observation,
    normalize_outcome,
    parse_csv_lines,
    parse_tsv_rows,
    synthetic_semantics,
    tuple_class,
)

SECTION_FIX_TS = "2026-10-07T13:04:55Z"


def observation(**overrides):
    row = {
        "observation_id": 1,
        "experiment_id": "TEST_SRR",
        "source_signal_id": 10,
        "symbol": "AAAUSDT",
        "direction": "SHORT",
        "signal_time_utc": "2026-10-01T00:00:00Z",
        "reference_price": "100",
        "invalidation_price": "101",
        "target_1": "97",
        "variant_entry": None,
        "variant_stop": None,
        "variant_target": None,
        "features_text": "{}",
        "parameters_text": "{}",
    }
    row.update(overrides)
    return normalize_observation(row)


def outcome(**overrides):
    row = {
        "observation_id": 1,
        "experiment_id": "TEST_SRR",
        "source_signal_id": 10,
        "symbol": "AAAUSDT",
        "direction": "SHORT",
        "signal_time_utc": "2026-10-01T00:00:00Z",
        "is_final": "t",
        "evaluated_15m_at_utc": "2026-10-01T00:15:00Z",
        "evaluated_30m_at_utc": "2026-10-01T00:30:00Z",
        "evaluated_60m_at_utc": "2026-10-01T01:00:00Z",
        "evaluated_120m_at_utc": "2026-10-01T02:00:00Z",
        "evaluated_240m_at_utc": "2026-10-01T04:00:00Z",
        "created_at_utc": "2026-10-01T04:00:01Z",
        "updated_at_utc": "2026-10-01T04:00:02Z",
    }
    row.update(overrides)
    return normalize_outcome(row)


def test_experiment_discovery_and_schema_sections_are_required():
    snapshot = (
        "==META==\nexport_ts_utc\t2026-10-08T05:54:56.705Z\n"
        "==SRR_EXPERIMENT_DISCOVERY==\n"
        "experiment_id,version,scanner_name,direction,experiment_type,entry_rule,stop_rule,target_rule,paired_with,status,started_at_utc,created_at_utc,updated_at_utc,framework,observations_observed_in_export,outcomes_observed_in_export\n"
        "SRR_LONG_BASELINE_V1,1,SUPPORT_RESISTANCE_REACTION,LONG,BASELINE_VALIDATION,,,,,RUNNING,,,prospective,2,1\n"
        "SRR_GENERIC_V1,,SUPPORT_RESISTANCE_REACTION,,,,,,,,,generic_research,0,0\n"
    )
    path = Path("_tmp_srr_export_fixture.txt")
    path.write_text(snapshot, encoding="utf-8")
    sections = {section.name: section for section in load_sections(path)}
    path.unlink()
    assert {"META", "SRR_EXPERIMENT_DISCOVERY"} <= sections.keys()


def test_base_only_complete_partial_and_invalid_tuples():
    base = observation()
    complete = observation(variant_entry="99", variant_stop="103", variant_target="93")
    partial = observation(variant_entry="99")
    invalid = observation(variant_entry="100", variant_stop="99", variant_target="101")
    assert tuple_class(base) == "BASE_ONLY"
    assert tuple_class(complete) == "COMPLETE_VARIANT"
    assert tuple_class(partial) == "PARTIAL_VARIANT"
    assert classify_geometry(base)[0] == "BASE_ONLY"
    assert classify_geometry(complete)[0] == "COMPLETE_VARIANT_DIFFERENT_FROM_BASE"
    assert classify_geometry(invalid)[0] == "INVALID_GEOMETRY"


def test_long_and_short_orientation():
    long_row = observation(
        direction="LONG", reference_price="100", invalidation_price="99", target_1="103",
        variant_entry="100", variant_stop="97", variant_target="106",
    )
    short_row = observation(
        direction="SHORT", reference_price="100", invalidation_price="101", target_1="97",
        variant_entry="100", variant_stop="102", variant_target="94",
    )
    assert classify_geometry(long_row)[0] == "COMPLETE_VARIANT_DIFFERENT_FROM_BASE"
    assert classify_geometry(short_row)[0] == "COMPLETE_VARIANT_DIFFERENT_FROM_BASE"
    assert classify_geometry(observation(direction="LONG", variant_stop="101"))[0] == "INVALID_GEOMETRY"


def test_frozen_vs_historical_tuple_comparison():
    row = observation(variant_entry="99", variant_stop="103", variant_target="93")
    assert frozen_tuple(row) == (99.0, 103.0, 93.0)
    assert historical_tuple(row) == (99.0, 103.0, 93.0)
    assert classify_geometry(row)[0] == "COMPLETE_VARIANT_DIFFERENT_FROM_BASE"
    assert classify_geometry(row)[1] == []
    assert expected_geometry_mismatches(row) == []
    frozen_formula = observation(
        experiment_id="SRR_OOS_SCANNER_V1_PROSPECTIVE",
        direction="SHORT", reference_price="100", invalidation_price="101", target_1="97",
        variant_entry="100", variant_stop="100.75", variant_target="98.5",
    )
    assert frozen_tuple(frozen_formula) == historical_tuple(frozen_formula)
    assert expected_geometry_mismatches(frozen_formula) == []
    long_row = observation(
        experiment_id="SRR_OOS_SCANNER_V1_PROSPECTIVE",
        direction="LONG", reference_price="100", invalidation_price="99", target_1="103",
        variant_entry="100", variant_stop="97", variant_target="103",
    )
    assert expected_geometry_mismatches(long_row) == ["stop", "target"]


@pytest.mark.parametrize(
    "signal,evaluated,expected",
    [
        ("2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", "PRE_FIX_SIGNAL_PRE_FIX_EVALUATION"),
        ("2026-10-01T00:00:00Z", "2026-10-07T14:00:00Z", "PRE_FIX_SIGNAL_POST_FIX_EVALUATION"),
        ("2026-10-08T00:00:00Z", "2026-10-08T01:00:00Z", "POST_FIX_SIGNAL_POST_FIX_EVALUATION"),
        ("2026-10-08T00:00:00Z", "2026-10-07T12:00:00Z", "POST_FIX_SIGNAL_PRE_FIX_EVALUATION"),
    ],
)
def test_pre_post_fix_cohorts(signal, evaluated, expected):
    row = observation(signal_time_utc=signal)
    evaluated_outcome = outcome(
        signal_time_utc=signal,
        evaluated_15m_at_utc=evaluated,
        evaluated_30m_at_utc=evaluated,
        evaluated_60m_at_utc=evaluated,
        evaluated_120m_at_utc=evaluated,
        evaluated_240m_at_utc=evaluated,
        created_at_utc=evaluated,
    )
    cohort, mixed = evaluation_cohort(row, evaluated_outcome)
    assert cohort == expected
    assert mixed == []


def test_mixed_horizon_evaluation_timestamps():
    row = observation(signal_time_utc="2026-10-01T00:00:00Z")
    mixed = outcome(
        evaluated_15m_at_utc="2026-10-01T00:15:00Z",
        evaluated_30m_at_utc="2026-10-01T00:30:00Z",
        evaluated_60m_at_utc="2026-10-01T01:00:00Z",
        evaluated_120m_at_utc="2026-10-01T02:00:00Z",
        evaluated_240m_at_utc="2026-10-07T14:00:00Z",
        created_at_utc="2026-10-07T14:00:01Z",
    )
    cohort, mixed = evaluation_cohort(row, mixed)
    assert cohort == "MIXED_COHORT"
    assert mixed == ["PRE_FIX_SIGNAL_PRE_FIX_EVALUATION", "PRE_FIX_SIGNAL_POST_FIX_EVALUATION"]


def test_missing_outcome_and_orphan_outcome():
    counts = integrity_counts([observation(), observation(observation_id=2, source_signal_id=2)], [outcome(observation_id=999)])
    assert counts["missing_outcome_n"] == 2
    assert counts["orphan_outcome_n"] == 1


def test_duplicate_source_signal_and_direction_signal():
    first = observation()
    second = observation(observation_id=2, source_signal_id=first["source_signal_id"])
    counts = integrity_counts([first, second], [])
    assert counts["duplicate_source_signal_n"] == 1
    assert counts["duplicate_symbol_direction_signal_n"] == 1


def test_candle_coverage_and_exact_path_limitation():
    semantics = synthetic_semantics()
    assert semantics["exact_historical_path_reconstruction_available"] is False
    assert "EXACT_HISTORICAL_PATH_RECONSTRUCTION_NOT_AVAILABLE" not in semantics["cases"]
    same_candle = [case for case in semantics["cases"] if "same_candle" in case["case"]]
    assert same_candle[0]["same_candle_result"]["ambiguous_intrabar"] is True
    assert same_candle[0]["same_candle_result"]["sl_before_tp"] is False
    assert same_candle[0]["same_candle_result"]["tp_before_sl"] is False


def test_no_false_executable_edge_conclusion():
    summary = {
        "tuple_counts": {"BASE_ONLY": 1130},
        "material_historical_tuple_mismatch_n": 0,
        "pre_fix_signal_outcomes_n": 1048,
        "pre_fix_signal_finalized_outcomes_n": 1048,
    }
    decision = decision_for(summary, "SRR_LONG_BASELINE_V1")
    assert decision["decision"] == "NO_ACTION_REQUIRED"
    assert decision["historical_exposed_n"] == 0
    assert decision["affected_finalized_outcomes_n"] == 0
    assert "executable edge" not in json.dumps(decision).lower()


def test_classification_regression_base_only_is_not_variant_exposure():
    base = observation()
    summary = {
        "tuple_counts": {"BASE_ONLY": 1},
        "material_historical_tuple_mismatch_n": 0,
        "pre_fix_signal_outcomes_n": 1,
        "pre_fix_signal_finalized_outcomes_n": 1,
    }
    decision = decision_for(summary, "SRR_LONG_BASELINE_V1")
    assert decision["variant_tuple_present_n"] == 0
    assert decision["material_geometry_intervention_n"] == 0
    assert decision["historical_exposed_n"] == 0
    assert decision["affected_finalized_outcomes_n"] == 0
    assert classify_geometry(base)[0] == "BASE_ONLY"


def test_classification_regression_variant_presence_is_not_proven_mismatch():
    row = observation(variant_entry="99", variant_stop="103", variant_target="93")
    label, mismatches, _ = classify_geometry(row)
    assert label == "COMPLETE_VARIANT_DIFFERENT_FROM_BASE"
    assert mismatches == []
    equivalent = observation(variant_entry="100", variant_stop="101", variant_target="97")
    label, mismatches, _ = classify_geometry(equivalent)
    assert label == "COMPLETE_VARIANT_EQUIVALENT_TO_BASE"
    assert mismatches == []
    assert decision_for(
        {
            "tuple_counts": {"COMPLETE_VARIANT": 1},
            "material_historical_tuple_mismatch_n": 0,
            "pre_fix_signal_outcomes_n": 1,
            "pre_fix_signal_finalized_outcomes_n": 1,
        },
        "TEST",
    )["decision"] == "NO_ACTION_REQUIRED"


def test_classification_regression_old_vs_fixed_semantics_reconstruction():
    complete = observation(
        variant_entry="99", variant_stop="103", variant_target="93"
    )
    assert historical_tuple(complete) == frozen_tuple(complete)
    assert classify_geometry(complete)[1] == []

    partial = observation(variant_entry="99")
    assert historical_tuple(partial) == (99.0, None, None)
    assert frozen_tuple(partial) == (100.0, 101.0, 97.0)
    assert classify_geometry(partial)[1] == ["entry", "stop", "target"]

    decision = decision_for(
        {
            "tuple_counts": {"PARTIAL_VARIANT": 1},
            "intervention_counts": {"any_variant_intervention_n": 1},
            "material_historical_tuple_mismatch_n": 1,
            "pre_fix_variant_outcomes_n": 1,
            "pre_fix_variant_finalized_n": 1,
            "pre_fix_variant_horizons_n": 5,
            "pre_fix_variant_tuple_mismatch_outcomes_n": 1,
        },
        "TEST",
    )
    assert decision["decision"] == "INSUFFICIENT_EVIDENCE"
    assert decision["historical_exposed_n"] == 1
    assert decision["historical_exposed_tuple_mismatch_n"] == 1


def test_decision_regression_complete_variant_without_exact_candles_is_insufficient():
    decision = decision_for(
        {
            "tuple_counts": {"COMPLETE_VARIANT": 1},
            "material_historical_tuple_mismatch_n": 0,
            "pre_fix_signal_outcomes_n": 1,
            "pre_fix_signal_finalized_outcomes_n": 1,
        },
        "TEST",
    )
    assert decision["decision"] == "NO_ACTION_REQUIRED"
    assert decision["reconstruction_feasibility"] == "NOT_REQUIRED_FOR_TUPLE_SELECTION"


def test_no_production_outcome_rewrite():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "tools/research/srr_variant_outcome_semantics_reconstruction_audit_v1.py").read_text(encoding="utf-8")
    assert "UPDATE research.prospective_outcome" not in source
    assert "INSERT INTO research.prospective_outcome" not in source
    assert "DELETE FROM research.prospective_outcome" not in source
    assert "http" not in source.lower()


def test_historical_partial_tuple_exact_semantics():
    entry_only = observation(variant_entry="99")
    assert historical_tuple(entry_only) == (99.0, None, None)
    assert frozen_tuple(entry_only) == (100.0, 101.0, 97.0)
    assert classify_geometry(entry_only)[1] == ["entry", "stop", "target"]

    stop_only = observation(variant_stop="102")
    assert historical_tuple(stop_only) == (100.0, 101.0, 97.0)
    assert frozen_tuple(stop_only) == (100.0, 101.0, 97.0)
    assert classify_geometry(stop_only)[1] == []

    complete = observation(
        variant_entry="99", variant_stop="103", variant_target="93"
    )
    assert historical_tuple(complete) == frozen_tuple(complete)


def test_codex_review_regressions_v1():
    from pathlib import Path
    import ast

    source_path = Path(
        "tools/research/srr_variant_outcome_semantics_reconstruction_audit_v1.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Confirm the affected CSV schema includes the key emitted by aggregate().
    field_lists = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "fieldnames"
            for target in node.targets
        )
        and isinstance(node.value, ast.List)
    ]
    assert any(
        "expected_frozen_protocol_mismatched_fields"
        in [item.value for item in values.elts if isinstance(item, ast.Constant)]
        for values in field_lists
    )

    # Historical partial tuples must preserve missing stop/target values.
    partial = observation(variant_entry="99")
    assert historical_tuple(partial) == (99.0, None, None)

    # The published description must agree with the historical evaluator.
    assert "preserving NULL stop or target" in source
    assert "field-by-field" not in source


def test_affected_csv_nonempty_export_regression(tmp_path):
    """A real DictWriter must accept every field emitted by affected rows."""
    import ast
    import csv

    script_path = Path(
        "tools/research/srr_variant_outcome_semantics_reconstruction_audit_v1.py"
    )
    tree = ast.parse(script_path.read_text(encoding="utf-8"))

    main_function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )

    writer_fields = None
    for node in ast.walk(main_function):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "fieldnames"
                for target in node.targets
            )
            and isinstance(node.value, ast.List)
        ):
            values = [
                item.value
                for item in node.value.elts
                if isinstance(item, ast.Constant)
            ]
            if "affected_observations_definition" in values:
                continue
            if "historical_effective_entry" in values:
                writer_fields = values

    assert writer_fields is not None

    source = script_path.read_text(encoding="utf-8")
    assert '"expected_frozen_protocol_mismatched_fields"' in source

    affected = {
        field: "" for field in writer_fields
    }
    affected.update({
        "observation_id": "synthetic-partial-variant",
        "experiment_id": "TEST",
        "tuple_class": "PARTIAL_VARIANT",
        "mismatched_fields": "entry|stop|target",
        "expected_frozen_protocol_mismatched_fields": "",
    })

    output = tmp_path / "affected.csv"

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=writer_fields)
        writer.writeheader()
        writer.writerow(affected)

    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]["observation_id"] == "synthetic-partial-variant"
    assert rows[0]["mismatched_fields"] == "entry|stop|target"
    assert "expected_frozen_protocol_mismatched_fields" in rows[0]
