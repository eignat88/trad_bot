"""Research-only two-way cluster-robust Student-t *candidate*, NOT validated CI.

Cameron-Gelbach-Miller inclusion-exclusion variance for an event-weighted
paired mean, small-cluster t critical values, and strict fail-closed rules.
This is a diagnostic candidate; it grants no inferential decision.
"""
from __future__ import annotations

from collections import defaultdict
from math import isfinite, sqrt
from statistics import mean, NormalDist

from .statistics_proposal import Pair

# Student-t 0.975 quantiles for degrees of freedom 1..30 (standard table).
T975 = (None, 12.706205, 4.302653, 3.182446, 2.776445, 2.570582,
        2.446912, 2.364624, 2.306004, 2.262157, 2.228139,
        2.200985, 2.178813, 2.160369, 2.144787, 2.131450,
        2.119905, 2.109816, 2.100922, 2.093024, 2.085963,
        2.079614, 2.073873, 2.068658, 2.063899, 2.059539,
        2.055529, 2.051831, 2.048407, 2.045230, 2.042272)


def two_way_t_candidate(pairs: list[Pair], *, min_clusters: int = 10) -> dict:
    if not pairs:
        raise ValueError("EMPTY_COHORT")
    if len({p.source_id for p in pairs}) != len(pairs):
        raise ValueError("DUPLICATE_SOURCE")
    if type(min_clusters) is not int or min_clusters < 2:
        raise ValueError("INVALID_MIN_CLUSTERS")
    if any(not p.symbol or not p.oos_day or not isfinite(p.delta) for p in pairs):
        raise ValueError("INVALID_PAIR")
    n = len(pairs)
    g_sym = len({p.symbol for p in pairs})
    g_day = len({p.oos_day for p in pairs})
    if g_sym < min_clusters or g_day < min_clusters:
        raise ValueError("INSUFFICIENT_CLUSTERS")
    mu = mean(p.delta for p in pairs)

    def component(key_fn):
        scores = defaultdict(float)
        for p in pairs:
            scores[key_fn(p)] += p.delta - mu
        g = len(scores)
        if g < 2:
            raise ValueError("INSUFFICIENT_CLUSTERS")
        return (g/(g-1)) * sum(s*s for s in scores.values())/(n*n)

    variance = (component(lambda p: p.symbol) +
                component(lambda p: p.oos_day) -
                component(lambda p: (p.symbol, p.oos_day)))
    if not isfinite(variance) or variance <= 0:
        raise ValueError("NONPOSITIVE_TWO_WAY_VARIANCE")

    df = min(g_sym, g_day)-1
    # For df > 30, conservative use of t(30); do not approximate with normal.
    critical = T975[min(df, 30)]
    se = sqrt(variance)
    return {
        "mean_delta_net_r": mu,
        "ci95_candidate": (mu-critical*se, mu+critical*se),
        "variance": variance,
        "critical": critical,
        "df_proxy": df,
        "symbols": g_sym,
        "days": g_day,
        "n": n,
        "status": "EXPLORATORY_TWO_WAY_T_NOT_APPROVED_FOR_CONFIRMATORY_INFERENCE",
    }
