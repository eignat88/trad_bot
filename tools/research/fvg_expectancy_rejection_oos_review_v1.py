"""FVG_EXPECTANCY_REJECTION_OOS_REVIEW_V1 — deterministic offline analysis script.

Reads frozen dataset export and computes all required review metrics.
Does NOT modify any production system.

Usage:
    python tools/research/fvg_expectancy_rejection_oos_review_v1.py \
        --dataset audit_db/fvg_oos_review_export/fvg_dataset.tsv \
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
NORMAL_COST = 0.0021  # 0.21% round-trip
STRESSED_COST = 0.0031  # 0.31% round-trip
EXPERIMENT_ID = "FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1"
CUTOFF_UTC = "2026-10-02T13:38:51+00:00"
GIT_HEAD = "41685a21a363aec6e84affaeb3a9cb5cf98e116f"


def parse_ts(s: str | None) -> str:
    return (s or "").strip()


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
            row["signal_time"] = parse_ts(r["signal_time"])
            for key in (
                "reference_price", "invalidation_price", "target_1", "target_2",
                "score", "variant_entry", "variant_stop", "variant_target",
                "mfe_15m", "mfe_30m", "mfe_60m", "mfe_120m", "mfe_240m",
                "mae_15m", "mae_30m", "mae_60m", "mae_120m", "mae_240m",
                "mfe_r_15m", "mfe_r_30m", "mfe_r_60m", "mfe_r_120m", "mfe_r_240m",
                "mae_r_15m", "mae_r_30m", "mae_r_60m", "mae_r_120m", "mae_r_240m",
                "return_at_15m", "return_at_30m", "return_at_60m",
                "return_at_120m", "return_at_240m",
            ):
                row[key] = parse_float(r.get(key, ""))
            for key in ("rule_passed", "tp_hit", "sl_hit", "tp_before_sl",
                        "sl_before_tp", "ambiguous_intrabar", "is_final"):
                row[key] = parse_bool(r.get(key, ""))
            rows.append(row)
    return rows


def integrity_checks(rows: list[dict]) -> dict:
    obs_ids = [r["observation_id"] for r in rows]
    dup_obs = len(obs_ids) - len(set(obs_ids))
    non_long = sum(1 for r in rows if r["direction"] != "LONG")
    null_ref = sum(1 for r in rows if r["reference_price"] is None)
    null_inv = sum(1 for r in rows if r["invalidation_price"] is None)
    null_target = sum(1 for r in rows if r["target_1"] is None)
    null_features = sum(1 for r in rows if not (r.get("features") or "").strip())
    after_cutoff = sum(1 for r in rows if r["signal_time"] > CUTOFF_UTC.replace("T", " ")[:19] + "+00")
    dup_sym_time = len(rows) - len({(r["symbol"], r["signal_time"]) for r in rows})
    obs_no_outcome = sum(1 for r in rows if r["is_final"] is None and not r.get("mfe_60m"))
    bad_prices = sum(1 for r in rows if r["reference_price"] and r["invalidation_price"] and r["reference_price"] <= r["invalidation_price"])
    bad_target = sum(1 for r in rows if r["reference_price"] and r["target_1"] and r["target_1"] <= r["reference_price"])
    return {
        "raw_n": len(rows),
        "duplicate_observation_ids": dup_obs,
        "non_long_rows": non_long,
        "null_reference_price": null_ref,
        "null_invalidation_price": null_inv,
        "null_target_1": null_target,
        "null_features_json": null_features,
        "observations_after_cutoff": after_cutoff,
        "duplicate_symbol_signal_time": dup_sym_time,
        "observations_without_outcome": obs_no_outcome,
        "invalid_stop_distance": bad_prices,
        "invalid_target_above_entry": bad_target,
    }


def eligible_population(rows: list[dict]) -> tuple[list[dict], dict]:
    """Primary eligible population: finalized at 240m horizon, valid LONG geometry."""
    eligible = []
    excluded = {"not_finalized_at_240m": 0, "invalid_geometry": 0}
    for r in rows:
        if r["direction"] != "LONG":
            excluded["invalid_geometry"] += 1
            continue
        if r.get("reference_price") is None or r.get("invalidation_price") is None or r.get("target_1") is None:
            excluded["invalid_geometry"] += 1
            continue
        if r["reference_price"] <= r["invalidation_price"] or r["target_1"] <= r["reference_price"]:
            excluded["invalid_geometry"] += 1
            continue
        if r["is_final"] and r.get("mfe_240m") is not None:
            eligible.append(r)
        else:
            excluded["not_finalized_at_240m"] += 1
    return eligible, {
        "eligible_n": len(eligible),
        "excluded_n": sum(excluded.values()),
        "exclusion_reasons": excluded,
    }


def compute_r_gross(row: dict) -> float | None:
    """Compute realized gross R using TP/SL/timeout exit semantics.

    FVG_EXPECTANCY_REJECT protocol: entry = reference_price,
    SL = invalidation_price, TP = target_1, target risk = RR * risk.

    Exit rules:
      TP first        -> +RR
      SL first        -> -1
      TIMEOUT (neither) -> 0
      AMBIGUOUS       -> STOP_FIRST (conservative, -1)
    """
    risk = abs(row["reference_price"] - row["invalidation_price"])
    if risk <= 0 or row.get("target_1") is None:
        return None
    rr = (row["target_1"] - row["reference_price"]) / risk

    tp = row.get("tp_before_sl")
    sl = row.get("sl_before_tp")
    amb = row.get("ambiguous_intrabar")
    tp_hit = row.get("tp_hit")
    sl_hit = row.get("sl_hit")

    if amb or (tp and sl):
        return -1.0
    if tp:
        return rr
    if sl:
        return -1.0
    if tp_hit and not sl_hit:
        return rr
    if sl_hit and not tp_hit:
        return -1.0
    return 0.0


def gross_metrics(eligible: list[dict]) -> dict:
    rs = []
    for r in eligible:
        g = compute_r_gross(r)
        if g is not None:
            rs.append(g)
    if not rs:
        return {"n": 0}
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x < 0]
    timeouts = [x for x in rs if x == 0]
    gross_win_sum = sum(x for x in wins)
    gross_loss_abs = abs(sum(losses))
    pf = gross_win_sum / gross_loss_abs if gross_loss_abs > 0 else float("inf")
    mfe = [r["mfe_r_240m"] for r in eligible if r.get("mfe_r_240m") is not None]
    mae = [r["mae_r_240m"] for r in eligible if r.get("mae_r_240m") is not None]
    return {
        "n": len(rs),
        "n_tp": len(wins),
        "n_sl": len(losses),
        "n_timeout": len(timeouts),
        "win_rate": len(wins) / len(rs),
        "gross_er": statistics.mean(rs),
        "gross_pf": pf,
        "gross_total_r": sum(rs),
        "median_r": statistics.median(rs),
        "mfe_mean": statistics.mean(mfe) if mfe else None,
        "mfe_median": statistics.median(mfe) if mfe else None,
        "mae_mean": statistics.mean(mae) if mae else None,
        "mae_median": statistics.median(mae) if mae else None,
    }


def net_metrics(eligible: list[dict], cost: float) -> dict:
    rs = []
    for r in eligible:
        g = compute_r_gross(r)
        if g is None:
            continue
        risk = abs(r["reference_price"] - r["invalidation_price"])
        if risk <= 0:
            continue
        cost_r = (cost * r["reference_price"]) / risk
        net_r = g - cost_r
        rs.append(net_r)
    if not rs:
        return {"n": 0}
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    win_sum = sum(wins)
    loss_abs = abs(sum(losses))
    pf = win_sum / loss_abs if loss_abs > 0 else float("inf")
    return {
        "n": len(rs),
        "win_rate": len(wins) / len(rs),
        "net_er": statistics.mean(rs),
        "net_pf": pf,
        "net_total_r": sum(rs),
        "median_net_r": statistics.median(rs),
    }


def bootstrap_ci(values: list[float], seed: int = SEED, n: int = BOOTSTRAP_N) -> dict:
    rng = random.Random(seed)
    means = []
    m = len(values)
    for _ in range(n):
        sample = [values[rng.randrange(m)] for _ in range(m)]
        means.append(statistics.mean(sample))
    means.sort()
    lo = means[int(n * 0.025)]
    hi = means[int(n * 0.975)]
    point = statistics.mean(values)
    p_pos = sum(1 for x in means if x > 0) / n
    return {
        "point_estimate": point,
        "bootstrap_mean": statistics.mean(means),
        "bootstrap_median": statistics.median(means),
        "ci_95_low": lo,
        "ci_95_high": hi,
        "p_er_gt_0": p_pos,
    }


def day_concentration(eligible: list[dict]) -> dict:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        day = r["signal_time"][:10]
        by_day[day].append(r)
    days = []
    total_n = sum(len(v) for v in by_day.values())
    for day in sorted(by_day):
        rs = [compute_r_gross(r) for r in by_day[day]]
        rs = [x for x in rs if x is not None]
        wins = [x for x in rs if x > 0]
        days.append({
            "day": day,
            "n": len(rs),
            "win_rate": len(wins) / len(rs) if rs else None,
            "gross_er": statistics.mean(rs) if rs else None,
        })
    largest_day = max(days, key=lambda d: d["n"]) if days else None
    largest_day_share = largest_day["n"] / total_n if largest_day else None
    # leave-one-day-out
    loo = []
    for d in days:
        subset = [r for r in eligible if r["signal_time"][:10] != d["day"]]
        gm = gross_metrics(subset)
        nm = net_metrics(subset, NORMAL_COST)
        loo.append({
            "excluded_day": d["day"],
            "n": gm.get("n", 0),
            "gross_er": gm.get("gross_er"),
            "gross_pf": gm.get("gross_pf"),
            "net_er": nm.get("net_er"),
            "net_pf": nm.get("net_pf"),
        })
    return {"days": days, "largest_day_share_n": largest_day_share, "loo": loo}


def symbol_concentration(eligible: list[dict]) -> dict:
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        by_sym[r["symbol"]].append(r)
    syms = []
    total_n = sum(len(v) for v in by_sym.values())
    for sym in sorted(by_sym, key=lambda s: -len(by_sym[s])):
        rs = [compute_r_gross(r) for r in by_sym[sym]]
        rs = [x for x in rs if x is not None]
        wins = [x for x in rs if x > 0]
        syms.append({
            "symbol": sym,
            "n": len(rs),
            "win_rate": len(wins) / len(rs) if rs else None,
            "gross_er": statistics.mean(rs) if rs else None,
        })
    top1_share = syms[0]["n"] / total_n if syms else None
    top3_share = sum(s["n"] for s in syms[:3]) / total_n if syms else None
    top5_share = sum(s["n"] for s in syms[:5]) / total_n if syms else None
    # exclude top-1 and top-3
    top1_sym = syms[0]["symbol"] if syms else None
    top3_syms = [s["symbol"] for s in syms[:3]]
    subset_wo_top1 = [r for r in eligible if r["symbol"] != top1_sym]
    subset_wo_top3 = [r for r in eligible if r["symbol"] not in top3_syms]
    return {
        "symbols": syms,
        "top1_share_n": top1_share,
        "top3_share_n": top3_share,
        "top5_share_n": top5_share,
        "exclude_top1": gross_metrics(subset_wo_top1) | net_metrics(subset_wo_top1, NORMAL_COST),
        "exclude_top3": gross_metrics(subset_wo_top3) | net_metrics(subset_wo_top3, NORMAL_COST),
    }


def regime_sensitivity(eligible: list[dict]) -> list[dict]:
    by_regime: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        by_regime[r.get("market_regime") or "UNKNOWN"].append(r)
    out = []
    for regime in sorted(by_regime):
        gm = gross_metrics(by_regime[regime])
        nm = net_metrics(by_regime[regime], NORMAL_COST)
        out.append({"regime": regime, **gm, **{k: nm[k] for k in ("net_er", "net_pf") if k in nm}})
    return out


def fvg_feature_diagnostics(eligible: list[dict]) -> list[dict]:
    """Pre-specified FVG features from registry/adapter, binned by median split."""
    import json as _json
    feats = []
    for r in eligible:
        try:
            f = _json.loads(r.get("features", "{}"))
        except Exception:
            f = {}
        feats.append({
            "fvg_atr": f.get("fvg_atr"),
            "c2_body_ratio": f.get("c2_body_ratio"),
            "bars_to_touch": f.get("bars_to_touch"),
            "r": compute_r_gross(r),
        })
    out = []
    for key in ("fvg_atr", "c2_body_ratio", "bars_to_touch"):
        vals = [x[key] for x in feats if x[key] is not None]
        if not vals:
            continue
        med = statistics.median(vals)
        lo = [x["r"] for x in feats if x[key] is not None and x[key] <= med and x["r"] is not None]
        hi = [x["r"] for x in feats if x[key] is not None and x[key] > med and x["r"] is not None]
        lo_wins = sum(1 for x in lo if x > 0)
        hi_wins = sum(1 for x in hi if x > 0)
        out.append({
            "feature": key,
            "median": med,
            "lo_n": len(lo), "lo_er": statistics.mean(lo) if lo else None,
            "lo_wr": lo_wins / len(lo) if lo else None,
            "hi_n": len(hi), "hi_er": statistics.mean(hi) if hi else None,
            "hi_wr": hi_wins / len(hi) if hi else None,
        })
    return out


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
    eligible, pop = eligible_population(rows)
    gm = gross_metrics(eligible)
    nm_normal = net_metrics(eligible, NORMAL_COST)
    nm_stress = net_metrics(eligible, STRESSED_COST)
    net_rs = []
    for r in eligible:
        g = compute_r_gross(r)
        if g is None:
            continue
        risk = abs(r["reference_price"] - r["invalidation_price"])
        if risk <= 0:
            continue
        cost_r = (NORMAL_COST * r["reference_price"]) / risk
        net_rs.append(g - cost_r)
    boot = bootstrap_ci(net_rs) if net_rs else {}
    day = day_concentration(eligible)
    sym = symbol_concentration(eligible)
    regime = regime_sensitivity(eligible)
    fvg_feats = fvg_feature_diagnostics(eligible)

    results = {
        "experiment_id": EXPERIMENT_ID,
        "cutoff_utc": CUTOFF_UTC,
        "git_head": GIT_HEAD,
        "integrity": integrity,
        "population": pop,
        "gross": gm,
        "net_normal": nm_normal,
        "net_stressed": nm_stress,
        "bootstrap": boot,
        "day_concentration": day,
        "symbol_concentration": sym,
        "regime_sensitivity": regime,
        "fvg_feature_diagnostics": fvg_feats,
        "costs": {"normal_round_trip": NORMAL_COST, "stressed_round_trip": STRESSED_COST},
        "random_seed": SEED,
        "bootstrap_n": BOOTSTRAP_N,
    }

    with open(outdir / "fvg_expectancy_rejection_oos_review_v1_results.json", "w") as f:
        json.dump(results, f, indent=2)

    # CSV of eligible observations
    with open(outdir / "fvg_expectancy_rejection_oos_review_v1_observations.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["observation_id", "symbol", "signal_time", "day",
                     "gross_r_240m", "net_r_normal", "mfe_r_240m", "mae_r_240m",
                     "market_regime", "fvg_atr", "c2_body_ratio", "bars_to_touch"])
        for r in eligible:
            g = compute_r_gross(r)
            risk = abs(r["reference_price"] - r["invalidation_price"]) if r["reference_price"] else 0
            cost_r = (NORMAL_COST * r["reference_price"]) / risk if risk > 0 else None
            n = g - cost_r if g is not None and cost_r is not None else None
            try:
                feats = json.loads(r.get("features", "{}"))
            except Exception:
                feats = {}
            w.writerow([
                r["observation_id"], r["symbol"], r["signal_time"], r["signal_time"][:10],
                g, n, r.get("mfe_r_240m"), r.get("mae_r_240m"),
                r.get("market_regime"), feats.get("fvg_atr"),
                feats.get("c2_body_ratio"), feats.get("bars_to_touch"),
            ])

    print(json.dumps({
        "raw_n": integrity["raw_n"],
        "eligible_n": pop["eligible_n"],
        "excluded_n": pop["excluded_n"],
        "gross_er": gm.get("gross_er"),
        "gross_pf": gm.get("gross_pf"),
        "net_er_normal": nm_normal.get("net_er"),
        "net_pf_normal": nm_normal.get("net_pf"),
        "bootstrap": boot,
    }, indent=2))


if __name__ == "__main__":
    main()
