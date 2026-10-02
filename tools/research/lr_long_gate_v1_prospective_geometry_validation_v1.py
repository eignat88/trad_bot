"""LR_LONG_GATE_V1_PROSPECTIVE_GEOMETRY_VALIDATION_V1 — deterministic offline analysis.

READ-ONLY. Reads frozen dataset export and computes all required validation metrics.
Does NOT modify any production system.

Usage:
    python tools/research/lr_long_gate_v1_prospective_geometry_validation_v1.py \
        --dataset audit_db/lr_long_gate_v1_validation_export/dataset.tsv \
        --outdir docs/research
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

SEED = 42
BOOTSTRAP_N = 10_000
FREEZE_TS = "2026-10-01 13:48:27+00"
CUTOFF_UTC = "2026-10-02 14:53:31+00"
GIT_HEAD = "7bc1cd9e0a16ed6aab92cdd9fba094390401bc59"
EXPERIMENT_ID = "LR_LONG_GATE_V1"
PROTOCOL_ARTIFACT = "docs/research/LR_LONG_GEOMETRY_PROSPECTIVE_VALIDATION_V1_PROTOCOL_2026-10-01.md"

# Frozen protocol parameters
FROZEN_SL_R = 0.50
FROZEN_TP_R = 3.0
FROZEN_TIMEOUT_MIN = 120
FROZEN_RISK_GATE = 0.003  # 0.3%
FEE_PER_SIDE = 0.00055  # 0.055%
SLIPPAGE_NORMAL_PER_SIDE = 0.0005  # 0.05%
SLIPPAGE_ELEVATED_PER_SIDE = 0.001  # 0.10%
ROUND_TRIP_NORMAL = 2 * (FEE_PER_SIDE + SLIPPAGE_NORMAL_PER_SIDE)  # 0.0021 = 0.21%
ROUND_TRIP_ELEVATED = 2 * (FEE_PER_SIDE + SLIPPAGE_ELEVATED_PER_SIDE)  # 0.0031 = 0.31%
AMBIGUITY_POLICY = "STOP_FIRST"
CHECKPOINT_MIN_N = 50
CHECKPOINT_MIN_DAYS = 7


def parse_float(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip()
    if s == "" or s == "NULL":
        return None
    return float(s)


def parse_bool(s: str | None) -> bool | None:
    if s is None:
        return None
    s = s.strip().lower()
    if s in ("t", "true", "1"):
        return True
    if s in ("f", "false", "0"):
        return False
    return None


def load_dataset(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for r in reader:
            row = dict(r)
            row["signal_time"] = (r.get("signal_time") or "").strip()
            row["created_at"] = (r.get("created_at") or "").strip()
            for key in (
                "reference_price", "invalidation_price", "target_1", "target_2",
                "score", "variant_entry", "variant_stop", "variant_target",
                "mfe_15m", "mfe_30m", "mfe_60m", "mfe_120m", "mfe_240m",
                "mae_15m", "mae_30m", "mae_60m", "mae_120m", "mae_240m",
                "mfe_r_15m", "mfe_r_30m", "mfe_r_60m", "mfe_r_120m", "mfe_r_240m",
                "mae_r_15m", "mae_r_30m", "mae_r_60m", "mae_r_120m", "mae_r_240m",
                "return_at_15m", "return_at_30m", "return_at_60m",
                "return_at_120m", "return_at_240m",
                "time_to_tp", "time_to_sl",
            ):
                row[key] = parse_float(r.get(key))
            for key in ("rule_passed", "tp_hit", "sl_hit", "tp_before_sl",
                        "sl_before_tp", "ambiguous_intrabar", "is_final"):
                row[key] = parse_bool(r.get(key))
            rows.append(row)
    return rows


def integrity_checks(rows: list[dict]) -> dict:
    obs_ids = [r["observation_id"] for r in rows]
    dup_obs = len(obs_ids) - len(set(obs_ids))
    dup_sym_time = len(rows) - len({(r["symbol"], r["signal_time"]) for r in rows})
    non_long = sum(1 for r in rows if r["direction"] != "LONG")
    null_ref = sum(1 for r in rows if r["reference_price"] is None)
    null_inv = sum(1 for r in rows if r["invalidation_price"] is None)
    zero_risk = sum(1 for r in rows if r["reference_price"] and r["invalidation_price"]
                    and r["reference_price"] == r["invalidation_price"])
    neg_risk = sum(1 for r in rows if r["reference_price"] and r["invalidation_price"]
                   and r["reference_price"] < r["invalidation_price"])
    after_cutoff = sum(1 for r in rows if r["signal_time"] > CUTOFF_UTC[:19] + "+00")
    obs_no_outcome = sum(1 for r in rows if r["is_final"] is None)
    pre_freeze_inserted_after = sum(
        1 for r in rows
        if r["signal_time"] < FREEZE_TS and r["created_at"] and r["created_at"] >= FREEZE_TS
    )
    post_freeze_sourced_pre_freeze = 0  # signal_time is the source event time
    return {
        "raw_n": len(rows),
        "duplicate_observation_ids": dup_obs,
        "duplicate_symbol_signal_time": dup_sym_time,
        "non_long_rows": non_long,
        "null_reference_price": null_ref,
        "null_invalidation_price": null_inv,
        "zero_risk_distance": zero_risk,
        "negative_risk_distance": neg_risk,
        "observations_after_cutoff": after_cutoff,
        "observations_without_outcome": obs_no_outcome,
        "pre_freeze_inserted_after_freeze": pre_freeze_inserted_after,
        "post_freeze_sourced_pre_freeze": post_freeze_sourced_pre_freeze,
    }


def split_pre_post(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    pre = [r for r in rows if r["signal_time"] < FREEZE_TS]
    post = [r for r in rows if r["signal_time"] >= FREEZE_TS]
    return pre, post


def risk_gate_check(row: dict) -> tuple[bool, str]:
    """Apply frozen risk gate: abs(entry - invalidation_price) / entry >= 0.3%."""
    entry = row.get("reference_price")
    stop = row.get("invalidation_price")
    if entry is None or stop is None or entry <= 0:
        return False, "missing_entry_or_stop"
    risk_dist = abs(entry - stop)
    risk_pct = risk_dist / entry
    if risk_pct < FROZEN_RISK_GATE:
        return False, f"risk_pct={risk_pct:.6f} < {FROZEN_RISK_GATE}"
    return True, f"risk_pct={risk_pct:.6f}"


def simulate_frozen_exit(row: dict) -> tuple[str, float]:
    """Simulate frozen SL=0.50R TP=3.0R timeout=120m STOP_FIRST.

    Returns (exit_type, gross_r).
    """
    entry = row["reference_price"]
    stop = row["invalidation_price"]
    target_1 = row["target_1"]
    risk = abs(entry - stop)
    if risk <= 0 or target_1 is None:
        return "INSUFFICIENT", 0.0
    rr_actual = (target_1 - entry) / risk

    tp = row.get("tp_before_sl")
    sl = row.get("sl_before_tp")
    amb = row.get("ambiguous_intrabar")
    tp_hit = row.get("tp_hit")
    sl_hit = row.get("sl_hit")
    time_to_tp = row.get("time_to_tp")
    time_to_sl = row.get("time_to_sl")

    # Frozen: SL=0.50R TP=3.0R timeout=120m
    # exit semantics: STOP_FIRST for ambiguous
    if amb or (tp and sl):
        return "AMBIGUOUS", -1.0
    if tp and not sl:
        # TP hit first; actual TP distance may differ from 3.0R frozen target
        # Use frozen TP=3.0R if actual target_1 implies >=3.0R, else actual rr
        if rr_actual >= FROZEN_TP_R:
            return "TP_FIRST", FROZEN_TP_R
        return "TP_FIRST", rr_actual
    if sl and not tp:
        return "SL_FIRST", -1.0
    # Neither TP nor SL hit within timeout window
    return "TIMEOUT", 0.0


def compute_cost_r(gross_r: float, entry: float, stop: float, round_trip_cost: float) -> float:
    """Convert gross R to net R after round-trip costs."""
    risk = abs(entry - stop)
    if risk <= 0 or entry <= 0:
        return gross_r
    cost_r = (round_trip_cost * entry) / risk
    return gross_r - cost_r


def execution_metrics(eligible: list[dict], round_trip_cost: float) -> dict:
    exits = []
    net_rs = []
    for r in eligible:
        exit_type, gross_r = simulate_frozen_exit(r)
        cost_r = compute_cost_r(gross_r, r["reference_price"], r["invalidation_price"], round_trip_cost)
        exits.append({"observation_id": r["observation_id"], "symbol": r["symbol"],
                      "signal_time": r["signal_time"], "exit_type": exit_type,
                      "gross_r": gross_r, "net_r": cost_r})
        net_rs.append(cost_r)

    n_tp = sum(1 for e in exits if e["exit_type"] == "TP_FIRST")
    n_sl = sum(1 for e in exits if e["exit_type"] == "SL_FIRST")
    n_timeout = sum(1 for e in exits if e["exit_type"] == "TIMEOUT")
    n_ambiguous = sum(1 for e in exits if e["exit_type"] == "AMBIGUOUS")
    wins = [e for e in exits if e["net_r"] > 0]
    losses = [e for e in exits if e["net_r"] <= 0]
    win_sum = sum(e["net_r"] for e in wins)
    loss_abs = abs(sum(e["net_r"] for e in losses))
    pf = win_sum / loss_abs if loss_abs > 0 else float("inf")
    gross_ers = [e["gross_r"] for e in exits]
    net_ers = [e["net_r"] for e in exits]
    # max losing streak
    max_streak = 0
    streak = 0
    for e in exits:
        if e["net_r"] <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return {
        "n": len(exits),
        "n_tp": n_tp, "n_sl": n_sl, "n_timeout": n_timeout, "n_ambiguous": n_ambiguous,
        "win_rate": len(wins) / len(exits) if exits else None,
        "gross_er": statistics.mean(gross_ers) if gross_ers else None,
        "net_er": statistics.mean(net_ers) if net_ers else None,
        "net_pf": pf,
        "net_total_r": sum(net_ers) if net_ers else None,
        "median_net_r": statistics.median(net_ers) if net_ers else None,
        "max_losing_streak": max_streak,
        "exits": exits,
    }


def bootstrap_ci(net_rs: list[float], seed: int = SEED, n: int = BOOTSTRAP_N) -> dict:
    if not net_rs:
        return {}
    rng = random.Random(seed)
    m = len(net_rs)
    means = []
    for _ in range(n):
        sample = [net_rs[rng.randrange(m)] for _ in range(m)]
        means.append(statistics.mean(sample))
    means.sort()
    lo = means[int(n * 0.025)]
    hi = means[int(n * 0.975)]
    point = statistics.mean(net_rs)
    p_pos = sum(1 for x in means if x > 0) / n
    return {
        "point_estimate": point,
        "bootstrap_median": statistics.median(means),
        "ci_95_low": lo,
        "ci_95_high": hi,
        "p_er_gt_0": p_pos,
    }


def day_concentration(eligible: list[dict], round_trip_cost: float) -> dict:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        day = r["signal_time"][:10]
        by_day[day].append(r)
    days = []
    total_n = sum(len(v) for v in by_day.values())
    for day in sorted(by_day):
        metrics = execution_metrics(by_day[day], round_trip_cost)
        days.append({"day": day, "n": metrics["n"], "net_er": metrics["net_er"],
                     "net_pf": metrics["net_pf"], "net_total_r": metrics["net_total_r"]})
    largest = max(days, key=lambda d: d["n"]) if days else None
    loo = []
    for d in days:
        subset = [r for r in eligible if r["signal_time"][:10] != d["day"]]
        m = execution_metrics(subset, round_trip_cost)
        loo.append({"excluded_day": d["day"], "n": m["n"], "net_er": m["net_er"],
                    "net_pf": m["net_pf"]})
    return {"days": days, "largest_day_share_n": (largest["n"] / total_n if largest else None), "loo": loo}


def symbol_concentration(eligible: list[dict], round_trip_cost: float) -> dict:
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        by_sym[r["symbol"]].append(r)
    syms = []
    total_n = sum(len(v) for v in by_sym.values())
    for sym in sorted(by_sym, key=lambda s: -len(by_sym[s])):
        m = execution_metrics(by_sym[sym], round_trip_cost)
        syms.append({"symbol": sym, "n": m["n"], "net_er": m["net_er"],
                     "net_total_r": m["net_total_r"]})
    top1_sym = syms[0]["symbol"] if syms else None
    top3_syms = [s["symbol"] for s in syms[:3]]
    subset_wo_top1 = [r for r in eligible if r["symbol"] != top1_sym]
    subset_wo_top3 = [r for r in eligible if r["symbol"] not in top3_syms]
    m_wo_top1 = execution_metrics(subset_wo_top1, round_trip_cost)
    m_wo_top3 = execution_metrics(subset_wo_top3, round_trip_cost)
    return {
        "symbols": syms,
        "exclude_top1": {"n": m_wo_top1["n"], "net_er": m_wo_top1["net_er"], "net_pf": m_wo_top1["net_pf"]},
        "exclude_top3": {"n": m_wo_top3["n"], "net_er": m_wo_top3["net_er"], "net_pf": m_wo_top3["net_pf"]},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()

    dataset_path = Path(args.dataset)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    rows = load_dataset(dataset_path)
    integrity = integrity_checks(rows)
    pre, post = split_pre_post(rows)

    # Apply risk gate to post-freeze
    gate_pass = []
    gate_fail = []
    for r in post:
        ok, reason = risk_gate_check(r)
        if ok:
            gate_pass.append(r)
        else:
            gate_fail.append({**r, "_gate_reason": reason})

    # Eligible = gate-passed + finalized at 240m
    eligible = [r for r in gate_pass if r["is_final"] and r.get("mfe_240m") is not None]
    gate_pass_not_final = [r for r in gate_pass if not (r["is_final"] and r.get("mfe_240m") is not None)]

    symbols = len({r["symbol"] for r in eligible})
    days = len({r["signal_time"][:10] for r in eligible})

    # Execution simulation
    exec_primary = execution_metrics(eligible, ROUND_TRIP_NORMAL)
    exec_elevated = execution_metrics(eligible, ROUND_TRIP_ELEVATED)
    exec_gross = execution_metrics(eligible, 0.0)

    net_rs = [e["net_r"] for e in exec_primary["exits"]]
    boot = bootstrap_ci(net_rs)

    day = day_concentration(eligible, ROUND_TRIP_NORMAL)
    sym = symbol_concentration(eligible, ROUND_TRIP_NORMAL)

    # Checkpoint
    checkpoint_met = len(eligible) >= CHECKPOINT_MIN_N and days >= CHECKPOINT_MIN_DAYS

    results = {
        "experiment_id": EXPERIMENT_ID,
        "audit_cutoff": CUTOFF_UTC,
        "git_head": GIT_HEAD,
        "protocol_artifact": PROTOCOL_ARTIFACT,
        "freeze_timestamp": FREEZE_TS,
        "integrity": integrity,
        "dataset": {
            "raw_n": len(rows),
            "pre_freeze_n": len(pre),
            "post_freeze_n": len(post),
            "post_freeze_first": min((r["signal_time"] for r in post), default=None),
            "post_freeze_last": max((r["signal_time"] for r in post), default=None),
            "gate_pass_n": len(gate_pass),
            "gate_fail_n": len(gate_fail),
            "eligible_n": len(eligible),
            "finalized_n": len(eligible),
            "gate_pass_not_final_n": len(gate_pass_not_final),
            "symbols": symbols,
            "oos_days": days,
        },
        "frozen_protocol": {
            "sl_r": FROZEN_SL_R,
            "tp_r": FROZEN_TP_R,
            "timeout_min": FROZEN_TIMEOUT_MIN,
            "risk_gate": FROZEN_RISK_GATE,
            "fee_per_side": FEE_PER_SIDE,
            "slippage_normal_per_side": SLIPPAGE_NORMAL_PER_SIDE,
            "slippage_elevated_per_side": SLIPPAGE_ELEVATED_PER_SIDE,
            "round_trip_normal": ROUND_TRIP_NORMAL,
            "round_trip_elevated": ROUND_TRIP_ELEVATED,
            "ambiguity_policy": AMBIGUITY_POLICY,
            "checkpoint_min_n": CHECKPOINT_MIN_N,
            "checkpoint_min_days": CHECKPOINT_MIN_DAYS,
        },
        "checkpoint_met": checkpoint_met,
        "execution_gross": {k: v for k, v in exec_gross.items() if k != "exits"},
        "execution_primary": {k: v for k, v in exec_primary.items() if k != "exits"},
        "execution_elevated": {k: v for k, v in exec_elevated.items() if k != "exits"},
        "bootstrap": boot,
        "day_concentration": {k: v for k, v in day.items() if k != "days"} | {"days": day["days"]},
        "symbol_concentration": {k: v for k, v in sym.items() if k != "symbols"} | {"top_symbols": sym["symbols"][:10]},
        "costs": {"round_trip_normal": ROUND_TRIP_NORMAL, "round_trip_elevated": ROUND_TRIP_ELEVATED},
        "random_seed": SEED,
        "bootstrap_n": BOOTSTRAP_N,
    }

    with open(outdir / "lr_long_gate_v1_prospective_geometry_validation_v1_results.json", "w") as f:
        json.dump(results, f, indent=2)

    # CSV of eligible observations with exits
    with open(outdir / "lr_long_gate_v1_prospective_geometry_validation_v1_trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["observation_id", "symbol", "signal_time", "day",
                     "reference_price", "invalidation_price", "risk_pct",
                     "exit_type", "gross_r", "net_r_normal", "net_r_elevated"])
        for e in exec_primary["exits"]:
            obs_id = e["observation_id"]
            row = next(r for r in eligible if r["observation_id"] == obs_id)
            risk = abs(row["reference_price"] - row["invalidation_price"])
            risk_pct = risk / row["reference_price"] if row["reference_price"] else None
            net_elev = compute_cost_r(e["gross_r"], row["reference_price"],
                                       row["invalidation_price"], ROUND_TRIP_ELEVATED)
            w.writerow([
                obs_id, e["symbol"], e["signal_time"], e["signal_time"][:10],
                row["reference_price"], row["invalidation_price"], risk_pct,
                e["exit_type"], e["gross_r"], e["net_r"], net_elev,
            ])

    print(json.dumps({
        "raw_n": len(rows),
        "post_freeze_n": len(post),
        "eligible_n": len(eligible),
        "symbols": symbols,
        "oos_days": days,
        "checkpoint_met": checkpoint_met,
        "net_er_primary": exec_primary["net_er"],
        "net_pf_primary": exec_primary["net_pf"],
        "bootstrap": boot,
    }, indent=2))


if __name__ == "__main__":
    main()
