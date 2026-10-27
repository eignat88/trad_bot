-- ============================================================
-- Issue #206: ME_SHORT_A_REFERENCE_TO_EXECUTION_PRICE_ALIGNMENT_AUDIT_V1
-- Machine-reproducible READ-ONLY SQL. SELECT statements only.
-- Run on the production DB (VPS) with a read-only role/session:
--   psql -X -v ON_ERROR_STOP=1 -1 -f \
--     sql/research/me_short_a_ref_exec_alignment_audit_v1.sql
-- Never writes. Never mutates Phase 1 evidence, freeze or status.
-- ============================================================

-- Q1. Phase 1 population overview (event count, symbols, capture lag,
--     reference-to-bid drift, structural stop, cost classes).
SELECT
    COUNT(*)                                                   AS events,
    COUNT(DISTINCT symbol)                                     AS symbols,
    MIN(signal_time)                                           AS first_signal,
    MAX(signal_time)                                           AS last_signal,
    ROUND(AVG(EXTRACT(EPOCH FROM (captured_at - signal_time)))::numeric, 3)
                                                               AS avg_capture_lag_s,
    ROUND(AVG(book_age_ms)::numeric, 1)                        AS avg_book_age_ms,
    ROUND(AVG(reference_to_bid_bps)::numeric, 2)               AS avg_ref_to_bid_bps,
    ROUND(MIN(reference_to_bid_bps)::numeric, 2)               AS min_ref_to_bid_bps,
    ROUND(MAX(reference_to_bid_bps)::numeric, 2)               AS max_ref_to_bid_bps,
    ROUND(AVG(structural_r_pct)::numeric, 4)                   AS avg_structural_r_pct,
    ROUND(AVG(estimated_roundtrip_cost_r)::numeric, 4)         AS avg_cost_r_base,
    COUNT(*) FILTER (WHERE feasibility_status = 'OK')          AS n_ok,
    COUNT(*) FILTER (WHERE feasibility_status <> 'OK')         AS n_not_ok,
    COUNT(*) FILTER (WHERE cost_class = 'NORMAL')              AS cost_normal,
    COUNT(*) FILTER (WHERE cost_class = 'ELEVATED')            AS cost_elevated,
    COUNT(*) FILTER (WHERE cost_class = 'ABOVE_ELEVATED')      AS cost_above_elevated
FROM research.me_short_a_fill_feasibility_v1;

-- Q2. Lineage join: Phase 1 snapshot -> source observation -> frozen outcome.
--     (Mismatches surface as NULLs on either side; they must be reported,
--      never filled in.)
SELECT
    e.event_key,
    e.symbol,
    e.signal_time,
    e.captured_at,
    e.reference_price,
    e.stop_price,
    e.target_price,
    e.bid,
    e.ask,
    e.estimated_fill,
    e.feasibility_status,
    e.cost_class,
    (e.source_observation_ids ->> 0)::bigint                   AS observation_id,
    o.experiment_id                                            AS obs_experiment,
    o.symbol                                                   AS obs_symbol,
    o.direction                                                AS obs_direction,
    o.signal_time                                              AS obs_signal_time,
    o.reference_price                                          AS obs_reference_price,
    o.invalidation_price                                       AS obs_stop_price,
    o.target_1                                                 AS obs_target_price,
    o.features ->> 'trigger_5m_close'                          AS trigger_5m_close,
    o.features ->> 'entry_zone_low'                            AS entry_zone_low,
    o.features ->> 'entry_zone_high'                           AS entry_zone_high,
    w.is_final,
    w.evaluated_240m_at,
    w.tp_hit,
    w.sl_hit,
    w.tp_before_sl,
    w.sl_before_tp,
    w.ambiguous_intrabar,
    w.return_at_240m,
    w.mae_240m
FROM research.me_short_a_fill_feasibility_v1 e
LEFT JOIN research.prospective_observation o
       ON o.observation_id = (e.source_observation_ids ->> 0)::bigint
      AND o.experiment_id = 'ME_SHORT_GEOM_A_EXECUTION_V1'
LEFT JOIN research.prospective_outcome w
       ON w.observation_id = o.observation_id
      AND w.experiment_id = o.experiment_id
ORDER BY e.signal_time, e.event_key;

-- Q3. Field coverage: how much of the required context is actually retained.
SELECT
    COUNT(*)                                                   AS rows_total,
    COUNT(*) FILTER (WHERE o.features ? 'trigger_5m_close')     AS has_trigger_5m_close,
    COUNT(*) FILTER (WHERE o.features ? 'entry_zone_low'
                        OR o.parameters ? 'entry_zone_low')     AS has_entry_zone_low,
    COUNT(*) FILTER (WHERE o.features ? 'entry_zone_high'
                        OR o.parameters ? 'entry_zone_high')    AS has_entry_zone_high,
    COUNT(*) FILTER (WHERE w.is_final)                          AS outcomes_final,
    COUNT(*) FILTER (WHERE w.evaluated_240m_at IS NOT NULL)     AS outcomes_matured_240m,
    COUNT(*) FILTER (WHERE (e.source_observation_ids ->> 0)::bigint IS NULL)
                                                                AS lineage_missing_ids,
    COUNT(*) FILTER (WHERE o.observation_id IS NULL)            AS lineage_observation_missing,
    COUNT(*) FILTER (WHERE w.observation_id IS NULL)            AS lineage_outcome_missing
FROM research.me_short_a_fill_feasibility_v1 e
LEFT JOIN research.prospective_observation o
       ON o.observation_id = (e.source_observation_ids ->> 0)::bigint
      AND o.experiment_id = 'ME_SHORT_GEOM_A_EXECUTION_V1'
LEFT JOIN research.prospective_outcome w
       ON w.observation_id = o.observation_id
      AND w.experiment_id = o.experiment_id;

-- Q4. Per-symbol and correlated-cluster coverage (XAU/XAUT cluster = XAU).
SELECT
    LEFT(translate(e.symbol, '0123456789', ''), 3)             AS symbol_cluster,
    e.symbol,
    COUNT(*)                                                   AS n,
    ROUND(AVG(e.reference_to_bid_bps)::numeric, 2)             AS avg_ref_to_bid_bps,
    COUNT(*) FILTER (WHERE e.feasibility_status = 'OK')        AS n_ok
FROM research.me_short_a_fill_feasibility_v1 e
GROUP BY 1, 2
ORDER BY 1, 2;

-- Q5. Timestamp / lookahead sanity.
-- First four counters: violations (expected zero).
-- Last two counters: capture-lag diagnostics, not automatic violations.
SELECT
    COUNT(*) FILTER (WHERE signal_time <= freeze_ts)                        AS signal_not_after_phase1_freeze,
    COUNT(*) FILTER (WHERE signal_time <= TIMESTAMPTZ '2026-10-19 08:26:00+00')
                                                                            AS signal_not_after_oos_freeze,
    COUNT(*) FILTER (WHERE captured_at < signal_time)                       AS capture_before_signal,
    COUNT(*) FILTER (WHERE book_time_ms IS NOT NULL
                        AND book_time_ms > EXTRACT(EPOCH FROM captured_at) * 1000 + 1000)
                                                                            AS book_in_future,
    COUNT(*) FILTER (WHERE captured_at = signal_time)                    AS exact_zero_lag,
    COUNT(*) FILTER (WHERE captured_at > signal_time
                        AND captured_at - signal_time < INTERVAL '1 second')
                                                                            AS subsecond_capture_lag
FROM research.me_short_a_fill_feasibility_v1;

-- Q6. Per-scenario min-constraint feasibility (UNKNOWN constraints excluded).
SELECT
    scenario ->> 'notional_usdt'                               AS notional_usdt,
    COUNT(*)                                                   AS n,
    COUNT(*) FILTER (WHERE scenario ->> 'constraints_known' = 'true')
                                                               AS constraints_known,
    COUNT(*) FILTER (WHERE scenario ->> 'constraints_known' <> 'true')
                                                               AS constraints_unknown_excluded,
    COUNT(*) FILTER (WHERE scenario ->> 'status' = 'OK')       AS n_ok,
    COUNT(*) FILTER (WHERE scenario ->> 'below_min_qty' = 'true')       AS n_below_min_qty,
    COUNT(*) FILTER (WHERE scenario ->> 'below_min_notional' = 'true')  AS n_below_min_notional
FROM research.me_short_a_fill_feasibility_v1 e,
     LATERAL jsonb_array_elements(
       CASE WHEN jsonb_typeof(e.scenario_results) = 'array'
            THEN e.scenario_results
            ELSE COALESCE(e.scenario_results -> 'scenarios', '[]'::jsonb)
       END) AS scenario
GROUP BY 1
ORDER BY 1;

-- Q7. Activation lifecycle state (read-only; never UPDATEd here).
SELECT experiment_id, status, freeze_ts
FROM research.me_short_a_fill_activation_v1
WHERE experiment_id = 'ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1';
-- Q8. Forward-only collector #208: event-key linkage and outcome readiness.
-- Diagnostic counts only; final economic eligibility is validated in Python.
SELECT
    COUNT(*) AS phase1_events,
    COUNT(s.event_key) AS forward_source_present,
    COUNT(l.event_key) AS forward_lineage_present,
    COUNT(o.event_key) AS forward_outcome_present,
    COUNT(*) FILTER (
        WHERE l.link_status = 'LINKED'
          AND l.event_key_integrity IS TRUE
          AND l.phase1_linked IS TRUE
          AND l.source_signal_present IS TRUE
          AND l.phase1_event_key = e.event_key
          AND o.outcome_status = 'MATCHED_FINALIZED'
          AND o.is_final IS TRUE
          AND o.coverage_complete IS TRUE
          AND o.asof_ok IS TRUE
    ) AS forward_status_ready
FROM research.me_short_a_fill_feasibility_v1 e
LEFT JOIN research.me_short_a_source_signal_v1 s
       ON s.event_key = e.event_key
LEFT JOIN research.me_short_a_event_lineage_v1 l
       ON l.event_key = e.event_key
LEFT JOIN research.me_short_a_outcome_observation_v1 o
       ON o.event_key = e.event_key;
