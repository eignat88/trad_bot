"""Issue #305 — read-only upstream source coverage reconciler.

Reconciles the independent upstream capture ledger against the V3 producer
ledger:

    research_me_upstream_coverage.source_event
        ↔  dds.me_long_v2_inverse_short_v3_event

matching on the NORMALIZED original signal identity
(``<scanner>|<symbol>|<direction>|<entry_timeframe>|<candle_ms>``) — the V3
``event_key`` is NEVER used as the upstream identity.

What the audit proves — and what it does NOT (requirement 4.5):

* it proves completeness of the CAPTURE of the detector-level output of
  ``scanner.scan(ctx)`` for the audited window;
* it does NOT prove the correctness of the detector itself, nor the
  completeness of market data feeding it.

Verdict vocabulary (deterministic, never optimistic):

===========================  ===============================================
``COVERAGE_PASS``            sufficient evidence: non-empty confirmed
                             activity window, >=1 matched pair, zero
                             conflicts / gaps / lags / capture errors
``COVERAGE_LAG``             unmatched events still inside the grace window
                             (eventual consistency — not yet a loss)
``COVERAGE_GAP``             confirmed missing events after the grace window
``SOURCE_COVERAGE_UNKNOWN``  coverage could not be proven: no frozen
                             activation, unknown activity period, capture
                             errors, or insufficient evidence
``INTEGRITY_CONFLICT``       immutable-lineage conflict, duplicate identity
                             in the V3 ledger, non-pre-freeze rejected
                             events, or a BLOCKED capture state
===========================  ===============================================

Precedence: INTEGRITY_CONFLICT > COVERAGE_GAP > SOURCE_COVERAGE_UNKNOWN >
COVERAGE_LAG > COVERAGE_PASS.  A PASS is impossible while any doubt exists.

Scope discipline: only the INTERSECTION of the confirmed activity periods of
both sources is audited; an absence of events is never called an error
before activation, delivery latency and source state are accounted for.

The function :func:`reconcile_coverage` is READ-ONLY (SELECT statements
only); persisting an audit row to ``coverage_audit`` is an explicit separate
call (:func:`save_coverage_audit`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .contracts import as_utc, comparable_lineage_view
from .identity import normalized_identity_values
from .protocol import EXPERIMENT_ID
from .store import (
    ACTIVATION_TABLE,
    HEALTH_TABLE,
    REJECTED_TABLE,
    STATE_TABLE,
    EVENT_TABLE,
    STATE_BLOCKED,
)

logger = logging.getLogger(__name__)

# Reconciliation target (READ-ONLY): the V3 producer ledger.
V3_EVENT_TABLE = "dds.me_long_v2_inverse_short_v3_event"

# Verdict statuses.
COVERAGE_PASS = "COVERAGE_PASS"
COVERAGE_LAG = "COVERAGE_LAG"
COVERAGE_GAP = "COVERAGE_GAP"
SOURCE_COVERAGE_UNKNOWN = "SOURCE_COVERAGE_UNKNOWN"
INTEGRITY_CONFLICT = "INTEGRITY_CONFLICT"

#: Deterministic verdict reasons.
REASON_SUFFICIENT_EVIDENCE = "SUFFICIENT_EVIDENCE"
REASON_WITHIN_GRACE = "UNMATCHED_WITHIN_GRACE_WINDOW"
REASON_CONFIRMED_GAP = "CONFIRMED_MISSING_AFTER_GRACE_WINDOW"
REASON_NOT_ACTIVATED = "NOT_ACTIVATED"
REASON_MISSING_FREEZE_TS = "MISSING_FREEZE_TS"
REASON_NO_UPSTREAM_ACTIVITY = "NO_UPSTREAM_ACTIVITY"
REASON_NO_V3_ACTIVITY = "NO_V3_ACTIVITY"
REASON_EMPTY_WINDOW = "EMPTY_ACTIVITY_WINDOW"
REASON_CAPTURE_ERRORS = "CAPTURE_ERRORS_COVERAGE_UNPROVEN"
REASON_NO_MATCHED_EVENTS = "NO_MATCHED_EVENTS_INSUFFICIENT_EVIDENCE"
REASON_LINEAGE_MISMATCH = "LINEAGE_MISMATCH"
REASON_V3_DUPLICATE_IDENTITY = "V3_DUPLICATE_IDENTITY"
REASON_REJECTED_EVENTS = "REJECTED_SOURCE_EVENTS"
REASON_CAPTURE_BLOCKED = "CAPTURE_STATE_BLOCKED"

#: An unmatched event younger than this is eventual-consistency lag, not a
#: coverage gap.  Mirrors the #296 grace semantics.
DEFAULT_GRACE_SECONDS = 900

#: The documented limitation of this verdict — attached to every result.
COVERAGE_SCOPE_NOTE = (
    "COVERAGE_PASS proves completeness of the CAPTURE of detector-level "
    "scanner output in the audited window only; it does NOT prove detector "
    "correctness or market-data completeness."
)

# Rejection reasons that are EXPECTED refusals (never integrity evidence).
EXPECTED_REJECTION_REASONS = frozenset({"PRE_FREEZE"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pure reconciliation core (fully unit-testable without a database)
# ---------------------------------------------------------------------------

def _normalize_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    side: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        if side == "v3" and "source_identity" not in entry:
            entry["source_identity"] = normalized_identity_values(
                scanner=str(entry.get("source_scanner") or ""),
                symbol=str(entry.get("symbol") or ""),
                direction=str(entry.get("source_direction") or ""),
                entry_timeframe=str(entry.get("entry_timeframe") or "5m"),
                signal_candle_open_ms=int(entry.get("signal_candle_open_time") or 0),
            )
        entry["signal_time"] = as_utc(entry["signal_time"])
        entry["ledger_time"] = as_utc(
            entry.get("captured_at")
            or entry.get("created_at")
            or entry["signal_time"]
        )
        normalized.append(entry)
    return normalized


def _lineage_differs(
    upstream_row: Mapping[str, Any],
    v3_row: Mapping[str, Any],
) -> bool:
    """Compare the frozen comparable (ORIGINAL-fact) lineage subset."""
    upstream_view = comparable_lineage_view(upstream_row.get("lineage") or {})
    v3_view = comparable_lineage_view(v3_row.get("lineage") or {})
    for key in sorted(set(upstream_view) | set(v3_view)):
        if not _values_equal(upstream_view.get(key), v3_view.get(key)):
            return True
    return False


def _values_equal(left: Any, right: Any) -> bool:
    """Structural equality with numeric tolerance (int/float interchangeable)."""
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if isinstance(left, bool) or isinstance(right, bool):
            return left is right
        return float(left) == float(right)
    if isinstance(left, dict) and isinstance(right, dict):
        return (
            set(left) == set(right)
            and all(_values_equal(left[k], right[k]) for k in left)
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _values_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def match_ledgers(
    upstream_rows: Sequence[Mapping[str, Any]],
    v3_rows: Sequence[Mapping[str, Any]],
    *,
    window_start: datetime,
    as_of: datetime,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
) -> dict[str, Any]:
    """Match both ledgers on normalized identity (pure, read-only)."""
    start = as_utc(window_start)
    now = as_utc(as_of)
    stale_cutoff = now - timedelta(seconds=grace_seconds)

    upstream_all = _normalize_rows(upstream_rows, side="upstream")
    v3_all = _normalize_rows(v3_rows, side="v3")

    # Scope: only the intersection of confirmed activity periods.
    upstream = [r for r in upstream_all if r["ledger_time"] >= start]
    v3 = [r for r in v3_all if r["ledger_time"] >= start]
    outside_upstream = len(upstream_all) - len(upstream)
    outside_v3 = len(v3_all) - len(v3)

    upstream_by_identity = {r["source_identity"]: r for r in upstream}
    v3_by_identity: dict[str, list[dict[str, Any]]] = {}
    for row in v3:
        v3_by_identity.setdefault(row["source_identity"], []).append(row)

    matched = 0
    lineage_mismatches = 0
    v3_duplicate_identities = 0
    upstream_only_lag = 0
    upstream_only_gap = 0
    v3_only_lag = 0
    v3_only_gap = 0

    for identity, up_row in upstream_by_identity.items():
        counterparts = v3_by_identity.get(identity) or []
        if not counterparts:
            if up_row["ledger_time"] <= stale_cutoff:
                upstream_only_gap += 1
            else:
                upstream_only_lag += 1
            continue
        matched += 1
        if len(counterparts) > 1:
            v3_duplicate_identities += 1
        if _lineage_differs(up_row, counterparts[0]):
            lineage_mismatches += 1

    for identity, rows in v3_by_identity.items():
        if identity in upstream_by_identity:
            continue
        if len(rows) > 1:
            v3_duplicate_identities += 1
        oldest = min(r["ledger_time"] for r in rows)
        if oldest <= stale_cutoff:
            v3_only_gap += len(rows)
        else:
            v3_only_lag += len(rows)

    latencies = [
        (as_utc(r["captured_at"]) - r["signal_time"]).total_seconds()
        for r in upstream_all
        if r.get("captured_at") is not None
    ]

    return {
        "upstream_events": len(upstream_all),
        "v3_events": len(v3_all),
        "matched": matched,
        "upstream_only_lag": upstream_only_lag,
        "upstream_only_gap": upstream_only_gap,
        "v3_only_lag": v3_only_lag,
        "v3_only_gap": v3_only_gap,
        "upstream_only": upstream_only_lag + upstream_only_gap,
        "v3_only": v3_only_lag + v3_only_gap,
        "lineage_mismatches": lineage_mismatches,
        "v3_duplicate_identities": v3_duplicate_identities,
        "outside_window_upstream": outside_upstream,
        "outside_window_v3": outside_v3,
        "max_delivery_latency_seconds": max(latencies) if latencies else None,
        "avg_delivery_latency_seconds": (
            sum(latencies) / len(latencies) if latencies else None
        ),
    }


def evaluate_coverage(
    *,
    activation_present: bool,
    freeze_present: bool,
    capture_state: str | None,
    counts: Mapping[str, Any],
) -> tuple[str, str]:
    """Deterministic verdict from reconciliation counts (pure)."""
    if not activation_present:
        return SOURCE_COVERAGE_UNKNOWN, REASON_NOT_ACTIVATED
    if not freeze_present:
        return SOURCE_COVERAGE_UNKNOWN, REASON_MISSING_FREEZE_TS

    if capture_state == STATE_BLOCKED:
        return INTEGRITY_CONFLICT, REASON_CAPTURE_BLOCKED

    if int(counts.get("lineage_mismatches", 0)) > 0:
        return INTEGRITY_CONFLICT, REASON_LINEAGE_MISMATCH
    if int(counts.get("v3_duplicate_identities", 0)) > 0:
        return INTEGRITY_CONFLICT, REASON_V3_DUPLICATE_IDENTITY
    if int(counts.get("rejected_events", 0)) > 0:
        return INTEGRITY_CONFLICT, REASON_REJECTED_EVENTS

    if not counts.get("upstream_activity_known", False):
        return SOURCE_COVERAGE_UNKNOWN, REASON_NO_UPSTREAM_ACTIVITY
    if not counts.get("v3_activity_known", False):
        return SOURCE_COVERAGE_UNKNOWN, REASON_NO_V3_ACTIVITY
    if not counts.get("window_non_empty", False):
        return SOURCE_COVERAGE_UNKNOWN, REASON_EMPTY_WINDOW

    if (
        int(counts.get("upstream_only_gap", 0)) > 0
        or int(counts.get("v3_only_gap", 0)) > 0
    ):
        return COVERAGE_GAP, REASON_CONFIRMED_GAP

    if int(counts.get("capture_errors", 0)) > 0:
        return SOURCE_COVERAGE_UNKNOWN, REASON_CAPTURE_ERRORS

    if (
        int(counts.get("upstream_only_lag", 0)) > 0
        or int(counts.get("v3_only_lag", 0)) > 0
    ):
        return COVERAGE_LAG, REASON_WITHIN_GRACE

    if int(counts.get("matched", 0)) >= 1:
        return COVERAGE_PASS, REASON_SUFFICIENT_EVIDENCE

    return SOURCE_COVERAGE_UNKNOWN, REASON_NO_MATCHED_EVENTS


# ---------------------------------------------------------------------------
# Read-only SQL reconciliation
# ---------------------------------------------------------------------------

def reconcile_coverage(
    connection: Any,
    *,
    as_of: datetime | None = None,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
) -> dict[str, Any]:
    """Read-only upstream ↔ V3 reconciliation (SELECT statements only)."""
    if type(grace_seconds) is not int or grace_seconds < 0:
        raise ValueError("INVALID_GRACE_SECONDS")

    now = as_utc(as_of) if as_of is not None else _utc_now()

    cur = connection.cursor()
    cur.execute(
        f"""
        SELECT status, freeze_ts
        FROM {ACTIVATION_TABLE}
        WHERE experiment_id = %s
        """,
        (EXPERIMENT_ID,),
    )
    activation_row = cur.fetchone()

    activation_present = activation_row is not None
    freeze_ts = None
    if activation_present and activation_row[1] is not None:
        freeze_ts = as_utc(activation_row[1])
    freeze_present = freeze_ts is not None

    if not activation_present or not freeze_present:
        result = _empty_result(
            now=now,
            grace_seconds=grace_seconds,
            activation_present=activation_present,
            freeze_present=freeze_present,
            capture_state=None,
        )
        result["status"], result["reason"] = evaluate_coverage(
            activation_present=activation_present,
            freeze_present=freeze_present,
            capture_state=None,
            counts={},
        )
        return result

    # Latest capture contour state (append-only ledger).
    cur.execute(
        f"""
        SELECT state FROM {STATE_TABLE}
        ORDER BY id DESC LIMIT 1
        """
    )
    state_row = cur.fetchone()
    capture_state = state_row[0] if state_row else None

    # Upstream ledger rows (this capture's own schema).
    cur.execute(
        f"""
        SELECT source_event_key, source_identity, symbol, signal_time,
               captured_at, created_at, lineage
        FROM {EVENT_TABLE}
        WHERE signal_time > %s
        """,
        (freeze_ts,),
    )
    upstream_rows = [
        {
            "source_event_key": r[0],
            "source_identity": r[1],
            "symbol": r[2],
            "signal_time": r[3],
            "captured_at": r[4],
            "created_at": r[5],
            "lineage": r[6],
        }
        for r in cur.fetchall()
    ]

    # V3 producer ledger (READ-ONLY).
    cur.execute(
        f"""
        SELECT event_key, symbol, signal_time, captured_at, created_at,
               source_scanner, source_direction, entry_timeframe,
               signal_candle_open_time, lineage
        FROM {V3_EVENT_TABLE}
        WHERE signal_time > %s
        """,
        (freeze_ts,),
    )
    v3_rows = [
        {
            "event_key": r[0],
            "symbol": r[1],
            "signal_time": r[2],
            "captured_at": r[3],
            "created_at": r[4],
            "source_scanner": r[5],
            "source_direction": r[6],
            "entry_timeframe": r[7],
            "signal_candle_open_time": r[8],
            "lineage": r[9],
        }
        for r in cur.fetchall()
    ]

    # Confirmed activity periods (first committed ledger timestamp each).
    upstream_activity_start = min(
        (as_utc(r["captured_at"] or r["created_at"]) for r in upstream_rows),
        default=None,
    )
    upstream_health_start = _first_health_time(cur)
    if upstream_activity_start is None:
        upstream_activity_start = upstream_health_start
    v3_activity_start = min(
        (as_utc(r["created_at"] or r["captured_at"]) for r in v3_rows),
        default=None,
    )

    upstream_activity_known = upstream_activity_start is not None
    v3_activity_known = v3_activity_start is not None

    if upstream_activity_known and v3_activity_known:
        window_start = max(freeze_ts, upstream_activity_start, v3_activity_start)
    elif upstream_activity_known:
        window_start = max(freeze_ts, upstream_activity_start)
    elif v3_activity_known:
        window_start = max(freeze_ts, v3_activity_start)
    else:
        window_start = freeze_ts

    counts = match_ledgers(
        upstream_rows,
        v3_rows,
        window_start=window_start,
        as_of=now,
        grace_seconds=grace_seconds,
    )

    # Delivery / error telemetry for the window (this capture's ledgers).
    cur.execute(
        f"""
        SELECT
            COALESCE(SUM(duplicates), 0),
            COALESCE(SUM(errors), 0)
        FROM {HEALTH_TABLE}
        WHERE recorded_at >= %s AND recorded_at <= %s
        """,
        (window_start, now),
    )
    health_row = cur.fetchone()
    duplicate_deliveries = int(health_row[0] or 0)
    capture_errors = int(health_row[1] or 0)

    cur.execute(
        f"""
        SELECT reason_code FROM {REJECTED_TABLE}
        WHERE recorded_at >= %s AND recorded_at <= %s
        """,
        (window_start, now),
    )
    # Expected refusals (PRE_FREEZE) are not integrity evidence; everything
    # else is a source event that could not enter the eligible cohort.
    rejected_events = sum(
        1
        for (reason_code,) in cur.fetchall()
        if reason_code not in EXPECTED_REJECTION_REASONS
    )

    counts.update({
        "upstream_activity_known": upstream_activity_known,
        "v3_activity_known": v3_activity_known,
        "window_non_empty": window_start < now,
        "window_start": window_start,
        "window_end": now,
        "window_seconds": (now - window_start).total_seconds(),
        "duplicate_deliveries": duplicate_deliveries,
        "capture_errors": capture_errors,
        "rejected_events": rejected_events,
        "upstream_activity_start": upstream_activity_start,
        "v3_activity_start": v3_activity_start,
    })

    status, reason = evaluate_coverage(
        activation_present=True,
        freeze_present=True,
        capture_state=capture_state,
        counts=counts,
    )

    result = {
        "experiment_id": EXPERIMENT_ID,
        "status": status,
        "reason": reason,
        "as_of": now,
        "grace_seconds": grace_seconds,
        "capture_state": capture_state,
        "scope_note": COVERAGE_SCOPE_NOTE,
    }
    result.update(counts)
    return result


def _first_health_time(cur: Any) -> datetime | None:
    cur.execute(
        f"SELECT MIN(recorded_at) FROM {HEALTH_TABLE}"
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        return as_utc(row[0])
    return None


def _empty_result(
    *,
    now: datetime,
    grace_seconds: int,
    activation_present: bool,
    freeze_present: bool,
    capture_state: str | None,
) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "reason": "",
        "as_of": now,
        "grace_seconds": grace_seconds,
        "capture_state": capture_state,
        "scope_note": COVERAGE_SCOPE_NOTE,
        "upstream_events": 0,
        "v3_events": 0,
        "matched": 0,
        "upstream_only": 0,
        "upstream_only_lag": 0,
        "upstream_only_gap": 0,
        "v3_only": 0,
        "v3_only_lag": 0,
        "v3_only_gap": 0,
        "lineage_mismatches": 0,
        "v3_duplicate_identities": 0,
        "duplicate_deliveries": 0,
        "capture_errors": 0,
        "rejected_events": 0,
        "outside_window_upstream": 0,
        "outside_window_v3": 0,
        "max_delivery_latency_seconds": None,
        "avg_delivery_latency_seconds": None,
        "upstream_activity_known": activation_present and freeze_present,
        "v3_activity_known": activation_present and freeze_present,
        "window_non_empty": False,
        "window_start": None,
        "window_end": now,
        "window_seconds": 0.0,
    }


# ---------------------------------------------------------------------------
# Explicit audit persistence (never part of the read-only reconciliation)
# ---------------------------------------------------------------------------

def save_coverage_audit(connection: Any, result: Mapping[str, Any]) -> None:
    """Persist ONE audit result row (explicit, separate from the read path)."""
    import json

    cur = connection.cursor()
    cur.execute(
        f"""
        INSERT INTO research_me_upstream_coverage.coverage_audit (
            experiment_id, as_of, window_start, window_end,
            upstream_events, v3_events, matched,
            upstream_only_lag, upstream_only_gap,
            v3_only_lag, v3_only_gap,
            duplicate_deliveries, v3_duplicate_identities,
            lineage_mismatches, capture_errors, rejected_events,
            max_delivery_latency_seconds, status, reason, diagnostics
        )
        VALUES (
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s::jsonb
        )
        """,
        (
            EXPERIMENT_ID,
            result.get("as_of"),
            result.get("window_start"),
            result.get("window_end"),
            int(result.get("upstream_events", 0)),
            int(result.get("v3_events", 0)),
            int(result.get("matched", 0)),
            int(result.get("upstream_only_lag", 0)),
            int(result.get("upstream_only_gap", 0)),
            int(result.get("v3_only_lag", 0)),
            int(result.get("v3_only_gap", 0)),
            int(result.get("duplicate_deliveries", 0)),
            int(result.get("v3_duplicate_identities", 0)),
            int(result.get("lineage_mismatches", 0)),
            int(result.get("capture_errors", 0)),
            int(result.get("rejected_events", 0)),
            result.get("max_delivery_latency_seconds"),
            result.get("status"),
            result.get("reason"),
            json.dumps(
                {k: str(v) for k, v in result.items()}, allow_nan=False,
            ),
        ),
    )
    connection.commit()
