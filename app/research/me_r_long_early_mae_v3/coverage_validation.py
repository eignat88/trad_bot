"""Research-only Monte Carlo diagnostics for PR #148 (no trading/DB dependencies).

The result is NOT a calibrated 95% confidence procedure. Synthetic outcomes
are paired null deltas; no inference verdict or protocol approval is emitted.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite, sqrt
from random import Random
from statistics import mean

from .statistics_proposal import Pair, primary_interval

SCENARIOS = (
    "IID", "SYMBOL_SHOCK", "DAY_SHOCK", "CROSSED_SHOCK",
    "UNBALANCED_CROSSED", "HEAVY_TAIL_CROSSED",
)


@dataclass(frozen=True)
class CoverageResult:
    scenario: str
    datasets: int
    valid: int
    invalid: int
    covered: int
    coverage_all: float
    coverage_valid: float | None
    mean_interval_width: float | None
    mc_standard_error: float
    status: str = "DIAGNOSTIC_ONLY_NO_CONFIRMATORY_APPROVAL"


def generate_pairs(scenario: str, *, seed: int, n: int = 100,
                   symbols: int = 20, days: int = 25) -> list[Pair]:
    """Random symbol/day incidence instead of i%20/i%25 deterministic pairing."""
    if scenario not in SCENARIOS:
        raise ValueError("UNKNOWN_SCENARIO")
    if n < 2 or symbols < 2 or days < 2:
        raise ValueError("INVALID_DESIGN")
    rng = Random(seed)
    symbol_shocks = [rng.gauss(0, .30) for _ in range(symbols)]
    day_shocks = [rng.gauss(0, .30) for _ in range(days)]
    rows = []
    for i in range(n):
        if scenario == "UNBALANCED_CROSSED":
            sym = rng.randrange(2) if rng.random() < .65 else rng.randrange(symbols)
            day = rng.randrange(2) if rng.random() < .65 else rng.randrange(days)
        else:
            sym, day = rng.randrange(symbols), rng.randrange(days)
        if scenario == "HEAVY_TAIL_CROSSED":
            z = rng.gauss(0, 1)
            chi2 = sum(rng.gauss(0, 1)**2 for _ in range(3))
            noise = .35*z/sqrt(chi2)  # normalized t(3), variance .35**2
        else:
            noise = rng.gauss(0, .35)
        delta = noise
        if scenario in ("SYMBOL_SHOCK", "CROSSED_SHOCK", "UNBALANCED_CROSSED", "HEAVY_TAIL_CROSSED"):
            delta += symbol_shocks[sym]
        if scenario in ("DAY_SHOCK", "CROSSED_SHOCK", "UNBALANCED_CROSSED", "HEAVY_TAIL_CROSSED"):
            delta += day_shocks[day]
        rows.append(Pair(i+1, f"S{sym:03d}", f"D{day:03d}", 0., delta))
    return rows


def run_coverage(scenario: str, *, datasets: int, reps: int,
                 seed: int = 20261014, n: int = 100,
                 symbols: int = 20, days: int = 25) -> CoverageResult:
    if type(datasets) is not int or datasets < 1:
        raise ValueError("INVALID_DATASETS")
    if type(seed) is not int:
        raise ValueError("INVALID_SEED")
    covered, invalid = 0, 0
    widths = []
    for k in range(datasets):
        rows = generate_pairs(scenario, seed=seed+7919*k, n=n, symbols=symbols, days=days)
        try:
            out = primary_interval(rows, reps=reps, seed=seed+15427*k)
        except ValueError as exc:
            if str(exc) != "INSUFFICIENT_CLUSTERS":
                raise
            invalid += 1
            continue
        lo, hi = out["screening_envelope_ci95"]
        if not (isfinite(lo) and isfinite(hi) and lo <= hi):
            raise RuntimeError("INVALID_CI")
        covered += int(lo <= 0 <= hi)
        widths.append(hi-lo)
    valid = len(widths)
    p = covered/datasets
    return CoverageResult(
        scenario, datasets, valid, invalid, covered, p,
        covered/valid if valid else None,
        mean(widths) if widths else None,
        sqrt(p*(1-p)/datasets),
    )


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets", type=int, default=100)
    parser.add_argument("--reps", type=int, default=300)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261014)
    args = parser.parse_args()
    for scenario in SCENARIOS:
        result = run_coverage(
            scenario, datasets=args.datasets, reps=args.reps,
            n=args.n, seed=args.seed,
        )
        print(json.dumps(asdict(result), sort_keys=True))
