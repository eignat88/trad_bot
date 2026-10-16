"""Controlled head-to-head coverage diagnostics; never authorizes inference."""
from __future__ import annotations
from dataclasses import asdict
from math import sqrt
from statistics import mean
from .coverage_validation import SCENARIOS, generate_pairs
from .statistics_proposal import primary_interval
from .two_way_candidate import two_way_t_candidate


def compare(scenario: str, *, datasets: int = 100, reps: int = 300,
            seed: int = 20261014, n: int = 100) -> dict:
    if scenario not in SCENARIOS:
        raise ValueError("UNKNOWN_SCENARIO")
    if type(datasets) is not int or datasets < 1:
        raise ValueError("INVALID_DATASETS")
    outcomes = {
        "envelope": {"covered": 0, "invalid": 0, "widths": []},
        "two_way_t": {"covered": 0, "invalid": 0, "widths": []},
    }
    for k in range(datasets):
        pairs = generate_pairs(scenario, seed=seed+7919*k, n=n)
        try:
            lo, hi = primary_interval(
                pairs, reps=reps, seed=seed+15427*k
            )["screening_envelope_ci95"]
            outcomes["envelope"]["covered"] += int(lo <= 0 <= hi)
            outcomes["envelope"]["widths"].append(hi-lo)
        except ValueError as exc:
            if str(exc) != "INSUFFICIENT_CLUSTERS":
                raise
            outcomes["envelope"]["invalid"] += 1
        try:
            lo, hi = two_way_t_candidate(pairs)["ci95_candidate"]
            outcomes["two_way_t"]["covered"] += int(lo <= 0 <= hi)
            outcomes["two_way_t"]["widths"].append(hi-lo)
        except ValueError as exc:
            if str(exc) not in ("INSUFFICIENT_CLUSTERS", "NONPOSITIVE_TWO_WAY_VARIANCE"):
                raise
            outcomes["two_way_t"]["invalid"] += 1
    results = {}
    for name, d in outcomes.items():
        valid = datasets-d["invalid"]
        p = d["covered"]/datasets
        results[name] = {
            "coverage_all": p,
            "coverage_valid": d["covered"]/valid if valid else None,
            "covered": d["covered"],
            "invalid": d["invalid"],
            "avg_width_valid": mean(d["widths"]) if valid else None,
            "mc_se_all": sqrt(p*(1-p)/datasets),
        }
    return {
        "scenario": scenario, "datasets": datasets, "n": n,
        "reps": reps, "seed": seed, "methods": results,
        "status": "PAIRWISE_SIMULATION_DIAGNOSTIC_ONLY_CONFIRMATORY_BLOCKED",
    }


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets", type=int, default=100)
    parser.add_argument("--reps", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20261014)
    parser.add_argument("--n", type=int, default=100)
    args = parser.parse_args()
    for s in SCENARIOS:
        print(json.dumps(compare(s, datasets=args.datasets, reps=args.reps,
                                 seed=args.seed, n=args.n), sort_keys=True))
