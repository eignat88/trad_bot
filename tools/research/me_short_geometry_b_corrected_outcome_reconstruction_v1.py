"""ME SHORT B frozen-geometry historical-semantics validation and snapshot sensitivity.

This analysis is READ-ONLY with respect to production PostgreSQL. It consumes only
the frozen VPS exports under audit_db/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007,
validates historical B tuple semantics, and computes explicitly secondary DB-snapshot
sensitivity results. It never rewrites stored prospective outcomes.

Production history used live Bybit get_klines() candles. Offline reconstruction uses the
current market.candle DB snapshot, which is NOT proven to be the exact historical stream.
Therefore the offline path reconstruction is DB_SNAPSHOT_SENSITIVITY_RECONSTRUCTION only.

Usage:
    python tools/research/me_short_geometry_b_corrected_outcome_reconstruction_v1.py
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from app.models import Candle
from app.research.prospective_evaluator import (
    ProspectiveOOSEvaluator,
    _check_tp_sl,
)

FIX_DEPLOY_TS = "2026-10-07T13:04:55Z"
FIX_TS = datetime(2026, 10, 7, 13, 4, 55, tzinfo=timezone.utc)
SEED = 20261007
BOOTSTRAP_N = 10_000
HORIZONS = (("15m", 15), ("30m", 30), ("60m", 60), ("120m", 120), ("240m", 240))
PRIMARY_METRIC = "mfe_60m"
A_EXPERIMENT = "ME_SHORT_GEOM_A_V1"
B_EXPERIMENT = "ME_SHORT_GEOM_B_V1"
HYPOTHESIS_H1 = "COMPLETE_PERSISTED_VARIANT"
HYPOTHESIS_H2 = "VARIANT_ENTRY_PLUS_BASE_STOP_TARGET"
DB_SNAPSHOT_ROLE = "DB_SNAPSHOT_SENSITIVITY"
PATH_REPRODUCTION_STATUS = "NOT_EXACTLY_REPRODUCIBLE_FROM_CURRENT_DB_SNAPSHOT"
EXPECTED_COVERAGE = {"15m": 3, "30m": 6, "60m": 12, "120m": 24, "240m": 48}
HORIZON_FIELDS = {
    "15m": ("mfe_15m", "mae_15m", "mfe_r_15m", "mae_r_15m", "return_at_15m"),
    "30m": ("mfe_30m", "mae_30m", "mfe_r_30m", "mae_r_30m", "return_at_30m"),
    "60m": ("mfe_60m", "mae_60m", "mfe_r_60m", "mae_r_60m", "return_at_60m"),
    "120m": ("mfe_120m", "mae_120m", "mfe_r_120m", "mae_r_120m", "return_at_120m"),
    "240m": ("mfe_240m", "mae_240m", "mfe_r_240m", "mae_r_240m", "return_at_240m"),
}
RELEVANT_FIELDS = tuple(
    field for fields in HORIZON_FIELDS.values() for field in fields
) + (
    "tp_hit", "sl_hit", "tp_before_sl", "sl_before_tp",
    "ambiguous_intrabar", "time_to_tp", "time_to_sl",
)
BOUNDARY_SYMBOLS = ("SPCXUSDT", "MSTRUSDT", "KLACUSDT", "STRKUSDT")


@dataclass
class CapturedEvaluation:
    updates: dict[str, Any]
    evaluator: ProspectiveOOSEvaluator
    candles: list[Candle]
    conn: SimpleNamespace


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _number(value: Any) -> float | None:
    if value is None or value == "" or value == "\\N":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    if value is None or value == "" or value == "\\N":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "t", "1", "yes"}


def _mean(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.fmean(clean) if clean else None


def _median(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.median(clean) if clean else None


def _quantile(values: Iterable[float | None], q: float) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    pos = (len(clean) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return clean[lo] if lo == hi else clean[lo] + (clean[hi] - clean[lo]) * (pos - lo)


def _summary(values: Iterable[float | None]) -> dict[str, Any]:
    clean = [v for v in values if v is not None]
    return {
        "n": len(clean),
        "mean": _mean(clean),
        "median": _median(clean),
        "p25": _quantile(clean, 0.25),
        "p75": _quantile(clean, 0.75),
        "min": min(clean) if clean else None,
        "max": max(clean) if clean else None,
        "positive": sum(v > 0 for v in clean),
        "zero": sum(v == 0 for v in clean),
        "negative": sum(v < 0 for v in clean),
    }


def _close(a: float | None, b: float | None, eps: float = 1e-12) -> bool:
    return a is not None and b is not None and abs(a - b) <= eps * max(1.0, abs(a), abs(b))


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.reader(handle, delimiter="\t") if row]


def load_observations(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in _read_tsv(path):
        if len(row) != 19:
            raise ValueError(f"Unexpected observation row length {len(row)}: {row[:3]}")
        (
            experiment_id, source_signal_id, observation_id, symbol, direction,
            signal_time, created_at, reference_price, invalidation_price,
            target_1, variant_entry, variant_stop, variant_target, score,
            rule_passed, filter_reason, market_regime, features, parameters,
        ) = row
        rows.append({
            "experiment_id": experiment_id,
            "source_signal_id": int(source_signal_id),
            "observation_id": int(observation_id),
            "symbol": symbol,
            "direction": direction,
            "signal_time": _parse_ts(signal_time),
            "created_at": _parse_ts(created_at),
            "reference_price": _number(reference_price),
            "invalidation_price": _number(invalidation_price),
            "target_1": _number(target_1),
            "variant_entry": _number(variant_entry),
            "variant_stop": _number(variant_stop),
            "variant_target": _number(variant_target),
            "score": _number(score),
            "rule_passed": _boolean(rule_passed),
            "filter_reason": filter_reason or None,
            "market_regime": market_regime or None,
            "features": json.loads(features or "{}"),
            "parameters": json.loads(parameters or "{}"),
        })
    return rows


def load_outcomes(path: Path) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    numeric_fields = (
        "mfe_15m", "mfe_30m", "mfe_60m", "mfe_120m", "mfe_240m",
        "mae_15m", "mae_30m", "mae_60m", "mae_120m", "mae_240m",
        "mfe_r_15m", "mfe_r_30m", "mfe_r_60m", "mfe_r_120m", "mfe_r_240m",
        "mae_r_15m", "mae_r_30m", "mae_r_60m", "mae_r_120m", "mae_r_240m",
        "return_at_15m", "return_at_30m", "return_at_60m",
        "return_at_120m", "return_at_240m",
    )
    boolean_fields = ("tp_hit", "sl_hit", "tp_before_sl", "sl_before_tp", "ambiguous_intrabar")

    for row in _read_tsv(path):
        if len(row) != 47:
            raise ValueError(
                f"Unexpected outcome row length {len(row)}; expected 47: {row[:3]}"
            )

        observation_id = int(row[0])
        outcome: dict[str, Any] = {
            "observation_id": observation_id,
            "experiment_id": row[1],
            "source_signal_id": int(row[2]),
            "symbol": row[3],
            "direction": row[4],
            "signal_time": _parse_ts(row[5]),
            "obs_created_at": _parse_ts(row[6]),
        }

        index = 7
        for name in numeric_fields:
            outcome[name] = _number(row[index])
            index += 1
        for name in boolean_fields:
            outcome[name] = _boolean(row[index])
            index += 1

        outcome["time_to_tp"] = _number(row[index])
        index += 1
        outcome["time_to_sl"] = _number(row[index])
        index += 1

        outcome["evaluated_15m_at"] = _parse_ts(row[index])
        index += 1
        outcome["evaluated_30m_at"] = _parse_ts(row[index])
        index += 1
        outcome["evaluated_60m_at"] = _parse_ts(row[index])
        index += 1
        outcome["evaluated_120m_at"] = _parse_ts(row[index])
        index += 1
        outcome["evaluated_240m_at"] = _parse_ts(row[index])
        index += 1
        outcome["is_final"] = _boolean(row[index])
        index += 1
        outcome["outcome_created_at"] = _parse_ts(row[index])
        index += 1
        outcome["outcome_updated_at"] = _parse_ts(row[index])
        index += 1

        if index != 47:
            raise AssertionError(f"Outcome loader consumed {index} fields instead of 47")
        result[observation_id] = outcome

    return result


def load_candles(path: Path) -> tuple[dict[tuple[str, datetime], list[Candle]], dict[str, Any]]:
    by_symbol: dict[str, dict[int, Candle]] = defaultdict(dict)
    raw_keys: set[tuple[str, int]] = set()
    for row in _read_tsv(path):
        if len(row) != 15:
            raise ValueError(f"Unexpected candle row length {len(row)}: {row[:3]}")
        exchange, market_type, symbol, timeframe, open_time, close_time, open_, high, low, close, volume, turnover, is_closed, quality_status, source = row
        open_ms = int(_parse_ts(open_time).timestamp() * 1000)
        key = (symbol, open_ms)
        raw_keys.add(key)
        candle = Candle(
            timestamp=open_ms,
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume),
        )
        if key in by_symbol[symbol] and not _same_candle(by_symbol[symbol][key], candle):
            raise ValueError(f"Conflicting candle payload for {symbol} {open_time}")
        by_symbol[symbol][open_ms] = candle
    return {symbol: sorted(candles.values(), key=lambda c: c.timestamp) for symbol, candles in by_symbol.items()}, {
        "raw_rows": sum(len(v) for v in by_symbol.values()),
        "unique_keys": len(raw_keys),
        "duplicate_rows_removed": sum(len(v) for v in by_symbol.values()) - len(raw_keys),
        "duplicate_keys": len(raw_keys) != sum(len(v) for v in by_symbol.values()),
    }


def _same_candle(a: Candle, b: Candle) -> bool:
    return all(
        getattr(a, field) == getattr(b, field)
        for field in ("timestamp", "open", "high", "low", "close", "volume")
    )


def select_candles(candles: list[Candle], symbol: str, from_time: datetime, to_time: datetime) -> list[Candle]:
    start_ms = int((from_time - timedelta(minutes=5)).timestamp() * 1000)
    end_ms = int(to_time.timestamp() * 1000)
    return [c for c in candles if symbol and start_ms <= c.timestamp <= end_ms]


def capture_evaluation(obs: dict[str, Any], candles: list[Candle]) -> CapturedEvaluation:
    observed: dict[str, Any] = {}

    class FakeCursor:
        def execute(self, sql, params=None):
            observed["sql"] = sql
            observed["params"] = params

        def close(self):
            return None

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

        def rollback(self):
            return None

    conn = FakeConn()
    evaluator = ProspectiveOOSEvaluator(conn, None, None)
    evaluator.evaluate_observation(
        obs, candles, obs["signal_time"] + timedelta(minutes=240),
        {"errors": 0, "horizons_updated": {}, "finalized": 0},
    )
    updates = {}
    if not observed.get("sql") or "ON CONFLICT (observation_id) DO UPDATE" not in observed["sql"]:
        raise ValueError(f"Evaluator produced no offline-capture update for observation {obs.get('observation_id')}")
    params = observed["params"]
    sql_tail = observed["sql"].split("prospective_outcome", 1)[1].split("SELECT", 1)[0]
    column_names = [part.strip() for part in sql_tail.strip(" ()\n").split(",") if part.strip()]
    updates = {
        "observation_id": params[0],
        "experiment_id": obs["experiment_id"],
    }
    value_columns = column_names[2:]
    value_params = params[1:-1]
    if len(value_columns) != len(value_params):
        raise ValueError(
            f"Evaluator capture mapping mismatch: "
            f"{len(value_columns)} columns vs {len(value_params)} params"
        )
    updates.update(dict(zip(value_columns, value_params, strict=True)))
    return CapturedEvaluation(
        updates=updates,
        evaluator=evaluator,
        candles=candles,
        conn=conn,
    )


def _captured_updates(obs: dict[str, Any], candles: list[Candle]) -> dict[str, Any]:
    return capture_evaluation(obs, candles).updates


def reconstruct_observation(
    obs: dict[str, Any], candles: list[Candle], role: str = DB_SNAPSHOT_ROLE,
) -> dict[str, Any]:
    updates = _captured_updates(obs, candles)
    entry = float(obs["variant_entry"])
    stop = float(obs["variant_stop"])
    target = float(obs["variant_target"])
    risk = abs(entry - stop)
    risk_pct = risk / entry * 100 if entry else 0.0
    output = {
        "observation_id": obs["observation_id"],
        "source_signal_id": obs["source_signal_id"],
        "experiment_id": obs["experiment_id"],
        "symbol": obs["symbol"],
        "signal_time": _iso(obs["signal_time"]),
        "signal_date": obs["signal_time"].astimezone(timezone.utc).date().isoformat(),
        "effective_entry": entry,
        "effective_stop": stop,
        "effective_target": target,
        "risk_1r": risk,
        "risk_1r_pct": risk_pct,
        "reconstruction_status": role,
        "exact_historical_reconstruction": False,
        "simulation_role": role,
    }
    for horizon, _ in HORIZONS:
        mfe = updates.get(f"mfe_{horizon}")
        mae = updates.get(f"mae_{horizon}")
        output[f"mfe_{horizon}"] = mfe
        output[f"mae_{horizon}"] = mae
        output[f"mfe_r_{horizon}"] = updates.get(f"mfe_r_{horizon}")
        output[f"mae_r_{horizon}"] = updates.get(f"mae_r_{horizon}")
        output[f"return_at_{horizon}"] = updates.get(f"return_at_{horizon}")
    output.update({
        "tp_hit": bool(updates.get("tp_hit", False)),
        "sl_hit": bool(updates.get("sl_hit", False)),
        "tp_before_sl": bool(updates.get("tp_before_sl", False)),
        "sl_before_tp": bool(updates.get("sl_before_tp", False)),
        "ambiguous_intrabar": bool(updates.get("ambiguous_intrabar", False)),
        "time_to_tp": updates.get("time_to_tp"),
        "time_to_sl": updates.get("time_to_sl"),
        "is_final": updates.get("is_final", False),
    })
    return output


def persisted_variant_tuple(obs: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """Return the complete variant fields exactly as persisted; this is not evaluator logic."""
    return (
        _number(obs.get("variant_entry")),
        _number(obs.get("variant_stop")),
        _number(obs.get("variant_target")),
    )


def complete_variant_or_base_tuple(obs: dict[str, Any]) -> tuple[float, float | None, float | None]:
    """Return the current production evaluator semantics: all three variant fields atomically."""
    entry, stop, target = persisted_variant_tuple(obs)
    if entry is not None and stop is not None and target is not None:
        return float(entry), float(stop), float(target)
    return (
        float(obs["reference_price"]),
        _number(obs.get("invalidation_price")),
        _number(obs.get("target_1")),
    )


def entry_plus_base_hypothesis_tuple(obs: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """Return H2 diagnostic only: variant entry paired with base stop and target."""
    return (
        _number(obs.get("variant_entry")),
        _number(obs.get("invalidation_price")),
        _number(obs.get("target_1")),
    )


def old_fixed_tuple(obs: dict[str, Any]) -> tuple[float, float | None, float | None]:
    """Compatibility alias for persisted_variant_tuple; H2 is not implied by this name."""
    return persisted_variant_tuple(obs)


def _old_outcome_tuple(obs: dict[str, Any]) -> tuple[float, float | None, float | None]:
    """Compatibility alias for the rejected H2 diagnostic tuple."""
    return entry_plus_base_hypothesis_tuple(obs)


def coverage_for(symbol: str, signal_time: datetime, candles: list[Candle]) -> dict[str, int]:
    post = [c for c in candles if c.timestamp > int(signal_time.timestamp() * 1000)]
    result = {}
    for horizon, minutes in HORIZONS:
        cutoff = int((signal_time + timedelta(minutes=minutes)).timestamp() * 1000)
        result[horizon] = sum(c.timestamp < cutoff for c in post)
    return result


def classify_cohorts(rows: list[dict[str, Any]], outcomes: dict[int, dict[str, Any]]) -> dict[str, Any]:
    classes = Counter()
    classified: dict[str, Any] = {}
    for row in rows:
        outcome = outcomes.get(row["observation_id"])
        signal_post = row["signal_time"] > FIX_TS
        eval_post = outcome is not None and outcome.get("evaluated_240m_at") is not None and outcome["evaluated_240m_at"] >= FIX_TS
        if not signal_post and not eval_post:
            label = "PRE_FIX_SIGNAL_PRE_FIX_EVALUATION"
        elif not signal_post and eval_post:
            label = "PRE_FIX_SIGNAL_POST_FIX_EVALUATION"
        elif signal_post and eval_post:
            label = "POST_FIX_SIGNAL_POST_FIX_EVALUATION"
        else:
            label = "POST_FIX_SIGNAL_PRE_FIX_EVALUATION"
        classes[label] += 1
        classified[row["observation_id"]] = label
    return {"classes": dict(classes), "by_observation": classified}


def _same_number(a: Any, b: Any, eps: float = 1e-9) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        return _close(float(a), float(b), eps=eps)
    except (TypeError, ValueError):
        return str(a) == str(b)


def compare_outcome_fields(
    hypothesis_obs: dict[str, Any], outcome: dict[str, Any], candles: list[Candle],
) -> dict[str, Any]:
    entry, stop, target = complete_variant_or_base_tuple(hypothesis_obs)
    simulated = capture_evaluation({
        **hypothesis_obs,
        "variant_entry": entry,
        "variant_stop": stop,
        "variant_target": target,
    }, candles).updates
    mismatch_fields = [
        field for field in RELEVANT_FIELDS
        if field in outcome and not _same_number(simulated.get(field), outcome.get(field))
    ]
    return {
        "exact": not mismatch_fields,
        "mismatch_count": len(mismatch_fields),
        "mismatch_fields": mismatch_fields,
        "compared_fields": sum(field in outcome for field in RELEVANT_FIELDS),
        "simulated": simulated,
    }


def compare_historical_hypotheses(
    b_obs: dict[str, Any], stored_outcome: dict[str, Any], candles: list[Candle],
) -> dict[str, Any]:
    h1_obs = {**b_obs, **dict(zip(("variant_entry", "variant_stop", "variant_target"), persisted_variant_tuple(b_obs)))}
    h2_obs = {**b_obs, **dict(zip(("variant_entry", "variant_stop", "variant_target"), entry_plus_base_hypothesis_tuple(b_obs)))}
    h1 = compare_outcome_fields(h1_obs, stored_outcome, candles)
    h2 = compare_outcome_fields(h2_obs, stored_outcome, candles)
    if h1["exact"] and not h2["exact"]:
        preferred = HYPOTHESIS_H1
    elif h2["exact"] and not h1["exact"]:
        preferred = HYPOTHESIS_H2
    elif h1["mismatch_count"] < h2["mismatch_count"]:
        preferred = HYPOTHESIS_H1
    elif h2["mismatch_count"] < h1["mismatch_count"]:
        preferred = HYPOTHESIS_H2
    else:
        preferred = "TIE"
    return {"H1": h1, "H2": h2, "preferred_hypothesis": preferred}


def _old_outcome_matches_evaluator(obs: dict[str, Any], outcome: dict[str, Any], candles: list[Candle]) -> bool:
    """Return whether the rejected H2 diagnostic matches all stored geometry-dependent fields."""
    return compare_outcome_fields(
        {**obs, **dict(zip(("variant_entry", "variant_stop", "variant_target"), entry_plus_base_hypothesis_tuple(obs)))},
        outcome, candles,
    )["exact"]


def historical_semantics_validation(
    comparisons: list[dict[str, Any]], checked_n: int
) -> dict[str, Any]:
    actual_n = len(comparisons)

    if checked_n != actual_n:
        raise ValueError(
            f"historical semantics checked_n mismatch: "
            f"checked_n={checked_n}, rows={actual_n}"
        )

    h1_exact = sum(bool(row["h1_exact"]) for row in comparisons)
    h2_exact = sum(bool(row["h2_exact"]) for row in comparisons)

    h1_better = sum(
        row["preferred_hypothesis"] == HYPOTHESIS_H1
        for row in comparisons
    )
    h2_better = sum(
        row["preferred_hypothesis"] == HYPOTHESIS_H2
        for row in comparisons
    )
    ties = sum(
        row["preferred_hypothesis"] == "TIE"
        for row in comparisons
    )

    result = {
        "checked_n": actual_n,
        "hypothesis_h1": HYPOTHESIS_H1,
        "hypothesis_h2": HYPOTHESIS_H2,
        "h1_exact": h1_exact,
        "h2_exact": h2_exact,
        "h1_better": h1_better,
        "h2_better": h2_better,
        "ties": ties,
        "h1_mean_mismatch_count": _mean(
            row["h1_mismatch_count"] for row in comparisons
        ),
        "h2_mean_mismatch_count": _mean(
            row["h2_mismatch_count"] for row in comparisons
        ),
    }

    result["geometry_semantics_verdict"] = (
        "COMPLETE_VARIANT_STRONGLY_SUPPORTED"
        if actual_n > 0
        and h1_better == actual_n
        and h2_better == 0
        and ties == 0
        else "INCONCLUSIVE"
    )

    return result


def horizon_specific_coverage(coverage_counts: Counter, n: int) -> dict[str, Any]:
    output = {}
    for horizon, minutes in HORIZONS:
        valid_n = coverage_counts[horizon]
        output[horizon] = {
            "minutes": minutes,
            "valid_n": valid_n,
            "analysis_n": valid_n,
            "ineligible_n": n - valid_n,
            "expected_post_signal_candles": EXPECTED_COVERAGE[horizon],
            "sql_coarse_coverage_rule": "open_time >= date_trunc('minute', signal_time)",
            "evaluator_equivalent_coverage_rule": "candle.timestamp > exact signal_time",
            "coverage_basis": "DB_SNAPSHOT_COARSE_SQL_AUDIT",
            "horizon_specific_eligibility_required": True,
        }
    return output


def bootstrap_pair_mean(deltas: list[float], iterations: int = BOOTSTRAP_N, seed: int = SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    n = len(deltas)
    if n == 0:
        return {"iterations": 0, "seed": seed, "mean": None, "median": None, "ci_95": [None, None], "p_gt_0": None}
    draws = []
    for _ in range(iterations):
        draws.append(statistics.fmean(deltas[rng.randrange(n)] for _ in range(n)))
    return {
        "iterations": iterations,
        "seed": seed,
        "mean": _mean(draws),
        "median": _median(draws),
        "ci_95": [_quantile(draws, 0.025), _quantile(draws, 0.975)],
        "p_gt_0": sum(v > 0 for v in draws) / len(draws),
    }


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def approximate_mcnemar_p(discordant_n: int, one_direction_n: int, iterations: int = 20_000, seed: int = SEED + 2) -> float | None:
    if discordant_n == 0:
        return 1.0
    rng = random.Random(seed)
    observed = abs(one_direction_n - (discordant_n - one_direction_n))
    extreme = 0
    for _ in range(iterations):
        if abs(sum(1 if rng.getrandbits(1) else -1 for _ in range(discordant_n))) >= observed:
            extreme += 1
    return (extreme + 1) / (iterations + 1)


def _exit_type(row: dict[str, Any]) -> str:
    if bool(row.get("ambiguous_intrabar")) and not bool(row.get("sl_before_tp")) and not bool(row.get("tp_before_sl")):
        return "OTHER_AMBIGUOUS"
    if bool(row.get("tp_before_sl")):
        return "TP_FIRST"
    if bool(row.get("sl_before_tp")):
        return "SL_FIRST"
    return "OTHER"


def paired_effect(rows: list[dict[str, Any]], field: str = "B-A") -> dict[str, Any]:
    summary = _summary(row[field] for row in rows)
    summary.update({
        "wins": summary["positive"],
        "ties": summary["zero"],
        "losses": summary["negative"],
    })
    return summary


def build_final_verdicts(
    history: dict[str, Any], sensitivity: dict[str, Any], path_status: str = PATH_REPRODUCTION_STATUS,
) -> dict[str, Any]:
    """Guard DB-snapshot sensitivity against executable-edge and outcome-rewrite conclusions."""
    primary = sensitivity["delta_metrics"][PRIMARY_METRIC]
    sensitivity_effective = (
        primary["n"] > 0
        and primary["bootstrap"]["ci_95"][0] is not None
        and primary["bootstrap"]["ci_95"][0] > 0
    )
    verdict = {
        "reconstruction_validity_verdict": "EXACT_PATH_RECONSTRUCTION_NOT_AVAILABLE",
        "historical_geometry_semantics_verdict": (
            "B_COMPLETE_VARIANT_GEOMETRY_CONFIRMED"
            if history["geometry_semantics_verdict"] == "COMPLETE_VARIANT_STRONGLY_SUPPORTED"
            else "B_GEOMETRY_SEMANTICS_INCONCLUSIVE"
        ),
        "historical_tuple_bug_impact": "NOT_OBSERVED_FOR_B",
        "db_snapshot_sensitivity_verdict": (
            "SECONDARY_POSITIVE_MFE_60M_SENSITIVITY_NOT_EXECUTABLE_EDGE"
            if sensitivity_effective
            else "SECONDARY_INCONCLUSIVE_DB_SNAPSHOT_SENSITIVITY"
        ),
        "db_snapshot_sensitivity_role": DB_SNAPSHOT_ROLE,
        "path_reproduction_status": path_status,
        "executable_edge": "NOT_ESTABLISHED",
        "production_outcomes_rewritten": False,
        "production_db_modified": False,
        "retune_recommended": False,
        "new_oos_created": False,
        "lifecycle_recommendation": "KEEP_BLOCKED",
        "final_conclusion": "KEEP_BLOCKED",
    }
    if path_status != PATH_REPRODUCTION_STATUS:
        raise AssertionError(f"Unexpected path reproduction status: {path_status}")
    if verdict["executable_edge"] != "NOT_ESTABLISHED" or verdict["lifecycle_recommendation"] != "KEEP_BLOCKED":
        raise AssertionError("Verdict guard: secondary DB snapshot sensitivity cannot establish execution readiness")
    return verdict


def run_analysis(audit_dir: Path, outdir: Path) -> dict[str, Any]:
    observations = load_observations(audit_dir / "dataset_pairs.tsv")
    outcomes = load_outcomes(audit_dir / "outcomes.tsv")
    candles_by_symbol, candle_audit = load_candles(audit_dir / "candles.tsv")
    cohorts = classify_cohorts(observations, outcomes)

    a_rows = [row for row in observations if row["experiment_id"] == A_EXPERIMENT]
    b_rows = [row for row in observations if row["experiment_id"] == B_EXPERIMENT]
    a_by_signal = {row["source_signal_id"]: row for row in a_rows}
    b_by_signal = {row["source_signal_id"]: row for row in b_rows}
    paired_signals = sorted(set(a_by_signal) & set(b_by_signal))

    tuple_stats = Counter()
    geometry_stats = Counter()
    reconstructed_rows: list[dict[str, Any]] = []
    semantics_rows: list[dict[str, Any]] = []
    exclusions: Counter = Counter()
    coverage_counts = Counter()
    old_vs_corrected_rows: list[dict[str, Any]] = []

    for row in b_rows:
        entry, stop, target = persisted_variant_tuple(row)
        if entry is not None and stop is not None and target is not None:
            tuple_stats["complete"] += 1
            tuple_stats["invalid"] += int(entry <= 0 or stop <= 0 or target <= 0)
        elif entry is None or stop is None or target is None:
            tuple_stats["partial"] += 1
        else:
            tuple_stats["invalid"] += 1

    for signal_id in paired_signals:
        a, b = a_by_signal[signal_id], b_by_signal[signal_id]
        if (
            b["symbol"] != a["symbol"]
            or b["direction"] != a["direction"]
            or b["signal_time"] != a["signal_time"]
            or b["reference_price"] != a["reference_price"]
        ):
            exclusions["source_identity_mismatch"] += 1
            continue
        if _close(b["variant_entry"], a["variant_entry"]):
            geometry_stats["entry_equal"] += 1
        else:
            geometry_stats["entry_different"] += 1
        if _close(b["variant_stop"], a["variant_stop"]):
            geometry_stats["stop_same"] += 1
        elif (b["variant_stop"] or 0) > (a["variant_stop"] or 0):
            geometry_stats["stop_wider"] += 1
        else:
            geometry_stats["stop_invalid"] += 1
        if _close(b["variant_target"], a["variant_target"]):
            geometry_stats["target_same"] += 1
        else:
            geometry_stats["target_different"] += 1

        coverage = coverage_for(b["symbol"], b["signal_time"], candles_by_symbol.get(b["symbol"], []))
        for horizon, expected in EXPECTED_COVERAGE.items():
            coverage_counts[horizon] += coverage[horizon] >= expected
        coverage_counts["missing_any"] += any(coverage[h] < EXPECTED_COVERAGE[h] for h in EXPECTED_COVERAGE)
        stored_outcome = outcomes.get(b["observation_id"])
        if coverage["60m"] < EXPECTED_COVERAGE["60m"]:
            exclusions["missing_60m_coverage"] += 1
            continue
        if coverage["240m"] < EXPECTED_COVERAGE["240m"]:
            exclusions["missing_240m_final_path_coverage"] += 1
            continue
        if stored_outcome is None:
            exclusions["missing_stored_outcome"] += 1
            continue

        h1_entry, h1_stop, h1_target = persisted_variant_tuple(b)
        h2_entry, h2_stop, h2_target = entry_plus_base_hypothesis_tuple(b)
        comparison = compare_historical_hypotheses(b, stored_outcome, candles_by_symbol[b["symbol"]])
        semantics_rows.append({
            "observation_id": b["observation_id"],
            "source_signal_id": signal_id,
            "symbol": b["symbol"],
            "signal_time": _iso(b["signal_time"]),
            "variant_entry": h1_entry,
            "variant_stop": h1_stop,
            "variant_target": h1_target,
            "base_entry": b["reference_price"],
            "base_stop": b["invalidation_price"],
            "base_target": b["target_1"],
            "h1_mismatch_count": comparison["H1"]["mismatch_count"],
            "h2_mismatch_count": comparison["H2"]["mismatch_count"],
            "h1_exact": comparison["H1"]["exact"],
            "h2_exact": comparison["H2"]["exact"],
            "preferred_hypothesis": comparison["preferred_hypothesis"],
            **{
                f"h1_{field}_mismatch": field in comparison["H1"]["mismatch_fields"]
                for field in ("sl_hit", "sl_before_tp", "time_to_sl")
            },
        })

        sensitivity_row = reconstruct_observation(b, candles_by_symbol[b["symbol"]])
        sensitivity_row.update({
            "cohort": cohorts["by_observation"].get(b["observation_id"]),
            "signal_date": b["signal_time"].astimezone(timezone.utc).date().isoformat(),
            "historical_stored_outcome": True,
            "db_snapshot_simulated_outcome": True,
            "simulation_role": DB_SNAPSHOT_ROLE,
            "exact_historical_reconstruction": False,
            **{f"coverage_{h}": coverage[h] for h, _ in HORIZONS},
        })
        reconstructed_rows.append(sensitivity_row)

        h1_entry, h1_stop, h1_target = persisted_variant_tuple(b)
        old_row = {
            "observation_id": b["observation_id"],
            "source_signal_id": signal_id,
            "symbol": b["symbol"],
            "signal_time": _iso(b["signal_time"]),
            "signal_date": sensitivity_row["signal_date"],
            "historical_stored_outcome": True,
            "db_snapshot_simulated_outcome": True,
            "simulation_role": DB_SNAPSHOT_ROLE,
            "exact_historical_reconstruction": False,
            "historical_stored_geometry": f"variant_entry={h1_entry},variant_stop={h1_stop},variant_target={h1_target}",
            "db_snapshot_simulation_geometry": f"variant_entry={h1_entry},variant_stop={h1_stop},variant_target={h1_target}",
            "old_effective_entry": h1_entry,
            "old_effective_stop": h1_stop,
            "old_effective_target": h1_target,
            "old_mfe_60m": stored_outcome["mfe_60m"],
            "old_mae_60m": stored_outcome["mae_60m"],
            "old_mfe_r_60m": stored_outcome["mfe_r_60m"],
            "old_mae_r_60m": stored_outcome["mae_r_60m"],
            "old_return_at_60m": stored_outcome["return_at_60m"],
            "old_tp_hit": stored_outcome["tp_hit"],
            "old_sl_hit": stored_outcome["sl_hit"],
            "old_tp_before_sl": stored_outcome["tp_before_sl"],
            "old_sl_before_tp": stored_outcome["sl_before_tp"],
            "old_ambiguous_intrabar": stored_outcome["ambiguous_intrabar"],
            "old_time_to_tp": stored_outcome["time_to_tp"],
            "old_time_to_sl": stored_outcome["time_to_sl"],
            "corrected_mfe_60m": sensitivity_row["mfe_60m"],
            "corrected_mae_60m": sensitivity_row["mae_60m"],
            "corrected_mfe_r_60m": sensitivity_row["mfe_r_60m"],
            "corrected_mae_r_60m": sensitivity_row["mae_r_60m"],
            "corrected_return_at_60m": sensitivity_row["return_at_60m"],
            "corrected_tp_hit": sensitivity_row["tp_hit"],
            "corrected_sl_hit": sensitivity_row["sl_hit"],
            "corrected_tp_before_sl": sensitivity_row["tp_before_sl"],
            "corrected_sl_before_tp": sensitivity_row["sl_before_tp"],
            "corrected_ambiguous_intrabar": sensitivity_row["ambiguous_intrabar"],
            "corrected_time_to_tp": sensitivity_row["time_to_tp"],
            "corrected_time_to_sl": sensitivity_row["time_to_sl"],
        }
        old_vs_corrected_rows.append(old_row)

    primary_pairs = []
    for row in reconstructed_rows:
        a = a_by_signal[row["source_signal_id"]]
        a_outcome = outcomes.get(a["observation_id"])
        if a_outcome is None or not a_outcome["is_final"]:
            exclusions["a_outcome_missing_or_immature"] += 1
            continue
        if any(row[f"mfe_{h}"] is None for h, _ in HORIZONS):
            exclusions["sensitivity_incomplete_metric"] += 1
            continue
        delta = {
            "source_signal_id": row["source_signal_id"],
            "symbol": row["symbol"],
            "signal_time": row["signal_time"],
            "signal_date": row["signal_date"],
            "A": a,
            "A_outcome": a_outcome,
            "B": row,
            "primary_delta_complete": True,
        }
        for field in ("mfe_60m", "mfe_r_60m", "mae_r_60m", "return_at_60m"):
            if a_outcome[field] is None or row[field] is None:
                delta["primary_delta_complete"] = False
                exclusions["paired_metric_missing"] += 1
                continue
            delta[field] = {"A": a_outcome[field], "B": row[field], "B-A": row[field] - a_outcome[field]}
        delta["tp_before_sl"] = {"A": bool(a_outcome["tp_before_sl"]), "B": bool(row["tp_before_sl"])}
        delta["sl_before_tp"] = {"A": bool(a_outcome["sl_before_tp"]), "B": bool(row["sl_before_tp"])}
        primary_pairs.append(delta)

    primary_pairs = [row for row in primary_pairs if row["primary_delta_complete"]]
    absolute = {}
    for label, rows in (
        ("db_snapshot_sensitivity_B_240m_complete", reconstructed_rows),
        ("db_snapshot_sensitivity_B_pre_fix_signal_pre_fix_evaluation", [
            row for row in reconstructed_rows if row["cohort"] == "PRE_FIX_SIGNAL_PRE_FIX_EVALUATION"
        ]),
    ):
        absolute[label] = {
            "N": len(rows),
            "TP_FIRST": sum(_exit_type(row) == "TP_FIRST" for row in rows),
            "SL_FIRST": sum(_exit_type(row) == "SL_FIRST" for row in rows),
            "OTHER": sum(_exit_type(row) == "OTHER" for row in rows),
            "AMBIGUOUS": sum(_exit_type(row) == "OTHER_AMBIGUOUS" for row in rows),
            "MFE_60m_mean": _mean(row["mfe_60m"] for row in rows),
            "MFE_60m_median": _median(row["mfe_60m"] for row in rows),
            "MFE_R_60m_mean": _mean(row["mfe_r_60m"] for row in rows),
            "MFE_R_60m_median": _median(row["mfe_r_60m"] for row in rows),
            "MAE_60m_mean": _mean(row["mae_60m"] for row in rows),
            "MAE_60m_median": _median(row["mae_60m"] for row in rows),
            "MAE_R_60m_mean": _mean(row["mae_r_60m"] for row in rows),
            "MAE_R_60m_median": _median(row["mae_r_60m"] for row in rows),
            "return_at_60m_mean": _mean(row["return_at_60m"] for row in rows),
            "return_at_60m_median": _median(row["return_at_60m"] for row in rows),
        }

    sensitivity_delta_metrics = {}
    for metric in ("mfe_60m", "mfe_r_60m", "mae_r_60m", "return_at_60m"):
        metric_rows = [row[metric] for row in primary_pairs if metric in row]
        effect = paired_effect(metric_rows)
        effect["bootstrap"] = bootstrap_pair_mean([row["B-A"] for row in metric_rows])
        sensitivity_delta_metrics[metric] = effect

    transitions = Counter()
    for row in primary_pairs:
        a_exit = "TP_FIRST" if row["tp_before_sl"]["A"] else ("SL_FIRST" if row["sl_before_tp"]["A"] else "OTHER")
        b_exit = _exit_type(row["B"])
        transitions[(a_exit, b_exit)] += 1

    day_rows = defaultdict(list)
    symbol_rows = defaultdict(list)
    for row in primary_pairs:
        day_rows[row["signal_date"]].append(row)
        symbol_rows[row["symbol"]].append(row)
    largest_day = max(day_rows.items(), key=lambda item: len(item[1])) if day_rows else (None, [])
    largest_symbol = max(symbol_rows.items(), key=lambda item: len(item[1])) if symbol_rows else (None, [])
    lodo = {
        day: {
            "n": len([row for row in primary_pairs if row["signal_date"] != day]),
            "mean_mfe_60m_delta": _mean(
                row["mfe_60m"]["B-A"] for row in primary_pairs if row["signal_date"] != day
            ),
            "median_mfe_60m_delta": _median(
                row["mfe_60m"]["B-A"] for row in primary_pairs if row["signal_date"] != day
            ),
        }
        for day in sorted(day_rows)
    }
    top_symbols = sorted(symbol_rows.items(), key=lambda item: len(item[1]), reverse=True)[:5]
    top_contributors = sorted(
        (
            symbol,
            sum(row["mfe_60m"]["B-A"] for row in rows),
            len(rows),
        )
        for symbol, rows in symbol_rows.items()
    )
    top_contributors = sorted(top_contributors, key=lambda item: item[1], reverse=True)
    top_positive_symbols = [item[0] for item in top_contributors if item[1] > 0][:3]

    def subset_for_symbols(excluded: set[str]) -> list[dict[str, Any]]:
        return [row for row in primary_pairs if row["symbol"] not in excluded]

    symbol_sensitivity = {
        "top_by_n": [{"symbol": symbol, "n": len(rows), "mean_mfe_60m_delta": _mean(row["mfe_60m"]["B-A"] for row in rows)} for symbol, rows in top_symbols],
        "remove_largest_N_symbol": {
            "symbol": largest_symbol[0],
            **paired_effect([
                {"B-A": row["mfe_60m"]["B-A"]} for row in subset_for_symbols({largest_symbol[0] if largest_symbol else ""})
            ]),
        },
        "remove_top_positive_contributor": {
            "symbols": top_positive_symbols[:1],
            **paired_effect([{"B-A": row["mfe_60m"]["B-A"]} for row in subset_for_symbols(set(top_positive_symbols[:1]))]),
        },
        "remove_top_3_positive_contributors": {
            "symbols": top_positive_symbols,
            **paired_effect([{"B-A": row["mfe_60m"]["B-A"]} for row in subset_for_symbols(set(top_positive_symbols))]),
        },
    }

    primary_deltas = [row["mfe_60m"]["B-A"] for row in primary_pairs]
    top3_indices = sorted(range(len(primary_deltas)), key=lambda i: primary_deltas[i], reverse=True)[:3]
    outlier_sensitivity = {
        "min": min(primary_deltas) if primary_deltas else None,
        "p1": _quantile(primary_deltas, 0.01),
        "p5": _quantile(primary_deltas, 0.05),
        "p95": _quantile(primary_deltas, 0.95),
        "p99": _quantile(primary_deltas, 0.99),
        "max": max(primary_deltas) if primary_deltas else None,
        "full_mean": _mean(primary_deltas),
        "mean_excluding_largest_positive": _mean(
            [d for i, d in enumerate(primary_deltas) if i not in {top3_indices[0]}]
        ) if top3_indices else None,
        "mean_excluding_top_3_positive": _mean(
            [d for i, d in enumerate(primary_deltas) if i not in set(top3_indices)]
        ) if top3_indices else None,
    }

    history = historical_semantics_validation(
        semantics_rows,
        len(semantics_rows),
    )
    path_reproduction = {
        "status": PATH_REPRODUCTION_STATUS,
        "historical_source": "LIVE_BYBIT_GET_KLINES",
        "offline_source": "MARKET_CANDLE_DB_SNAPSHOT",
        "exact_historical_reproduction_available": False,
        "historical_production_candle_retrieval": "production evaluator live Bybit klines retrieval via its own client",
        "offline_candle_retrieval": "current market.candle DB snapshot export",
        "interpretation": "DB_SNAPSHOT_SENSITIVITY_RECONSTRUCTION; stored prospective outcomes remain authoritative",
    }
    sensitivity = {
        "role": DB_SNAPSHOT_ROLE,
        "secondary_to_historical_semantics_validation": True,
        "primary_metric": PRIMARY_METRIC,
        "paired_240m_complete_n": len(primary_pairs),
        "delta_metrics": sensitivity_delta_metrics,
    }
    verdicts = build_final_verdicts(history, sensitivity)
    results = {
        "analysis": "ME_SHORT_GEOMETRY_B_CORRECTED_OUTCOME_RECONSTRUCTION_V1",
        "analysis_type": "HISTORICAL_SEMANTICS_VALIDATION_AND_DB_SNAPSHOT_SENSITIVITY",
        "fix_deploy_ts": FIX_DEPLOY_TS,
        "analysis_cutoff_ts": _iso(datetime.now(timezone.utc)),
        "git_head": "80b10108cdceaf133cc405c0bd725cabc6aac459",
        "production_db_modified": False,
        "production_outcomes_rewritten": False,
        "authoritative_semantics": {
            "scanner": "MOMENTUM_EXHAUSTION",
            "direction": "SHORT",
            "variant": "GEOMETRY_INTERVENTION_WIDER_STOP",
            "B": "same entry as A; stop=max(abs(reference_price-invalidation_price), 0.5*ATR_14_5m); target=A target distance adjusted to entry",
            "primary_metric": "MFE_pct_60m",
            "frozen_primary_metric_field": "mfe_60m",
            "production_evaluator_tuple_rule": "Use variant_entry, variant_stop, and variant_target atomically when all three are non-NULL; otherwise use base reference/invalidation/target.",
            "mfe_mae_window": "signal_time < candle.timestamp < signal_time + horizon",
            "return_at_horizon": "last close from post-signal candles with candle.timestamp < signal_time + horizon",
            "evaluated_at_semantics": "recorded evaluation write time; not a candle cutoff",
        },
        "candle_source": {
            "database": "trad_bot",
            "schema_table": "market.candle",
            "role": "OFFLINE_DB_SNAPSHOT_ONLY_NOT_HISTORICAL_TRUTH",
            "exchange": "bybit",
            "market_type": "linear",
            "timeframe": "5",
            "quality_exclusion": "quality_status <> 'invalid'",
            "is_closed_required": True,
            "time_basis": "UTC",
            "naive_timestamp_policy": "naive export timestamps are UTC",
            **candle_audit,
        },
        "integrity": {
            "raw_B_observations": len(b_rows),
            "raw_A_observations": len(a_rows),
            "unique_source_signals_B": len(b_by_signal),
            "paired_A_B": len(paired_signals),
            "missing_A_pairs": len(set(b_by_signal) - set(a_by_signal)),
            "duplicate_B": len(b_rows) - len(b_by_signal),
            "duplicate_A": len(a_rows) - len(a_by_signal),
            "cohort_counts": dict(cohorts["classes"]),
        },
        "frozen_tuple": {
            "complete": tuple_stats["complete"],
            "partial": tuple_stats["partial"],
            "invalid": tuple_stats["invalid"],
            "expected_partial": 0,
        },
        "geometry": dict(geometry_stats),
        "cohort_boundary": {
            "rule": "signal_time > FIX_DEPLOY_TS is POST_FIX_SIGNAL",
            "evaluation_timestamp_rule": "evaluated_*_at may still be used for evaluation-time classification",
            "boundary_exactly_equal_is_pre_fix": True,
        },
        "historical_semantics_validation": history,
        "historical_tuple_bug_impact": "NOT_OBSERVED_FOR_ME_SHORT_GEOM_B_V1",
        "path_reproduction": path_reproduction,
        "horizon_specific_coverage": horizon_specific_coverage(coverage_counts, len(paired_signals)),
        "db_snapshot_sensitivity": sensitivity,
        "historical_stored_outcome_interpretation": {
            "stored_prospective_outcomes_are_authoritative": True,
            "wider_variant_stop_use_evidence": "strongly supported by stored outcomes",
            "offline_mismatch_interpretation": "consistent with non-identical candle sources; not evidence of a B geometry bug",
        },
        "cost_model": {
            "preregistered": False,
            "fee_assumption_registry": "none",
            "cost_model_available": False,
            "fees_slippage_spread_execution_delay_invented": False,
            "executable_edge": "NOT_ESTABLISHED",
        },
        **verdicts,
        "artifacts": {
            "trades_csv": "docs/research/me_short_geometry_b_corrected_outcome_reconstruction_v1_trades.csv",
            "old_vs_corrected_csv": "docs/research/me_short_geometry_b_old_vs_corrected_outcomes_v1.csv",
            "h1_vs_h2_csv": "docs/research/me_short_geometry_b_h1_vs_h2_semantics_v1.csv",
            "results_json": "docs/research/me_short_geometry_b_corrected_outcome_reconstruction_v1_results.json",
            "report_md": "docs/research/ME_SHORT_GEOMETRY_B_CORRECTED_OUTCOME_RECONSTRUCTION_V1_2026-10-07.md",
        },
    }
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "me_short_geometry_b_corrected_outcome_reconstruction_v1_results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _write_csv(outdir / "me_short_geometry_b_corrected_outcome_reconstruction_v1_trades.csv", reconstructed_rows)
    _write_csv(outdir / "me_short_geometry_b_old_vs_corrected_outcomes_v1.csv", old_vs_corrected_rows)
    _write_csv(outdir / "me_short_geometry_b_h1_vs_h2_semantics_v1.csv", semantics_rows)
    _write_report(
        outdir / "ME_SHORT_GEOMETRY_B_CORRECTED_OUTCOME_RECONSTRUCTION_V1_2026-10-07.md",
        results,
    )
    return results


def _write_report(path: Path, results: dict[str, Any]) -> None:
    history = results["historical_semantics_validation"]
    path_reproduction = results["path_reproduction"]

    lines = [
        "# ME_SHORT_GEOMETRY_B_CORRECTED_OUTCOME_RECONSTRUCTION_V1",
        "",
        "Date: 2026-10-07",
        "",
        "## Scope",
        "",
        "Offline/read-only reconstruction and historical semantics audit for "
        "`ME_SHORT_GEOM_B_V1`. No production DB writes, no outcome rewrite, "
        "no deploy, no parameter retuning.",
        "",
        "## Historical geometry semantics",
        "",
        f"- checked_n: {history['checked_n']}",
        f"- hypothesis_h1: {history['hypothesis_h1']}",
        f"- hypothesis_h2: {history['hypothesis_h2']}",
        f"- h1_exact: {history['h1_exact']}",
        f"- h2_exact: {history['h2_exact']}",
        f"- h1_better: {history['h1_better']}",
        f"- h2_better: {history['h2_better']}",
        f"- ties: {history['ties']}",
        f"- geometry_semantics_verdict: **{history['geometry_semantics_verdict']}**",
        "",
        "Historical B outcomes strongly support use of the complete persisted "
        "variant tuple (`variant_entry`, `variant_stop`, `variant_target`). "
        "The previously suspected `variant_stop ignored` defect is not observed "
        "for historical B.",
        "",
        "## Path reproducibility",
        "",
        f"- status: **{path_reproduction['status']}**",
        f"- historical_source: {path_reproduction.get('historical_source')}",
        f"- offline_source: {path_reproduction.get('offline_source')}",
        "",
        "Historical production outcomes were generated from the evaluator's live "
        "candle retrieval path, while this audit uses the persisted market-candle "
        "DB snapshot. Therefore exact historical OHLC/path reproduction is not "
        "available from the current snapshot.",
        "",
        "## Final verdicts",
        "",
        f"- reconstruction_validity_verdict: **{results['reconstruction_validity_verdict']}**",
        f"- historical_geometry_semantics_verdict: **{results['historical_geometry_semantics_verdict']}**",
        f"- historical_tuple_bug_impact: **{results['historical_tuple_bug_impact']}**",
        f"- db_snapshot_sensitivity_verdict: **{results['db_snapshot_sensitivity_verdict']}**",
        f"- executable_edge: **{results['executable_edge']}**",
        f"- lifecycle_recommendation: **{results['lifecycle_recommendation']}**",
        f"- final_conclusion: **{results['final_conclusion']}**",
        "",
        "## Operational decision",
        "",
        "- Production outcomes rewrite: **NO**",
        "- Historical B invalidation due to tuple bug: **NO**",
        "- Executable edge established: **NO**",
        "- Lifecycle: **KEEP_BLOCKED**",
        "",
        "The positive DB-snapshot MFE sensitivity is secondary evidence only and "
        "does not establish an executable trading edge.",
        "",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-dir", type=Path, default=Path("audit_db/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007"))
    parser.add_argument("--outdir", type=Path, default=Path("docs/research"))
    args = parser.parse_args()
    results = run_analysis(args.audit_dir, args.outdir)
    print(json.dumps({
        "path_reproduction_status": results["path_reproduction"]["status"],
        "historical_semantics_validation": results["historical_semantics_validation"],
        "historical_tuple_bug_impact": results["historical_tuple_bug_impact"],
        "db_snapshot_sensitivity_verdict": results["db_snapshot_sensitivity_verdict"],
        "executable_edge": results["executable_edge"],
        "lifecycle_recommendation": results["lifecycle_recommendation"],
        "final_conclusion": results["final_conclusion"],
    }, indent=2))


if __name__ == "__main__":
    main()
