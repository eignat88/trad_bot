"""Read-only prospective execution analysis for SRR SHORT execution-R expansion.

This module is deliberately independent of production DB access. It consumes
prospective_observation/outcome rows and derives a single consistent execution-R
normalization:

    structural_R = abs(entry - invalidation_price)
    execution_R  = 2.00 * structural_R

For SHORT trades:

    gross_R(TP) = +0.75
    gross_R(SL) = -1.00
    gross_R(AMBIGUOUS under STOP_FIRST) = -1.00
    gross_R(TIMEOUT) = (entry - timeout_close) / execution_R

TIMEOUT may be derived from the evaluator's ``return_at_120m`` field, which is a
conventional long-direction percentage return based on the last eligible candle
close before the 120m cutoff. For SHORT:

    timeout_close = entry * (1.0 + return_at_120m / 100.0)
    timeout_gross_R = -(return_at_120m / 100.0) * entry / execution_R

True AMBIGUOUS is derived only from ``ambiguous_intrabar == true``. It is never
derived from ``tp_hit and sl_hit``.
"""
from __future__ import annotations

import random
import statistics
from collections import Counter, defaultdict
from typing import Any, Iterable, Sequence

EXPERIMENT_ID = "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
NORMAL_ROUND_TRIP = 0.0021
ELEVATED_ROUND_TRIP = 0.0031
EXECUTION_R_MULTIPLIER = 2.0
DEFAULT_BOOTSTRAP_N = 10_000
DEFAULT_BOOTSTRAP_SEED = 20261007


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def derive_execution_r(entry: float, invalidation_price: float) -> float:
    structural_r = abs(float(entry) - float(invalidation_price))
    return EXECUTION_R_MULTIPLIER * structural_r


def derive_realized_path_class(
    *,
    ambiguous_intrabar: bool = False,
    tp_before_sl: bool = False,
    sl_before_tp: bool = False,
) -> str:
    """Return TP_FIRST / SL_FIRST / AMBIGUOUS / TIMEOUT.

    The forbidden legacy shortcut ``tp_hit AND sl_hit -> AMBIGUOUS`` is not
    used. A path is AMBIGUOUS only when the evaluator explicitly marked the
    first executable candle as ambiguous. STOP_FIRST is represented by the
    evaluator's ``sl_before_tp`` flag on that same ambiguous candle.
    """
    if ambiguous_intrabar:
        return "AMBIGUOUS"
    if tp_before_sl:
        return "TP_FIRST"
    if sl_before_tp:
        return "SL_FIRST"
    return "TIMEOUT"


def derive_timeout_close(entry: float, return_at_120m: float | None) -> float | None:
    """Derive timeout close from conventional LONG percentage return semantics."""
    ret = _as_float(return_at_120m)
    if ret is None:
        return None
    return float(entry) * (1.0 + ret / 100.0)


def derive_timeout_gross_r(
    entry: float,
    invalidation_price: float,
    return_at_120m: float | None,
    *,
    timeout_close: float | None = None,
) -> float | None:
    """Derive SHORT timeout gross R using execution_R normalization."""
    execution_r = derive_execution_r(entry, invalidation_price)
    if execution_r <= 0:
        return None
    close = _as_float(timeout_close)
    if close is None:
        close = derive_timeout_close(entry, return_at_120m)
    if close is None:
        return None
    return (float(entry) - close) / execution_r


def derive_gross_r(
    *,
    entry: float,
    invalidation_price: float,
    path_class: str,
    return_at_120m: float | None = None,
    timeout_close: float | None = None,
) -> float | None:
    execution_r = derive_execution_r(entry, invalidation_price)
    if execution_r <= 0:
        return None
    if path_class == "TP_FIRST":
        return 0.75
    if path_class in {"SL_FIRST", "AMBIGUOUS"}:
        return -1.0
    if path_class == "TIMEOUT":
        return derive_timeout_gross_r(
            entry,
            invalidation_price,
            return_at_120m,
            timeout_close=timeout_close,
        )
    raise ValueError(f"unknown path class: {path_class}")


def derive_cost_r(
    entry: float,
    invalidation_price: float,
    *,
    round_trip: float = NORMAL_ROUND_TRIP,
) -> float:
    execution_r = derive_execution_r(entry, invalidation_price)
    if execution_r <= 0:
        raise ValueError("execution_R must be positive")
    return float(round_trip) * float(entry) / execution_r


def profit_factor(values: Iterable[float]) -> float | None:
    values = list(values)
    gross = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses <= 0:
        return None
    return gross / losses


def quantile(values: Sequence[float], probability: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def bootstrap_mean(
    values: Sequence[float],
    *,
    n_resamples: int = DEFAULT_BOOTSTRAP_N,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any] | None:
    values = [float(value) for value in values if value is not None]
    if not values:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        means.append(statistics.fmean(values[rng.randrange(len(values))] for _ in values))
    return {
        "point_estimate": statistics.fmean(values),
        "ci_95_low": quantile(means, 0.025),
        "ci_95_high": quantile(means, 0.975),
        "p_er_gt_0": sum(value > 0 for value in means) / len(means),
        "n_resamples": len(means),
        "seed": seed,
    }


def _normalize_observation(row: dict[str, Any]) -> dict[str, Any]:
    entry = _as_float(row.get("variant_entry")) or _as_float(row.get("reference_price"))
    invalidation = _as_float(row.get("invalidation_price"))
    if entry is None or invalidation is None:
        raise ValueError("observation requires entry/reference_price and invalidation_price")
    outcome = row.get("outcome") or {}
    if isinstance(outcome, str):
        raise TypeError("outcome must be a mapping, not JSON text")
    path_class = derive_realized_path_class(
        ambiguous_intrabar=bool(outcome.get("ambiguous_intrabar", False)),
        tp_before_sl=bool(outcome.get("tp_before_sl", False)),
        sl_before_tp=bool(outcome.get("sl_before_tp", False)),
    )
    gross_r = derive_gross_r(
        entry=entry,
        invalidation_price=invalidation,
        path_class=path_class,
        return_at_120m=outcome.get("return_at_120m"),
        timeout_close=outcome.get("timeout_close"),
    )
    normal_cost_r = derive_cost_r(
        entry, invalidation, round_trip=NORMAL_ROUND_TRIP,
    )
    elevated_cost_r = derive_cost_r(
        entry, invalidation, round_trip=ELEVATED_ROUND_TRIP,
    )
    signal_time = row.get("signal_time")
    day = row.get("day")
    if day is None and signal_time is not None:
        day = str(signal_time)[:10]
    return {
        "observation_id": row.get("observation_id"),
        "symbol": row.get("symbol"),
        "signal_time": signal_time,
        "day": day,
        "entry": entry,
        "invalidation_price": invalidation,
        "execution_r": derive_execution_r(entry, invalidation),
        "path_class": path_class,
        "gross_r": gross_r,
        "normal_cost_r": normal_cost_r,
        "elevated_cost_r": elevated_cost_r,
        "net_r_normal": None if gross_r is None else gross_r - normal_cost_r,
        "net_r_elevated": None if gross_r is None else gross_r - elevated_cost_r,
    }


def _metrics(rows: Sequence[dict[str, Any]], net_key: str) -> dict[str, Any]:
    net = [row[net_key] for row in rows if row.get(net_key) is not None]
    gross = [row["gross_r"] for row in rows if row.get("gross_r") is not None]
    counts = Counter(row["path_class"] for row in rows)
    return {
        "n": len(rows),
        "tp_first": counts.get("TP_FIRST", 0),
        "sl_first": counts.get("SL_FIRST", 0),
        "ambiguous": counts.get("AMBIGUOUS", 0),
        "timeout": counts.get("TIMEOUT", 0),
        "wins": sum(value > 0 for value in net),
        "losses": sum(value < 0 for value in net),
        "gross_er": statistics.fmean(gross) if gross else None,
        "net_er": statistics.fmean(net) if net else None,
        "gross_pf": profit_factor(gross),
        "net_pf": profit_factor(net),
        "total_gross_r": sum(gross),
        "total_net_r": sum(net),
        "win_rate": (sum(value > 0 for value in net) / len(net)) if net else None,
        "normal_cost_r_median": statistics.median(
            [row["normal_cost_r"] for row in rows if row.get("normal_cost_r") is not None]
        ) if any(row.get("normal_cost_r") is not None for row in rows) else None,
        "bootstrap": bootstrap_mean(net) if net_key == "net_r_normal" else None,
    }


def _concentration(
    rows: Sequence[dict[str, Any]], key: str, net_key: str = "net_r_normal",
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key))].append(row)
    result = []
    for group_key, group_rows in grouped.items():
        metrics = _metrics(group_rows, net_key)
        result.append({"key": group_key, **metrics})
    return sorted(result, key=lambda item: item.get("total_net_r") or 0.0, reverse=True)


def analyze_observations(
    rows: Sequence[dict[str, Any]],
    *,
    bootstrap_n: int = DEFAULT_BOOTSTRAP_N,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Analyze already-mature prospective observations without writing to DB."""
    normalized = [_normalize_observation(row) for row in rows]
    normal = _metrics(normalized, "net_r_normal")
    elevated = _metrics(normalized, "net_r_elevated")
    normal["bootstrap"] = bootstrap_mean(
        [row["net_r_normal"] for row in normalized if row["net_r_normal"] is not None],
        n_resamples=bootstrap_n,
        seed=seed,
    )
    days = _concentration(normalized, "day")
    symbols = _concentration(normalized, "symbol")
    leave_one_day_out = []
    for day_row in days:
        subset = [row for row in normalized if row["day"] != day_row["key"]]
        leave_one_day_out.append({
            "excluded_day": day_row["key"],
            **_metrics(subset, "net_r_normal"),
        })
    symbol_totals = {row["key"]: row["total_net_r"] for row in symbols}
    top_symbols = sorted(
        symbol_totals,
        key=lambda symbol: symbol_totals[symbol] or 0.0,
        reverse=True,
    )
    top_1 = top_symbols[:1]
    top_3 = top_symbols[:3]
    return {
        "experiment_id": EXPERIMENT_ID,
        "eligible_n": len(normalized),
        "symbols": len({row["symbol"] for row in normalized if row.get("symbol")}),
        "oos_days": len({row["day"] for row in normalized if row.get("day")}),
        "first_signal": min((row["signal_time"] for row in normalized if row.get("signal_time")), default=None),
        "last_signal": max((row["signal_time"] for row in normalized if row.get("signal_time")), default=None),
        "normal": normal,
        "elevated": elevated,
        "day_concentration": days,
        "symbol_concentration": symbols,
        "leave_one_day_out": leave_one_day_out,
        "exclude_top_1_positive_symbol": _metrics(
            [row for row in normalized if row["symbol"] not in top_1],
            "net_r_normal",
        ),
        "exclude_top_3_positive_symbols": _metrics(
            [row for row in normalized if row["symbol"] not in top_3],
            "net_r_normal",
        ),
        "normalization": {
            "structural_r": "abs(entry - invalidation_price)",
            "execution_r": "2.00 * structural_R",
            "tp_r": 0.75,
            "sl_r": -1.0,
            "ambiguous_stop_first_r": -1.0,
            "timeout_source": "last eligible 5m close before 120m cutoff, via return_at_120m",
        },
    }
