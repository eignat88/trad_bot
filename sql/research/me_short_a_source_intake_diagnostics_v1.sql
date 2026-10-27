-- ============================================================
-- Issue #208 corrective: ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1
-- Machine-reproducible READ-ONLY observability of the ME SHORT A
-- source-lineage gap fix. SELECT statements only. Never writes.
--
-- Run on the production DB (VPS) with a read-only role/session:
--   psql -X -v ON_ERROR_STOP=1 -1 -f \
--     sql/research/me_short_a_source_intake_diagnostics_v1.sql
--
-- Scope rule (honest cohort statistics): the fixed mechanism observes a
-- strictly forward-only cohort (signal_time > intake_freeze_ts). Historical
-- gaps — the confirmed incident event (TSLAUSDT 2026-10-27
-- 10:43:57.672455+00) included — are NEVER counted in the new cohort's
-- success statistics; they are surfaced separately (Q9) as history.
--
-- Never mutates the intake, Phase 1 evidence, the four Issue #208 capture
-- tables, the activation freeze/status, or the closed frozen OOS cohort.
-- ============================================================

-- Q1. Intake activation lifecycle (read-only; never UPDATEd here).
SELECT
    experiment_id,
    protocol_version,
    status,
    intake_freeze_ts,
    created_at,
    updated_at
FROM research.me_short_a_source_intake_activation_v1
WHERE experiment_id = 'ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1';

-- Q2. Source events saved, by capture route — every trading decision is
--     observable. REGIME_FILTER_REJECTED is the confirmed incident class:
--     events the trading regime excluded but research kept.
SELECT
    capture_route,
    COUNT(*)                                        AS events,
    COUNT(DISTINCT symbol)                          AS symbols,
    MIN(signal_time)                                AS first_signal,
    MAX(signal_time)                                AS last_signal,
    COUNT(*) FILTER (WHERE blocked_stage IS NOT NULL) AS with_block_metadata
FROM research.me_short_a_source_intake_v1
GROUP BY capture_route
ORDER BY capture_route;

-- Q3. Source-capture funnel: intake rows vs. what the trading flow saved.
--     "excluded_by_regime_but_captured" must equal Q2's REGIME_FILTER_REJECTED
--     count; a Phase 1 row without a source row is a SILENT-LOSS signal.
SELECT
    (SELECT COUNT(*) FROM research.me_short_a_source_intake_v1)     AS source_events_saved,
    (SELECT COUNT(*) FROM research.me_short_a_source_intake_v1
      WHERE capture_route = 'REGIME_FILTER_REJECTED')               AS excluded_by_regime_but_captured,
    (SELECT COUNT(*) FROM research.me_short_a_source_intake_v1
      WHERE capture_route = 'DIRECTION_GATE_BLOCKED')               AS excluded_by_gate_but_captured,
    (SELECT COUNT(*) FROM research.me_short_a_source_signal_v1)     AS runtime_source_signals,
    (SELECT COUNT(*) FROM research.me_short_a_event_lineage_v1)     AS runtime_lineages;

-- Q4. Phase 1 snapshots with a CONFIRMED link (identical event_key) to the
--     Issue #208 chain. Unlinked Phase 1 rows are history, not failures of
--     the new cohort — see Q9.
SELECT
    p.event_key,
    p.symbol,
    p.signal_time,
    (s.event_key IS NOT NULL)                       AS source_signal_present,
    (l.event_key IS NOT NULL)                       AS lineage_present,
    (i.event_key IS NOT NULL)                       AS intake_present,
    l.phase1_linked,
    o.outcome_status,
    o.is_final
FROM research.me_short_a_fill_feasibility_v1 p
LEFT JOIN research.me_short_a_source_intake_v1 i       ON i.event_key = p.event_key
LEFT JOIN research.me_short_a_source_signal_v1 s       ON s.event_key = p.event_key
LEFT JOIN research.me_short_a_event_lineage_v1 l       ON l.event_key = p.event_key
LEFT JOIN research.me_short_a_outcome_observation_v1 o ON o.event_key = p.event_key
WHERE p.signal_time > (
    SELECT intake_freeze_ts
    FROM research.me_short_a_source_intake_activation_v1
    WHERE experiment_id = 'ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1')
ORDER BY p.signal_time, p.event_key;

-- Q5. Silent-loss detector for the NEW cohort: a Phase 1 snapshot that the
--     Issue #208 chain cannot see must NEVER be silent. Anything listed
--     here after activation is a live capture gap, not history.
SELECT
    p.event_key,
    p.symbol,
    p.signal_time,
    p.feasibility_status,
    (i.event_key IS NOT NULL)                       AS intake_present,
    (s.event_key IS NOT NULL)                       AS source_signal_present,
    (l.event_key IS NOT NULL)                       AS lineage_present
FROM research.me_short_a_fill_feasibility_v1 p
LEFT JOIN research.me_short_a_source_intake_v1 i ON i.event_key = p.event_key
LEFT JOIN research.me_short_a_source_signal_v1 s ON s.event_key = p.event_key
LEFT JOIN research.me_short_a_event_lineage_v1 l ON l.event_key = p.event_key
WHERE p.signal_time > (
    SELECT intake_freeze_ts
    FROM research.me_short_a_source_intake_activation_v1
    WHERE experiment_id = 'ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1')
  AND (s.event_key IS NULL OR l.event_key IS NULL)
ORDER BY p.signal_time;

-- Q6. Status distribution of the fixed mechanism — exactly four statuses,
--     all always shown (a zero row is evidence, not an omission).
WITH wanted AS (
    SELECT t.lineage_status
    FROM (VALUES
        ('MISSING_LINEAGE'),
        ('OUTCOME_UNMATURED'),
        ('SOURCE_INCOMPLETE'),
        ('MATCHED_FINALIZED')
    ) AS t(lineage_status)
),
actual AS (
    SELECT v.lineage_status, COUNT(*) AS events
    FROM research.v_me_short_a_lineage_status_v1 v
    GROUP BY v.lineage_status
)
SELECT
    w.lineage_status,
    COALESCE(a.events, 0) AS events
FROM wanted w
LEFT JOIN actual a ON a.lineage_status = w.lineage_status
ORDER BY w.lineage_status;

-- Q7. Persistence / retry diagnostics of the forward outcome runtime
--     (heartbeat counters and the last error), plus the scanner-side
--     source-capture error sink. Never silent by construction.
SELECT
    runner_id,
    captured_at,
    counter_name,
    counter_value,
    last_error
FROM research.me_short_a_forward_outcome_runtime_heartbeat_v1
WHERE counter_name IN (
    'candidates_seen', 'signals_captured', 'skipped_idempotent',
    'skipped_invalid_candidate', 'skipped_unmatured', 'errors')
ORDER BY captured_at DESC, counter_name
LIMIT 100;

SELECT
    created_at,
    symbol,
    scanner_name,
    error_type,
    error_message
FROM dds.scanner_error
WHERE error_type = 'ME_SHORT_A_SOURCE_INTAKE_ERROR'
ORDER BY created_at DESC
LIMIT 100;

-- Q8. event_key integrity across the whole chain: the same deterministic
--     identity must join every layer. A NULL layer is reported, never faked.
SELECT
    i.event_key,
    i.symbol,
    i.signal_time,
    i.capture_route,
    (p.event_key IS NOT NULL)                       AS phase1_present,
    (s.event_key IS NOT NULL)                       AS source_signal_present,
    (l.event_key IS NOT NULL)                       AS lineage_present,
    l.event_key_integrity,
    (o.event_key IS NOT NULL)                       AS outcome_present,
    o.outcome_status
FROM research.me_short_a_source_intake_v1 i
LEFT JOIN research.me_short_a_fill_feasibility_v1 p       ON p.event_key = i.event_key
LEFT JOIN research.me_short_a_source_signal_v1 s          ON s.event_key = i.event_key
LEFT JOIN research.me_short_a_event_lineage_v1 l          ON l.event_key = i.event_key
LEFT JOIN research.me_short_a_outcome_observation_v1 o    ON o.event_key = i.event_key
ORDER BY i.signal_time, i.event_key;

-- Q9. HISTORY ONLY — the known pre-activation gap (the confirmed incident:
--     TSLAUSDT 2026-10-27 10:43:57.672455+00). It is intentionally OUTSIDE
--     the fixed mechanism's cohort (strict signal_time > intake_freeze_ts)
--     and must never appear in Q2-Q6 statistics. No backfill is possible or
--     desired; this query documents the boundary.
SELECT
    p.event_key,
    p.symbol,
    p.signal_time,
    p.feasibility_status,
    'HISTORICAL_GAP_NOT_IN_NEW_COHORT' AS cohort_note
FROM research.me_short_a_fill_feasibility_v1 p
LEFT JOIN research.me_short_a_source_signal_v1 s ON s.event_key = p.event_key
LEFT JOIN research.me_short_a_event_lineage_v1 l ON l.event_key = p.event_key
WHERE p.signal_time <= (
    SELECT intake_freeze_ts
    FROM research.me_short_a_source_intake_activation_v1
    WHERE experiment_id = 'ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1')
  AND (s.event_key IS NULL OR l.event_key IS NULL)
ORDER BY p.signal_time, p.event_key;
