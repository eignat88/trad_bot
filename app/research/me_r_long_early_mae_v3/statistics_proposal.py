"""Standalone research-only statistical proposal. Never called by capture/runtime.

The two marginal cluster intervals are enveloped; this is a conservative
engineering sensitivity screen, NOT a validated simultaneous coverage claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from random import Random
from statistics import mean

PRIMARY_ID = "MAE_15m_050"
DEFAULT_SEED = 20261014
DEFAULT_REPS = 10_000


@dataclass(frozen=True)
class Pair:
    source_id: int
    symbol: str
    oos_day: str
    baseline_net_r: float
    primary_net_r: float

    @property
    def delta(self) -> float:
        return self.primary_net_r - self.baseline_net_r


def _quantile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values or not 0 <= probability <= 1:
        raise ValueError("INVALID_QUANTILE")
    ix = (len(sorted_values) - 1) * probability
    lo = int(ix)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (ix - lo)


def _cluster_interval(pairs: tuple[Pair, ...], field: str, reps: int, seed: int) -> tuple[float, float]:
    buckets: dict[str, list[float]] = {}
    for item in pairs:
        buckets.setdefault(getattr(item, field), []).append(item.delta)
    keys = sorted(buckets)
    if len(keys) < 2:
        raise ValueError("INSUFFICIENT_CLUSTERS")
    rng = Random(seed)
    replicates: list[float] = []
    for _ in range(reps):
        sampled = [buckets[keys[rng.randrange(len(keys))]] for _ in keys]
        values = [v for bucket in sampled for v in bucket]
        replicates.append(mean(values))
    replicates.sort()
    return _quantile(replicates, 0.025), _quantile(replicates, 0.975)


def primary_interval(
    pairs: list[Pair],
    *,
    reps: int = DEFAULT_REPS,
    seed: int = DEFAULT_SEED,
) -> dict[str, object]:
    """Compute reproducible cluster sensitivity intervals from ALREADY validated pairs.

    Input must be a fully locked, post-activation, paired-finalized extract.
    This function does not decide cohort eligibility or positive-edge status.
    """
    if type(reps) is not int or reps < 100:
        raise ValueError("INVALID_REPS")
    if type(seed) is not int:
        raise ValueError("INVALID_SEED")
    if not pairs:
        raise ValueError("EMPTY_COHORT")
    if len({p.source_id for p in pairs}) != len(pairs):
        raise ValueError("DUPLICATE_SOURCE")
    if any(
        not p.symbol or not p.oos_day
        or not all(isfinite(float(v)) for v in (p.baseline_net_r, p.primary_net_r))
        for p in pairs
    ):
        raise ValueError("INVALID_PAIR")
    ordered = tuple(sorted(pairs, key=lambda p: p.source_id))
    sym = _cluster_interval(ordered, "symbol", reps, seed)
    day = _cluster_interval(ordered, "oos_day", reps, seed + 1)
    envelope = (min(sym[0], day[0]), max(sym[1], day[1]))
    return {
        "primary_variant": PRIMARY_ID,
        "paired_finalized_n": len(ordered),
        "symbols": len({p.symbol for p in ordered}),
        "oos_days": len({p.oos_day for p in ordered}),
        "mean_delta_net_r": mean(p.delta for p in ordered),
        "symbol_cluster_ci95": sym,
        "day_cluster_ci95": day,
        "screening_envelope_ci95": envelope,
        "reps": reps,
        "seed": seed,
        "status": "SENSITIVITY_ONLY_NOT_APPROVED_CONFIRMATORY_CI",
    }


def checkpoint_status(*, filled_n: int, symbols: int, days: int, paired_finalized_n: int) -> str:
    if any(type(v) is not int or v < 0 for v in (filled_n, symbols, days, paired_finalized_n)):
        raise ValueError("INVALID_COUNTS")
    if paired_finalized_n > filled_n:
        raise ValueError("FINALIZED_EXCEEDS_FILLED")
    if filled_n < 30 or symbols < 10 or days < 5:
        return "BELOW_OPERATIONAL"
    if filled_n < 100 or symbols < 20 or days < 20 or paired_finalized_n < 100:
        return "OPERATIONAL_ONLY"
    return "COUNT_GATE_MET_VERDICT_NOT_AUTHORIZED"
