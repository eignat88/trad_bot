"""Issue #296: prospective source coverage and integrity audit.

Read-only. Never repairs, backfills or modifies source events.

Coverage semantics (PR #303 review remediation):

* ``SOURCE_COVERAGE_VALIDATED`` — reconciliation with the AVAILABLE V3
  ledger passed (scope: ``V3_LEDGER_COVERAGE_VALIDATED`` only);
* ``COVERAGE_LAG`` — unprocessed eligible events exist but are still
  inside the eventual-consistency grace window (transient, non-blocking:
  a batch that has not completed yet is not a permanent loss);
* ``COVERAGE_GAP`` — unprocessed eligible events persist beyond the
  grace window (escalated by the observer);
* ``INTEGRITY_CONFLICT`` — a source/ledger integrity violation exists;
* ``SOURCE_COVERAGE_UNKNOWN`` — coverage could not be established at all
  (no frozen activation), and — always — the independent completeness of
  the upstream capture is not proven (``UPSTREAM_SOURCE_COVERAGE_UNPROVEN``).

Source-independence limitation (Issue #296 / PR #303 review):

This observer CONSUMES the V3 producer ledger
(``dds.me_long_v2_inverse_short_v3_event``). It is an independent
consumer but NOT a scanner-level source capture. A match between the V3
ledger and the research ledger therefore NEVER upgrades to a full
``SOURCE_VALIDATED`` claim — see :func:`verdict_gate`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

try:  # pragma: no cover - driver present in deployment, optional in unit tests
    from psycopg2.extras import RealDictCursor
except ImportError:  # noqa: F401
    RealDictCursor = None

from .contracts import EXPERIMENT_ID, as_utc


# Audit outcome statuses.
AUDIT_SOURCE_COVERAGE_VALIDATED = "SOURCE_COVERAGE_VALIDATED"
AUDIT_COVERAGE_LAG = "COVERAGE_LAG"
AUDIT_COVERAGE_GAP = "COVERAGE_GAP"
AUDIT_INTEGRITY_CONFLICT = "INTEGRITY_CONFLICT"
AUDIT_SOURCE_COVERAGE_UNKNOWN = "SOURCE_COVERAGE_UNKNOWN"

# Two-dimension coverage vocabulary (P2: keep them separate).
LEDGER_COVERAGE_VALIDATED = "V3_LEDGER_COVERAGE_VALIDATED"
LEDGER_COVERAGE_GAP = "V3_LEDGER_COVERAGE_GAP"
LEDGER_COVERAGE_CONFLICT = "V3_LEDGER_INTEGRITY_CONFLICT"
LEDGER_COVERAGE_UNKNOWN = "V3_LEDGER_COVERAGE_UNKNOWN"
UPSTREAM_SOURCE_COVERAGE_UNPROVEN = "UPSTREAM_SOURCE_COVERAGE_UNPROVEN"

#: An unprocessed event younger than this is eventual-consistency lag,
#: not a coverage gap. Events older than this are persistently missing.
DEFAULT_GAP_GRACE_SECONDS = 900

#: Deterministic audit reason codes.
REASON_NOT_ACTIVATED = "NOT_ACTIVATED"
REASON_MISSING_FREEZE_TS = "MISSING_FREEZE_TS"
REASON_AUDIT_CLEAN = "AUDIT_CLEAN"
REASON_COVERAGE_LAG = "COVERAGE_LAG"
REASON_COVERAGE_GAP = "COVERAGE_GAP"
REASON_INTEGRITY_CONFLICT = "INTEGRITY_CONFLICT"
REASON_UPSTREAM_UNPROVEN = "UPSTREAM_SOURCE_COVERAGE_UNPROVEN"

#: Categories that make up the integrity-conflict total. A source event
#: can contribute to more than one category; the per-category counters
#: are the diagnostic truth.
CONFLICT_CATEGORIES = (
    "accepted_without_source",
    "rejected_in_cohort",
    "lineage_mismatches",
    "orphan_research_events",
    "ledger_experiment_mismatches",
)


def _audit_row(**overrides: Any) -> dict:
    row = {
        "status": AUDIT_SOURCE_COVERAGE_UNKNOWN,
        "reason": REASON_UPSTREAM_UNPROVEN,
        "ledger_coverage": LEDGER_COVERAGE_UNKNOWN,
        "upstream_source_coverage": UPSTREAM_SOURCE_COVERAGE_UNPROVEN,
        "full_source_validated": False,
        "eligible_source": 0,
        "unprocessed": 0,
        "unprocessed_stale": 0,
        "accepted": 0,
        "rejected": 0,
        "accepted_without_source": 0,
        "rejected_in_cohort": 0,
        "lineage_mismatches": 0,
        "orphan_research_events": 0,
        "ledger_experiment_mismatches": 0,
        "integrity_conflicts": 0,
    }
    row.update(overrides)
    return row


def audit_source_integrity(
    connection: Any,
    *,
    gap_grace_seconds: int = DEFAULT_GAP_GRACE_SECONDS,
    as_of: datetime | None = None,
) -> dict:
    """Compare original V3 source with the research ledger (read-only).

    ``as_of`` and ``gap_grace_seconds`` are injectable so the coverage
    lag / gap decision is deterministic and testable; production passes
    ``as_of=None`` (= database wall clock at call time).
    """

    if type(gap_grace_seconds) is not int or gap_grace_seconds < 0:
        raise ValueError("INVALID_GAP_GRACE_SECONDS")

    if as_of is None:
        now = datetime.now(timezone.utc)
    else:
        now = as_utc(as_of)

    stale_cutoff = now - timedelta(seconds=gap_grace_seconds)

    # 1. A frozen activation is REQUIRED before any coverage claim
    #    (Codex review, PR #303): without it the audit window is empty
    #    and an empty audit must never read as validated coverage.
    with connection.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT freeze_ts
            FROM research_me_entry_quality.activation
            WHERE experiment_id = %s
            """,
            (EXPERIMENT_ID,),
        )
        activation = cur.fetchone()

    if activation is None:
        return _audit_row(
            status=AUDIT_SOURCE_COVERAGE_UNKNOWN,
            reason=REASON_NOT_ACTIVATED,
        )

    if activation["freeze_ts"] is None:
        return _audit_row(
            status=AUDIT_SOURCE_COVERAGE_UNKNOWN,
            reason=REASON_MISSING_FREEZE_TS,
        )

    # 2. Full read-only reconciliation of eligible V3 source rows,
    #    the research event ledger and the processing ledger.
    with connection.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            WITH activation AS (
                SELECT freeze_ts
                FROM research_me_entry_quality.activation
                WHERE experiment_id = %s
                  AND freeze_ts IS NOT NULL
            ),
            eligible AS (
                SELECT s.event_key, s.lineage, s.symbol,
                       s.signal_time, s.source_direction,
                       s.source_scanner, s.created_at
                FROM dds.me_long_v2_inverse_short_v3_event s
                CROSS JOIN activation a
                WHERE s.signal_time > a.freeze_ts
                  AND s.created_at >= a.freeze_ts
                  AND s.captured_at >= a.freeze_ts
            ),
            joined AS (
                SELECT
                    s.event_key AS source_key,
                    e.event_key AS research_key,
                    p.processing_status AS processing_status,
                    (
                        p.event_key IS NOT NULL
                        AND p.processing_status = 'ACCEPTED'
                        AND e.event_key IS NULL
                    ) AS accepted_without_source,
                    (
                        p.event_key IS NOT NULL
                        AND p.processing_status = 'REJECTED'
                        AND e.event_key IS NOT NULL
                    ) AS rejected_in_cohort,
                    (
                        e.event_key IS NOT NULL
                        AND (
                            e.lineage IS DISTINCT FROM s.lineage
                            OR e.symbol IS DISTINCT FROM s.symbol
                            OR e.signal_time IS DISTINCT FROM s.signal_time
                            OR e.source_direction
                               IS DISTINCT FROM s.source_direction
                            OR e.source_scanner
                               IS DISTINCT FROM s.source_scanner
                        )
                    ) AS lineage_mismatch,
                    (p.event_key IS NULL) AS unprocessed,
                    (
                        p.event_key IS NULL
                        AND s.created_at <= %s
                    ) AS unprocessed_stale
                FROM eligible s
                LEFT JOIN research_me_entry_quality.processed_event p
                  ON p.event_key = s.event_key
                 AND p.experiment_id = %s
                LEFT JOIN research_me_entry_quality.source_event e
                  ON e.event_key = s.event_key
                 AND e.experiment_id = %s
            )
            SELECT
                (SELECT COUNT(*) FROM eligible) AS eligible_source,
                COALESCE(SUM((unprocessed)::int), 0) AS unprocessed,
                COALESCE(SUM((unprocessed_stale)::int), 0)
                    AS unprocessed_stale,
                COALESCE(SUM((processing_status = 'ACCEPTED')::int), 0)
                    AS accepted,
                COALESCE(SUM((processing_status = 'REJECTED')::int), 0)
                    AS rejected,
                COALESCE(SUM((accepted_without_source)::int), 0)
                    AS accepted_without_source,
                COALESCE(SUM((rejected_in_cohort)::int), 0)
                    AS rejected_in_cohort,
                COALESCE(SUM((lineage_mismatch)::int), 0)
                    AS lineage_mismatches,
                (
                    SELECT COUNT(*)
                    FROM research_me_entry_quality.source_event e
                    WHERE e.experiment_id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM eligible s
                          WHERE s.event_key = e.event_key
                      )
                ) AS orphan_research_events,
                (
                    SELECT COUNT(*)
                    FROM research_me_entry_quality.processed_event p
                    WHERE p.experiment_id IS DISTINCT FROM %s
                ) AS ledger_experiment_mismatches
            FROM joined
            """,
            (
                EXPERIMENT_ID,
                stale_cutoff,
                EXPERIMENT_ID,
                EXPERIMENT_ID,
                EXPERIMENT_ID,
                EXPERIMENT_ID,
            ),
        )

        counts = dict(cur.fetchone())

    conflicts = sum(int(counts[name]) for name in CONFLICT_CATEGORIES)

    if conflicts:
        return _audit_row(
            **counts,
            status=AUDIT_INTEGRITY_CONFLICT,
            reason=REASON_INTEGRITY_CONFLICT,
            ledger_coverage=LEDGER_COVERAGE_CONFLICT,
            integrity_conflicts=conflicts,
            as_of=now.isoformat(),
            gap_grace_seconds=gap_grace_seconds,
        )

    if counts["unprocessed_stale"]:
        return _audit_row(
            **counts,
            status=AUDIT_COVERAGE_GAP,
            reason=REASON_COVERAGE_GAP,
            ledger_coverage=LEDGER_COVERAGE_GAP,
            integrity_conflicts=0,
            as_of=now.isoformat(),
            gap_grace_seconds=gap_grace_seconds,
        )

    if counts["unprocessed"]:
        return _audit_row(
            **counts,
            status=AUDIT_COVERAGE_LAG,
            reason=REASON_COVERAGE_LAG,
            ledger_coverage=LEDGER_COVERAGE_GAP,
            integrity_conflicts=0,
            as_of=now.isoformat(),
            gap_grace_seconds=gap_grace_seconds,
        )

    return _audit_row(
        **counts,
        status=AUDIT_SOURCE_COVERAGE_VALIDATED,
        reason=REASON_AUDIT_CLEAN,
        ledger_coverage=LEDGER_COVERAGE_VALIDATED,
        integrity_conflicts=0,
        as_of=now.isoformat(),
        gap_grace_seconds=gap_grace_seconds,
    )


def verdict_gate(
    *,
    contour_state: str | None,
    audit: Mapping[str, Any] | None,
) -> dict:
    """Deterministic gate for SOURCE_VALIDATED / final OOS verdict issuance.

    Rules (Issue #296 review, P1 + P2):

    * ``SOURCE_VALIDATED`` is NEVER issued while the upstream capture
      completeness is unproven — a clean V3-ledger reconciliation only
      yields ``V3_LEDGER_COVERAGE_VALIDATED``;
    * a source-integrity violation or a persistent coverage gap forbids
      any validation claim;
    * the final OOS verdict is never allowed in Phase 1 (Task 0 is a
      source observer only).
    """

    audit_status = None
    ledger_validated = False

    if audit is not None:
        audit_status = str(audit.get("status") or "")
        ledger_validated = (
            audit.get("ledger_coverage") == LEDGER_COVERAGE_VALIDATED
        )

    if contour_state == "BLOCKED":
        return {
            "source_validated": False,
            "source_validated_scope": "NONE",
            "v3_ledger_coverage_validated": ledger_validated,
            "upstream_source_coverage": UPSTREAM_SOURCE_COVERAGE_UNPROVEN,
            "source_coverage_status": (
                audit_status
                if audit_status
                in (AUDIT_COVERAGE_GAP, AUDIT_INTEGRITY_CONFLICT)
                else AUDIT_SOURCE_COVERAGE_UNKNOWN
            ),
            "final_oos_verdict_allowed": False,
            "reason": "BLOCKED_CONTOUR",
        }

    if audit_status in (AUDIT_INTEGRITY_CONFLICT, AUDIT_COVERAGE_GAP):
        return {
            "source_validated": False,
            "source_validated_scope": "NONE",
            "v3_ledger_coverage_validated": False,
            "upstream_source_coverage": UPSTREAM_SOURCE_COVERAGE_UNPROVEN,
            "source_coverage_status": audit_status,
            "final_oos_verdict_allowed": False,
            "reason": REASON_INTEGRITY_CONFLICT
            if audit_status == AUDIT_INTEGRITY_CONFLICT
            else REASON_COVERAGE_GAP,
        }

    # Best case: the V3-ledger reconciliation passed. The independent
    # completeness of the upstream capture is still unproven, so the
    # issued coverage status stays SOURCE_COVERAGE_UNKNOWN and no full
    # SOURCE_VALIDATED exists.
    return {
        "source_validated": False,
        "source_validated_scope": "NONE",
        "v3_ledger_coverage_validated": ledger_validated,
        "upstream_source_coverage": UPSTREAM_SOURCE_COVERAGE_UNPROVEN,
        "source_coverage_status": AUDIT_SOURCE_COVERAGE_UNKNOWN,
        "final_oos_verdict_allowed": False,
        "reason": REASON_UPSTREAM_UNPROVEN,
    }
