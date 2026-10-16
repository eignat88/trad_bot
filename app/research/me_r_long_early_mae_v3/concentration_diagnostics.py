"""Research-only concentration and crossed-cluster diagnostics.

No thresholds are fitted to historical coverage outcomes. Weak structural
designs are flagged; only operators can approve final preregistration.
"""
from __future__ import annotations

from collections import Counter
from math import isfinite
from .statistics_proposal import Pair


def concentration_diagnostics(pairs: list[Pair]) -> dict:
    if not pairs:
        raise ValueError("EMPTY_COHORT")
    if len({p.source_id for p in pairs}) != len(pairs):
        raise ValueError("DUPLICATE_SOURCE")
    if any(not p.symbol or not p.oos_day or not isfinite(p.delta) for p in pairs):
        raise ValueError("INVALID_PAIR")
    n = len(pairs)
    cs = Counter(p.symbol for p in pairs)
    cd = Counter(p.oos_day for p in pairs)
    cells = Counter((p.symbol, p.oos_day) for p in pairs)

    def metrics(counts):
        shares = sorted((count/n for count in counts.values()), reverse=True)
        hhi = sum(v*v for v in shares)
        return {
            "clusters": len(shares),
            "max_share": shares[0],
            "top_two_share": sum(shares[:2]),
            "hhi": hhi,
            "effective_clusters": 1/hhi,
            "single_event_clusters": sum(v == 1 for v in counts.values()),
        }

    sym, day, cell = metrics(cs), metrics(cd), metrics(cells)
    return {
        "n": n, "symbol": sym, "day": day, "cell": cell,
        "incidence_density": len(cells)/(len(cs)*len(cd)),
        "max_cell_events": max(cells.values()),
        "status": "DIAGNOSTIC_ONLY_THRESHOLDS_NOT_APPROVED",
    }


def structural_quality_gate(pairs: list[Pair], *,
                            min_symbols: int = 20,
                            min_days: int = 20,
                            min_effective_symbols: float = 10.,
                            min_effective_days: float = 10.) -> dict:
    """Sensitivity gate, NOT approved protocol-level quality requirements.

    Default thresholds are engineering *proposals* that need independent
    preregistration. This gate cannot issue a positive or negative verdict.
    """
    if any(type(v) is not int or v < 2 for v in (min_symbols, min_days)):
        raise ValueError("INVALID_CLUSTER_MINIMUM")
    if any(not isfinite(v) or v < 2 for v in
           (min_effective_symbols, min_effective_days)):
        raise ValueError("INVALID_EFFECTIVE_MINIMUM")
    report = concentration_diagnostics(pairs)
    reasons = []
    for key, min_clusters, min_effective in (
        ("symbol", min_symbols, min_effective_symbols),
        ("day", min_days, min_effective_days),
    ):
        if report[key]["clusters"] < min_clusters:
            reasons.append(f"{key.upper()}_CLUSTERS_BELOW_PROPOSAL")
        if report[key]["effective_clusters"] < min_effective:
            reasons.append(f"{key.upper()}_EFFECTIVE_BELOW_PROPOSAL")
    return {
        "decision": "PROVISIONAL_SCREEN_ONLY" if not reasons else "INCONCLUSIVE",
        "reasons": reasons,
        "diagnostics": report,
        "status": "UNAPPROVED_QUALITY_THRESHOLDS_NO_INFERENCE_AUTHORIZED",
    }
