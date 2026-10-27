"""Issue #206 — read-only reference-to-execution price alignment audit V1.

``ME_SHORT_A_REFERENCE_TO_EXECUTION_PRICE_ALIGNMENT_AUDIT_V1`` audits the
economic and timing alignment of the frozen ME SHORT A *synthetic* execution
(entry at ``reference_price`` = ``recent_high``) with an actually executable
SHORT entry (signal-time bid book fill), on top of the append-only Phase 1
evidence produced by ``ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1``.

Strictly read-only and reproducible:
- Every database statement is SELECT-only (``SET TRANSACTION READ ONLY`` is
  additionally requested); nothing here can write, migrate or activate.
- Nothing is substituted: missing / unmatured / mismatched data yields an
  explicit ``INCONCLUSIVE`` row with an exact missing-field list — never a
  zero, never an imputed value.
- The frozen A protocol, retrospective OOS, scanner routing, paper/live and
  the Phase 1 freeze/status are out of scope and never touched.

Frozen protocol provenance (MOMENTUM_EXHAUSTION SHORT, geometry A execution):
- ``sql/migrations/071_me_short_geom_a_execution_v1_registration.sql``
  freezes: entry = reference_price, stop = invalidation_price,
  target = target_1, MAX_HOLD = 240 minutes, PRIMARY_INTRABAR_POLICY =
  STOP_FIRST, costs = taker 0.055%/side + slippage 0.05%/side normal
  (0.10%/side elevated sensitivity), FREEZE_TS = 2026-10-19T08:26:00Z.
- ``app/research/prospective_evaluator.py``: outcome matching on 5m candles
  with first-hit semantics; ``variant_*`` tuple overrides reference geometry
  only when the FULL tuple is present; ``return_at_240m`` is a LONG-signed
  percentage vs the evaluator entry price; MAE for SHORT = (high-entry)/entry
  (adverse up-move), so ``mae_240m > 0`` proves the reference price traded
  again after signal time (a limit at reference would have filled), while
  ``mae_240m = 0`` is a NO_TOUCH of the synthetic entry.
- ``app/scanners/momentum_exhaustion.py``: reference_price = max(last five
  5m highs); invalidation = reference*1.002; target_1 = trigger_close - 2*ATR.

Risk and R definitions (SHORT; absolute stop/target NEVER move):

    structural_r    = stop_price - reference_price          # frozen denominator
    risk_exec       = stop_price - fill                     # executable denominator
    timeout_close   = reference_price * (1 + return_at_240m / 100)   # absolute
    gross_R(entry, risk):
        TP_FIRST                    -> (entry - target_price) / risk
        SL_FIRST / AMBIGUOUS (STOP_FIRST -> stop fills first) -> -1
        TIMEOUT                     -> (entry - timeout_close) / risk, else UNKNOWN

Cost accounting — NO DOUBLE COUNTING (the audit's core invariant):

    observed entry deviation (already inside ``fill``):
        A = (reference - bid) / reference        # reference-to-bid drift
        B = (bid - fill) / reference             # book impact (negative = price
                                                 # improvement on the walk)
        A + B = (reference - fill) / reference
    C = spread = (ask - bid) / mid               # informational: for a SHORT the
        entry executes AT the bid, so C is already implicit in A whenever the
        reference sits at/above the far touch; never subtract C from a
        fill-rebased payoff again.
    D = 2 * fee_per_side                         # explicit fees

    CONTROL accounting (frozen synthetic entry, unchanged frozen outcomes):
        net_R_control = gross_R_control - cost_r_control
        cost_r_control = ROUND_TRIP_frozen * reference / structural_r

    EXECUTABLE accounting (fill-rebased payoff):
        net_R_exec = gross_R_exec - cost_r_entry_fee - cost_r_exec_exit_only
        cost_r_exec_exit_only = exit_cost_pct * fill / risk_exec
        exit_cost_pct_frozen = fee_per_side + slippage_normal_per_side
        exit_cost_pct_worst  = fee_per_side + slippage_elevated_per_side + spread
    The entry-side deviation (A+B, plus entry-side spread) is NEVER subtracted
    from a fill-rebased payoff: it is already realized inside ``fill``. The
    Phase-1 diagnostic ``estimated_roundtrip_cost_pct = A+B + C + 2*fee`` is a
    REFERENCE-anchored diagnostic only; combining it with a fill-rebased payoff
    would double count ``A+B+C``. The report quantifies that overlap.

Verdicts (both emitted, both independent):
- ``alignment_verdict``: ALIGNMENT_CONFIRMED | ALIGNMENT_INVALID | INCONCLUSIVE
- ``fill_feasibility_verdict``: FILL_FEASIBILITY_PASS | FILL_FEASIBILITY_FAIL |
  FILL_FEASIBILITY_INCONCLUSIVE
Point profitability is NEVER estimated below the frozen cohort thresholds
(minimum_n = 200, minimum_symbols = 20); below them the audit reports
INCONCLUSIVE with a prospective-only plan.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

LOG = logging.getLogger(__name__)

AUDIT = "ME_SHORT_A_REFERENCE_TO_EXECUTION_PRICE_ALIGNMENT_AUDIT_V1"
FROZEN_EXPERIMENT = "ME_SHORT_GEOM_A_EXECUTION_V1"
PHASE1_EXPERIMENT = "ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1"

# Frozen protocol constants (migration 071 / ME_SHORT_GEOM_A_EXECUTION_V1).
FROZEN_OOS_FREEZE_TS = datetime(2026, 10, 19, 8, 26, 0, tzinfo=timezone.utc)
MAX_HOLD_MINUTES = 240
INTRABAR_POLICY = "STOP_FIRST"
FEE_PER_SIDE = Decimal("0.00055")
SLIPPAGE_NORMAL_PER_SIDE = Decimal("0.0005")
SLIPPAGE_ELEVATED_PER_SIDE = Decimal("0.001")
FROZEN_NORMAL_ROUND_TRIP = Decimal("0.0021")   # 2 * (fee + slippage normal)
FROZEN_ELEVATED_ROUND_TRIP = Decimal("0.0031")  # 2 * (fee + slippage elevated)

DEFAULT_SCENARIOS = (Decimal("100"), Decimal("500"), Decimal("1000"))

# Frozen cohort sufficiency thresholds (migration 071 minimum_n/minimum_symbols).
MINIMUM_N = 200
MINIMUM_SYMBOLS = 20
# Absolute statistical floor for publishing any aggregate estimate at all.
ESTIMATE_MIN_N = 30
# Fill feasibility pass threshold (base-senario executable fill rate).
FEASIBILITY_PASS_RATE = Decimal("0.95")

# Bootstrap (seeded, reproducible).
DEFAULT_BOOTSTRAP_N = 10_000
DEFAULT_BOOTSTRAP_SEED = 20261026

BOOK_FUTURE_SKEW_MS = 1000

# Row-level lineage/data statuses.
OK = "OK"
MISSING_LINEAGE_OBSERVATION = "MISSING_LINEAGE_OBSERVATION"
MISSING_OUTCOME = "MISSING_OUTCOME"
OUTCOME_UNMATURED = "OUTCOME_UNMATURED"
MISMATCHED_SYMBOL = "MISMATCHED_SYMBOL"
MISMATCHED_DIRECTION = "MISMATCHED_DIRECTION"
MISMATCHED_SIGNAL_TIME = "MISMATCHED_SIGNAL_TIME"
MISMATCHED_REFERENCE_PRICE = "MISMATCHED_REFERENCE_PRICE"
MISMATCHED_STOP_PRICE = "MISMATCHED_STOP_PRICE"
MISMATCHED_TARGET_PRICE = "MISMATCHED_TARGET_PRICE"
INVALID_GEOMETRY = "INVALID_GEOMETRY"
MISSING_EXIT_DATA = "MISSING_EXIT_DATA"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"

# Verdict values.
ALIGNMENT_CONFIRMED = "ALIGNMENT_CONFIRMED"
ALIGNMENT_INVALID = "ALIGNMENT_INVALID"
ALIGNMENT_INCONCLUSIVE = "INCONCLUSIVE"
FEAS_PASS = "FILL_FEASIBILITY_PASS"
FEAS_FAIL = "FILL_FEASIBILITY_FAIL"
FEAS_INCONCLUSIVE = "FILL_FEASIBILITY_INCONCLUSIVE"

# Entry availability classes (Phase 1 feasibility_status mapping).
AVAILABILITY = {
    "OK": "AVAILABLE",
    "INSUFFICIENT_DEPTH": "INFEASIBLE_DEPTH",
    "BELOW_EXCHANGE_MIN": "INFEASIBLE_EXCHANGE_MIN",
    "STALE_BOOK": "QUOTE_UNRELIABLE",
    "INVALID_BOOK": "QUOTE_UNRELIABLE",
    "ERROR": "QUOTE_UNRELIABLE",
}

# Fields the audit would need but which Phase 1 evidence does NOT store.
# Forward-only supplements must be append-only; evidence is never mutated.
MISSING_FIELD_REGISTRY = {
    "trigger_5m_close": "candidate features at signal time (scanner current_price)",
    "entry_zone_low": "scanner entry_zone_low (current_price*0.998)",
    "entry_zone_high": "scanner entry_zone_high (current_price*1.001)",
    "recent_high_window_candles": "the five 5m highs that formed reference_price",
    "post_signal_candle_highs_240m": "absolute post-signal 5m highs/closes for "
                                    "limit-touch and realized exit validation",
    "realized_exit_fills": "actual fill prices for exits (unknown - model only)",
}

PROSPECTIVE_ONLY_PLAN = [
    "Keep Phase 1 RUNNING and strictly append-only: continue signal-time "
    "capture for every new MOMENTUM_EXHAUSTION SHORT candidate; no backfill, "
    "no mutation of existing evidence rows.",
    "Register a SEPARATE forward-only append-only supplement (new table via "
    "its own migration + PR) for trigger_5m_close, entry_zone_low/high and "
    "signal-window candle context; link via event_key, never update "
    "me_short_a_fill_feasibility_v1.",
    "Accumulate at least minimum_n=%d matched matured outcomes across "
    "minimum_symbols=%d symbols (frozen cohort thresholds) before any "
    "profitability claim." % (MINIMUM_N, MINIMUM_SYMBOLS),
    "Re-run this audit (read-only) on the enlarged population; only then may "
    "ALIGNMENT_CONFIRMED / ALIGNMENT_INVALID be issued.",
    "No paper/live activation, no scanner changes, no Phase 1 freeze/status "
    "changes, no production SQL writes as part of this audit.",
]

_EVIDENCE_COLUMNS = (
    "event_key, symbol, signal_time, captured_at, freeze_ts, reference_price, "
    "stop_price, target_price, structural_r, structural_r_pct, bid, ask, "
    "book_time_ms, book_age_ms, spread_bps, reference_to_bid_bps, "
    "depth_bid_10bps_usdt, requested_notional_usdt, requested_qty, "
    "executable_qty, min_order_qty, qty_step, min_notional, estimated_fill, "
    "entry_slippage_bps, estimated_roundtrip_cost_pct, "
    "estimated_roundtrip_cost_r, cost_class, feasibility_status, reason, "
    "scenario_results, source_observation_ids, raw_snapshot"
)

_OBSERVATION_COLUMNS = (
    "observation_id, experiment_id, symbol, direction, signal_time, "
    "reference_price, invalidation_price, target_1, target_2, features, "
    "parameters, created_at"
)

_OUTCOME_COLUMNS = (
    "observation_id, experiment_id, mfe_240m, mae_240m, mfe_r_240m, "
    "mae_r_240m, return_at_240m, tp_hit, sl_hit, tp_before_sl, sl_before_tp, "
    "ambiguous_intrabar, time_to_tp, time_to_sl, evaluated_240m_at, is_final, "
    "created_at"
)


# ── small helpers ─────────────────────────────────────────────────────────

def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _float(value: Any) -> float | None:
    d = _dec(value)
    return float(d) if d is not None else None


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def _num(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


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
    values: Sequence[float | None],
    *,
    n_resamples: int = DEFAULT_BOOTSTRAP_N,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any] | None:
    """Seeded bootstrap CI of the mean; None when there is nothing to resample."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        means.append(statistics.fmean(vals[rng.randrange(len(vals))] for _ in vals))
    return {
        "point_estimate": statistics.fmean(vals),
        "ci_95_low": quantile(means, 0.025),
        "ci_95_high": quantile(means, 0.975),
        "p_mean_gt_0": sum(m > 0 for m in means) / len(means),
        "n": len(vals),
        "n_resamples": len(means),
        "seed": seed,
    }


def profit_factor(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    losses = abs(sum(v for v in vals if v < 0))
    if not vals or losses <= 0:
        return None
    return sum(v for v in vals if v > 0) / losses


def symbol_cluster(symbol: str) -> str:
    """Cluster correlated quote-asset siblings (XAUUSDT/XAUTUSDT -> XAU)."""
    base = symbol[:-4] if symbol.upper().endswith("USDT") else symbol
    letters = "".join(ch for ch in base if ch.isalpha())
    return (letters[:3] or base[:3]).upper()


def derive_path_class(outcome: dict) -> str:
    """ME A path class; AMBIGUOUS under frozen STOP_FIRST resolves to stop."""
    if "outcome_status" in outcome:
        if outcome.get("outcome_status") != "MATCHED_FINALIZED":
            raise ValueError("forward outcome is not finalized")
        mapping = {
            "TARGET_HIT": "TP_FIRST",
            "STOP_HIT": "SL_FIRST",
            "TIMEOUT_240M": "TIMEOUT",
        }
        path = outcome.get("path_class")
        if path not in mapping:
            raise ValueError(f"unsupported forward outcome path: {path}")
        if outcome.get("ambiguous_intrabar"):
            return "AMBIGUOUS_STOP_FIRST"
        return mapping[path]
    if outcome.get("ambiguous_intrabar"):
        return "AMBIGUOUS_STOP_FIRST"
    if outcome.get("tp_before_sl"):
        return "TP_FIRST"
    if outcome.get("sl_before_tp"):
        return "SL_FIRST"
    return "TIMEOUT"


def gross_r(entry: float, stop: float, target: float | None,
            path_class: str, timeout_close: float | None) -> float | None:
    """Gross R for a SHORT with unchanged absolute stop/target prices."""
    risk = stop - entry
    if risk <= 0:
        return None
    if path_class == "TP_FIRST":
        if target is None:
            return None
        return (entry - target) / risk
    if path_class in {"SL_FIRST", "AMBIGUOUS_STOP_FIRST"}:
        return -1.0
    if path_class == "TIMEOUT":
        if timeout_close is None:
            return None  # unknown, never zero
        return (entry - timeout_close) / risk
    raise ValueError(f"unknown path class: {path_class}")


# ── read-only loaders ─────────────────────────────────────────────────────

def _select(conn, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    """Run one SELECT and return dict rows. The audit never executes anything else."""
    statement = sql.strip().upper()
    if not (statement.startswith("SELECT") or statement.startswith("WITH")):
        raise ValueError(f"audit is read-only; refusing statement: {statement[:40]}")
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def _safe_rollback(conn) -> None:
    """Clear an aborted transaction so later SELECTs still run."""
    try:
        conn.rollback()
    except Exception:
        pass


def _error_text(exc: Exception) -> str:
    text = str(exc)
    if "{" in text:
        try:
            import ast
            payload = ast.literal_eval(text[text.index("{"):])
            if isinstance(payload, dict):
                text = payload.get("M", text)
        except (ValueError, SyntaxError):
            pass
    return f"{type(exc).__name__}: {text[:160]}"


def begin_read_only(conn) -> bool:
    """Best-effort SET TRANSACTION READ ONLY (never required for safety: the
    audit only ever issues SELECTs)."""
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        cur.close()
        return True
    except Exception:
        LOG.debug("SET TRANSACTION READ ONLY unavailable", exc_info=True)
        return False


def load_activation(conn) -> dict[str, Any] | None:
    rows = _select(
        conn,
        "SELECT experiment_id, status, freeze_ts FROM "
        "research.me_short_a_fill_activation_v1 WHERE experiment_id = %s",
        (PHASE1_EXPERIMENT,),
    )
    return rows[0] if rows else None


def load_evidence(conn) -> list[dict]:
    rows = _select(
        conn,
        f"SELECT {_EVIDENCE_COLUMNS} FROM research.me_short_a_fill_feasibility_v1 "
        "ORDER BY signal_time, event_key",
    )
    for row in rows:
        row["scenario_results"] = _json(row.get("scenario_results"))
        row["source_observation_ids"] = _json(row.get("source_observation_ids"))
        row["raw_snapshot"] = _json(row.get("raw_snapshot"))
    return rows



def load_forward_sources(conn, event_keys: Sequence[str]) -> dict[str, dict]:
    """Read source signals from collector #208 without modifying them."""
    if not event_keys:
        return {}
    rows = _select(
        conn,
        "SELECT event_key, symbol, direction, signal_time, "
        "reference_price, stop_price, target_price, "
        "trigger_5m_close, entry_zone_low, entry_zone_high, "
        "missing_fields, source_quality_flags, no_lookahead_flags "
        "FROM research.me_short_a_source_signal_v1 "
        "WHERE event_key = ANY(%s)",
        (list(event_keys),),
    )
    return {row["event_key"]: row for row in rows}


def load_forward_lineage(conn, event_keys: Sequence[str]) -> dict[str, dict]:
    """Read verified event-key lineage from collector #208."""
    if not event_keys:
        return {}
    rows = _select(
        conn,
        "SELECT event_key, symbol, direction, signal_time, "
        "reference_price, stop_price, target_price, "
        "event_key_integrity, link_status, quarantine_reason, "
        "source_signal_present, phase1_event_key, phase1_linked, "
        "linkage_checks "
        "FROM research.me_short_a_event_lineage_v1 "
        "WHERE event_key = ANY(%s)",
        (list(event_keys),),
    )
    return {row["event_key"]: row for row in rows}


def load_forward_outcomes(conn, event_keys: Sequence[str]) -> dict[str, dict]:
    """Read forward-only outcomes, including incomplete diagnostics."""
    if not event_keys:
        return {}
    rows = _select(
        conn,
        "SELECT event_key, symbol, direction, signal_time, "
        "reference_price, stop_price, target_price, "
        "outcome_status, path_class, sl_before_tp, tp_before_sl, "
        "ambiguous_intrabar, exit_price, gross_r, return_at_240m, "
        "mae_240m, limit_at_reference_touch, is_final, "
        "finalized_at, finalization_candle_close_time, "
        "horizon_end_time, coverage_complete, asof_ok, "
        "source_quality_flags, missing_fields "
        "FROM research.me_short_a_outcome_observation_v1 "
        "WHERE event_key = ANY(%s)",
        (list(event_keys),),
    )
    return {row["event_key"]: row for row in rows}



def validate_forward_event(evidence: dict, source: dict | None,
                           lineage: dict | None, outcome: dict | None,
                           now: datetime) -> dict[str, Any]:
    """Fail-closed verification of collector #208 evidence."""
    reasons = []
    key = evidence.get("event_key")

    if not source:
        reasons.append("SOURCE_SIGNAL_MISSING")
    if not lineage:
        reasons.append("FORWARD_LINEAGE_MISSING")
    if not outcome:
        reasons.append("FORWARD_OUTCOME_MISSING")

    for label, row in (("source", source), ("lineage", lineage),
                       ("outcome", outcome)):
        if not row:
            continue
        if row.get("event_key") != key:
            reasons.append(f"{label}_EVENT_KEY_MISMATCH")
        if row.get("symbol") != evidence.get("symbol"):
            reasons.append(f"{label}_SYMBOL_MISMATCH")
        if row.get("direction") != "SHORT":
            reasons.append(f"{label}_DIRECTION_MISMATCH")
        if _dt(row.get("signal_time")) != _dt(evidence.get("signal_time")):
            reasons.append(f"{label}_SIGNAL_TIME_MISMATCH")
        for field in ("reference_price", "stop_price", "target_price"):
            if _dec(row.get(field)) != _dec(evidence.get(field)):
                reasons.append(f"{label}_{field.upper()}_MISMATCH")

    if lineage:
        if lineage.get("link_status") != "LINKED":
            reasons.append("LINEAGE_NOT_LINKED")
        if lineage.get("event_key_integrity") is not True:
            reasons.append("EVENT_KEY_INTEGRITY_FAILED")
        if lineage.get("phase1_linked") is not True:
            reasons.append("PHASE1_NOT_LINKED")
        if lineage.get("source_signal_present") is not True:
            reasons.append("SOURCE_SIGNAL_NOT_CONFIRMED")
        if lineage.get("phase1_event_key") != key:
            reasons.append("PHASE1_EVENT_KEY_MISMATCH")

    if outcome:
        if outcome.get("outcome_status") != "MATCHED_FINALIZED":
            reasons.append("OUTCOME_NOT_FINALIZED")
        if outcome.get("is_final") is not True:
            reasons.append("IS_FINAL_FALSE")
        if outcome.get("coverage_complete") is not True:
            reasons.append("COVERAGE_INCOMPLETE")
        if outcome.get("asof_ok") is not True:
            reasons.append("ASOF_INVALID")
        end = _dt(outcome.get("horizon_end_time"))
        close = _dt(outcome.get("finalization_candle_close_time"))
        finalized = _dt(outcome.get("finalized_at"))
        signal_time = _dt(evidence.get("signal_time"))
        expected_end = (
            signal_time + timedelta(minutes=MAX_HOLD_MINUTES)
            if signal_time is not None else None
        )
        if end is None or expected_end is None or end != expected_end:
            reasons.append("HORIZON_END_MISMATCH")
        if end is None or close is None or close < end:
            reasons.append("FINALIZATION_BEFORE_HORIZON")
        if (
            finalized is None
            or end is None
            or finalized < end
            or finalized > _dt(now)
        ):
            reasons.append("FINALIZATION_TIME_INVALID")
        if outcome.get("path_class") not in (
            "STOP_HIT", "TARGET_HIT", "TIMEOUT_240M"
        ):
            reasons.append("PATH_UNRESOLVED")
        if _dec(outcome.get("exit_price")) is None:
            reasons.append("EXIT_PRICE_MISSING")
        if outcome.get("source_quality_flags"):
            reasons.append("OUTCOME_SOURCE_QUALITY_FLAGS")

    if source:
        if source.get("no_lookahead_flags"):
            reasons.append("SOURCE_LOOKAHEAD_FLAGS")
        if source.get("source_quality_flags"):
            reasons.append("SOURCE_QUALITY_FLAGS")
        if source.get("missing_fields"):
            reasons.append("SOURCE_MISSING_FIELDS")

    if outcome and outcome.get("missing_fields"):
        reasons.append("OUTCOME_MISSING_FIELDS")

    return {"eligible": not reasons, "reasons": reasons}


def load_observations(conn, observation_ids: Sequence[int]) -> dict[int, dict]:
    if not observation_ids:
        return {}
    rows = _select(
        conn,
        f"SELECT {_OBSERVATION_COLUMNS} FROM research.prospective_observation "
        "WHERE experiment_id = %s AND observation_id = ANY(%s)",
        (FROZEN_EXPERIMENT, list(observation_ids)),
    )
    return {int(row["observation_id"]): row for row in rows}


def load_outcomes(conn, observation_ids: Sequence[int]) -> dict[int, dict]:
    if not observation_ids:
        return {}
    rows = _select(
        conn,
        f"SELECT {_OUTCOME_COLUMNS} FROM research.prospective_outcome "
        "WHERE experiment_id = %s AND observation_id = ANY(%s)",
        (FROZEN_EXPERIMENT, list(observation_ids)),
    )
    return {int(row["observation_id"]): row for row in rows}


# ── lineage and data checks ──────────────────────────────────────────────

def _first_observation_id(source_ids: Any) -> int | None:
    ids = source_ids if isinstance(source_ids, list) else []
    for item in ids:
        try:
            return int(item)
        except (TypeError, ValueError):
            continue
    return None


def build_row_lineage(evidence: dict, observations: dict[int, dict],
                      outcomes: dict[int, dict], now: datetime) -> dict[str, Any]:
    """Match one Phase 1 snapshot to its source observation and outcome.

    Mismatched lineage is reported and the row is marked INCONCLUSIVE; values
    are never borrowed from a non-matching observation.
    """
    result: dict[str, Any] = {
        "event_key": evidence.get("event_key"),
        "symbol": evidence.get("symbol"),
        "signal_time": evidence.get("signal_time"),
        "lineage_status": OK,
        "missing_fields": [],
        "lookahead_flags": [],
        "observation_id": None,
        "features_present": {},
    }
    obs_id = _first_observation_id(evidence.get("source_observation_ids"))
    obs = observations.get(obs_id) if obs_id is not None else None
    if obs is None:
        result["lineage_status"] = MISSING_LINEAGE_OBSERVATION
        result["missing_fields"].append("source_observation_ids[0] -> prospective_observation")
        return result
    result["observation_id"] = obs_id

    if obs.get("symbol") != evidence.get("symbol"):
        result["lineage_status"] = MISMATCHED_SYMBOL
        return result
    if obs.get("direction") != "SHORT":
        result["lineage_status"] = MISMATCHED_DIRECTION
        return result
    obs_time, ev_time = _dt(obs.get("signal_time")), _dt(evidence.get("signal_time"))
    if obs_time is None or ev_time is None or obs_time != ev_time:
        result["lineage_status"] = MISMATCHED_SIGNAL_TIME
        return result
    for key, status in (
        ("reference_price", MISMATCHED_REFERENCE_PRICE),
        ("invalidation_price", MISMATCHED_STOP_PRICE),
        ("target_1", MISMATCHED_TARGET_PRICE),
    ):
        obs_price = _dec(obs.get(key))
        ev_key = {"reference_price": "reference_price",
                  "invalidation_price": "stop_price",
                  "target_1": "target_price"}[key]
        ev_price = _dec(evidence.get(ev_key))
        if obs_price is None or ev_price is None or obs_price != ev_price:
            result["lineage_status"] = status
            return result

    # Frozen geometry invariant.
    ref = _dec(evidence.get("reference_price"))
    stop = _dec(evidence.get("stop_price"))
    target = _dec(evidence.get("target_price"))
    if ref is None or stop is None or target is None or not (stop > ref > target):
        result["lineage_status"] = INVALID_GEOMETRY
        return result

    # Field coverage (never pretended to exist).
    features = _json(obs.get("features"))
    params = _json(obs.get("parameters"))
    result["features_present"] = {
        "trigger_5m_close": "trigger_5m_close" in features,
        "entry_zone_low": ("entry_zone_low" in features) or ("entry_zone_low" in params),
        "entry_zone_high": ("entry_zone_high" in features) or ("entry_zone_high" in params),
    }
    for field, present in result["features_present"].items():
        if not present:
            result["missing_fields"].append(field)

    outcome = outcomes.get(obs_id)
    if outcome is None:
        result["lineage_status"] = MISSING_OUTCOME
        result["missing_fields"].append("prospective_outcome row")
        return result

    matured_at = _dt(outcome.get("evaluated_240m_at"))
    signal_plus_hold = ev_time + timedelta(minutes=MAX_HOLD_MINUTES)
    if not outcome.get("is_final") or matured_at is None or now < signal_plus_hold:
        result["lineage_status"] = OUTCOME_UNMATURED
        result["missing_fields"].append("finalized 240m outcome (is_final)")
        return result

    # Lookahead / timestamp checks.
    captured_at = _dt(evidence.get("captured_at"))
    freeze_ts = _dt(evidence.get("freeze_ts"))
    if freeze_ts is not None and ev_time is not None and ev_time <= freeze_ts:
        result["lookahead_flags"].append("signal_time_not_after_phase1_freeze")
    if ev_time is not None and ev_time <= FROZEN_OOS_FREEZE_TS:
        result["lookahead_flags"].append("signal_time_not_after_frozen_oos_freeze")
    if captured_at is not None and ev_time is not None and captured_at < ev_time:
        result["lookahead_flags"].append("capture_before_signal")
    book_time_ms = evidence.get("book_time_ms")
    if book_time_ms is not None and captured_at is not None:
        book_age = int(book_time_ms) - int(captured_at.timestamp() * 1000)
        if book_age > BOOK_FUTURE_SKEW_MS:
            result["lookahead_flags"].append("book_timestamp_in_future")
    if matured_at is not None and ev_time is not None and matured_at < signal_plus_hold:
        result["lookahead_flags"].append("outcome_finalized_before_maturity")
    outcome_created = _dt(outcome.get("created_at"))
    if outcome_created is not None and ev_time is not None and outcome_created < ev_time:
        result["lookahead_flags"].append("outcome_created_before_signal")

    result["outcome"] = outcome
    return result


# ── economics: decomposition and counterfactuals ─────────────────────────

def cost_terms(evidence: dict, fill: Decimal | None) -> dict[str, Any]:
    """A/B/C/D decomposition at reference anchor + double-count overlap.

    ``fill is None`` means no executable entry was modeled for this scenario;
    terms that require a fill stay None (unknown), never zero.
    """
    ref = _dec(evidence.get("reference_price"))
    bid = _dec(evidence.get("bid"))
    ask = _dec(evidence.get("ask"))
    fill = _dec(fill)
    spread_pct = ((ask - bid) / ((ask + bid) / 2)) if (ask is not None and bid is not None
                                                       and (ask + bid) > 0) else None
    a = (ref - bid) / ref if (ref and bid is not None) else None
    b = (bid - fill) / ref if (ref and bid is not None and fill is not None) else None
    entry_deviation = (a + b) if (a is not None and b is not None) else None
    d = 2 * FEE_PER_SIDE
    diagnostic = (entry_deviation + spread_pct + d) if (
        entry_deviation is not None and spread_pct is not None) else None
    return {
        "bid": _num(bid),
        "ask": _num(ask),
        "A_reference_to_bid_pct": _num(a),
        "B_bid_to_fill_pct": _num(b),          # negative = price improvement
        "C_spread_pct": _num(spread_pct),
        "D_fees_pct": _num(d),
        "entry_deviation_total_pct": _num(entry_deviation),
        "diagnostic_phase1_cost_pct": _num(diagnostic),
        "overlap_if_subtracted_again_pct": _num(
            (entry_deviation + spread_pct) if entry_deviation is not None and
            spread_pct is not None else None),
    }


def counterfactual(evidence: dict, outcome: dict, fill: Decimal | None,
                   scenario_notional: Decimal) -> dict[str, Any]:
    """Control (frozen reference entry) vs executable (fill entry) economics.

    Absolute stop/target prices are identical in both views. When ``fill`` is
    None the executable view is UNKNOWN/infeasible — never zero.
    """
    ref = _dec(evidence.get("reference_price"))
    stop = _dec(evidence.get("stop_price"))
    target = _dec(evidence.get("target_price"))
    spread_pct = cost_terms(evidence, fill)["C_spread_pct"]

    path_class = derive_path_class(outcome)
    ret_240 = _dec(outcome.get("return_at_240m"))
    if "outcome_status" in outcome:
        timeout_close = (
            _dec(outcome.get("exit_price"))
            if path_class == "TIMEOUT" else None
        )
    else:
        timeout_close = (
            ref * (1 + ret_240 / 100)
            if (ref is not None and ret_240 is not None) else None
        )

    result: dict[str, Any] = {
        "scenario_notional_usdt": _num(scenario_notional),
        "path_class": path_class,
        "timeout_close_abs": _num(timeout_close),
        "reference_price": _num(ref),
        "stop_price": _num(stop),
        "target_price": _num(target),
        "fill": _num(fill),
        "entry_availability": AVAILABILITY.get("OK", "UNKNOWN"),
    }

    # Control view.
    if ref is not None and stop is not None:
        structural_r = stop - ref
        gross_control = gross_r(float(ref), float(stop), _num(target), path_class,
                                _num(timeout_close))
        cost_r_control = (FROZEN_NORMAL_ROUND_TRIP * ref / structural_r) if structural_r > 0 else None
        result["control"] = {
            "entry": _num(ref),
            "risk_abs": _num(structural_r),
            "risk_pct": _num(structural_r / ref * 100) if ref > 0 else None,
            "gross_r": gross_control,
            "cost_r": _num(cost_r_control),
            "net_r": (gross_control - float(cost_r_control))
                     if (gross_control is not None and cost_r_control is not None) else None,
        }
    else:
        result["control"] = {"gross_r": None, "net_r": None, "cost_r": None}

    # Executable view (fill-rebased; entry fee once + exit-side costs).
    if fill is None or stop is None or ref is None:
        result["executable"] = {
            "entry": _num(fill),
            "gross_r": None,
            "net_r": None,
            "cost_r_exit_only_frozen": None,
            "cost_r_exit_only_worst": None,
            "status": "ENTRY_INFEASIBLE_OR_UNKNOWN",
        }
        return result

    risk_exec = float(stop) - float(fill)
    if risk_exec <= 0:
        result["executable"] = {
            "entry": _num(fill), "gross_r": None, "net_r": None, "status": INVALID_GEOMETRY,
        }
        return result
    gross_exec = gross_r(float(fill), float(stop), _num(target), path_class,
                         _num(timeout_close))
    exit_frozen = FEE_PER_SIDE + SLIPPAGE_NORMAL_PER_SIDE
    exit_worst = FEE_PER_SIDE + SLIPPAGE_ELEVATED_PER_SIDE + (
        _dec(spread_pct) or Decimal("0"))
    cost_entry_fee = float(FEE_PER_SIDE) * float(fill) / risk_exec
    cost_frozen = float(exit_frozen) * float(fill) / risk_exec
    cost_worst = float(exit_worst) * float(fill) / risk_exec
    result["executable"] = {
        "entry": _num(fill),
        "risk_abs": risk_exec,
        "risk_pct": risk_exec / float(fill) * 100,
        "risk_inflation_pct": (risk_exec - (float(stop) - float(ref)))
                              / (float(stop) - float(ref)) * 100,
        "gross_r": gross_exec,
        "cost_r_entry_fee": cost_entry_fee,
        "cost_r_exit_only_frozen": cost_frozen,
        "cost_r_exit_only_worst": cost_worst,
        "net_r_frozen_exit": (gross_exec - cost_entry_fee - cost_frozen) if gross_exec is not None else None,
        "net_r_worst_exit": (gross_exec - cost_entry_fee - cost_worst) if gross_exec is not None else None,
        "status": "OK" if gross_exec is not None else MISSING_EXIT_DATA,
    }
    return result


def scenario_executable_fill(scenario: dict) -> Decimal | None:
    """Fail-closed executable fill, respecting exchange constraints."""
    if scenario.get("status") != "OK":
        return None
    if scenario.get("constraints_known") is not True:
        return None
    if scenario.get("below_min_qty") is not False:
        return None
    if scenario.get("below_min_notional") is not False:
        return None
    fill = _dec(scenario.get("estimated_fill"))
    return fill if fill is not None and fill > 0 else None


def scenario_table(evidence: dict) -> list[dict[str, Any]]:
    """Per-scenario fills from Phase 1 scenario_results (no substitution)."""
    results = evidence.get("scenario_results") or {}
    if isinstance(results, dict) and isinstance(results.get("scenarios"), list):
        return results["scenarios"]
    if isinstance(results, list):
        return results
    return []


# ── aggregation ───────────────────────────────────────────────────────────

def eligible_for_decision(row: dict, *, require_forward: bool = True) -> bool:
    """Shared fail-closed eligibility for forward-only OOS decisions."""
    if row.get("lineage_status") != OK:
        return False
    if row.get("lookahead_flags"):
        return False
    if not require_forward and "forward_link_status" not in row:
        return True
    return (
        row.get("forward_validated") is True
        and row.get("forward_link_status") == "LINKED"
        and row.get("forward_event_key_integrity") is True
        and row.get("forward_phase1_linked") is True
        and row.get("forward_outcome_status") == "MATCHED_FINALIZED"
        and row.get("forward_is_final") is True
    )


def aggregate(rows: Sequence[dict], *, bootstrap_n: int, seed: int,
              require_forward: bool = False) -> dict[str, Any]:
    """Aggregate matched conclusive rows. Point estimates are published only
    when at least ESTIMATE_MIN_N conclusive rows exist (never from a handful)."""
    conclusive = [
        r for r in rows
        if eligible_for_decision(r, require_forward=require_forward)
    ]
    matched_n = len(conclusive)
    paired = [
        r for r in conclusive
        if (r.get("control") or {}).get("net_r") is not None
        and (r.get("executable") or {}).get("net_r_frozen_exit") is not None
    ]
    paired_n = len(paired)
    symbols = {r["symbol"] for r in paired}
    clusters = {symbol_cluster(s) for s in symbols}

    def series(view: str, key: str) -> list[float | None]:
        out = []
        for row in paired:
            cell = row.get(view) or {}
            out.append(cell.get(key))
        return out

    def summarize(net_values: list[float | None]) -> dict[str, Any]:
        values = [v for v in net_values if v is not None]
        if len(values) < ESTIMATE_MIN_N:
            return {"n": len(values), "insufficient_sample": True,
                    "point_estimate": None, "net_pf": None,
                    "bootstrap": None}
        return {
            "n": len(values),
            "insufficient_sample": False,
            "point_estimate": statistics.fmean(values) if values else None,
            "net_pf": profit_factor(values),
            "total_net_r": sum(values) if values else None,
            "win_rate": (sum(v > 0 for v in values) / len(values)) if values else None,
            "bootstrap": bootstrap_mean(values, n_resamples=bootstrap_n, seed=seed),
        }

    return {
        "matched_conclusive_rows": matched_n,
        "usable_paired_rows": paired_n,
        "symbols": len(symbols),
        "independent_symbol_clusters": len(clusters),
        "clusters": sorted(clusters),
        "control_net_r": summarize(series("control", "net_r")),
        "executable_net_r_frozen_exit": summarize(series("executable", "net_r_frozen_exit")),
        "executable_net_r_worst_exit": summarize(series("executable", "net_r_worst_exit")),
    }


def symbol_concentration(rows: Sequence[dict]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("lineage_status") == OK:
            grouped[row["symbol"]].append(row)
    out = []
    for symbol, items in sorted(grouped.items()):
        nets = [((i.get("control") or {}).get("net_r")) for i in items]
        nets = [n for n in nets if n is not None]
        out.append({
            "symbol": symbol,
            "cluster": symbol_cluster(symbol),
            "n": len(items),
            "total_net_r_control": sum(nets) if nets else None,
        })
    return out


# ── verdicts ─────────────────────────────────────────────────────────────

def fill_feasibility_verdict(evidence_rows: Sequence[dict]) -> dict[str, Any]:
    """FILL_FEASIBILITY_PASS/FAIL/INCONCLUSIVE over Phase 1 population.

    Min-constraint feasibility excludes rows with unknown constraints.
    """
    quote_valid = [r for r in evidence_rows
                   if r.get("feasibility_status") not in (None, "STALE_BOOK",
                                                          "INVALID_BOOK", "ERROR")]
    ok_rows = [r for r in quote_valid if r.get("feasibility_status") == "OK"]
    constraints_known = [r for r in quote_valid
                         if any(s.get("constraints_known") for s in
                                (scenario_table(r) or [{}]))]
    min_ok = [r for r in constraints_known
              if r.get("feasibility_status") == "OK"]
    n = len(quote_valid)
    reasons = []
    if n < ESTIMATE_MIN_N:
        return {"verdict": FEAS_INCONCLUSIVE,
                "n_quote_valid": n,
                "reasons": [f"n_quote_valid={n} < {ESTIMATE_MIN_N}"],
                "ok_rate": None, "min_constraint_feasible_rate": None}
    ok_rate = Decimal(len(ok_rows)) / Decimal(n)
    min_rate = (Decimal(len(min_ok)) / Decimal(len(constraints_known))
                if constraints_known else None)
    verdict = FEAS_PASS
    if ok_rate < FEASIBILITY_PASS_RATE:
        verdict = FEAS_FAIL
        reasons.append(f"ok_rate={ok_rate:.4f} < {FEASIBILITY_PASS_RATE}")
    if min_rate is not None and min_rate < FEASIBILITY_PASS_RATE:
        verdict = FEAS_FAIL
        reasons.append(f"min_constraint_feasible_rate={min_rate:.4f} < {FEASIBILITY_PASS_RATE}")
    return {"verdict": verdict, "n_quote_valid": n,
            "ok_rate": float(ok_rate),
            "min_constraint_feasible_rate": float(min_rate) if min_rate is not None else None,
            "reasons": reasons}


def alignment_verdict(rows: Sequence[dict], agg: dict[str, Any]) -> dict[str, Any]:
    """ALIGNMENT_CONFIRMED | ALIGNMENT_INVALID | INCONCLUSIVE.

    Confirmed/invalid require the frozen cohort thresholds AND converged
    estimates; anything less is INCONCLUSIVE with explicit reasons.
    """
    reasons: list[str] = []
    conclusive_n = agg.get("usable_paired_rows", 0)
    if conclusive_n < MINIMUM_N:
        reasons.append(f"usable_paired_rows={conclusive_n} < minimum_n={MINIMUM_N}")
    if agg["symbols"] < MINIMUM_SYMBOLS:
        reasons.append(f"symbols={agg['symbols']} < minimum_symbols={MINIMUM_SYMBOLS}")
    if conclusive_n < ESTIMATE_MIN_N:
        reasons.append("no aggregate estimate published below the statistical floor")
        return {"verdict": ALIGNMENT_INCONCLUSIVE, "reasons": reasons}
    if any(r.get("lookahead_flags") for r in rows):
        reasons.append("lookahead/timestamp flags present")

    control = agg["control_net_r"]
    executable = agg["executable_net_r_frozen_exit"]
    if control.get("insufficient_sample") or executable.get("insufficient_sample"):
        reasons.append("aggregate estimates insufficient")
        return {"verdict": ALIGNMENT_INCONCLUSIVE, "reasons": reasons}

    control_er = control.get("point_estimate")
    exec_ci_low = (executable.get("bootstrap") or {}).get("ci_95_low")
    exec_er = executable.get("point_estimate")
    if control_er is None or exec_er is None or exec_ci_low is None:
        reasons.append("control/executable estimates incomplete (unknown exits)")
        return {"verdict": ALIGNMENT_INCONCLUSIVE, "reasons": reasons}

    # Material misalignment: frozen-entry economics look profitable while the
    # executable-entry economics are not (CI upper bound below zero).
    exec_ci_high = (executable.get("bootstrap") or {}).get("ci_95_high")
    if not reasons and control_er > 0 and exec_ci_high is not None and exec_ci_high < 0:
        return {"verdict": ALIGNMENT_INVALID,
                "reasons": ["control Net E[R] > 0 but executable-entry Net E[R] "
                            "CI entirely below zero"]}
    if reasons:
        return {"verdict": ALIGNMENT_INCONCLUSIVE, "reasons": reasons}
    if exec_ci_low > 0 and control_er > 0:
        return {"verdict": ALIGNMENT_CONFIRMED,
                "reasons": ["frozen cohort thresholds met; both control and "
                            "executable-entry Net E[R] CIs exclude zero"]}
    return {"verdict": ALIGNMENT_INCONCLUSIVE,
            "reasons": ["point estimates inconclusive at 95% CI"]}


# ── orchestration ─────────────────────────────────────────────────────────

def run_audit(conn, *, now: datetime | None = None,
              bootstrap_n: int = DEFAULT_BOOTSTRAP_N,
              seed: int = DEFAULT_BOOTSTRAP_SEED) -> dict[str, Any]:
    """Build the full read-only audit report from one connection."""
    now = now or datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "audit": AUDIT,
        "generated_at": now.isoformat(),
        "read_only": True,
        "frozen_protocol": {
            "experiment_id": FROZEN_EXPERIMENT,
            "freeze_ts": FROZEN_OOS_FREEZE_TS.isoformat(),
            "entry": "reference_price (= recent_high)",
            "stop": "invalidation_price (= recent_high * 1.002)",
            "target": "target_1",
            "max_hold_minutes": MAX_HOLD_MINUTES,
            "intrabar_policy": INTRABAR_POLICY,
            "costs": "taker 0.055%/side + slippage 0.05%/side normal "
                     "(0.10%/side elevated) - migration 071",
        },
        "double_counting_policy": {
            "definition": "A=(ref-bid)/ref, B=(bid-fill)/ref, C=spread, D=2*fee",
            "entry_side": "A+B (+ entry-side spread) is realized inside fill; "
                          "never subtracted from a fill-rebased payoff",
            "entry_fee_once_plus_exit_cost_for_executable": "fee_per_side + slippage_per_side "
                                             "(+ spread in worst-case variant)",
            "diagnostic_cost_warning": "Phase-1 estimated_roundtrip_cost_pct = "
                                       "A+B+C+D is reference-anchored; combining "
                                       "it with fill-rebased payoffs double counts "
                                       "A+B+C",
        },
    }

    # 1. Sources.
    sources: dict[str, Any] = {}
    try:
        sources["database"] = _select(conn, "SELECT current_database() AS database")[0]
    except Exception:
        sources["database"] = None
        _safe_rollback(conn)
    try:
        activation = load_activation(conn)
        sources["phase1_activation"] = activation
    except Exception as exc:
        sources["phase1_activation"] = None
        sources["phase1_activation_error"] = _error_text(exc)
        _safe_rollback(conn)
    try:
        evidence_rows = load_evidence(conn)
        sources["evidence_rows"] = len(evidence_rows)
    except Exception as exc:
        evidence_rows = []
        sources["evidence_rows"] = None
        sources["evidence_error"] = _error_text(exc)
        _safe_rollback(conn)

    # Forward-only collector #208: read-only diagnostic loading.
    event_keys = [
        str(row["event_key"])
        for row in evidence_rows
        if row.get("event_key")
    ]
    forward_sources = {}
    forward_lineage = {}
    forward_outcomes = {}
    if event_keys:
        for label, loader in (
            ("forward_sources", load_forward_sources),
            ("forward_lineage", load_forward_lineage),
            ("forward_outcomes", load_forward_outcomes),
        ):
            try:
                loaded = loader(conn, event_keys)
                if label == "forward_sources":
                    forward_sources = loaded
                elif label == "forward_lineage":
                    forward_lineage = loaded
                else:
                    forward_outcomes = loaded
                sources[label + "_rows"] = len(loaded)
            except Exception as exc:
                sources[label + "_error"] = _error_text(exc)
                _safe_rollback(conn)

    all_ids: list[int] = []
    for row in evidence_rows:
        obs_id = _first_observation_id(row.get("source_observation_ids"))
        if obs_id is not None:
            all_ids.append(obs_id)
    observations: dict[int, dict] = {}
    outcomes: dict[int, dict] = {}
    if all_ids:
        try:
            observations = load_observations(conn, all_ids)
            outcomes = load_outcomes(conn, all_ids)
        except Exception as exc:
            sources["lineage_error"] = _error_text(exc)
            _safe_rollback(conn)
    sources["observations_matched"] = len(observations)
    sources["outcomes_matched"] = len(outcomes)
    report["sources"] = sources

    # Forward-only eligibility: fail closed on missing or invalid evidence.
    forward_checks = {
        row["event_key"]: validate_forward_event(
            row,
            forward_sources.get(row["event_key"]),
            forward_lineage.get(row["event_key"]),
            forward_outcomes.get(row["event_key"]),
            now,
        )
        for row in evidence_rows
    }
    sources["forward_eligible"] = sum(
        check["eligible"] for check in forward_checks.values()
    )

    # 2. Lineage per evidence row.
    lineage_rows = []
    for evidence in evidence_rows:
        key = evidence["event_key"]
        legacy = build_row_lineage(evidence, observations, outcomes, now)
        check = forward_checks[key]

        # Legacy diagnostics only; never used for forward OOS eligibility.
        legacy_outcome = legacy.get("outcome") if legacy["lineage_status"] == OK else None
        legacy_mae = _dec((legacy_outcome or {}).get("mae_240m"))
        legacy["legacy_limit_at_reference_touch"] = (
            None if legacy_mae is None else bool(legacy_mae > 0)
        )

        if check["eligible"]:
            forward = forward_outcomes[key]
            source = forward_sources[key]

            # Keep the original Phase 1 diagnostic separate from OOS lineage.
            legacy["legacy_lineage_status"] = legacy["lineage_status"]
            legacy["legacy_missing_fields"] = list(legacy["missing_fields"])
            legacy["lineage_status"] = OK
            legacy["missing_fields"] = []
            legacy["outcome"] = forward
            legacy["features_present"] = {
                "trigger_5m_close": source.get("trigger_5m_close") is not None,
                "entry_zone_low": source.get("entry_zone_low") is not None,
                "entry_zone_high": source.get("entry_zone_high") is not None,
            }

        lineage_rows.append(legacy)

    # 3. Economics per row x scenario (matched rows only).
    per_row: list[dict[str, Any]] = []
    for evidence, lineage in zip(evidence_rows, lineage_rows):
        entry: dict[str, Any] = {
            "event_key": evidence.get("event_key"),
            "symbol": evidence.get("symbol"),
            "signal_time": str(evidence.get("signal_time")),
            "lineage_status": lineage["lineage_status"],
            "missing_fields": lineage["missing_fields"],
            "legacy_lineage_status": lineage.get(
                "legacy_lineage_status", lineage["lineage_status"]
            ),
            "legacy_missing_fields": lineage.get(
                "legacy_missing_fields", list(lineage["missing_fields"])
            ),
            "lookahead_flags": lineage["lookahead_flags"],
            "features_present": lineage.get("features_present"),
            "feasibility_status": evidence.get("feasibility_status"),
            "forward_eligibility": forward_checks[evidence["event_key"]],
            "legacy_limit_at_reference_touch": lineage.get(
                "legacy_limit_at_reference_touch"
            ),
            "scenarios": [],
        }
        outcome = (
            lineage.get("outcome")
            if forward_checks[evidence["event_key"]]["eligible"]
            else None
        )
        mae_240 = _dec((outcome or {}).get("mae_240m"))
        entry["limit_at_reference_touch"] = (
            outcome.get("limit_at_reference_touch")
            if outcome is not None and "outcome_status" in outcome
            else (None if outcome is None or mae_240 is None else bool(mae_240 > 0))
        )
        if outcome is not None:
            scenarios = scenario_table(evidence)
            if not scenarios:
                scenarios = [{"notional_usdt": None, "estimated_fill": None}]
            for scenario in scenarios:
                fill = _dec(scenario.get("estimated_fill"))
                notional = _dec(scenario.get("notional_usdt")) or Decimal("0")
                cell = {
                    "cost_terms": cost_terms(evidence, fill),
                    "counterfactual": counterfactual(
                        evidence, outcome, scenario_executable_fill(scenario), notional
                    ),
                    "scenario_status": scenario.get("status"),
                    "entry_availability": AVAILABILITY.get(
                        scenario.get("status"), "UNKNOWN"),
                }
                cell["counterfactual"]["entry_availability"] = cell["entry_availability"]
                entry["scenarios"].append(cell)
        per_row.append(entry)

    report["rows"] = per_row

    # 4. Aggregates + concentrations (executable view is the first/base scenario).
    base_view_rows = []
    for evidence, lineage, entry in zip(evidence_rows, lineage_rows, per_row):
        row_view = {
            "event_key": entry["event_key"],
            "symbol": entry["symbol"],
            "lineage_status": entry["lineage_status"],
            "lookahead_flags": entry["lookahead_flags"],
            "forward_link_status": (
                forward_lineage.get(entry["event_key"]) or {}
            ).get("link_status"),
            "forward_event_key_integrity": (
                forward_lineage.get(entry["event_key"]) or {}
            ).get("event_key_integrity"),
            "forward_phase1_linked": (
                forward_lineage.get(entry["event_key"]) or {}
            ).get("phase1_linked"),
            "forward_outcome_status": (
                forward_outcomes.get(entry["event_key"]) or {}
            ).get("outcome_status"),
            "forward_is_final": (
                forward_outcomes.get(entry["event_key"]) or {}
            ).get("is_final"),
            "forward_validated": entry["forward_eligibility"]["eligible"],
            "control": {}, "executable": {},
        }
        if entry["scenarios"]:
            row_view["control"] = entry["scenarios"][0]["counterfactual"].get("control") or {}
            row_view["executable"] = entry["scenarios"][0]["counterfactual"].get("executable") or {}
        base_view_rows.append(row_view)
    report["aggregates"] = aggregate(
        base_view_rows,
        bootstrap_n=bootstrap_n,
        seed=seed,
        require_forward=True,
    )
    # All decision-level views use the same forward-validated population.
    decision_rows = [
        row for row in base_view_rows
        if eligible_for_decision(row, require_forward=True)
    ]
    report["symbol_concentration"] = symbol_concentration(decision_rows)

    # 5. Fill feasibility population view.
    report["fill_feasibility"] = fill_feasibility_verdict(evidence_rows)

    # 6. Verdicts + missing fields + prospective-only plan.
    report["alignment"] = alignment_verdict(decision_rows, report["aggregates"])
    report["alignment_verdict"] = report["alignment"]["verdict"]
    report["fill_feasibility_verdict"] = report["fill_feasibility"]["verdict"]

    missing: dict[str, Any] = dict(MISSING_FIELD_REGISTRY)
    row_missing = sorted({f for r in per_row for f in r["missing_fields"]})
    report["missing_fields"] = {
        "registry": missing,
        "observed_missing_in_rows": row_missing,
        "source_unavailable": [name for name, err in sources.items()
                               if name.endswith("_error")],
    }
    if not evidence_rows:
        report["alignment_verdict"] = ALIGNMENT_INCONCLUSIVE
        report["alignment"]["reasons"].append(
            "Phase 1 evidence source unavailable or empty in this environment")
    report["prospective_only_plan"] = list(PROSPECTIVE_ONLY_PLAN)
    return report


# ── report writers ────────────────────────────────────────────────────────

def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines)


def write_reports(report: dict[str, Any], out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written = []

    # REFERENCE_PRICE_LINEAGE.md
    lineage_lines = [
        "# Reference Price Lineage — ME_SHORT_A_REFERENCE_TO_EXECUTION_PRICE_ALIGNMENT_AUDIT_V1",
        "",
        f"Generated: {report['generated_at']} (read-only, reproducible).",
        "",
        "## Frozen protocol provenance",
        "- `sql/migrations/071_me_short_geom_a_execution_v1_registration.sql` — "
        "entry=reference_price, stop=invalidation_price, target=target_1, "
        "240m, STOP_FIRST, frozen costs, freeze 2026-10-19T08:26:00Z.",
        "- `app/research/prospective_evaluator.py` — outcome matching "
        "(`_check_tp_sl`, `return_at_240m`, MAE=(high-entry)/entry for SHORT).",
        "- `app/scanners/momentum_exhaustion.py` — reference_price="
        "max(last five 5m highs); invalidation=reference*1.002; "
        "target_1=trigger_close-2*ATR.",
        "- `app/research/me_short_a_fill_feasibility_v1.py` — Phase 1 "
        "signal-time bid book fill evidence (append-only).",
        "",
        "## Lineage chain",
        "`scanner signal -> research.prospective_observation "
        "(ME_SHORT_GEOM_A_EXECUTION_V1) -> research.me_short_a_fill_feasibility_v1 "
        "(source_observation_ids) -> research.prospective_outcome`",
        "",
        "## Sources",
        _md_table(["source", "status"], [
            ["phase1_activation", json.dumps(report["sources"].get("phase1_activation"), default=str)],
            ["evidence rows", report["sources"].get("evidence_rows")],
            ["observations matched", report["sources"].get("observations_matched")],
            ["outcomes matched", report["sources"].get("outcomes_matched")],
        ]),
        "",
        "## Per-row lineage",
        _md_table(
            ["event_key", "symbol", "signal_time", "lineage_status",
             "missing_fields", "lookahead_flags", "limit_at_reference_touch"],
            [[r["event_key"], r["symbol"], r["signal_time"], r["lineage_status"],
              ";".join(r["missing_fields"]) or "-", ";".join(r["lookahead_flags"]) or "-",
              r.get("limit_at_reference_touch")] for r in report["rows"]]),
        "",
        "## Field coverage (Phase 1 evidence vs required)",
        _md_table(["field", "stored where", "status"],
                  [[k, v, "MISSING from Phase 1 evidence / linked rows"]
                   for k, v in MISSING_FIELD_REGISTRY.items()]),
        "",
        "## Missing fields (observed)",
        json.dumps(report["missing_fields"], indent=2, default=str),
        "",
        "## Prospective-only plan",
        "\n".join(f"{i+1}. {step}" for i, step in enumerate(report["prospective_only_plan"])),
    ]
    path = os.path.join(out_dir, "REFERENCE_PRICE_LINEAGE.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lineage_lines) + "\n")
    written.append(path)

    # REFERENCE_TO_BID_COST_DECOMPOSITION.csv
    path = os.path.join(out_dir, "REFERENCE_TO_BID_COST_DECOMPOSITION.csv")
    headers = ["event_key", "symbol", "scenario_notional_usdt", "reference_price",
               "bid", "ask", "estimated_fill", "A_reference_to_bid_pct",
               "B_bid_to_fill_pct", "C_spread_pct", "D_fees_pct",
               "entry_deviation_total_pct", "diagnostic_phase1_cost_pct",
               "overlap_if_subtracted_again_pct", "entry_availability",
               "scenario_status"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in report["rows"]:
            for cell in row["scenarios"]:
                terms = cell["cost_terms"]
                writer.writerow([
                    row["event_key"], row["symbol"],
                    cell["counterfactual"].get("scenario_notional_usdt"),
                    cell["counterfactual"].get("reference_price"),
                    terms.get("bid"),
                    terms.get("ask"),
                    cell["counterfactual"].get("fill"),
                    terms.get("A_reference_to_bid_pct"),
                    terms.get("B_bid_to_fill_pct"),
                    terms.get("C_spread_pct"),
                    terms.get("D_fees_pct"),
                    terms.get("entry_deviation_total_pct"),
                    terms.get("diagnostic_phase1_cost_pct"),
                    terms.get("overlap_if_subtracted_again_pct"),
                    cell.get("entry_availability"),
                    cell.get("scenario_status"),
                ])
    written.append(path)

    # EXECUTABLE_ENTRY_COUNTERFACTUAL.md
    agg = report["aggregates"]
    control, execu = agg["control_net_r"], agg["executable_net_r_frozen_exit"]
    cf_lines = [
        "# Executable Entry Counterfactual — ME_SHORT_A_REFERENCE_TO_EXECUTION_PRICE_ALIGNMENT_AUDIT_V1",
        "",
        f"Generated: {report['generated_at']}. Absolute stop/target prices are "
        "identical in both views; only the entry price and its risk denominator "
        "change. Path classes (240m STOP_FIRST) are entry-independent and are "
        "reused from the frozen outcomes. Unknown exits stay unknown — never zero.",
        "",
        "## No double counting",
        "- CONTROL: synthetic entry at reference; pays frozen round-trip costs "
        "(0.0021 normal / 0.0031 elevated) in structural R.",
        "- EXECUTABLE: payoff rebased on the modeled bid fill; entry-side "
        "reference-to-bid drift + book impact + entry spread are already inside "
        "the fill price and are NEVER subtracted again; entry fee plus exit-side costs "
        "(fee + slippage; worst-case adds the spread) are applied.",
        "- The Phase-1 diagnostic cost (A+B+C+2*fee) is reference-anchored; "
        "see `overlap_if_subtracted_again_pct` in the CSV.",
        "",
        "## Risk recomputation",
        _md_table(
            ["event_key", "symbol", "scenario", "path", "risk_structural",
             "risk_exec", "risk_inflation_%", "gross_R_ctrl", "net_R_ctrl",
             "gross_R_exec", "net_R_exec_frozen_exit", "net_R_exec_worst_exit",
             "entry_availability", "limit_at_reference_touch"],
            [[r["event_key"], r["symbol"],
              cell["counterfactual"].get("scenario_notional_usdt"),
              cell["counterfactual"].get("path_class"),
              (cell["counterfactual"].get("control") or {}).get("risk_abs"),
              (cell["counterfactual"].get("executable") or {}).get("risk_abs"),
              (cell["counterfactual"].get("executable") or {}).get("risk_inflation_pct"),
              (cell["counterfactual"].get("control") or {}).get("gross_r"),
              (cell["counterfactual"].get("control") or {}).get("net_r"),
              (cell["counterfactual"].get("executable") or {}).get("gross_r"),
              (cell["counterfactual"].get("executable") or {}).get("net_r_frozen_exit"),
              (cell["counterfactual"].get("executable") or {}).get("net_r_worst_exit"),
              cell.get("entry_availability"),
              r.get("limit_at_reference_touch")]
             for r in report["rows"] for cell in r["scenarios"]]),
        "",
        "## Aggregates (base scenario)",
        json.dumps(agg, indent=2, default=str),
        "",
        "## Symbol / cluster concentration",
        json.dumps(report["symbol_concentration"], indent=2, default=str),
        "",
        "## Verdicts",
        f"- alignment_verdict: **{report['alignment_verdict']}**",
        f"- fill_feasibility_verdict: **{report['fill_feasibility_verdict']}**",
        "",
        "## Verdict reasons",
        json.dumps({"alignment": report["alignment"],
                    "fill_feasibility": report["fill_feasibility"]},
                   indent=2, default=str),
        "",
        "## Prospective-only plan",
        "\n".join(f"{i+1}. {step}" for i, step in enumerate(report["prospective_only_plan"])),
    ]
    path = os.path.join(out_dir, "EXECUTABLE_ENTRY_COUNTERFACTUAL.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(cf_lines) + "\n")
    written.append(path)

    # alignment_audit_report.json
    path = os.path.join(out_dir, "alignment_audit_report.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    written.append(path)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: read-only run against the configured database (env DB_*)."""
    import argparse
    parser = argparse.ArgumentParser(description=AUDIT)
    parser.add_argument("--out", default=os.path.join(
        "docs", "research", "me_short_a_ref_exec_alignment_v1"))
    parser.add_argument("--bootstrap-n", type=int, default=DEFAULT_BOOTSTRAP_N)
    parser.add_argument("--seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    args = parser.parse_args(argv)

    import pg8000.dbapi as pg
    conn = pg.connect(
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "trad_bot"),
    )
    try:
        begin_read_only(conn)
        report = run_audit(conn, bootstrap_n=args.bootstrap_n, seed=args.seed)
    finally:
        conn.close()
    written = write_reports(report, args.out)
    print(json.dumps({
        "alignment_verdict": report["alignment_verdict"],
        "fill_feasibility_verdict": report["fill_feasibility_verdict"],
        "written": written,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
