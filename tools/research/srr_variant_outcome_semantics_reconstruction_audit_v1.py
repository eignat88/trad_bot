"""Deterministic SRR variant/outcome semantics reconstruction audit.

This is an offline analysis over a saved READ-ONLY production export. It does
not connect to a database, fetch candles, rewrite outcomes, or make trading
decisions. Its purpose is to answer whether historical SRR outcomes were
computed from the complete persisted variant tuple or from the pre-fix hybrid
tuple (variant entry plus base stop/target).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

FIX_DEPLOY_TS = datetime(2026, 10, 7, 13, 4, 55, tzinfo=timezone.utc)
FIX_COMMIT = "38e6ef197dba9b648f22fc56952b295919e61063"
FIX_MERGE_COMMIT = "80b10108cdceaf133cc405c0bd725cabc6aac459"
LOCAL_GIT_HEAD = "93a060f9eba309c80a76b3fb1713a4f95e838376"
VPS_GIT_HEAD = "93a060f9eba309c80a76b3fb1713a4f95e838376"
SRR_OBSERVATION_HEADER = (
    "observation_id,experiment_id,source_signal_id,source_observation_id,symbol,"
    "direction,signal_time_utc,reference_price,invalidation_price,target_1,target_2,"
    "variant_entry,variant_stop,variant_target,score,rule_passed,filter_reason,"
    "gate_result,features_text,parameters_text,market_regime,created_at_utc"
)
SRR_OUTCOME_HEADER = (
    "observation_id,experiment_id,source_signal_id,symbol,direction,signal_time_utc,"
    "mfe_15m,mae_15m,mfe_30m,mae_30m,mfe_60m,mae_60m,mfe_120m,mae_120m,"
    "mfe_240m,mae_240m,mfe_r_15m,mae_r_15m,mfe_r_30m,mae_r_30m,mfe_r_60m,"
    "mae_r_60m,mfe_r_120m,mae_r_120m,mfe_r_240m,mae_r_240m,return_at_15m,"
    "return_at_30m,return_at_60m,return_at_120m,return_at_240m,tp_hit,sl_hit,"
    "tp_before_sl,sl_before_tp,ambiguous_intrabar,time_to_tp,time_to_sl,"
    "evaluated_15m_at_utc,evaluated_30m_at_utc,evaluated_60m_at_utc,"
    "evaluated_120m_at_utc,evaluated_240m_at_utc,is_final,created_at_utc,updated_at_utc"
)
NUMERIC_OBS_COLUMNS = (
    "reference_price", "invalidation_price", "target_1", "target_2",
    "variant_entry", "variant_stop", "variant_target", "score",
)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", "\\N", "NULL", "None"):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _number(value: Any) -> float | None:
    parsed = _decimal(value)
    return float(parsed) if parsed is not None else None


def _bool(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"t", "true", "1", "yes"}:
        return True
    if text in {"f", "false", "0", "no"}:
        return False
    return None


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sort_key(value: Any) -> tuple[str, str]:
    return (str(value), "")


@dataclass(frozen=True)
class Section:
    name: str
    lines: list[str]


def load_sections(path: Path) -> list[Section]:
    sections: list[Section] = []
    current_name: str | None = None
    current: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("==") and line.endswith("=="):
            if current_name is not None:
                sections.append(Section(current_name, current))
            current_name = line.strip("=")
            current = []
        elif current_name is not None:
            current.append(line)
    if current_name is not None:
        sections.append(Section(current_name, current))
    return sections


def section_map(sections: Iterable[Section]) -> dict[str, Section]:
    return {section.name: section for section in sections}


def parse_csv_lines(
    lines: Iterable[str], expected_header: str | None = None, expected_width: int | None = None,
) -> list[dict[str, Any]]:
    text = "\n".join(line for line in lines if line.strip())
    if not text:
        return []
    parsed_rows = list(csv.reader(text.splitlines()))
    if len(parsed_rows[0]) < 2 or expected_header is not None and parsed_rows[0] != expected_header.split(","):
        if expected_header is not None:
            raise ValueError(
                "Malformed export section header: expected {!r}, got {!r}".format(
                    expected_header, parsed_rows[0],
                )
            )
        raise ValueError("Malformed headerless TSV export section")
    if expected_width is None:
        expected_width = len(parsed_rows[0])
    normalized_rows: list[dict[str, Any]] = []
    for index, values in enumerate(parsed_rows[1:], start=2):
        if len(values) != expected_width:
            raise ValueError(
                "Malformed row {} width: expected {}, got {}".format(index, expected_width, len(values))
            )
        if expected_header is None:
            normalized_rows.append(dict(zip([f"col_{position}" for position in range(expected_width)], values)))
        else:
            normalized_rows.append(dict(zip(expected_header.split(","), values)))
    return normalized_rows


def parse_tsv_rows(lines: Iterable[str], names: list[str]) -> list[dict[str, Any]]:
    expected_width = len(names)
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != expected_width:
            raise ValueError(
                "Malformed row {} width: expected {}, got {}".format(index, expected_width, len(parts))
            )
        rows.append(dict(zip(names, parts)))
    return rows


def normalize_observation(row: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, Any] = dict(row)
    normalized["observation_id"] = int(row["observation_id"])
    normalized["source_signal_id"] = int(row["source_signal_id"])
    normalized["source_observation_id"] = (
        int(row["source_observation_id"]) if row.get("source_observation_id") else None
    )
    for column in NUMERIC_OBS_COLUMNS:
        normalized[column] = _number(row.get(column))
    normalized["signal_dt"] = _ts(row.get("signal_time_utc"))
    normalized["created_dt"] = _ts(row.get("created_at_utc"))
    normalized["rule_passed"] = _bool(row.get("rule_passed"))
    try:
        normalized["features"] = json.loads(row.get("features_text") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Malformed features JSON for observation_id={}".format(row["observation_id"])
        ) from exc
    try:
        normalized["parameters"] = json.loads(row.get("parameters_text") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Malformed parameters JSON for observation_id={}".format(row["observation_id"])
        ) from exc
    return normalized


def normalize_outcome(row: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, Any] = dict(row)
    normalized["observation_id"] = int(row["observation_id"])
    normalized["source_signal_id"] = int(row["source_signal_id"])
    normalized["signal_dt"] = _ts(row.get("signal_time_utc"))
    for column, value in list(normalized.items()):
        if column.endswith("_at") or column == "signal_time_utc":
            continue
        if column in {"observation_id", "source_signal_id", "experiment_id", "symbol", "direction"}:
            continue
        normalized[column] = _number(value)
    for column in ("tp_hit", "sl_hit", "tp_before_sl", "sl_before_tp", "ambiguous_intrabar", "is_final"):
        normalized[column] = _bool(row.get(column))
    for column in ("created_at_utc", "updated_at_utc"):
        normalized[column] = _ts(row.get(column))
    for horizon in ("15m", "30m", "60m", "120m", "240m"):
        normalized[f"evaluated_{horizon}_dt"] = _ts(row.get(f"evaluated_{horizon}_at_utc"))
    return normalized


def tolerance_for_price(price: float | None) -> float:
    if price is None or price <= 0:
        return 1e-12
    return max(1e-12, abs(price) * 1e-12)


def close_price(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return abs(left - right) <= tolerance_for_price(left)


def tuple_class(observation: dict[str, Any]) -> str:
    fields = [observation[column] for column in ("variant_entry", "variant_stop", "variant_target")]
    present = sum(value is not None for value in fields)
    if present == 0:
        return "BASE_ONLY"
    if present == 3:
        return "COMPLETE_VARIANT"
    return "PARTIAL_VARIANT"


def orientation_valid(observation: dict[str, Any], entry: float | None, stop: float | None, target: float | None) -> bool:
    if entry is None or stop is None:
        return False
    if entry <= 0:
        return False
    if observation["direction"] == "SHORT":
        return stop > entry and (target is None or target < entry)
    if observation["direction"] == "LONG":
        return stop < entry and (target is None or target > entry)
    return False


def frozen_tuple(observation: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    if tuple_class(observation) == "COMPLETE_VARIANT":
        return (
            observation["variant_entry"],
            observation["variant_stop"],
            observation["variant_target"],
        )
    return (
        observation["reference_price"],
        observation["invalidation_price"],
        observation["target_1"],
    )


def _old_evaluator_tuple(observation: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """Exact pre-fix evaluator field selection from Git history."""
    if observation["variant_entry"] is not None:
        return (
            observation["variant_entry"],
            observation["variant_stop"],
            observation["variant_target"],
        )
    return (
        observation["reference_price"],
        observation["invalidation_price"],
        observation["target_1"],
    )


def historical_tuple(observation: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """Pre-fix evaluator tuple."""
    return _old_evaluator_tuple(observation)


def frozen_variant_formula(observation: dict[str, Any]) -> tuple[float | None, float | None, float | None] | None:
    """Frozen SRR intervention formula; None when no intervention formula applies."""
    if tuple_class(observation) != "COMPLETE_VARIANT":
        return None
    entry = observation["reference_price"]
    invalidation = observation["invalidation_price"]
    if entry is None or invalidation is None or entry <= 0:
        return None
    structural_r = abs(entry - invalidation)
    if structural_r <= 0:
        return None
    if observation["experiment_id"] == "SRR_OOS_SCANNER_V1_PROSPECTIVE":
        if observation["direction"] == "SHORT":
            return entry, entry + 0.75 * structural_r, entry - 1.50 * structural_r
        return entry, entry - 0.75 * structural_r, entry + 1.50 * structural_r
    if observation["experiment_id"] == "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1":
        return entry, entry + 2.00 * structural_r, entry - 1.50 * structural_r
    return None


def classify_geometry(observation: dict[str, Any]) -> tuple[str, list[str], dict[str, bool]]:
    kind = tuple_class(observation)
    frozen = frozen_tuple(observation)
    historical = historical_tuple(observation)

    mismatches = [
        name
        for name, frozen_value, historical_value in zip(
            ("entry", "stop", "target"), frozen, historical
        )
        if not close_price(frozen_value, historical_value)
    ]

    if kind == "BASE_ONLY":
        return "BASE_ONLY", mismatches, {
            "entry": False, "stop": False, "target": False
        }

    interventions = {
        "entry": (
            observation["variant_entry"] is not None
            and not close_price(observation["variant_entry"], observation["reference_price"])
        ),
        "stop": (
            observation["variant_stop"] is not None
            and not close_price(observation["variant_stop"], observation["invalidation_price"])
        ),
        "target": (
            observation["variant_target"] is not None
            and not close_price(observation["variant_target"], observation["target_1"])
        ),
    }

    if not orientation_valid(observation, *frozen):
        label = "INVALID_GEOMETRY"
    elif kind == "PARTIAL_VARIANT":
        label = "PARTIAL_VARIANT"
    elif any(interventions.values()):
        label = "COMPLETE_VARIANT_DIFFERENT_FROM_BASE"
    else:
        label = "COMPLETE_VARIANT_EQUIVALENT_TO_BASE"

    return label, mismatches, interventions


def evaluation_cohort(observation: dict[str, Any], outcome: dict[str, Any] | None) -> tuple[str, list[str]]:
    if outcome is None:
        return "NO_OUTCOME", []
    signal_time = observation["signal_dt"]
    post_signal_fields = [
        outcome[f"evaluated_{horizon}_dt"]
        for horizon in ("15m", "30m", "60m", "120m", "240m")
        if outcome[f"evaluated_{horizon}_dt"] is not None
    ]
    if not post_signal_fields:
        return "UNKNOWN_EVALUATION_BOUNDARY", []
    signal_pre_fix = signal_time is not None and signal_time < FIX_DEPLOY_TS
    pre_fix_evaluations = sum(timestamp < FIX_DEPLOY_TS for timestamp in post_signal_fields)
    post_fix_evaluations = sum(timestamp >= FIX_DEPLOY_TS for timestamp in post_signal_fields)
    cohorts: list[str] = []
    if signal_pre_fix and pre_fix_evaluations:
        cohorts.append("PRE_FIX_SIGNAL_PRE_FIX_EVALUATION")
    if signal_pre_fix and post_fix_evaluations:
        cohorts.append("PRE_FIX_SIGNAL_POST_FIX_EVALUATION")
    if not signal_pre_fix and post_fix_evaluations:
        cohorts.append("POST_FIX_SIGNAL_POST_FIX_EVALUATION")
    if not signal_pre_fix and pre_fix_evaluations:
        cohorts.append("POST_FIX_SIGNAL_PRE_FIX_EVALUATION")
    if not cohorts:
        return "UNKNOWN_EVALUATION_BOUNDARY", []
    if len(cohorts) == 1:
        return cohorts[0], []
    return "MIXED_COHORT", cohorts


def horizon_cohorts(outcome: dict[str, Any]) -> list[str]:
    cohorts: list[str] = []
    for horizon in ("15m", "30m", "60m", "120m", "240m"):
        timestamp = outcome[f"evaluated_{horizon}_dt"]
        if timestamp is None:
            cohorts.append(f"{horizon}:NOT_EVALUATED")
        elif timestamp < FIX_DEPLOY_TS:
            cohorts.append(f"{horizon}:PRE_FIX")
        else:
            cohorts.append(f"{horizon}:POST_FIX")
    return cohorts


def integrity_counts(observations: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    observation_by_id = {row["observation_id"]: row for row in observations}
    outcome_by_id = {row["observation_id"]: row for row in outcomes}
    observation_ids = [row["observation_id"] for row in observations]
    outcome_ids = [row["observation_id"] for row in outcomes]
    source_ids = Counter(row["source_signal_id"] for row in observations)
    symbol_signal = Counter((row["symbol"], row["direction"], _iso(row["signal_dt"])) for row in observations)
    return {
        "observations_n": len(observations),
        "outcomes_n": len(outcomes),
        "finalized_n": sum(row["is_final"] is True for row in outcomes),
        "missing_outcome_n": sum(row["observation_id"] not in outcome_by_id for row in observations),
        "orphan_outcome_n": sum(row["observation_id"] not in observation_by_id for row in outcomes),
        "duplicate_observation_id_n": sum(count - 1 for count in Counter(observation_ids).values() if count > 1),
        "duplicate_outcome_id_n": sum(count - 1 for count in Counter(outcome_ids).values() if count > 1),
        "duplicate_source_signal_n": sum(count - 1 for count in source_ids.values() if count > 1),
        "duplicate_symbol_direction_signal_n": sum(count - 1 for count in symbol_signal.values() if count > 1),
        "directions": dict(sorted(Counter(row["direction"] for row in observations).items())),
    }


def _expected_variant_geometry(observation: dict[str, Any]) -> tuple[float | None, float | None, float | None] | None:
    if tuple_class(observation) != "COMPLETE_VARIANT":
        return None
    entry = observation["reference_price"]
    invalidation = observation["invalidation_price"]
    if entry is None or invalidation is None or entry <= 0:
        return None
    structural_r = abs(entry - invalidation)
    if structural_r <= 0:
        return None
    if observation["experiment_id"] == "SRR_OOS_SCANNER_V1_PROSPECTIVE":
        if observation["direction"] == "SHORT":
            return entry, entry + 0.75 * structural_r, entry - 1.50 * structural_r
        return entry, entry - 0.75 * structural_r, entry + 1.50 * structural_r
    if observation["experiment_id"] == "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1":
        return entry, entry + 2.00 * structural_r, entry - 1.50 * structural_r
    return None


def expected_geometry_mismatches(observation: dict[str, Any]) -> list[str]:
    expected = frozen_variant_formula(observation)
    if expected is None:
        return []
    historical = historical_tuple(observation)
    return [
        name
        for name, expected_value, historical_value in zip(
            ("entry", "stop", "target"), expected, historical,
        )
        if not close_price(expected_value, historical_value)
    ]


def aggregate(observations: list[dict[str, Any]], outcomes: list[dict[str, Any]], experiment_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outcome_by_id = {row["observation_id"]: row for row in outcomes}
    geometry_counts: Counter[str] = Counter()
    tuple_counts: Counter[str] = Counter()
    cohort_counts: Counter[str] = Counter()
    intervention_counts = Counter()
    affected_rows: list[dict[str, Any]] = []
    horizon_counts = Counter()
    per_direction: dict[str, Counter[str]] = defaultdict(Counter)
    pre_fix_observations = 0
    pre_fix_outcomes = 0
    unknown_cohort_n = 0
    pre_fix_finalized_n = 0
    material_mismatch_n = 0
    expected_protocol_mismatch_n = 0
    pre_fix_evaluated_horizons: Counter[str] = Counter()
    post_fix_evaluated_horizons: Counter[str] = Counter()
    final_outcome_evaluation_boundary = Counter()
    pre_fix_variant_outcomes_n = 0
    pre_fix_variant_finalized_n = 0
    pre_fix_variant_horizons_n = 0
    pre_fix_variant_tuple_mismatch_outcomes_n = 0

    for observation in sorted(observations, key=lambda row: (row["source_signal_id"], row["symbol"], _iso(row["signal_dt"]), row["observation_id"])):
        label, mismatches, interventions = classify_geometry(observation)
        expected_mismatches = expected_geometry_mismatches(observation)
        outcome = outcome_by_id.get(observation["observation_id"])
        cohort, mixed = evaluation_cohort(observation, outcome)
        kind = tuple_class(observation)
        geometry_counts[label] += 1
        tuple_counts[kind] += 1
        cohort_counts[cohort] += 1
        per_direction[observation["direction"]]["observations"] += 1
        per_direction[observation["direction"]][kind] += 1
        if mismatches:
            material_mismatch_n += 1
            per_direction[observation["direction"]]["material_variant_tuple_mismatch"] += 1
        if expected_mismatches:
            expected_protocol_mismatch_n += 1
        for field, active in interventions.items():
            if active:
                intervention_counts[f"variant_{field}_intervention_n"] += 1
                per_direction[observation["direction"]][f"variant_{field}_intervention"] += 1
        if any(interventions.values()):
            intervention_counts["any_variant_intervention_n"] += 1
        signal_pre_fix = observation["signal_dt"] is not None and observation["signal_dt"] < FIX_DEPLOY_TS
        if signal_pre_fix:
            pre_fix_observations += 1
            if outcome is not None:
                pre_fix_outcomes += 1
                if outcome["is_final"] is True:
                    pre_fix_finalized_n += 1
        if outcome is not None:
            final_evaluation_timestamps = [
                outcome[f"evaluated_{horizon}_dt"]
                for horizon in ("15m", "30m", "60m", "120m", "240m")
                if outcome[f"evaluated_{horizon}_dt"] is not None
            ]
            for horizon in ("15m", "30m", "60m", "120m", "240m"):
                timestamp = outcome[f"evaluated_{horizon}_dt"]
                if timestamp is None:
                    continue
                target = pre_fix_evaluated_horizons if timestamp < FIX_DEPLOY_TS else post_fix_evaluated_horizons
                target[horizon] += 1
            if outcome["is_final"] is True and final_evaluation_timestamps:
                latest = max(final_evaluation_timestamps)
                final_outcome_evaluation_boundary["PRE_FIX"] += latest < FIX_DEPLOY_TS
                final_outcome_evaluation_boundary["POST_FIX"] += latest >= FIX_DEPLOY_TS
        if kind != "BASE_ONLY" and outcome is not None:
            pre_fix_horizons = [
                horizon
                for horizon in ("15m", "30m", "60m", "120m", "240m")
                if outcome[f"evaluated_{horizon}_dt"] is not None
                and outcome[f"evaluated_{horizon}_dt"] < FIX_DEPLOY_TS
            ]
            if pre_fix_horizons:
                pre_fix_variant_outcomes_n += 1
                pre_fix_variant_horizons_n += len(pre_fix_horizons)
                if outcome["is_final"]:
                    pre_fix_variant_finalized_n += 1
                if mismatches:
                    pre_fix_variant_tuple_mismatch_outcomes_n += 1

        if cohort == "UNKNOWN_EVALUATION_BOUNDARY":
            unknown_cohort_n += 1
        if outcome is not None:
            for horizon_cohort in horizon_cohorts(outcome):
                horizon_counts[horizon_cohort] += 1
        if label != "BASE_ONLY" and mismatches:
            frozen = frozen_tuple(observation)
            historical = historical_tuple(observation)
            affected_rows.append({
                "observation_id": observation["observation_id"],
                "experiment_id": experiment_id,
                "source_signal_id": observation["source_signal_id"],
                "symbol": observation["symbol"],
                "direction": observation["direction"],
                "signal_time_utc": _iso(observation["signal_dt"]),
                "observation_created_at_utc": _iso(observation["created_dt"]),
                "outcome_created_at_utc": _iso(outcome["created_at_utc"]) if outcome else None,
                "outcome_updated_at_utc": _iso(outcome["updated_at_utc"]) if outcome else None,
                "tuple_class": kind,
                "geometry_exposure": label,
                "mismatched_fields": "|".join(mismatches),
                "expected_frozen_protocol_mismatched_fields": "|".join(expected_mismatches),
                "entry_intervention": interventions["entry"],
                "stop_intervention": interventions["stop"],
                "target_intervention": interventions["target"],
                "frozen_effective_entry": frozen[0],
                "frozen_effective_stop": frozen[1],
                "frozen_effective_target": frozen[2],
                "historical_effective_entry": historical[0],
                "historical_effective_stop": historical[1],
                "historical_effective_target": historical[2],
                "evaluation_cohort": cohort,
                "mixed_evaluation_cohorts": "|".join(mixed),
                "horizon_cohorts": "|".join(horizon_cohorts(outcome)) if outcome else "",
                "is_final": outcome["is_final"] if outcome else None,
                "exposure_reason": "VARIANT_BASE_GEOMETRY_MISMATCH" if mismatches else "PRE_FIX_EVALUATION_BOUNDARY",
            })

    integrity = integrity_counts(observations, outcomes)
    result = {
        "experiment_id": experiment_id,
        "direction": observations[0]["direction"] if observations else None,
        **integrity,
        "tuple_counts": dict(sorted(tuple_counts.items())),
        "geometry_exposure_counts": dict(sorted(geometry_counts.items())),
        "evaluation_cohort_counts": dict(sorted(cohort_counts.items())),
        "intervention_counts": dict(sorted(intervention_counts.items())),
        "per_direction": {key: dict(sorted(value.items())) for key, value in sorted(per_direction.items())},
        "horizon_evaluation_counts": dict(sorted(horizon_counts.items())),
        "pre_fix_signal_observations_n": pre_fix_observations,
        "pre_fix_signal_outcomes_n": pre_fix_outcomes,
        "pre_fix_signal_finalized_outcomes_n": pre_fix_finalized_n,
        "unknown_evaluation_boundary_n": unknown_cohort_n,
        "material_historical_tuple_mismatch_n": material_mismatch_n,
        "pre_fix_variant_outcomes_n": pre_fix_variant_outcomes_n,
        "pre_fix_variant_finalized_n": pre_fix_variant_finalized_n,
        "pre_fix_variant_horizons_n": pre_fix_variant_horizons_n,
        "pre_fix_variant_tuple_mismatch_outcomes_n": pre_fix_variant_tuple_mismatch_outcomes_n,
        "expected_frozen_protocol_mismatch_n": expected_protocol_mismatch_n,
        "pre_fix_evaluated_horizons": dict(sorted(pre_fix_evaluated_horizons.items())),
        "post_fix_evaluated_horizons": dict(sorted(post_fix_evaluated_horizons.items())),
        "final_outcome_evaluation_boundary": dict(sorted(final_outcome_evaluation_boundary.items())),
        "first_signal_time_utc": _iso(min((row["signal_dt"] for row in observations if row["signal_dt"]), default=None)),
        "last_signal_time_utc": _iso(max((row["signal_dt"] for row in observations if row["signal_dt"]), default=None)),
    }
    return result, affected_rows

OBS_NAMES = SRR_OBSERVATION_HEADER.split(",")
OUTCOME_NAMES = SRR_OUTCOME_HEADER.split(",")
DISCOVERY_NAMES = (
    "experiment_id,version,scanner_name,direction,experiment_type,entry_rule,stop_rule,"
    "target_rule,paired_with,status,started_at_utc,created_at_utc,updated_at_utc,framework,"
    "observations_observed_in_export,outcomes_observed_in_export"
).split(",")
GENERIC_NAMES = (
    "experiment_id,scanner_name,research_observation_n,min_signal_time,max_signal_time"
).split(",")


def experiment_inventory(section: Section) -> list[dict[str, Any]]:
    return parse_tsv_rows(section.lines, DISCOVERY_NAMES)


def make_synthetic_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    def base_case(**overrides: Any) -> dict[str, Any]:
        row = {
            "observation_id": 1, "experiment_id": "SYNTHETIC", "source_signal_id": 1,
            "symbol": "AAAUSDT", "direction": "SHORT", "signal_time_utc": "2026-10-07T12:00:00Z",
            "reference_price": "100", "invalidation_price": "101", "target_1": "97",
            "target_2": None, "variant_entry": None, "variant_stop": None,
            "variant_target": None, "score": "0", "rule_passed": "t",
            "filter_reason": "", "gate_result": "", "features_text": "{}",
            "parameters_text": "{}", "market_regime": "RANGE",
            "created_at_utc": "2026-10-07T12:00:01Z",
        }
        row.update(overrides)
        return normalize_observation(row)

    cases.append(("base_only_short", base_case()))
    cases.append(("base_only_long", base_case(direction="LONG", invalidation_price="99", target_1="103")))
    cases.append(("complete_variant_entry", base_case(variant_entry="99.5", variant_stop="101", variant_target="97")))
    cases.append(("complete_variant_stop", base_case(variant_entry="100", variant_stop="102", variant_target="97")))
    cases.append(("complete_variant_target", base_case(variant_entry="100", variant_stop="101", variant_target="95")))
    cases.append(("complete_multiple_variant", base_case(variant_entry="99.5", variant_stop="102", variant_target="95")))
    cases.append(("partial_variant_entry", base_case(variant_entry="99.5")))
    cases.append(("partial_variant_stop", base_case(variant_stop="102")))
    cases.append(("partial_variant_target", base_case(variant_target="95")))
    cases.append(("invalid_short_orientation", base_case(variant_entry="100", variant_stop="99", target_1="97", variant_target="101")))
    return cases


def synthetic_semantics() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for name, observation in make_synthetic_cases():
        frozen = frozen_tuple(observation)
        historical = historical_tuple(observation)
        label, mismatches, interventions = classify_geometry(observation)
        cases.append({
            "case": name,
            "direction": observation["direction"],
            "tuple_class": tuple_class(observation),
            "geometry_exposure": label,
            "frozen_effective_tuple": list(frozen),
            "historical_effective_tuple": list(historical),
            "mismatched_fields": mismatches,
            "interventions": interventions,
            "old_evaluator_uses_variant_entry": observation["variant_entry"] is not None,
            "fixed_evaluator_uses_complete_variant": tuple_class(observation) == "COMPLETE_VARIANT",
        })
    same_candle_long = _check_tp_sl(
        [{"high": 103.5, "low": 97.5}], 100.0, 98.0, 103.0, 240, "LONG", "ORDER_FIRST",
    )
    same_candle_short = _check_tp_sl(
        [{"high": 102.5, "low": 94.5}], 100.0, 102.0, 95.0, 240, "SHORT", "STOP_FIRST",
    )
    cases.append({
        "case": "same_candle_tp_sl_ambiguity_long",
        "same_candle_result": same_candle_long,
    })
    cases.append({
        "case": "same_candle_tp_sl_ambiguity_short_stop_first",
        "same_candle_result": same_candle_short,
    })
    return {
        "cases": cases,
        "old_evaluator_tuple": "variant_entry when non-NULL; otherwise reference_price; stop/target selected field-by-field from variant_* when non-NULL, otherwise base",
        "fixed_evaluator_tuple": "complete variant_entry/variant_stop/variant_target atomically; otherwise complete base tuple",
        "material_mismatch_possible": True,
        "exact_historical_path_reconstruction_available": False,
        "exact_historical_path_status": "EXACT_HISTORICAL_PATH_RECONSTRUCTION_NOT_AVAILABLE",
    }


def _check_tp_sl(candles: list[dict[str, float]], entry: float, stop: float, target: float, max_minutes: int, direction: str, policy: str) -> dict[str, Any]:
    result = {
        "tp_hit": False, "sl_hit": False,
        "tp_before_sl": False, "sl_before_tp": False,
        "ambiguous_intrabar": False,
    }
    for minute, candle in enumerate(candles, start=5):
        if minute >= max_minutes:
            break
        if direction == "SHORT":
            tp = candle["low"] <= target
            sl = candle["high"] >= stop
        else:
            tp = candle["high"] >= target
            sl = candle["low"] <= stop
        if tp and sl:
            result.update(tp_hit=True, sl_hit=True, ambiguous_intrabar=True)
            result["sl_before_tp"] = policy == "STOP_FIRST"
            result["tp_before_sl"] = policy == "TP_FIRST"
            break
        if tp and not result["tp_before_sl"]:
            result["tp_hit"] = True
            result["tp_before_sl"] = True
        if sl and not result["sl_before_tp"]:
            result["sl_hit"] = True
            result["sl_before_tp"] = True
    return result


def decision_for(summary: dict[str, Any], experiment_id: str) -> dict[str, Any]:
    counts = summary["tuple_counts"]
    complete = counts.get("COMPLETE_VARIANT", 0)
    partial = counts.get("PARTIAL_VARIANT", 0)
    variant_n = complete + partial

    interventions = summary.get("intervention_counts", {})
    material_n = interventions.get("any_variant_intervention_n", 0)

    exposed_n = summary.get("pre_fix_variant_outcomes_n", 0)
    exposed_final_n = summary.get("pre_fix_variant_finalized_n", 0)
    exposed_horizons_n = summary.get("pre_fix_variant_horizons_n", 0)

    tuple_mismatch_n = summary["material_historical_tuple_mismatch_n"]
    exposed_tuple_mismatch_n = summary.get(
        "pre_fix_variant_tuple_mismatch_outcomes_n", 0
    )
    protocol_mismatch_n = summary.get(
        "expected_frozen_protocol_mismatch_n", 0
    )

    common = {
        "variant_tuple_present_n": variant_n,
        "material_geometry_intervention_n": material_n,
        "historical_exposed_n": exposed_n,
        "historical_exposed_finalized_n": exposed_final_n,
        "historical_exposed_horizons_n": exposed_horizons_n,
        "historical_tuple_mismatch_n": tuple_mismatch_n,
        "historical_exposed_tuple_mismatch_n": exposed_tuple_mismatch_n,
        "frozen_protocol_mismatch_n": protocol_mismatch_n,
        "proven_outcome_mismatch_n": 0,
        "affected_finalized_outcomes_n": 0,
    }

    if variant_n == 0:
        return {
            **common,
            "historical_exposure": "NO_VARIANT_TUPLE_EXPOSURE",
            "historical_outcome_trust": "NO_VARIANT_SEMANTICS_DEFECT_DETECTED",
            "decision": "NO_ACTION_REQUIRED",
            "reconstruction_feasibility": "NOT_REQUIRED",
            "next_action": (
                "No variant tuple exposure. Retain historical outcomes "
                "for this specific evaluator semantics audit."
            ),
        }

    if partial > 0 or exposed_tuple_mismatch_n > 0:
        return {
            **common,
            "historical_exposure": "POTENTIALLY_AFFECTED_PRE_FIX_VARIANT",
            "historical_outcome_trust": "NOT_ESTABLISHED",
            "decision": "INSUFFICIENT_EVIDENCE",
            "reconstruction_feasibility": (
                "EXACT_HISTORICAL_PATH_RECONSTRUCTION_NOT_AVAILABLE"
            ),
            "next_action": (
                "Review partial tuples and observation-level pre-fix "
                "mismatches. Determine whether a separate offline "
                "reconstruction or clean holdout is necessary."
            ),
        }

    if protocol_mismatch_n > 0:
        return {
            **common,
            "historical_exposure": "FROZEN_PROTOCOL_GEOMETRY_DISCREPANCY",
            "historical_outcome_trust": "NOT_ESTABLISHED",
            "decision": "INSUFFICIENT_EVIDENCE",
            "reconstruction_feasibility": (
                "EXACT_HISTORICAL_PATH_RECONSTRUCTION_NOT_AVAILABLE"
            ),
            "next_action": (
                "Investigate frozen-protocol geometry discrepancies "
                "separately from evaluator tuple-selection semantics."
            ),
        }

    return {
        **common,
        "historical_exposure": (
            "PRE_FIX_COMPLETE_VARIANT_EVALUATION"
            if exposed_n else "NO_PRE_FIX_VARIANT_EVALUATION"
        ),
        "historical_outcome_trust": "TUPLE_SELECTION_VALIDATED_OUTCOMES_NOT_REPLAYED",
        "decision": "NO_ACTION_REQUIRED",
        "reconstruction_feasibility": "NOT_REQUIRED_FOR_TUPLE_SELECTION",
        "next_action": (
            "No historical tuple-selection mismatch demonstrated. "
            "Do not reconstruct outcomes solely because a complete "
            "variant tuple was evaluated before the fix. "
            "Exact candle-path correctness remains unverified."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--affected-csv", required=True, type=Path)
    parser.add_argument("--experiments-csv", required=True, type=Path)
    parser.add_argument("--summary-csv", required=True, type=Path)
    parser.add_argument("--audit-cutoff", default="2026-10-08T05:54:56.705Z")
    args = parser.parse_args()

    sections = section_map(load_sections(args.export))
    required = {
        "META", "SCHEMA_PROSPECTIVE_EXPERIMENT", "SCHEMA_PROSPECTIVE_OBSERVATION",
        "SCHEMA_PROSPECTIVE_OUTCOME", "SCHEMA_PROSPECTIVE_EXPERIMENT_DIRECTION_STATE",
        "SCHEMA_RESEARCH_EXPERIMENT", "SRR_EXPERIMENT_DISCOVERY", "SRR_DIRECTION_STATE",
        "SRR_PROSPECTIVE_OBSERVATIONS", "SRR_PROSPECTIVE_OUTCOMES",
        "GENERIC_SRR_CLASSIFICATION", "SRR_INTEGRITY_AGGREGATES",
        "SRR_TUPLE_AND_COHORT_COUNTS",
    }
    missing = sorted(required - set(sections))
    if missing:
        raise SystemExit("Malformed export snapshot: missing sections {}".format(missing))

    observations = [normalize_observation(row) for row in parse_tsv_rows(
        sections["SRR_PROSPECTIVE_OBSERVATIONS"].lines, OBS_NAMES,
    )]
    outcomes = [normalize_outcome(row) for row in parse_tsv_rows(
        sections["SRR_PROSPECTIVE_OUTCOMES"].lines, OUTCOME_NAMES,
    )]
    by_experiment_obs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_experiment_out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        by_experiment_obs[row["experiment_id"]].append(row)
    for row in outcomes:
        by_experiment_out[row["experiment_id"]].append(row)

    summaries: list[dict[str, Any]] = []
    affected: list[dict[str, Any]] = []
    experiment_inventory_rows = experiment_inventory(sections["SRR_EXPERIMENT_DISCOVERY"])
    for experiment_id in sorted(by_experiment_obs):
        summary, rows = aggregate(
            by_experiment_obs[experiment_id], by_experiment_out.get(experiment_id, []), experiment_id,
        )
        summary["decision"] = decision_for(summary, experiment_id)
        summaries.append(summary)
        if summary["decision"]["decision"] != "NO_ACTION_REQUIRED":
            affected.extend(rows)

    inventory_by_id = {row["experiment_id"]: row for row in experiment_inventory_rows if row["framework"] == "prospective"}
    experiment_rows: list[dict[str, Any]] = []
    for summary in summaries:
        registry = inventory_by_id.get(summary["experiment_id"], {})
        experiment_rows.append({
            "experiment_id": summary["experiment_id"],
            "registry_direction": registry.get("direction"),
            "registry_status": registry.get("status"),
            "direction": summary["direction"],
            "observations_n": summary["observations_n"],
            "outcomes_n": summary["outcomes_n"],
            "finalized_n": summary["finalized_n"],
            "base_only_n": summary["tuple_counts"].get("BASE_ONLY", 0),
            "complete_variant_n": summary["tuple_counts"].get("COMPLETE_VARIANT", 0),
            "partial_variant_n": summary["tuple_counts"].get("PARTIAL_VARIANT", 0),
            "material_geometry_mismatch_n": summary["material_historical_tuple_mismatch_n"],
            "pre_fix_signal_outcomes_n": summary["pre_fix_signal_outcomes_n"],
            "pre_fix_signal_finalized_n": summary["pre_fix_signal_finalized_outcomes_n"],
            "pre_fix_evaluated_horizons": summary["pre_fix_evaluated_horizons"],
            "post_fix_evaluated_horizons": summary["post_fix_evaluated_horizons"],
            "final_outcome_evaluation_boundary": summary["final_outcome_evaluation_boundary"],
            "pre_fix_evaluated_horizons": summary["pre_fix_evaluated_horizons"],
            "post_fix_evaluated_horizons": summary["post_fix_evaluated_horizons"],
            "final_outcome_evaluation_boundary": summary["final_outcome_evaluation_boundary"],
            "variant_tuple_present_n": summary["decision"]["variant_tuple_present_n"],
            "material_geometry_intervention_n": summary["decision"]["material_geometry_intervention_n"],
            "historical_exposed_n": summary["decision"]["historical_exposed_n"],
            "proven_outcome_mismatch_n": summary["decision"]["proven_outcome_mismatch_n"],
            "affected_finalized_outcomes_n": summary["decision"]["affected_finalized_outcomes_n"],
            "unknown_evaluation_boundary_n": summary["unknown_evaluation_boundary_n"],
            "missing_outcome_n": summary["missing_outcome_n"],
            "orphan_outcome_n": summary["orphan_outcome_n"],
            "duplicate_source_signal_n": summary["duplicate_source_signal_n"],
            "decision": summary["decision"]["decision"],
            "decision_evidence": summary["decision"]["next_action"],
        })


    with args.summary_csv.open("w", newline="", encoding="utf-8") as handle:
        summary_fields = [
            "experiment_id", "base_only_n", "complete_variant_n", "partial_variant_n",
            "variant_tuple_present_n", "material_geometry_intervention_n",
            "historical_exposed_n", "proven_outcome_mismatch_n",
            "affected_finalized_outcomes_n", "decision",
        ]
        writer = csv.DictWriter(handle, fieldnames=summary_fields)
        writer.writeheader()
        for row in experiment_rows:
            writer.writerow({field: row[field] for field in summary_fields})

    args.affected_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.affected_csv.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "observation_id", "experiment_id", "source_signal_id", "symbol", "direction",
            "signal_time_utc", "observation_created_at_utc", "outcome_created_at_utc",
            "outcome_updated_at_utc", "tuple_class", "geometry_exposure", "mismatched_fields",
            "entry_intervention", "stop_intervention", "target_intervention",
            "frozen_effective_entry", "frozen_effective_stop", "frozen_effective_target",
            "historical_effective_entry", "historical_effective_stop", "historical_effective_target",
            "evaluation_cohort", "mixed_evaluation_cohorts", "horizon_cohorts", "is_final", "exposure_reason",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in affected:
            writer.writerow(row)

    with args.experiments_csv.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = list(experiment_rows[0].keys()) if experiment_rows else []
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            for row in experiment_rows:
                writer.writerow(row)

    results = {
        "analysis_id": "SRR_VARIANT_OUTCOME_SEMANTICS_RECONSTRUCTION_AUDIT_V1",
        "audit_cutoff_utc": _iso(_ts(args.audit_cutoff)),
        "audit_export_sha256": _sha256(args.export),
        "local_git_head": LOCAL_GIT_HEAD,
        "vps_git_head": VPS_GIT_HEAD,
        "evaluator_fix": {
            "commit": FIX_COMMIT,
            "merge_commit": FIX_MERGE_COMMIT,
            "deploy_ts": _iso(FIX_DEPLOY_TS),
            "source_evidence": [
                "audit_db/me_short_geometry_abc_evaluator_fix_deploy_v1_20261007_restart.txt",
                "audit_db/me_short_geometry_abc_evaluator_fix_deploy_v1_20261007_vps_deploy.txt",
                "docs/research/ME_SHORT_GEOMETRY_ABC_EVALUATOR_FIX_DEPLOY_V1_2026-10-07.md",
            ],
        },
        "schema_evidence": {
            "prospective_observation_columns": sections["SCHEMA_PROSPECTIVE_OBSERVATION"].lines,
            "prospective_outcome_columns": sections["SCHEMA_PROSPECTIVE_OUTCOME"].lines,
        },
        "experiment_registry": experiment_inventory_rows,
        "generic_framework_classification": parse_tsv_rows(
            sections["GENERIC_SRR_CLASSIFICATION"].lines, GENERIC_NAMES,
        ),
        "experiments": summaries,
        "experiment_inventory": experiment_rows,
        "affected_observations_n": len(affected),
        "affected_observations_definition": (
            "Observations with a material persisted variant tuple vs old-evaluator tuple mismatch; "
            "BASE_ONLY rows and PRE_FIX cohorts alone are excluded."
        ),
        "synthetic_semantics": synthetic_semantics(),
        "candle_source": {
            "production_evaluator_source": "app/research/prospective_evaluator.py::_get_candles -> BybitClient.get_klines",
            "market_db_snapshot_equivalence": False,
            "exact_historical_path_reconstruction_available": False,
            "limitation": "Historical outcomes were produced from time-of-evaluation Bybit klines; current DB/API snapshots are not proven byte-equivalent. Exact H1/H2 replay is EXACT_HISTORICAL_PATH_RECONSTRUCTION_NOT_AVAILABLE.",
        },
        "production_safety": {
            "db_writes": False, "scanner_restart": False, "paper_restart": False,
            "evaluator_restart": False, "registry_changes": False, "outcome_rewrite": False,
        },
    }
    args.output_json.write_text(json.dumps(results, indent=2, sort_keys=False), encoding="utf-8")
    print(json.dumps({
        "experiments": [row["experiment_id"] for row in experiment_rows],
        "affected_observations_n": len(affected),
        "decisions": {row["experiment_id"]: row["decision"] for row in experiment_rows},
    }, indent=2))


if __name__ == "__main__":
    main()
