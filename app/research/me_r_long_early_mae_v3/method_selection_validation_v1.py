"""Predeclared research-only screening of two existing CI candidates.

Not a proof of coverage for unmodelled dependencies. No OOS/DB input.
The only possible disposition is SCREENING_PASS or NO_METHOD_APPROVED.
"""
from __future__ import annotations

import argparse
import json
from math import sqrt

from .coverage_validation import SCENARIOS
from .paired_candidate_comparison import compare

HOLDOUT_SEED = 20261119
PILOT_SEED = 20261014
METHODS = ("envelope", "two_way_t")
DESIGNS = (100, 300)
MIN_HOLDOUT_DATASETS = 1000
MIN_HOLDOUT_REPS = 300
NOMINAL_COVERAGE = 0.95
MAX_COVERAGE_DEFICIT = 0.02


def wilson_lower(covered: int, total: int, *, z: float = 1.96) -> float:
    if total < 1 or not 0 <= covered <= total:
        raise ValueError("INVALID_COVERAGE_COUNTS")
    p = covered / total
    zz = z*z
    return ((p + zz/(2*total)) -
            z*sqrt(p*(1-p)/total + zz/(4*total*total))) / (1+zz/total)


def run_screen(*, mode: str, datasets: int, reps: int) -> dict:
    if mode not in ("pilot", "holdout"):
        raise ValueError("UNKNOWN_MODE")
    if type(datasets) is not int or datasets < 1:
        raise ValueError("INVALID_DATASETS")
    if type(reps) is not int or reps < 100:
        raise ValueError("INVALID_REPS")
    if mode == "holdout" and (datasets < MIN_HOLDOUT_DATASETS or reps < MIN_HOLDOUT_REPS):
        raise ValueError("UNDERPOWERED_HOLDOUT")
    seed = PILOT_SEED if mode == "pilot" else HOLDOUT_SEED
    results = []
    passed = {name: True for name in METHODS}
    for n in DESIGNS:
        for scenario in SCENARIOS:
            result = compare(scenario, datasets=datasets, reps=reps, seed=seed, n=n)
            metrics = {}
            for method in METHODS:
                entry = result["methods"][method]
                lo = wilson_lower(entry["covered"], datasets)
                # Invalid/undefined intervals are failures in the unconditional count.
                eligible = entry["invalid"] == 0 and lo >= NOMINAL_COVERAGE - MAX_COVERAGE_DEFICIT
                if not eligible:
                    passed[method] = False
                metrics[method] = {
                    **entry, "wilson95_lower_all": lo, "screening_gate_pass": eligible
                }
            results.append({"scenario": scenario, "n": n, "methods": metrics})
    # No statistical method is approved even if all screening gates pass.
    return {
        "mode": mode,
        "datasets_per_scenario_design": datasets,
        "bootstrap_reps": reps,
        "seed": seed,
        "scenarios": list(SCENARIOS),
        "design_n": list(DESIGNS),
        "screening_pass": passed if mode == "holdout" else None,
        "results": results,
        "confirmatory_disposition": "NO_METHOD_APPROVED",
        "requires_operator_method_approval": True,
        "status": "RESEARCH_ONLY_NO_OOS_OUTCOMES",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("pilot", "holdout"), default="pilot")
    parser.add_argument("--datasets", type=int, default=100)
    parser.add_argument("--reps", type=int, default=300)
    args = parser.parse_args()
    print(json.dumps(run_screen(mode=args.mode, datasets=args.datasets, reps=args.reps),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
