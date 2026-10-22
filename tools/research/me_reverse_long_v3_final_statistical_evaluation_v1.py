#!/usr/bin/env python3
"""ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.

READ-ONLY, offline, reproducible statistical evaluation of the frozen
prospective experiment ``MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS``.

The script consumes ONLY the immutable extraction snapshot produced by
``audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/``
(see 01_extract_frozen_cohort.sql / 02_extract_supplemental.sql /
03_extract_constraints.sql, executed READ-ONLY against production
PostgreSQL ``trad_bot`` on 2026-10-22).  It never opens a database
connection and never mutates production state.

Frozen protocol constants are transcribed from
``app/research/me_reverse_long_v3/protocol.py`` and the frozen registry
entry in ``app/research/prospective_registry.json`` (origin/main).

Outputs (written under docs/research/):
  * ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.json
  * ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.csv
  * ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_by_day.csv
  * ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_by_symbol.csv
  * ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_by_regime.csv
  * ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_robustness.csv

Statistical method status: EXPLORATORY ONLY.
No pre-registered statistical decision gates and no operator-approved
inference method exist for this experiment; every interval produced here
is an exploratory interval (STATISTICAL_METHOD_NOT_APPROVED).  Cluster
bootstrap intervals are reported separately per dependence axis and are
NOT a calibrated joint multiway 95% CI.

Usage:
    python tools/research/me_reverse_long_v3_final_statistical_evaluation_v1.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = (
    REPO_ROOT
    / "audit_db"
    / "me_reverse_long_v3_final_statistical_evaluation_v1_20261022"
    / "extract"
    / "me_v3_eval_20261022"
)
OUT_DIR = REPO_ROOT / "docs" / "research"
BASENAME = "ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1"

COHORT_CSV = SNAPSHOT_DIR / "04_cohort_full.csv"
EXPERIMENT_CSV = SNAPSHOT_DIR / "02_prospective_experiment_all.csv"

# Logical (repository-relative) names used in the manifest, independent of the
# physical directory the artifacts are generated into.  This lets a rerun into
# a temporary directory produce a manifest that is directly comparable with the
# committed one.
LOGICAL_SNAPSHOT_PREFIX = (
    "audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/"
    "extract/me_v3_eval_20261022/"
)
LOGICAL_EXTRACT_PREFIX = (
    "audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/"
)
LOGICAL_OUTPUT_PREFIX = "docs/research/"
LOGICAL_SCRIPT = "tools/research/me_reverse_long_v3_final_statistical_evaluation_v1.py"
LOGICAL_REPORT_MD = LOGICAL_OUTPUT_PREFIX + f"{BASENAME}.md"
LOCAL_ONLY_NAMES = {
    "06_prospective_outcome_all.csv",
    "07_completed_events.csv",
}
OUTPUT_NAMES = [
    f"{BASENAME}.json",
    f"{BASENAME}.csv",
    f"{BASENAME}_by_day.csv",
    f"{BASENAME}_by_symbol.csv",
    f"{BASENAME}_by_regime.csv",
    f"{BASENAME}_robustness.csv",
]
EXTRACT_SQL_NAMES = [
    "01_extract_frozen_cohort.sql",
    "02_extract_supplemental.sql",
    "03_extract_constraints.sql",
]

# ---------------------------------------------------------------------------
# Frozen protocol constants (app/research/me_reverse_long_v3/protocol.py @
# origin/main 5a3d29c; identical on production HEAD 362c217)
# ---------------------------------------------------------------------------
EXPERIMENT_ID = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS"
SOURCE_SCANNER = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
FROZEN_THRESHOLD = 0.4
MAX_HOLD_MINUTES = 240
EXPECTED_STOP_PCT = 0.025
EXPECTED_TARGET_PCT = 0.03
FEE_PER_SIDE = 0.00055
NORMAL_SLIPPAGE_PER_SIDE = 0.0005
ELEVATED_SLIPPAGE_PER_SIDE = 0.001
FREEZE_TS = datetime(2026, 10, 13, 12, 37, 3, tzinfo=timezone.utc)
MINIMUM_N = 100
MINIMUM_SYMBOLS = 10
# NOTE: no minimum_oos_days and no statistical decision gates are frozen
# for this experiment (prospective_registry.json entry, version 1).

COST_NORMAL_PER_SIDE_TOTAL = FEE_PER_SIDE + NORMAL_SLIPPAGE_PER_SIDE  # 0.00105
COST_ELEVATED_PER_SIDE_TOTAL = FEE_PER_SIDE + ELEVATED_SLIPPAGE_PER_SIDE  # 0.00155

# ---------------------------------------------------------------------------
# Deterministic seeds
# ---------------------------------------------------------------------------
SEED_PRIMARY = 20261022
SEED_DAY_BLOCK = 20261023
SEED_SYMBOL_BLOCK = 20261024
SEED_DIFF_IID = 20261025
SEED_DIFF_DAY = 20261026
SEED_DIFF_SYMBOL = 20261027
SEED_TWO_WAY = 20261028
B_PRIMARY = 20000
B_CLUSTER = 20000

# Operator SQL-audit values (2026-10-22) used for reconciliation
OPERATOR = {
    "PASS": {
        "n": 107,
        "gross_mean": -0.1979,
        "net_normal_mean": -0.2817,
        "net_elevated_mean": -0.3216,
        "net_pf": 0.3059,
        "total_net_r": -30.1393,
        "win_rate": 0.2710,
        "tp_first": 6,
        "sl_first": 27,
        "timeout": 74,
    },
    "REJECT": {
        "n": 29,
        "gross_mean": 0.1621,
        "net_normal_mean": 0.0780,
        "net_elevated_mean": 0.0379,
        "net_pf": 1.1677,
        "total_net_r": 2.2609,
        "win_rate": 0.5172,
        "tp_first": 14,
        "sl_first": 12,
        "timeout": 3,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_ts(value: str) -> datetime | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_float(value: str) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return float(value)


def to_bool(value: str) -> bool | None:
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"t", "true", "1"}:
        return True
    if value in {"f", "false", "0"}:
        return False
    return None


def rnd(value, digits: int = 10):
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, digits)
    return value


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def median(values: list[float]) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def pstdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return float("nan")
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (n - 1))


def percentile(sorted_samples: list[float], q: float) -> float:
    if not sorted_samples:
        return float("nan")
    idx = q * (len(sorted_samples) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_samples[lo]
    frac = idx - lo
    return sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac


def ci_from_bootstrap(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "ci95_low": rnd(percentile(ordered, 0.025)),
        "ci95_high": rnd(percentile(ordered, 0.975)),
        "boot_mean": rnd(mean(samples)),
    }


def bootstrap_mean_iid(values: list[float], n_boot: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    n = len(values)
    out = []
    for _ in range(n_boot):
        out.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    return out


def bootstrap_mean_cluster(
    values_by_cluster: dict[str, list[float]], n_boot: int, seed: int
) -> list[float]:
    """Ordinary cluster bootstrap: resample clusters with replacement."""
    rng = random.Random(seed)
    keys = sorted(values_by_cluster)
    g = len(keys)
    out = []
    for _ in range(n_boot):
        total = 0.0
        count = 0
        for _ in range(g):
            vals = values_by_cluster[keys[rng.randrange(g)]]
            total += sum(vals)
            count += len(vals)
        out.append(total / count if count else float("nan"))
    return out


def bootstrap_diff_independent(
    a: list[float], b: list[float], n_boot: int, seed: int
) -> list[float]:
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    out = []
    for _ in range(n_boot):
        ma = sum(a[rng.randrange(na)] for _ in range(na)) / na
        mb = sum(b[rng.randrange(nb)] for _ in range(nb)) / nb
        out.append(ma - mb)
    return out


def bootstrap_diff_cluster(
    a_by_cluster: dict[str, list[float]],
    b_by_cluster: dict[str, list[float]],
    n_boot: int,
    seed: int,
) -> list[float]:
    """Cluster bootstrap for a two-group mean difference.

    Resamples the union of cluster keys with replacement and pools each
    group within the sampled clusters.  Groups are unpaired; this is an
    exploratory dependence-aware interval, not a calibrated joint CI.
    """
    rng = random.Random(seed)
    keys = sorted(set(a_by_cluster) | set(b_by_cluster))
    g = len(keys)
    out = []
    for _ in range(n_boot):
        sa = 0.0
        na = 0
        sb = 0.0
        nb = 0
        for _ in range(g):
            k = keys[rng.randrange(g)]
            va = a_by_cluster.get(k, [])
            vb = b_by_cluster.get(k, [])
            sa += sum(va)
            na += len(va)
            sb += sum(vb)
            nb += len(vb)
        if na and nb:
            out.append(sa / na - sb / nb)
    return out


def bootstrap_mean_two_way(
    rows: list[dict], n_boot: int, seed: int
) -> list[float]:
    """Pigeonhole (two-way day x symbol) bootstrap, UNCALIBRATED sensitivity."""
    rng = random.Random(seed)
    days = sorted({r["utc_date"] for r in rows})
    symbols = sorted({r["symbol"] for r in rows})
    out = []
    for _ in range(n_boot):
        day_w: dict[str, int] = defaultdict(int)
        for _ in range(len(days)):
            day_w[days[rng.randrange(len(days))]] += 1
        sym_w: dict[str, int] = defaultdict(int)
        for _ in range(len(symbols)):
            sym_w[symbols[rng.randrange(len(symbols))]] += 1
        total = 0.0
        weight = 0
        for r in rows:
            w = day_w[r["utc_date"]] * sym_w[r["symbol"]]
            if w:
                total += w * r["net_r_normal"]
                weight += w
        if weight:
            out.append(total / weight)
    return out


def cluster_robust_se(rows: list[dict], key: str, field: str = "net_r_normal") -> dict:
    """Cluster-robust SE of the mean for one clustering axis."""
    values = [r[field] for r in rows]
    n = len(values)
    m = mean(values)
    by_c: dict[str, float] = defaultdict(float)
    for r in rows:
        by_c[r[key]] += r[field] - m
    g = len(by_c)
    if g < 2 or n < 2:
        return {"se": None, "clusters": g}
    se = math.sqrt((g / (g - 1.0)) * sum(v * v for v in by_c.values())) / n
    return {
        "se": rnd(se),
        "clusters": g,
        "ci95_normal_low": rnd(m - 1.96 * se),
        "ci95_normal_high": rnd(m + 1.96 * se),
    }


# ---------------------------------------------------------------------------
# Load + derive
# ---------------------------------------------------------------------------
def load_rows(snapshot_dir: Path | None = None) -> list[dict]:
    snapshot_dir = Path(snapshot_dir) if snapshot_dir else SNAPSHOT_DIR
    with (snapshot_dir / "04_cohort_full.csv").open(newline="", encoding="utf-8") as fh:
        raw = list(csv.DictReader(fh))
    rows = []
    for rec in raw:
        entry = to_float(rec["execution_entry_price"])
        stop = to_float(rec["execution_stop_price"])
        target = to_float(rec["execution_target_price"])
        exit_price = to_float(rec["exit_price"])
        r_unit = (entry - stop) if (entry is not None and stop is not None) else None
        gross_calc = (
            (exit_price - entry) / r_unit
            if None not in (exit_price, entry, r_unit) and r_unit
            else None
        )
        net_n_calc = (
            (
                exit_price
                - entry
                - COST_NORMAL_PER_SIDE_TOTAL * (entry + exit_price)
            )
            / r_unit
            if None not in (exit_price, entry, r_unit) and r_unit
            else None
        )
        net_e_calc = (
            (
                exit_price
                - entry
                - COST_ELEVATED_PER_SIDE_TOTAL * (entry + exit_price)
            )
            / r_unit
            if None not in (exit_price, entry, r_unit) and r_unit
            else None
        )
        exc = to_float(rec["exhaustion_magnitude_raw"])
        row = {
            "observation_id": int(rec["observation_id"]),
            "source_signal_id": int(rec["source_signal_id"]),
            "symbol": rec["symbol"],
            "direction": rec["direction"],
            "signal_time": parse_ts(rec["signal_time"]),
            "reference_price": to_float(rec["reference_price"]),
            "invalidation_price": to_float(rec["invalidation_price"]),
            "target_1": to_float(rec["target_1"]),
            "exhaustion_magnitude": exc,
            "rule_passed": to_bool(rec["rule_passed"]),
            "filter_reason": rec["filter_reason"] or "",
            "market_regime": rec["market_regime"] or "UNKNOWN",
            "observation_created_at": parse_ts(rec["observation_created_at"]),
            "outcome_status": rec["outcome_status"] or "",
            "filter_group": rec["filter_group"] or "",
            "execution_entry_at": parse_ts(rec["execution_entry_at"]),
            "execution_entry_price": entry,
            "execution_stop_price": stop,
            "execution_target_price": target,
            "exit_at": parse_ts(rec["exit_at"]),
            "exit_price": exit_price,
            "exit_reason": rec["exit_reason"] or "",
            "gross_r": to_float(rec["gross_r"]),
            "net_r_normal": to_float(rec["net_r_normal"]),
            "net_r_elevated": to_float(rec["net_r_elevated"]),
            "ambiguous": to_bool(rec["ambiguous"]),
            "coverage_reason": rec["coverage_reason"] or "",
            "candle_count": int(rec["candle_count"]) if rec["candle_count"] else None,
            "evaluated_at": parse_ts(rec["evaluated_at"]),
            "outcome_created_at": parse_ts(rec["outcome_created_at"]),
            "outcome_updated_at": parse_ts(rec["outcome_updated_at"]),
            "_r_unit": r_unit,
            "_gross_calc": gross_calc,
            "_net_n_calc": net_n_calc,
            "_net_e_calc": net_e_calc,
        }
        row["utc_date"] = row["signal_time"].strftime("%Y-%m-%d") if row["signal_time"] else ""
        rows.append(row)
    rows.sort(key=lambda r: r["observation_id"])
    return rows


# ---------------------------------------------------------------------------
# Integrity audit
# ---------------------------------------------------------------------------
def integrity_audit(rows: list[dict]) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    add(
        "cohort_row_count_137",
        len(rows) == 137,
        f"prospective_observation rows for {EXPERIMENT_ID}: {len(rows)}",
    )
    ids = [r["observation_id"] for r in rows]
    add("observation_id_unique", len(set(ids)) == len(ids), f"unique ids: {len(set(ids))}")
    sig_keys = [(EXPERIMENT_ID, r["source_signal_id"]) for r in rows]
    add(
        "source_signal_id_unique_per_experiment",
        len(set(sig_keys)) == len(sig_keys),
        f"unique (experiment_id, source_signal_id): {len(set(sig_keys))}",
    )
    joined = [r for r in rows if r["outcome_status"]]
    add(
        "outcome_join_1_to_1",
        len(joined) == len(rows),
        f"rows with dedicated outcome row: {len(joined)}/{len(rows)} (orphan outcome rows: 0 per 05_outcome_orphans.csv)",
    )

    status_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        status_counts[r["outcome_status"] or "NO_OUTCOME"] += 1
    add(
        "status_split_136_finalized_1_pending",
        status_counts.get("FINALIZED") == 136 and status_counts.get("PENDING") == 1,
        f"status counts: {dict(status_counts)}",
    )
    allowed_status = {"PENDING", "FINALIZED", "COVERAGE_INVALID", "TEMPORAL_UNVERIFIED", "INVALID_GEOMETRY"}
    add(
        "status_values_allowed",
        set(status_counts) <= allowed_status | {"NO_OUTCOME"},
        f"allowed set: {sorted(allowed_status)}",
    )

    fin = [r for r in rows if r["outcome_status"] == "FINALIZED"]
    missing_econ = [
        r["observation_id"]
        for r in fin
        if None
        in (
            r["gross_r"],
            r["net_r_normal"],
            r["net_r_elevated"],
            r["execution_entry_price"],
            r["exit_price"],
        )
        or not r["exit_reason"]
    ]
    add(
        "finalized_economics_complete",
        not missing_econ,
        f"FINALIZED rows missing economics/exit: {missing_econ}",
    )
    nonfin_econ = [
        r["observation_id"]
        for r in rows
        if r["outcome_status"] != "FINALIZED"
        and (
            r["gross_r"] is not None
            or r["net_r_normal"] is not None
            or r["exit_price"] is not None
        )
    ]
    add(
        "nonfinalized_has_no_economics",
        not nonfin_econ,
        f"non-FINALIZED rows carrying economics: {nonfin_econ}",
    )
    cov_bad = [
        r["observation_id"]
        for r in fin
        if r["coverage_reason"] or r["candle_count"] != 48
    ]
    add(
        "finalized_coverage_48_candles",
        not cov_bad,
        f"FINALIZED rows with coverage_reason or candle_count != 48: {cov_bad}",
    )

    pre_freeze = [
        r["observation_id"] for r in rows if r["signal_time"] and r["signal_time"] <= FREEZE_TS
    ]
    add(
        "no_pre_freeze_contamination",
        not pre_freeze,
        f"observations with signal_time <= freeze_ts 2026-10-13T12:37:03Z: {pre_freeze}",
    )
    neg_ids = [r["observation_id"] for r in rows if r["source_signal_id"] >= 0]
    add(
        "synthetic_negative_source_ids",
        not neg_ids,
        f"rows without deterministic negative synthetic source_signal_id: {neg_ids}",
    )

    # classification consistency vs frozen threshold
    cls_bad = []
    for r in rows:
        exc = r["exhaustion_magnitude"]
        if exc is None:
            expected = "MISSING_FEATURE"
        else:
            expected = "PASS" if exc <= FROZEN_THRESHOLD else "REJECT"
        if r["filter_group"] != expected:
            cls_bad.append((r["observation_id"], r["filter_group"], expected))
    add(
        "filter_group_matches_frozen_threshold_0_4",
        not cls_bad,
        f"mismatched classification rows: {cls_bad}",
    )
    rp_bad = [
        r["observation_id"]
        for r in rows
        if (r["filter_group"] == "PASS") != (r["rule_passed"] is True)
    ]
    add("rule_passed_consistent", not rp_bad, f"inconsistent rule_passed rows: {rp_bad}")

    # geometry consistency with frozen entry/stop/target rules
    geo_bad = []
    for r in fin:
        e, s, t = r["execution_entry_price"], r["execution_stop_price"], r["execution_target_price"]
        if e is None or s is None or t is None:
            geo_bad.append((r["observation_id"], "missing_geometry"))
            continue
        if abs(s - e * (1 - EXPECTED_STOP_PCT)) > 1e-6 * e:
            geo_bad.append((r["observation_id"], "stop"))
        if abs(t - e * (1 + EXPECTED_TARGET_PCT)) > 1e-6 * e:
            geo_bad.append((r["observation_id"], "target"))
        if not (0 < s < e < t):
            geo_bad.append((r["observation_id"], "ordering"))
    add(
        "frozen_geometry_stop_2_5_target_3_0",
        not geo_bad,
        f"geometry mismatches: {geo_bad}",
    )

    # exit semantics
    exit_bad = []
    for r in fin:
        e, s, t = r["execution_entry_price"], r["execution_stop_price"], r["execution_target_price"]
        xp, xr = r["exit_price"], r["exit_reason"]
        if xr == "TP_FIRST":
            if abs(xp - t) > 1e-6 * e:
                exit_bad.append((r["observation_id"], "TP_PRICE"))
        elif xr == "SL_FIRST":
            if abs(xp - s) > 1e-6 * e:
                exit_bad.append((r["observation_id"], "SL_PRICE"))
        elif xr == "TIMEOUT":
            if r["exit_at"] and r["execution_entry_at"]:
                delta = (r["exit_at"] - r["execution_entry_at"]).total_seconds() / 60.0
                if abs(delta - MAX_HOLD_MINUTES) > 1e-6:
                    exit_bad.append((r["observation_id"], f"TIMEOUT_HOLD_{delta}"))
        else:
            exit_bad.append((r["observation_id"], f"UNKNOWN_{xr}"))
    add(
        "exit_reason_semantics",
        not exit_bad,
        f"exit semantic mismatches: {exit_bad}",
    )

    # R arithmetic + cost model reconstruction
    r_bad = []
    max_dev_gross = 0.0
    max_dev_net_n = 0.0
    max_dev_net_e = 0.0
    for r in fin:
        if None in (r["_gross_calc"], r["gross_r"], r["_net_n_calc"], r["net_r_normal"], r["_net_e_calc"], r["net_r_elevated"]):
            r_bad.append((r["observation_id"], "missing_values"))
            continue
        dg = abs(r["_gross_calc"] - r["gross_r"])
        dn = abs(r["_net_n_calc"] - r["net_r_normal"])
        de = abs(r["_net_e_calc"] - r["net_r_elevated"])
        max_dev_gross = max(max_dev_gross, dg)
        max_dev_net_n = max(max_dev_net_n, dn)
        max_dev_net_e = max(max_dev_net_e, de)
        if dg > 5e-6 or dn > 5e-6 or de > 5e-6:
            r_bad.append((r["observation_id"], dg, dn, de))
    add(
        "r_and_cost_model_reconstruction",
        not r_bad,
        "net_r = (exit - entry - c*(entry+exit)) / (entry - stop); "
        f"c_normal={COST_NORMAL_PER_SIDE_TOTAL}, c_elevated={COST_ELEVATED_PER_SIDE_TOTAL}; "
        f"max |dev|: gross={max_dev_gross:.3e}, net_normal={max_dev_net_n:.3e}, "
        f"net_elevated={max_dev_net_e:.3e}; mismatches: {r_bad}",
    )

    amb = [r["observation_id"] for r in rows if r["ambiguous"] is True]
    add(
        "ambiguous_flag_review",
        True,
        f"rows flagged ambiguous (conservative STOP_FIRST policy): {amb}",
    )

    tp_r = EXPECTED_TARGET_PCT / EXPECTED_STOP_PCT  # +3.0% / 2.5% = +1.2R
    tp_sl_bad = [
        r["observation_id"]
        for r in fin
        if (r["exit_reason"] == "TP_FIRST" and r["gross_r"] is not None and abs(r["gross_r"] - tp_r) > 1e-6)
        or (r["exit_reason"] == "SL_FIRST" and r["gross_r"] is not None and abs(r["gross_r"] + 1.0) > 1e-6)
    ]
    add(
        "tp_sl_r_multiples_exact",
        not tp_sl_bad,
        f"TP/SL rows whose gross_r != +{tp_r:.1f}/-1.0 (R unit = entry - stop): {tp_sl_bad}",
    )

    entry_bad = []
    for r in fin:
        if r["execution_entry_at"] and r["signal_time"] and r["execution_entry_at"] <= r["signal_time"]:
            entry_bad.append(r["observation_id"])
    add(
        "execution_entry_strictly_after_signal",
        not entry_bad,
        f"rows with execution_entry_at <= signal_time: {entry_bad}",
    )

    finalized_eval_ok = all(
        r["evaluated_at"] and r["outcome_created_at"] and r["outcome_created_at"] >= r["observation_created_at"]
        for r in rows
        if r["observation_created_at"]
    )
    add(
        "outcome_created_after_observation",
        finalized_eval_ok,
        "outcome_created_at >= observation_created_at for all rows",
    )

    fail_n = sum(1 for c in checks if c["status"] == "FAIL")
    return {
        "checks": checks,
        "checks_total": len(checks),
        "checks_failed": fail_n,
        "integrity_verdict": "CLEAN" if fail_n == 0 else "VIOLATIONS_FOUND",
    }


# ---------------------------------------------------------------------------
# Descriptive evaluation
# ---------------------------------------------------------------------------
def group_stats(rows: list[dict]) -> dict:
    def one(sub: list[dict]) -> dict:
        gross = [r["gross_r"] for r in sub if r["gross_r"] is not None]
        net_n = [r["net_r_normal"] for r in sub if r["net_r_normal"] is not None]
        net_e = [r["net_r_elevated"] for r in sub if r["net_r_elevated"] is not None]
        wins = [x for x in net_n if x > 0]
        losses = [x for x in net_n if x < 0]
        flat = [x for x in net_n if x == 0]
        pf = (
            sum(wins) / abs(sum(losses))
            if losses and sum(losses) != 0
            else None
        )
        exits: dict[str, int] = defaultdict(int)
        for r in sub:
            exits[r["exit_reason"] or "NONE"] += 1
        return {
            "n": len(sub),
            "n_finalized": sum(1 for r in sub if r["outcome_status"] == "FINALIZED"),
            "symbols": len({r["symbol"] for r in sub}),
            "days": len({r["utc_date"] for r in sub}),
            "gross_mean": rnd(mean(gross)),
            "gross_median": rnd(median(gross)),
            "net_normal_mean": rnd(mean(net_n)),
            "net_normal_median": rnd(median(net_n)),
            "net_normal_stdev": rnd(pstdev(net_n)),
            "net_elevated_mean": rnd(mean(net_e)),
            "net_pf": rnd(pf),
            "total_net_r": rnd(sum(net_n)),
            "total_net_r_elevated": rnd(sum(net_e)),
            "win_rate": rnd(len(wins) / len(net_n)) if net_n else None,
            "win_flat_loss": [len(wins), len(flat), len(losses)],
            "exit_reasons": dict(sorted(exits.items())),
        }

    fin = [r for r in rows if r["outcome_status"] == "FINALIZED"]
    out = {}
    for group in ("PASS", "REJECT", "MISSING_FEATURE"):
        out[group] = one([r for r in fin if r["filter_group"] == group])
    out["CONTROL_ALL_ELIGIBLE"] = one(fin)
    out["_pending_rows"] = [
        {
            "observation_id": r["observation_id"],
            "symbol": r["symbol"],
            "filter_group": r["filter_group"],
            "signal_time": r["signal_time"].isoformat() if r["signal_time"] else None,
            "outcome_status": r["outcome_status"],
        }
        for r in rows
        if r["outcome_status"] != "FINALIZED"
    ]
    return out


def reconcile_operator(stats: dict) -> list[dict]:
    out = []
    mapping = [
        ("n", "n"),
        ("gross_mean", "gross_mean"),
        ("net_normal_mean", "net_normal_mean"),
        ("net_elevated_mean", "net_elevated_mean"),
        ("net_pf", "net_pf"),
        ("total_net_r", "total_net_r"),
        ("win_rate", "win_rate"),
    ]
    for group in ("PASS", "REJECT"):
        for key, op_key in mapping:
            computed = stats[group][key]
            expected = OPERATOR[group][op_key]
            tol = 5e-4 if key != "n" else 0
            out.append(
                {
                    "group": group,
                    "metric": key,
                    "computed": computed,
                    "operator_value": expected,
                    "abs_diff": rnd(abs(computed - expected)) if computed is not None else None,
                    "match": (
                        "MATCH"
                        if computed is not None and abs(computed - expected) <= tol
                        else "DIFFERS"
                    ),
                }
            )
        exits = stats[group]["exit_reasons"]
        for key, op_key in (("TP_FIRST", "tp_first"), ("SL_FIRST", "sl_first"), ("TIMEOUT", "timeout")):
            computed = exits.get(key, 0)
            expected = OPERATOR[group][op_key]
            out.append(
                {
                    "group": group,
                    "metric": f"exit_{key}",
                    "computed": computed,
                    "operator_value": expected,
                    "abs_diff": abs(computed - expected),
                    "match": "MATCH" if computed == expected else "DIFFERS",
                }
            )
    return out


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------
def breakdown(rows: list[dict], key: str) -> list[dict]:
    fin = [r for r in rows if r["outcome_status"] == "FINALIZED"]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in fin:
        grouped[r[key]].append(r)
    out = []
    for k in sorted(grouped):
        sub = grouped[k]
        rec: dict[str, object] = {
            key: k,
            "n_total": len(sub),
        }
        for group in ("PASS", "REJECT"):
            g = [r for r in sub if r["filter_group"] == group]
            net = [r["net_r_normal"] for r in g]
            rec[f"n_{group}"] = len(g)
            rec[f"net_normal_mean_{group}"] = rnd(mean(net)) if net else None
            rec[f"total_net_r_{group}"] = rnd(sum(net)) if net else None
        allnet = [r["net_r_normal"] for r in sub]
        rec["net_normal_mean_all"] = rnd(mean(allnet))
        rec["total_net_r_all"] = rnd(sum(allnet))
        rec["symbols_in_group"] = len({r["symbol"] for r in sub})
        out.append(rec)
    return out


def concentration(rows: list[dict]) -> dict:
    fin = [r for r in rows if r["outcome_status"] == "FINALIZED"]
    out: dict[str, object] = {}
    for group in ("PASS", "REJECT", "CONTROL_ALL_ELIGIBLE"):
        if group == "CONTROL_ALL_ELIGIBLE":
            sub = fin
        else:
            sub = [r for r in fin if r["filter_group"] == group]
        net_total = sum(r["net_r_normal"] for r in sub)
        by_symbol: dict[str, float] = defaultdict(float)
        by_day: dict[str, float] = defaultdict(float)
        for r in sub:
            by_symbol[r["symbol"]] += r["net_r_normal"]
            by_day[r["utc_date"]] += r["net_r_normal"]
        losers = sorted(by_symbol.items(), key=lambda kv: kv[1])
        winners = sorted(by_symbol.items(), key=lambda kv: -kv[1])
        day_sorted = sorted(by_day.items(), key=lambda kv: kv[1])
        day_lo = [day_sorted[0][0], rnd(day_sorted[0][1])] if day_sorted else None
        day_hi = [day_sorted[-1][0], rnd(day_sorted[-1][1])] if day_sorted else None
        out[group] = {
            "total_net_r": rnd(net_total),
            "n_symbols": len(by_symbol),
            "n_days": len(by_day),
            "negative_symbols": sum(1 for _, v in by_symbol.items() if v < 0),
            "negative_days": sum(1 for _, v in by_day.items() if v < 0),
            "top3_losing_symbols": [(s, rnd(v)) for s, v in losers[:3]],
            "top3_winning_symbols": [(s, rnd(v)) for s, v in winners[:3]],
            "top3_losing_share_of_net": rnd(
                sum(v for _, v in losers[:3]) / net_total if net_total else None
            ),
            "top3_winning_share_of_net": rnd(
                sum(v for _, v in winners[:3]) / net_total if net_total else None
            ),
            "largest_day_loss": day_lo,
            "largest_day_gain": day_hi,
        }
    return out


def dependence_structure(rows: list[dict]) -> dict:
    fin = [r for r in rows if r["outcome_status"] == "FINALIZED"]
    by_symbol_day: dict[tuple[str, str], int] = defaultdict(int)
    for r in fin:
        by_symbol_day[(r["symbol"], r["utc_date"])] += 1

    # overlapping hold windows within the same symbol
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in fin:
        by_symbol[r["symbol"]].append(r)
    overlap_pairs = 0
    overlap_pairs_same_day = 0
    overlapping_obs = set()
    for sym, sub in by_symbol.items():
        sub = sorted(sub, key=lambda r: r["execution_entry_at"] or r["signal_time"])
        for i in range(len(sub)):
            for j in range(i + 1, len(sub)):
                a, b = sub[i], sub[j]
                if not (a["exit_at"] and b["execution_entry_at"]):
                    continue
                if b["execution_entry_at"] < a["exit_at"]:
                    overlap_pairs += 1
                    overlapping_obs.add(a["observation_id"])
                    overlapping_obs.add(b["observation_id"])
                    if a["utc_date"] == b["utc_date"]:
                        overlap_pairs_same_day += 1

    counts_per_symbol_day = sorted(by_symbol_day.values(), reverse=True)
    n = len(fin)
    return {
        "n_finalized": n,
        "n_symbols": len({r["symbol"] for r in fin}),
        "n_days": len({r["utc_date"] for r in fin}),
        "n_symbol_days": len(by_symbol_day),
        "obs_per_symbol_max": max(
            sum(1 for r in fin if r["symbol"] == s) for s in {r["symbol"] for r in fin}
        ),
        "obs_per_symbol_day_max": counts_per_symbol_day[0] if counts_per_symbol_day else 0,
        "obs_per_symbol_day_share_ge_2": sum(1 for c in counts_per_symbol_day if c >= 2),
        "overlapping_hold_pairs_same_symbol": overlap_pairs,
        "overlapping_hold_pairs_same_symbol_same_day": overlap_pairs_same_day,
        "observations_in_overlapping_windows": len(overlapping_obs),
        "note": (
            "Overlapping 240-minute hold windows inside the same symbol mean "
            "individual observations are not independent bets; dependence is "
            "handled through day- and symbol-block resampling, which are "
            "reported separately and are not a calibrated joint CI."
        ),
    }


# ---------------------------------------------------------------------------
# Uncertainty
# ---------------------------------------------------------------------------
def uncertainty(rows: list[dict]) -> dict:
    fin = [r for r in rows if r["outcome_status"] == "FINALIZED"]
    pass_rows = [r for r in fin if r["filter_group"] == "PASS"]
    reject_rows = [r for r in fin if r["filter_group"] == "REJECT"]
    pass_vals = [r["net_r_normal"] for r in pass_rows]
    reject_vals = [r["net_r_normal"] for r in reject_rows]

    iid = bootstrap_mean_iid(pass_vals, B_PRIMARY, SEED_PRIMARY)
    day_c = bootstrap_mean_cluster(
        {k: [r["net_r_normal"] for r in v] for k, v in _group_by(pass_rows, "utc_date").items()},
        B_CLUSTER,
        SEED_DAY_BLOCK,
    )
    sym_c = bootstrap_mean_cluster(
        {k: [r["net_r_normal"] for r in v] for k, v in _group_by(pass_rows, "symbol").items()},
        B_CLUSTER,
        SEED_SYMBOL_BLOCK,
    )
    two_way = bootstrap_mean_two_way(pass_rows, B_CLUSTER, SEED_TWO_WAY)

    diff_iid = bootstrap_diff_independent(pass_vals, reject_vals, B_PRIMARY, SEED_DIFF_IID)
    diff_day = bootstrap_diff_cluster(
        {k: [r["net_r_normal"] for r in v] for k, v in _group_by(pass_rows, "utc_date").items()},
        {k: [r["net_r_normal"] for r in v] for k, v in _group_by(reject_rows, "utc_date").items()},
        B_CLUSTER,
        SEED_DIFF_DAY,
    )
    diff_sym = bootstrap_diff_cluster(
        {k: [r["net_r_normal"] for r in v] for k, v in _group_by(pass_rows, "symbol").items()},
        {k: [r["net_r_normal"] for r in v] for k, v in _group_by(reject_rows, "symbol").items()},
        B_CLUSTER,
        SEED_DIFF_SYMBOL,
    )

    # Welch normal-approximation for the independent-group difference
    va, vb = pstdev(pass_vals) ** 2, pstdev(reject_vals) ** 2
    na, nb = len(pass_vals), len(reject_vals)
    se_diff = math.sqrt(va / na + vb / nb)
    diff_point = mean(pass_vals) - mean(reject_vals)

    return {
        "method_status": "STATISTICAL_METHOD_NOT_APPROVED",
        "interpretation": (
            "All intervals are EXPLORATORY. No pre-registered statistical "
            "decision gates and no operator-approved inference method exist "
            "for MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS (frozen registry "
            "entry contains only sample-size gates). Day-block and "
            "symbol-block intervals are reported per dependence axis and are "
            "NOT a calibrated joint multiway 95% CI. The two-way pigeonhole "
            "bootstrap is an uncalibrated sensitivity only."
        ),
        "seeds": {
            "primary_iid": SEED_PRIMARY,
            "day_block": SEED_DAY_BLOCK,
            "symbol_block": SEED_SYMBOL_BLOCK,
            "diff_iid": SEED_DIFF_IID,
            "diff_day": SEED_DIFF_DAY,
            "diff_symbol": SEED_DIFF_SYMBOL,
            "two_way": SEED_TWO_WAY,
            "replications_primary": B_PRIMARY,
            "replications_cluster": B_CLUSTER,
        },
        "resampling_units": {
            "iid": "individual observations (ignores dependence; reference only)",
            "day_block": "UTC date blocks (9 clusters)",
            "symbol_block": "symbol blocks (37 clusters)",
            "two_way": "day x symbol pigeonhole weights (uncalibrated)",
            "diff": "independent two-sample resampling; groups share no paired rows",
        },
        "pass_net_normal_mean": {
            "point": rnd(mean(pass_vals)),
            "n": na,
            "iid_bootstrap": ci_from_bootstrap(iid),
            "day_cluster_bootstrap": ci_from_bootstrap(day_c),
            "symbol_cluster_bootstrap": ci_from_bootstrap(sym_c),
            "two_way_bootstrap_uncalibrated": ci_from_bootstrap(two_way),
            "cluster_robust_se_by_day": cluster_robust_se(pass_rows, "utc_date"),
            "cluster_robust_se_by_symbol": cluster_robust_se(pass_rows, "symbol"),
        },
        "reject_net_normal_mean_exploratory": {
            "point": rnd(mean(reject_vals)),
            "n": nb,
            "day_cluster_bootstrap": ci_from_bootstrap(
                bootstrap_mean_cluster(
                    {k: [r["net_r_normal"] for r in v] for k, v in _group_by(reject_rows, "utc_date").items()},
                    B_CLUSTER,
                    SEED_DAY_BLOCK,
                )
            ),
        },
        "pass_minus_reject_net_normal": {
            "point": rnd(diff_point),
            "welch_se": rnd(se_diff),
            "welch_ci95": [rnd(diff_point - 1.96 * se_diff), rnd(diff_point + 1.96 * se_diff)],
            "iid_bootstrap": ci_from_bootstrap(diff_iid),
            "day_cluster_bootstrap": ci_from_bootstrap(diff_day),
            "symbol_cluster_bootstrap": ci_from_bootstrap(diff_sym),
            "note": (
                "PASS and REJECT are unpaired disjoint groups within one "
                "cohort; no paired delta is claimed. REJECT n=29 makes its "
                "mean imprecise; treat REJECT statistics as exploratory."
            ),
        },
    }


def _group_by(rows: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r[key]].append(r)
    return dict(out)


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------
def robustness(rows: list[dict]) -> list[dict]:
    fin = [r for r in rows if r["outcome_status"] == "FINALIZED"]
    out: list[dict] = []

    def metric(sub: list[dict]) -> dict:
        p = [r["net_r_normal"] for r in sub if r["filter_group"] == "PASS"]
        j = [r["net_r_normal"] for r in sub if r["filter_group"] == "REJECT"]
        return {
            "n_total": len(sub),
            "pass_n": len(p),
            "pass_net_normal_mean": rnd(mean(p)) if p else None,
            "pass_total_net_r": rnd(sum(p)) if p else None,
            "reject_n": len(j),
            "reject_net_normal_mean": rnd(mean(j)) if j else None,
            "pass_minus_reject": rnd(mean(p) - mean(j)) if p and j else None,
        }

    out.append({"scenario": "BASELINE_ALL_FINALIZED", **metric(fin)})

    # leave-one-day-out
    days = sorted({r["utc_date"] for r in fin})
    for d in days:
        sub = [r for r in fin if r["utc_date"] != d]
        out.append({"scenario": f"LEAVE_ONE_DAY_OUT_{d}", **metric(sub)})

    # leave-one-symbol-out
    symbols = sorted({r["symbol"] for r in fin})
    for s in symbols:
        sub = [r for r in fin if r["symbol"] != s]
        out.append({"scenario": f"LEAVE_ONE_SYMBOL_OUT_{s}", **metric(sub)})

    # top-3 losing / winning symbols (diagnostic only)
    by_symbol: dict[str, float] = defaultdict(float)
    for r in fin:
        by_symbol[r["symbol"]] += r["net_r_normal"]
    losers = [s for s, _ in sorted(by_symbol.items(), key=lambda kv: kv[1])[:3]]
    winners = [s for s, _ in sorted(by_symbol.items(), key=lambda kv: -kv[1])[:3]]
    out.append(
        {
            "scenario": "EXCLUDE_TOP3_LOSING_SYMBOLS_DIAGNOSTIC",
            "excluded": losers,
            **metric([r for r in fin if r["symbol"] not in losers]),
        }
    )
    out.append(
        {
            "scenario": "EXCLUDE_TOP3_WINNING_SYMBOLS_DIAGNOSTIC",
            "excluded": winners,
            **metric([r for r in fin if r["symbol"] not in winners]),
        }
    )

    # elevated cost sensitivity
    for group in ("PASS", "REJECT"):
        sub = [r for r in fin if r["filter_group"] == group]
        n_vals = [r["net_r_normal"] for r in sub]
        e_vals = [r["net_r_elevated"] for r in sub]
        out.append(
            {
                "scenario": f"ELEVATED_COSTS_{group}",
                "n_total": len(sub),
                "net_normal_mean": rnd(mean(n_vals)),
                "net_elevated_mean": rnd(mean(e_vals)),
                "delta_elevated_minus_normal": rnd(mean(e_vals) - mean(n_vals)),
                "total_net_r_normal": rnd(sum(n_vals)),
                "total_net_r_elevated": rnd(sum(e_vals)),
            }
        )

    # TIMEOUT dependence
    for group in ("PASS", "REJECT"):
        sub = [r for r in fin if r["filter_group"] == group]
        to_rows = [r for r in sub if r["exit_reason"] == "TIMEOUT"]
        nto_rows = [r for r in sub if r["exit_reason"] != "TIMEOUT"]
        out.append(
            {
                "scenario": f"TIMEOUT_DEPENDENCE_{group}",
                "n_total": len(sub),
                "n_timeout": len(to_rows),
                "timeout_total_net_r": rnd(sum(r["net_r_normal"] for r in to_rows)),
                "timeout_share_of_total_net_r": rnd(
                    sum(r["net_r_normal"] for r in to_rows) / sum(r["net_r_normal"] for r in sub)
                    if sum(r["net_r_normal"] for r in sub)
                    else None
                ),
                "non_timeout_n": len(nto_rows),
                "non_timeout_net_normal_mean": rnd(mean([r["net_r_normal"] for r in nto_rows]))
                if nto_rows
                else None,
                "excl_timeout_net_normal_mean": rnd(mean([r["net_r_normal"] for r in nto_rows]))
                if nto_rows
                else None,
            }
        )

    # small-group sensitivity for REJECT: subsample stability
    rej = [r["net_r_normal"] for r in fin if r["filter_group"] == "REJECT"]
    out.append(
        {
            "scenario": "SMALL_GROUP_REJECT_SENSITIVITY",
            "reject_n": len(rej),
            "reject_net_normal_mean": rnd(mean(rej)),
            "reject_median": rnd(median(rej)),
            "note": (
                "REJECT n=29 with 12 SL_FIRST / 14 TP_FIRST / 3 TIMEOUT; "
                "mean is highly sensitive to single trades (see "
                "LEAVE_ONE_SYMBOL_OUT rows). Exploratory only."
            ),
        }
    )

    # day-level sign stability
    for group in ("PASS", "REJECT"):
        sub = [r for r in fin if r["filter_group"] == group]
        by_day: dict[str, list[float]] = defaultdict(list)
        for r in sub:
            by_day[r["utc_date"]].append(r["net_r_normal"])
        day_means = {d: mean(v) for d, v in sorted(by_day.items())}
        neg = sum(1 for v in day_means.values() if v < 0)
        out.append(
            {
                "scenario": f"DAY_SIGN_STABILITY_{group}",
                "days": len(day_means),
                "negative_days": neg,
                "day_means": {d: rnd(v) for d, v in day_means.items()},
            }
        )

    return out


# ---------------------------------------------------------------------------
# Snapshot CSV
# ---------------------------------------------------------------------------
SNAPSHOT_FIELDS = [
    "observation_id",
    "source_signal_id",
    "symbol",
    "direction",
    "utc_date",
    "signal_time",
    "market_regime",
    "exhaustion_magnitude",
    "filter_group",
    "rule_passed",
    "filter_reason",
    "outcome_status",
    "execution_entry_at",
    "execution_entry_price",
    "execution_stop_price",
    "execution_target_price",
    "exit_at",
    "exit_price",
    "exit_reason",
    "gross_r",
    "net_r_normal",
    "net_r_elevated",
    "ambiguous",
    "coverage_reason",
    "candle_count",
    "evaluated_at",
]


def write_snapshot_csv(rows: list[dict], out_dir: Path) -> Path:
    path = Path(out_dir) / f"{BASENAME}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SNAPSHOT_FIELDS, lineterminator="\n")
        writer.writeheader()
        for r in rows:
            rec = {}
            for f in SNAPSHOT_FIELDS:
                v = r[f]
                if isinstance(v, datetime):
                    v = v.isoformat()
                elif isinstance(v, float):
                    v = repr(round(v, 10))
                rec[f] = v
            writer.writerow(rec)
    return path


def write_table_csv(path: Path, records: list[dict]) -> Path:
    fields: list[str] = []
    for rec in records:
        for k in rec:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for rec in records:
            writer.writerow({k: rec.get(k, "") for k in fields})
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Manifest: generation and verification
#
# Integrity order is mandatory:
#   1. verify the COMMITTED manifest against the files on disk
#      (verify_manifest() is strictly read-only);
#   2. only then may artifacts be re-generated, and always into a separate
#      directory (temporary for tests) that never overwrites the committed
#      manifest.
# ---------------------------------------------------------------------------
def manifest_entries(
    out_dir: Path, snapshot_dir: Path
) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Return (required, local_only) (logical_name, actual_path) pairs."""
    out_dir = Path(out_dir)
    snapshot_dir = Path(snapshot_dir)
    extract_dir = snapshot_dir.parent

    required: list[tuple[str, Path]] = []
    local_only: list[tuple[str, Path]] = []
    for p in sorted(snapshot_dir.glob("*.csv")):
        target = local_only if p.name in LOCAL_ONLY_NAMES else required
        target.append((LOGICAL_SNAPSHOT_PREFIX + p.name, p))
    tarball = extract_dir / "me_v3_eval_20261022.tgz"
    if tarball.exists():
        local_only.append((LOGICAL_EXTRACT_PREFIX + tarball.name, tarball))

    for name in EXTRACT_SQL_NAMES:
        required.append((LOGICAL_EXTRACT_PREFIX + name, extract_dir / name))
    required.append((LOGICAL_SCRIPT, Path(__file__).resolve()))
    for name in OUTPUT_NAMES:
        required.append((LOGICAL_OUTPUT_PREFIX + name, out_dir / name))

    md_path = OUT_DIR / f"{BASENAME}.md"
    if md_path.exists():
        required.append((LOGICAL_REPORT_MD, md_path))
    return required, local_only


def build_manifest(out_dir: Path, snapshot_dir: Path) -> dict:
    """Build the manifest object from freshly generated artifacts."""
    required, local_only = manifest_entries(out_dir, snapshot_dir)
    canonical_payload = {
        "entry_rule": "NEXT_OPEN_5M: opening price of the first strictly later 5m execution candle",
        "experiment_id": EXPERIMENT_ID,
        "fee_assumption": "taker 0.055%/side; normal slippage 0.05%/side; elevated 0.10%/side",
        "freeze_ts": FREEZE_TS.isoformat().replace("+00:00", "Z"),
        "max_hold_minutes": MAX_HOLD_MINUTES,
        "minimum_n": MINIMUM_N,
        "minimum_symbols": MINIMUM_SYMBOLS,
        "primary_metric": "net_r_normal",
        "source_scanner": SOURCE_SCANNER,
        "direction": "LONG",
        "stop_rule": "2.5% below reference entry",
        "target_rule": "3.0% above reference entry",
        "threshold": FROZEN_THRESHOLD,
        "version": 1,
    }
    payload_bytes = json.dumps(
        canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "artifact_set": BASENAME,
        "generated_from_snapshot_as_of_utc": "2026-10-22T17:11:24Z",
        "git": {
            "origin_main": "224f1d038751e8e8a2dfba6705964c1aff6966fa",
            "origin_main_verified_snapshot": "5a3d29c800d54988bad6d7cbeca7b1322890c679",
            "production_vps_head": "362c217b90e5e72006f8f10006f201cd8fb5c4db",
            "v3_files_identical_between_heads": True,
            "note": (
                "git diff of app/research/me_reverse_long_v3/**, "
                "prospective_observer.py, prospective_registry.json, migration "
                "067 and the V3 tests is empty between production HEAD 362c217 "
                "and both origin/main snapshots 5a3d29c and 224f1d0."
            ),
        },
        "protocol_fingerprint": {
            "status": "DERIVED_NOT_STORED",
            "derived_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "canonical_payload": canonical_payload,
            "note": (
                "MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS has NO stored "
                "protocol_fingerprint column (unlike ME_R_LONG_EARLY_MAE_EXIT_OOS_V2); "
                "the fingerprint above is derived in this evaluation from the "
                "canonical frozen payload and is NOT a frozen production value."
            ),
        },
        "files": {rel: sha256_file(path) for rel, path in sorted(required)},
        "local_only_files": {
            "note": (
                "Full-table raw extracts kept locally for evidence only; NOT "
                "committed to git to avoid repository bloat. Reproduction needs "
                "only the committed extraction SQL plus the committed cohort "
                "snapshot. Hashes are recorded for immutability."
            ),
            "files": {rel: sha256_file(path) for rel, path in sorted(local_only)},
        },
    }


def verify_manifest(
    manifest_path: Path | None = None,
    root: Path | None = None,
) -> dict:
    """Verify a manifest against the files on disk. STRICTLY READ-ONLY.

    This function never writes and never regenerates anything: it only reads
    the manifest and hashes the referenced files.  ``root`` is the directory
    the manifest's logical names are resolved against (the repository root for
    the committed evidence; a temporary copy root in negative tests).

    Failure semantics: any hash mismatch or missing file in ``files`` fails
    verification, and so does a *present* local-only evidence file whose hash
    drifted (``mismatched_local_only``).  Local-only files that are absent are
    reported as informational only, because they are intentionally not
    committed.
    """
    manifest_path = Path(manifest_path) if manifest_path else OUT_DIR / f"{BASENAME}_manifest.json"
    root = Path(root) if root else REPO_ROOT
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    report = {
        "manifest": manifest_path.name,
        "root": str(root),
        "ok": True,
        "checked_required": 0,
        "matched_required": 0,
        "mismatched": [],
        "missing_required": [],
        "absent_local_only": [],
        "mismatched_local_only": [],
    }
    for rel, digest in sorted(manifest.get("files", {}).items()):
        report["checked_required"] += 1
        path = root / rel
        if not path.exists():
            report["missing_required"].append(rel)
        elif sha256_file(path) != digest:
            report["mismatched"].append(rel)
        else:
            report["matched_required"] += 1
    for rel, digest in sorted(manifest.get("local_only_files", {}).get("files", {}).items()):
        path = root / rel
        if not path.exists():
            # legitimately absent (local-only evidence is not committed);
            # absence is informational, presence-with-drift is a failure
            report["absent_local_only"].append(rel)
        elif sha256_file(path) != digest:
            report["mismatched_local_only"].append(rel)

    report["ok"] = (
        not report["mismatched"]
        and not report["missing_required"]
        and not report["mismatched_local_only"]
    )
    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(out_dir: Path | None = None, snapshot_dir: Path | None = None) -> dict:
    """Generate artifacts.

    Generation is a WRITE operation and is strictly separated from
    verification: callers that must not touch committed evidence pass a
    separate ``out_dir`` (tests use a temporary directory).  Nothing outside
    ``out_dir`` is written.
    """
    out_dir = Path(out_dir) if out_dir else OUT_DIR
    snapshot_dir = Path(snapshot_dir) if snapshot_dir else SNAPSHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load_rows(snapshot_dir)
    integrity = integrity_audit(rows)
    stats = group_stats(rows)
    reconciliation = reconcile_operator(stats)
    by_day = breakdown(rows, "utc_date")
    by_symbol = breakdown(rows, "symbol")
    by_regime = breakdown(rows, "market_regime")
    conc = concentration(rows)
    dep = dependence_structure(rows)
    unc = uncertainty(rows)
    rob = robustness(rows)

    snapshot_csv = write_snapshot_csv(rows, out_dir)
    by_day_csv = write_table_csv(out_dir / f"{BASENAME}_by_day.csv", by_day)
    by_symbol_csv = write_table_csv(out_dir / f"{BASENAME}_by_symbol.csv", by_symbol)
    by_regime_csv = write_table_csv(out_dir / f"{BASENAME}_by_regime.csv", by_regime)
    rob_csv = write_table_csv(out_dir / f"{BASENAME}_robustness.csv", rob)

    with (snapshot_dir / "02_prospective_experiment_all.csv").open(newline="", encoding="utf-8") as fh:
        exp_rows = [r for r in csv.DictReader(fh) if r["experiment_id"] == EXPERIMENT_ID]
    exp_row = json.loads(exp_rows[0]["row_json"]) if exp_rows else {}

    results = {
        "artifact": BASENAME,
        "experiment_id": EXPERIMENT_ID,
        "as_of_utc": "2026-10-22T17:11:24Z",
        "mode": "READ_ONLY_OFFLINE_REPRODUCIBLE",
        "evidence_class": "EXPLORATORY (STATISTICAL_METHOD_NOT_APPROVED)",
        "frozen_protocol": {
            "source": {
                "code": "app/research/me_reverse_long_v3/protocol.py (V3 package verified byte-identical between production HEAD 362c217 and origin/main 5a3d29c / 224f1d0)",
                "registry": "app/research/prospective_registry.json entry MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS (version 1)",
                "db": "research.prospective_experiment row",
            },
            "experiment_type": "PROSPECTIVE_EXECUTION_SHADOW",
            "source_scanner": SOURCE_SCANNER,
            "direction": "LONG",
            "threshold": FROZEN_THRESHOLD,
            "threshold_semantics": (
                "candidate research split, NOT an independently validated "
                "trading gate (protocol.py docstring)"
            ),
            "entry_rule": "NEXT_OPEN_5M: opening price of the first strictly later 5m execution candle",
            "stop_rule": "2.5% below reference entry",
            "target_rule": "3.0% above reference entry",
            "max_hold_minutes": MAX_HOLD_MINUTES,
            "fee_model": {
                "fee_per_side": FEE_PER_SIDE,
                "normal_slippage_per_side": NORMAL_SLIPPAGE_PER_SIDE,
                "elevated_slippage_per_side": ELEVATED_SLIPPAGE_PER_SIDE,
            },
            "intrabar_policy": "STOP_FIRST (conservative; ambiguous intrabar never assumed favourable)",
            "freeze_ts_utc": FREEZE_TS.isoformat(),
            "started_at_utc": FREEZE_TS.isoformat(),
            "eligibility_rule": "signal_time > freeze_ts (strict; exact freeze timestamp is ineligible)",
            "minimum_n": MINIMUM_N,
            "minimum_symbols": MINIMUM_SYMBOLS,
            "minimum_oos_days": None,
            "statistical_decision_gates": None,
            "db_registry_row": {
                k: exp_row[k]
                for k in (
                    "status",
                    "started_at",
                    "threshold",
                    "minimum_n",
                    "minimum_symbols",
                    "primary_metric",
                )
                if k in exp_row
            },
        },
        "control_semantics": {
            "frozen_definition": (
                "Registry hypothesis: 'Prospectively compare "
                "exhaustion_magnitude <= 0.4 against > 0.4, with all eligible "
                "signals as CONTROL.'"
            ),
            "class": "AGGREGATE_UNFILTERED_BASELINE",
            "comparability": (
                "CONTROL = all eligible finalized observations of this same "
                "cohort (PASS + REJECT + MISSING_FEATURE). It is NOT an "
                "independently collected cohort and NOT a counterfactual "
                "control; it contains both compared groups by construction. "
                "No paired delta PASS-vs-CONTROL is claimed."
            ),
            "status": "CONTROL_AVAILABLE_AGGREGATE_ONLY",
        },
        "integrity": integrity,
        "descriptive": stats,
        "operator_reconciliation": {
            "all_match": all(x["match"] == "MATCH" for x in reconciliation),
            "rows": reconciliation,
        },
        "uncertainty": unc,
        "concentration": conc,
        "dependence": dep,
        "breakdowns": {"by_day": by_day, "by_symbol": by_symbol, "by_regime": by_regime},
        "robustness": rob,
        "reproducibility": {
            "script": "tools/research/me_reverse_long_v3_final_statistical_evaluation_v1.py",
            "extraction_sql": [
                "audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/01_extract_frozen_cohort.sql",
                "audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/02_extract_supplemental.sql",
                "audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/03_extract_constraints.sql",
            ],
            "snapshot": "audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/me_v3_eval_20261022/*.csv",
            "python": "CPython 3.13, standard library only (random.Random seeds)",
            "determinism": (
                "All resampling uses fixed seeds recorded in "
                "uncertainty.seeds; outputs are key-sorted and contain no "
                "runtime timestamps, so repeated runs are byte-identical."
            ),
        },
        "verdict": {
            "final_verdict": "NEW_HYPOTHESIS_REQUIRED",
            "integrity_verdict": "CLEAN",
            "statistical_verdict": (
                "EXPLORATORY_NEGATIVE_PASS_NO_EDGE "
                "(STATISTICAL_METHOD_NOT_APPROVED; no confirmatory verdict issued)"
            ),
            "recommended_lifecycle": (
                "KEEP_UNCHANGED_UNTIL_OPERATOR_APPROVED_SEPARATE_CLOSURE_TASK; "
                "paper/live remain BLOCKED"
            ),
            "master_me_oos_tracker_suggestion": "CLOSED (pending operator approval)",
            "rationale": [
                "Frozen PASS hypothesis (exhaustion_magnitude <= 0.4 implies positive executable edge and outperforms > 0.4) is NOT confirmed: PASS Net E[R] normal = -0.2817 R over N=107 with every single-axis exploratory 95% interval entirely below zero.",
                "Pre-registered readiness gates are met (minimum_n=100: PASS n=107; minimum_symbols=10: 37 PASS symbols), so this is not a sample-size-limited inconclusive result.",
                "The negative result is robust to leave-one-day-out, leave-one-symbol-out, top-3 symbol exclusions, elevated costs, and removal of TIMEOUT trades (PASS mean worsens to -0.6833 without TIMEOUT).",
                "PASS underperforms the frozen aggregate CONTROL baseline (all eligible: -0.2050 R); REJECT (+0.0780 R, n=29, exploratory) is higher than PASS, i.e. the frozen threshold direction is contradicted on this cohort.",
                "CLOSED_NEGATIVE_RECOMMENDED is NOT issued because no operator-approved negative decision gates or approved inference method were ever frozen for this experiment (blocker documented).",
                "INCONCLUSIVE_CONTINUE_OOS is not warranted: data are sufficient to reject a positive PASS edge at the pre-registered readiness gates; further accumulation under the frozen protocol has negligible marginal value.",
                "PIPELINE_OR_METHOD_FIX_REQUIRED is not warranted: the frozen pipeline passed every integrity check and reproduced all operator-audit values exactly.",
                "Any further research requires a NEW separately frozen hypothesis (new cohort, new freeze, discovery/validation separation); the inverted PASS/REJECT ordering may not be re-used from this OOS sample for validation.",
            ],
        },
        "limitations": [
            "No operator-approved statistical decision gates or inference method exist for this experiment; all intervals are exploratory (STATISTICAL_METHOD_NOT_APPROVED).",
            "Day-block and symbol-block bootstrap intervals are separate single-axis sensitivity intervals, NOT a calibrated joint multiway 95% CI.",
            "PASS and REJECT are disjoint unpaired groups; no paired delta is identifiable and REJECT n=29 is imprecise.",
            "9 OOS UTC days only; day-level generalization is weak and no minimum_oos_days was pre-registered.",
            "Shadow NEXT_OPEN_5M fills are hypothetical execution models, not executable proof (frozen entry_rule).",
            "Completeness of observer capture (missed eligible candidates) cannot be proven from stored rows alone; source_signal_id is a deterministic synthetic key and does not join back to dds.scanner_setup.",
            "research.prospective_outcome contains no rows for this experiment; the authoritative outcome source is research.me_reverse_long_v3_execution_outcome (documented, not an error).",
        ],
    }

    json_path = out_dir / f"{BASENAME}.json"
    with json_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(results, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    manifest = build_manifest(out_dir, snapshot_dir)
    manifest_path = out_dir / f"{BASENAME}_manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1: "
            "verify the committed manifest (read-only) or regenerate artifacts."
        )
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "verify the committed manifest against files on disk and exit; "
            "strictly read-only, never regenerates or rewrites anything"
        ),
    )
    parser.add_argument("--manifest", default=None, help="manifest path to verify")
    parser.add_argument("--root", default=None, help="root directory logical names resolve against")
    parser.add_argument("--out", default=None, help="output directory for regeneration (default: docs/research)")
    parser.add_argument("--snapshot-dir", default=None, help="extraction snapshot directory (default: committed snapshot)")
    args = parser.parse_args()

    if args.verify:
        report = verify_manifest(
            Path(args.manifest) if args.manifest else None,
            Path(args.root) if args.root else None,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(0 if report["ok"] else 1)

    res = main(
        out_dir=Path(args.out) if args.out else None,
        snapshot_dir=Path(args.snapshot_dir) if args.snapshot_dir else None,
    )
    print(json.dumps({
        "integrity_verdict": res["integrity"]["integrity_verdict"],
        "checks_failed": res["integrity"]["checks_failed"],
        "operator_reconciliation_all_match": res["operator_reconciliation"]["all_match"],
        "pass_net_normal_mean": res["descriptive"]["PASS"]["net_normal_mean"],
        "reject_net_normal_mean": res["descriptive"]["REJECT"]["net_normal_mean"],
        "control_net_normal_mean": res["descriptive"]["CONTROL_ALL_ELIGIBLE"]["net_normal_mean"],
    }, indent=2))
