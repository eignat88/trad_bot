-- ============================================================
-- Issue #208: ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1
-- Machine-reproducible READ-ONLY replay of the forward-only lineage.
-- SELECT statements only. Never writes.
--
-- Run on the production DB (VPS) with a read-only role/session:
--   psql -X -v ON_ERROR_STOP=1 -1 -f \
--     sql/research/me_short_a_forward_outcome_lineage_audit_v1.sql
--
-- Never mutates Phase 1 evidence, its source_observation_ids, the activation
-- freeze/status, or the closed frozen OOS cohort. Never activates capture.
-- ============================================================

-- Q1. Activation lifecycle (read-only; never UPDATEd here).
SELECT
    experiment_id,
    protocol_version,
    protocol_hash,
    status,
    freeze_ts,
    created_at,
    updated_at
FROM research.me_short_a_lineage_activation_v1
WHERE experiment_id = 'ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1';

-- Q2. Population overview of the NEW forward-only cohort only.
SELECT
    COUNT(*)                                                        AS events,
    COUNT(DISTINCT symbol)                                          AS symbols,
    MIN(signal_time)                                                AS first_signal,
    MAX(signal_time)                                                AS last_signal,
    COUNT(*) FILTER (WHERE link_status = 'LINKED')                  AS linked,
    COUNT(*) FILTER (WHERE link_status = 'PENDING')                 AS pending,
    COUNT(*) FILTER (WHERE link_status = 'QUARANTINED')             AS quarantined,
    COUNT(*) FILTER (WHERE link_status = 'PHASE1_PENDING')          AS phase1_pending,
    COUNT(*) FILTER (WHERE phase1_linked)                           AS phase1_linked,
    COUNT(*) FILTER (WHERE source_observation_id IS NOT NULL)       AS with_source_observation_id
FROM research.me_short_a_event_lineage_v1;

-- Q3. Three-layer lineage join on the deterministic event_key.
--     Missing layers surface as NULLs and are reported, never filled in.
SELECT
    l.event_key,
    l.symbol,
    l.signal_time,
    l.protocol_version,
    l.protocol_hash,
    l.event_key_recomputed,
    l.event_key_integrity,
    l.link_status,
    l.quarantine_reason,
    s.event_key                                                    AS source_signal_key,
    s.trigger_5m_close,
    s.entry_zone_low,
    s.entry_zone_high,
    s.recent_high_window_candles,
    s.signal_candle_close_time,
    p.event_key                                                    AS phase1_event_key,
    (p.source_observation_ids ->> 0)::bigint                       AS phase1_observation_id,
    o.event_key                                                    AS outcome_key,
    o.outcome_status,
    o.path_class,
    o.is_final,
    o.gross_r,
    o.net_r_normal,
    o.horizon_end_time,
    o.finalized_at,
    o.finalization_candle_close_time
FROM research.me_short_a_event_lineage_v1 l
LEFT JOIN research.me_short_a_source_signal_v1 s
       ON s.event_key = l.event_key
LEFT JOIN research.me_short_a_fill_feasibility_v1 p
       ON p.event_key = l.event_key
LEFT JOIN research.me_short_a_outcome_observation_v1 o
       ON o.event_key = l.event_key
ORDER BY l.signal_time, l.event_key;

-- Q4. Output status distribution — exactly four statuses, all always shown
--     (a zero row is evidence, not an omission).
WITH wanted AS (
    SELECT t.lineage_status
    FROM (VALUES ('MISSING_LINEAGE'),
                 ('OUTCOME_UNMATURED'),
                 ('SOURCE_INCOMPLETE'),
                 ('MATCHED_FINALIZED')) AS t(lineage_status)
)
SELECT
    w.lineage_status,
    COUNT(v.event_key)                                              AS n,
    COUNT(DISTINCT v.symbol)                                        AS symbols
FROM wanted w
LEFT JOIN research.v_me_short_a_lineage_status_v1 v
       ON v.lineage_status = w.lineage_status
GROUP BY w.lineage_status
ORDER BY w.lineage_status;

-- Q5. Identity integrity: every stored key must be re-derivable.
--     All counts except key_mismatches must be zero.
SELECT
    COUNT(*)                                                        AS rows_total,
    COUNT(*) FILTER (WHERE event_key <> event_key_recomputed)       AS key_mismatches,
    COUNT(*) FILTER (WHERE NOT event_key_integrity)                 AS integrity_failures,
    COUNT(*) FILTER (WHERE link_status = 'QUARANTINED')             AS quarantined,
    COUNT(*) FILTER (WHERE link_status = 'QUARANTINED'
                        AND quarantine_reason IS NULL)              AS quarantine_without_reason,
    COUNT(*) FILTER (WHERE link_status = 'LINKED'
                        AND (NOT source_signal_present
                             OR NOT event_key_integrity
                             OR NOT phase1_linked))           AS invalid_links,
    COUNT(*) FILTER (WHERE link_status = 'LINKED'
                        AND NOT phase1_linked)                AS linked_without_phase1,
    COUNT(*) FILTER (WHERE link_status = 'PHASE1_PENDING'
                        AND quarantine_reason IS NULL)        AS phase1_pending_without_reason,
    COUNT(*) FILTER (WHERE phase1_linked AND phase1_event_key <> event_key)
                                                                    AS phase1_key_mismatch
FROM research.me_short_a_event_lineage_v1;

-- Q6. Timestamp / no-lookahead sanity. Every count MUST be zero.
SELECT
    (SELECT COUNT(*) FROM research.me_short_a_source_signal_v1
      WHERE signal_time <= freeze_ts)                               AS source_pre_freeze,
    (SELECT COUNT(*) FROM research.me_short_a_source_signal_v1
      WHERE captured_at < signal_time)                              AS source_captured_before_signal,
    (SELECT COUNT(*) FROM research.me_short_a_source_signal_v1
      WHERE recent_high_window_end > signal_time)                   AS source_window_lookahead,
    (SELECT COUNT(*) FROM research.me_short_a_source_signal_v1
      WHERE signal_candle_close_time > signal_time)                 AS signal_candle_lookahead,
    (SELECT COUNT(*) FROM research.me_short_a_post_signal_candle_v1
      WHERE signal_time <= freeze_ts)                               AS candle_pre_freeze,
    (SELECT COUNT(*) FROM research.me_short_a_post_signal_candle_v1
      WHERE candle_open_time <= signal_time)                        AS candle_not_post_signal,
    (SELECT COUNT(*) FROM research.me_short_a_post_signal_candle_v1
      WHERE candle_close_time > retrieved_at)                       AS candle_lookahead,
    (SELECT COUNT(*) FROM research.me_short_a_post_signal_candle_v1
      WHERE NOT is_closed)                                          AS candle_not_closed,
    (SELECT COUNT(*) FROM research.me_short_a_outcome_observation_v1
      WHERE signal_time <= freeze_ts)                               AS outcome_pre_freeze,
    (SELECT COUNT(*) FROM research.me_short_a_outcome_observation_v1
      WHERE max_candle_close_time > asof_time)                      AS outcome_asof_violation,
    (SELECT COUNT(*) FROM research.me_short_a_outcome_observation_v1
      WHERE is_final
        AND finalization_candle_close_time > asof_time)             AS outcome_final_lookahead,
    (SELECT COUNT(*) FROM research.me_short_a_outcome_observation_v1
      WHERE is_final
        AND finalization_candle_close_time < horizon_end_time)      AS outcome_premature_final;

-- Q7. Source coverage and controlled finalization.
SELECT
    outcome_status,
    COUNT(*)                                                        AS n,
    COUNT(*) FILTER (WHERE is_final)                                AS n_final,
    COUNT(*) FILTER (WHERE coverage_complete)                       AS n_coverage_complete,
    COUNT(*) FILTER (WHERE asof_ok)                                 AS n_asof_ok,
    MIN(candles_present)                                            AS min_candles_present,
    MAX(candles_present)                                            AS max_candles_present,
    MAX(candles_expected)                                           AS max_candles_expected
FROM research.me_short_a_outcome_observation_v1
GROUP BY 1
ORDER BY 1;

-- Q8. Invariant: a source-missing row can never carry economics.
--     The count MUST be zero.
SELECT COUNT(*) AS incomplete_rows_with_economics
FROM research.me_short_a_outcome_observation_v1
WHERE outcome_status <> 'MATCHED_FINALIZED'
  AND (path_class IS NOT NULL
       OR exit_price IS NOT NULL
       OR gross_r IS NOT NULL
       OR net_r_normal IS NOT NULL
       OR net_r_elevated IS NOT NULL
       OR return_at_240m IS NOT NULL);

-- Q9. STOP_FIRST semantics invariant. The count MUST be zero.
SELECT COUNT(*) AS stop_first_violations
FROM research.me_short_a_outcome_observation_v1
WHERE (tp_before_sl AND sl_before_tp)
   OR (ambiguous_intrabar AND NOT (sl_before_tp AND NOT tp_before_sl))
   OR (path_class = 'STOP_HIT' AND NOT sl_before_tp)
   OR (path_class = 'TARGET_HIT' AND NOT tp_before_sl)
   OR (path_class = 'STOP_HIT' AND gross_r <> -1)
   OR (path_class = 'TIMEOUT_240M'
       AND (sl_before_tp OR tp_before_sl OR ambiguous_intrabar))
   OR exit_fill_semantics <> 'MODELED_UNKNOWN'
   OR partial_fill_status <> 'UNKNOWN_NOT_INDEPENDENTLY_CAPTURED'
   OR touch_basis <> 'CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL';

-- Q10. Source field coverage of the supplement that PR #207 reported as MISSING.
SELECT
    COUNT(*)                                                        AS rows_total,
    COUNT(*) FILTER (WHERE trigger_5m_close IS NOT NULL)            AS has_trigger_5m_close,
    COUNT(*) FILTER (WHERE entry_zone_low IS NOT NULL)              AS has_entry_zone_low,
    COUNT(*) FILTER (WHERE entry_zone_high IS NOT NULL)             AS has_entry_zone_high,
    COUNT(*) FILTER (WHERE recent_high_window_candles IS NOT NULL)  AS has_recent_high_window,
    COUNT(*) FILTER (WHERE signal_candle_close_time IS NOT NULL)    AS has_signal_candle_close,
    COUNT(*) FILTER (WHERE jsonb_array_length(missing_fields) > 0)  AS rows_with_explicit_missing
FROM research.me_short_a_source_signal_v1;

-- Q11. Post-signal 5m provenance coverage and candle-touch classification.
SELECT
    COUNT(*)                                                        AS candle_rows,
    COUNT(DISTINCT event_key)                                       AS events_with_candles,
    COUNT(*) FILTER (WHERE high_touches_stop)                       AS touches_stop,
    COUNT(*) FILTER (WHERE low_touches_target)                      AS touches_target,
    COUNT(*) FILTER (WHERE high_touches_reference)                  AS touches_reference,
    COUNT(DISTINCT event_key) FILTER (WHERE high_touches_reference) AS events_limit_touch
FROM research.me_short_a_post_signal_candle_v1;

-- Q12. Accumulation gate (minimum eligibility, NOT an automatic PASS).
--     #206 may only be evaluated at >= 200 MATCHED_FINALIZED across >= 20 symbols.
SELECT
    COUNT(*)                                                        AS matched_finalized,
    COUNT(DISTINCT symbol)                                          AS symbols,
    MIN(signal_time)                                                AS first_signal,
    MAX(signal_time)                                                AS last_signal,
    (COUNT(*) >= 200)                                               AS gate_minimum_n_met,
    (COUNT(DISTINCT symbol) >= 20)                                  AS gate_minimum_symbols_met
FROM research.v_me_short_a_lineage_status_v1
WHERE lineage_status = 'MATCHED_FINALIZED';

-- Q13. Isolation guard: the frozen Phase 1 evidence must be untouched by this
--      protocol. These numbers describe Phase 1 as-is and must never change
--      because of the forward-only collector.
SELECT
    COUNT(*)                                                        AS phase1_rows,
    COUNT(*) FILTER (WHERE source_observation_ids IS NULL)          AS phase1_rows_without_source_ids,
    MIN(signal_time)                                                AS phase1_first_signal,
    MAX(signal_time)                                                AS phase1_last_signal
FROM research.me_short_a_fill_feasibility_v1;

-- Q14. Isolation guard: no pre-freeze evidence may exist in ANY capture table.
SELECT
    (SELECT COUNT(*) FROM research.me_short_a_source_signal_v1
      WHERE signal_time <= (SELECT freeze_ts
                            FROM research.me_short_a_lineage_activation_v1
                            WHERE experiment_id =
                              'ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1'))
                                                                    AS source_before_freeze,
    (SELECT COUNT(*) FROM research.me_short_a_outcome_observation_v1
      WHERE signal_time <= (SELECT freeze_ts
                            FROM research.me_short_a_lineage_activation_v1
                            WHERE experiment_id =
                              'ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1'))
                                                                    AS outcome_before_freeze;
