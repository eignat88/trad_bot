# ME_R_EARLY_MAE_V3 Verification Plan

**Status:** DRAFT
**Mode:** read-only / non-invasive until separately approved

## A. Local code checks

1. Confirm the new package is importable without importing paper or live engines.
2. Confirm no production scanner source contains `ME_R_LONG_EARLY_MAE_EXIT_OOS_V3`.
3. Confirm the closed-candle context rejects unclosed 5m, 15m, and 1h candles.
4. Confirm indicators are recomputed from completed 1h candles.
5. Confirm detector parity against the frozen V1 rules on completed-candle fixtures.
6. Confirm all source timestamps are sourced from candles or measured runtime times.
7. Confirm no look-ahead: no candle with close time after ASOF is used.
8. Confirm source identity is stable under duplicate replay.
9. Confirm conflicting duplicate identity fails closed.
10. Confirm `NEXT_OPEN_5M` uses the first completed 5m open after eligibility and that the signal is no later than 300 seconds after signal-candle close.
11. Confirm no-fill only after complete entry coverage.
12. Confirm baseline stop/target/timeout and ambiguity semantics.
13. Confirm all seven variants are written atomically with exactly one PRIMARY, `MAE_15m_050`.
14. Confirm `HORIZON_END_CONDITIONAL_MAE_EXIT` uses the completed horizon-boundary 5m CLOSE only after threshold observation, never immediate first-touch exit.
15. Confirm finalized outcomes cannot be overwritten.
16. Confirm paper/live isolation and frozen V2 invariance.

## B. SQL checks after an approved migration

The following are read-only validation queries for the new schema:

```sql
-- Protocol must be one immutable row per approved version.
SELECT protocol_version, experiment_id, frozen_at, activation_at,
       protocol_fingerprint
FROM research_me_early_mae_v3.protocol
ORDER BY protocol_version;

-- No duplicate source identity.
SELECT protocol_version, source_setup_id, COUNT(*)
FROM research_me_early_mae_v3.source_event
GROUP BY protocol_version, source_setup_id
HAVING COUNT(*) > 1;

-- No duplicate instrument/candle/source identity.
SELECT protocol_version, instrument_id, scanner_name, direction,
       source_candle_open_at, COUNT(*)
FROM research_me_early_mae_v3.source_event
GROUP BY protocol_version, instrument_id, scanner_name, direction,
         source_candle_open_at
HAVING COUNT(*) > 1;

-- Closed-candle temporal violations must be zero.
SELECT source_event_id, source_candle_close_at, detected_at
FROM research_me_early_mae_v3.source_event
WHERE source_candle_close_at > detected_at;

-- No source may exceed the approved 300-second maximum source delay.
SELECT source_event_id, source_candle_close_at, source_available_at
FROM research_me_early_mae_v3.source_event
WHERE source_available_at > source_candle_close_at + INTERVAL '300 seconds';

-- Entry may not precede eligibility.
SELECT source_event_id, entry_eligible_at, entry_filled_at
FROM research_me_early_mae_v3.source_event
WHERE entry_filled_at IS NOT NULL
  AND entry_filled_at < entry_eligible_at;

-- Every eligible filled source needs exactly seven variants.
SELECT s.source_event_id, COUNT(o.variant_id) AS variant_count
FROM research_me_early_mae_v3.source_event s
LEFT JOIN research_me_early_mae_v3.variant_outcome o
  ON o.source_event_id = s.source_event_id
 AND o.protocol_version = s.protocol_version
WHERE s.source_status = 'ENTRY_FILLED'
GROUP BY s.source_event_id
HAVING COUNT(o.variant_id) <> 7;

-- No finalized outcome may have missing paired fields.
SELECT source_event_id, variant_id
FROM research_me_early_mae_v3.variant_outcome
WHERE variant_status = 'FINALIZED'
  AND (variant_exit_at IS NULL
       OR variant_net_r IS NULL
       OR delta_net_r IS NULL);

-- No finalized source may have incomplete coverage.
SELECT source_event_id, source_status, coverage_status
FROM research_me_early_mae_v3.source_event
WHERE source_status = 'FINALIZED'
  AND coverage_status <> 'COMPLETE';
```

## C. Evaluator checks

- Start only with explicit opt-in and a reviewed fingerprint.
- Refuse startup when freeze/activation is absent or malformed.
- Refuse startup when the code fingerprint differs from the DB protocol.
- Process only post-activation source events.
- Never insert historical source events.
- Log pipeline run start, counts, coverage diagnostics, and errors.
- Recover idempotently after restart.
- Never write to paper/live tables.
- Never touch frozen V2 tables or the existing ME Reverse Long V3 tables.

## D. Natural E2E acceptance

Natural acceptance requires actual prospective data after a separately approved activation; synthetic fixtures are insufficient.

Accept only when:

1. The dedicated service is explicitly enabled and active.
2. The first source event is after activation.
3. The source candle closes before detection.
4. All required candles and indicators are closed at detection.
5. One source row and exactly seven variant rows are created atomically.
6. All seven outcomes are based on the same eligible filled source event.
7. Exactly one outcome is PRIMARY, and it is `MAE_15m_050`.
8. No pre-activation or historical row is eligible.
9. No duplicate source identity exists.
10. Restart replay does not duplicate or mutate finalized outcomes.
11. No paper/live trade, balance, position, direction gate, cooldown, or production risk control changed.
12. Frozen V2 and the existing ME Reverse Long V3 experiment remain unchanged.

## E. Evidence boundary

Before natural OOS collection and approved activation, the correct status is:

`DESIGN_READY_FOR_OPERATOR_APPROVAL`

No claim of accumulating OOS data or positive MAE edge is permitted.
