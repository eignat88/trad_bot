# Reference Price Lineage — ME_SHORT_A_REFERENCE_TO_EXECUTION_PRICE_ALIGNMENT_AUDIT_V1

Generated: 2026-10-26T13:35:20.970845+00:00 (read-only, reproducible).

## Frozen protocol provenance
- `sql/migrations/071_me_short_geom_a_execution_v1_registration.sql` — entry=reference_price, stop=invalidation_price, target=target_1, 240m, STOP_FIRST, frozen costs, freeze 2026-10-19T08:26:00Z.
- `app/research/prospective_evaluator.py` — outcome matching (`_check_tp_sl`, `return_at_240m`, MAE=(high-entry)/entry for SHORT).
- `app/scanners/momentum_exhaustion.py` — reference_price=max(last five 5m highs); invalidation=reference*1.002; target_1=trigger_close-2*ATR.
- `app/research/me_short_a_fill_feasibility_v1.py` — Phase 1 signal-time bid book fill evidence (append-only).

## Lineage chain
`scanner signal -> research.prospective_observation (ME_SHORT_GEOM_A_EXECUTION_V1) -> research.me_short_a_fill_feasibility_v1 (source_observation_ids) -> research.prospective_outcome`

## Sources
| source | status |
| --- | --- |
| phase1_activation | null |
| evidence rows |  |
| observations matched | 0 |
| outcomes matched | 0 |

## Per-row lineage
| event_key | symbol | signal_time | lineage_status | missing_fields | lookahead_flags | limit_at_reference_touch |
| --- | --- | --- | --- | --- | --- | --- |

## Field coverage (Phase 1 evidence vs required)
| field | stored where | status |
| --- | --- | --- |
| trigger_5m_close | candidate features at signal time (scanner current_price) | MISSING from Phase 1 evidence / linked rows |
| entry_zone_low | scanner entry_zone_low (current_price*0.998) | MISSING from Phase 1 evidence / linked rows |
| entry_zone_high | scanner entry_zone_high (current_price*1.001) | MISSING from Phase 1 evidence / linked rows |
| recent_high_window_candles | the five 5m highs that formed reference_price | MISSING from Phase 1 evidence / linked rows |
| post_signal_candle_highs_240m | absolute post-signal 5m highs/closes for limit-touch and realized exit validation | MISSING from Phase 1 evidence / linked rows |
| realized_exit_fills | actual fill prices for exits (unknown - model only) | MISSING from Phase 1 evidence / linked rows |

## Missing fields (observed)
{
  "registry": {
    "trigger_5m_close": "candidate features at signal time (scanner current_price)",
    "entry_zone_low": "scanner entry_zone_low (current_price*0.998)",
    "entry_zone_high": "scanner entry_zone_high (current_price*1.001)",
    "recent_high_window_candles": "the five 5m highs that formed reference_price",
    "post_signal_candle_highs_240m": "absolute post-signal 5m highs/closes for limit-touch and realized exit validation",
    "realized_exit_fills": "actual fill prices for exits (unknown - model only)"
  },
  "observed_missing_in_rows": [],
  "source_unavailable": [
    "phase1_activation_error",
    "evidence_error"
  ]
}

## Prospective-only plan
1. Keep Phase 1 RUNNING and strictly append-only: continue signal-time capture for every new MOMENTUM_EXHAUSTION SHORT candidate; no backfill, no mutation of existing evidence rows.
2. Register a SEPARATE forward-only append-only supplement (new table via its own migration + PR) for trigger_5m_close, entry_zone_low/high and signal-window candle context; link via event_key, never update me_short_a_fill_feasibility_v1.
3. Accumulate at least minimum_n=200 matched matured outcomes across minimum_symbols=20 symbols (frozen cohort thresholds) before any profitability claim.
4. Re-run this audit (read-only) on the enlarged population; only then may ALIGNMENT_CONFIRMED / ALIGNMENT_INVALID be issued.
5. No paper/live activation, no scanner changes, no Phase 1 freeze/status changes, no production SQL writes as part of this audit.
