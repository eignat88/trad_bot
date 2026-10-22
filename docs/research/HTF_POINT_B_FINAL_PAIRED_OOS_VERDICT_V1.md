# HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1 - Issue #169

## Objective
Evaluate **incremental Point-B uplift** compared with BASELINE on the identical underlying `setup_event_id` per direction. This is a **gross, event-conditional** research comparison, not a portfolio backtest or a deploy-to-trading authorization.

Estimand: `Mean paired delta R = Mean(Point-B R - Baseline R)` on valid matched, finalized events only, reported separately for LONG and SHORT.

## Frozen source
- Point-B: `HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE`
- Control: `HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE`
- Strict freeze: `signal_time > 2026-10-06T14:00:00Z`
- Trade path: `SL=1R / TP=2R / STOP_FIRST / 240m`
- The evaluator's `return_at_240m` is a **raw, unsigned-for-direction price percentage return**. SHORT TIMEOUT R changes sign.
- If frozen policy or target 2R differs, **reject the row** with a diagnostic reason; never silently retrofit geometry.
- `is_final` does **not** independently establish contiguous market candle coverage, a known research limitation.

### Frozen R computations (verified against `app/research/evaluator.py`, `app/research/prospective_evaluator.py`)
- `return_at_240m = (last_close_in_(signal, signal+240m] - entry) / entry * 100` (raw direction-agnostic %).
- `risk = |reference_price - invalidation_price|`; `target_1` must equal exactly 2R (`|target - entry| / risk = 2`, tolerance 1e-5) or the row is rejected.
- `TP_FIRST -> +2.0 R`, `SL_FIRST -> -1.0 R`.
- Same-candle TP+SL collision (`ambiguous_intrabar`) under frozen `STOP_FIRST` resolves to the stop: `-1.0 R`.
- `TIMEOUT -> R = sign(direction) * return_at_240m * entry / (100 * risk)` (SHORT flips sign).

## Event selection and attrition
Matching requires exact event ID, direction, symbol, baseline time <= Point-B time and both outcomes finalized. Reject duplicates; report Point-B-only, baseline-only, identity and time-order violations. The paired estimand **conditions on Point-B completion**. Missing completion and asymmetric observation counts are essential funnel data, not evidence of causality.

## Final matched OOS results (frozen snapshot 2026-10-21T13:40:50.808807Z, after P2 repair v2)
Snapshot: one `BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` (verified `transaction_read_only=on`, role `trad_bot`, `pg_current_snapshot()=1894134:1894134:`), taken after the 14-day gate (2026-10-20T14:00:00Z): OOS elapsed 359.68 h (14.99 d), 16 UTC calendar dates (2026-10-06 .. 2026-10-21).

Lineage funnel (conservation exact): 19 112 source event identities = 18 753 matched finalized + 50 matched excluded (47 immature, 3 mature-but-unfinalized) + 305 Point-B-only (LONG 127 / SHORT 178) + 0 baseline-only + 4 invalid chronology (`baseline_after_point_b`, excluded with event IDs) + 0 duplicate / cross-symbol / cross-direction collisions. Matched counts are identical before and after the repair (PAIRS_EXCLUDED_NEW_N = 0).

Classification: gross research R under `first_exit_v2` (first-exit chronology repair, see "P2 repair v2" below).

| Metric | LONG | SHORT |
|---|---:|---:|
| Matched finalized N | 8 870 | 9 883 |
| Point-B mean gross R | -0.015665 | +0.150499 |
| Baseline mean gross R | +0.202427 | +0.268763 |
| Paired delta R (mean) | -0.218092 | -0.118264 |
| Paired delta R (median) | 0.0 | 0.0 |
| Day-cluster 95% CI | [-0.31710, -0.11603] | [-0.22196, -0.01380] |
| Symbol-cluster 95% CI | [-0.28717, -0.14665] | [-0.17652, -0.06445] |
| Event-IID 95% CI (secondary) | [-0.24760, -0.18880] | [-0.14673, -0.09042] |
| Leave-one-day-out range (sign flips) | [-0.26729, -0.18982] (0) | [-0.14682, -0.07946] (0) |
| Max single-day share of total delta | 23.6% | 40.7% |
| Profit factor PB vs BL | 0.966 vs 1.462 | 1.406 vs 1.662 |
| **Verdict** | **NO_EDGE** | **NO_EDGE** |

- `LONG`: both day- and symbol-cluster CIs strictly below zero, all maturity gates passed -> `NO_EDGE`.
- `SHORT`: both cluster CIs now strictly below zero -> `NO_EDGE` (before the repair the day CI crossed zero -> `INCONCLUSIVE`).
- `OVERALL_INCREMENTAL_POINT_B_HYPOTHESIS = NOT_SUPPORTED_BOTH_DIRECTIONS`.
- Verdict policy: `NO_EDGE` requires mean <= 0 and both cluster CIs strictly < 0; `PROMISING` requires both > 0; otherwise `INCONCLUSIVE`; integrity failure -> `BLOCKED`.
- Bootstrap: cluster resampling with deterministic `seed=169`, 10 000 iterations; Kish effective clusters reported.

## P2 repair v2 (Codex review 5604710893 of 6de58c7ac1)
Two research-only classification defects were fixed; the frozen evaluator, historical outcomes and the frozen snapshot were **not** modified.

1. **First-exit chronology (P2-1)**: the frozen evaluator sets `ambiguous_intrabar` on ANY candle spanning both levels, including candles *after* the first exit was established, and `STOP_FIRST` then resolves that candle as the stop. The repaired classification instead prioritizes (a) the captured first-exit chronology (`time_to_tp`/`time_to_sl`, comparable minutes-from-signal), (b) frozen `STOP_FIRST` only for a true same-candle first-exit collision (equal first-hit times + `ambiguous_intrabar`), (c) fail-closed `UNRESOLVED_FIRST_EXIT_ORDER` for irreconcilable evidence (excluded and counted; 0 rows on this snapshot).
2. **Variant geometry (P2-2)**: outcomes evaluated on a complete `variant_entry/variant_stop/variant_target` tuple are excluded fail-closed unless the tuple is exactly equal to the reference geometry; partial tuples are `INVALID_VARIANT_GEOMETRY`; outcomes with unestablishable geometry are `UNVERIFIED_OUTCOME_GEOMETRY`. On this snapshot `variant_none_n = 37 919` and all variant counters are 0 (re-verified from raw snapshot rows), so the gate is preventive hardening here.

Impact counters (full set in `HTF_POINT_B_P2_REPAIR_IMPACT_V2.json`): SOURCE_SNAPSHOT_ROWS 37 919; MATCHED_FINALIZED_BEFORE = AFTER = 18 753; TP_FIRST_RECLASSIFIED_N **556** (all `late_ambiguity_after_tp`, -1R -> +2R each) / SL_FIRST_RECLASSIFIED_N 0; LATE_AMBIGUITY_AFFECTED_N 1 493; SAME_CANDLE_AMBIGUITY_N 773; UNRESOLVED_ORDER_N 0; PAIRS_EXCLUDED_NEW_N 0; PAIRS_RECLASSIFIED_N 538 (LONG_DELTA_CHANGED_N 285 / SHORT_DELTA_CHANGED_N 244). Changed rows: 414 baseline legs (LONG 221 / SHORT 193) and 142 Point-B legs (LONG 72 / SHORT 70) - the pre-repair flags had attributed proven TP-first outcomes to the stop mostly on the baseline cohort, overstating the incremental Point-B result; after the repair the negative verdict strengthens.

### Before / after (before = frozen flag semantics, regression reference)
| Metric | LONG before | LONG after | SHORT before | SHORT after |
|---|---:|---:|---:|---:|
| Matched N | 8 870 | 8 870 | 9 883 | 9 883 |
| Paired delta R (mean) | -0.167697 | -0.218092 | -0.078802 | -0.118264 |
| Day-cluster 95% CI | [-0.25836, -0.06947] | [-0.31710, -0.11603] | [-0.17874, +0.02371] | [-0.22196, -0.01380] |
| Symbol-cluster 95% CI | [-0.23396, -0.10035] | [-0.28717, -0.14665] | [-0.14056, -0.02156] | [-0.17652, -0.06445] |
| Verdict | NO_EDGE | NO_EDGE | INCONCLUSIVE | NO_EDGE |

Overall: `NOT_SUPPORTED_LONG__INCONCLUSIVE_SHORT` -> `NOT_SUPPORTED_BOTH_DIRECTIONS`. The before-column reproduces the previously published frozen-flags result exactly (regression reference; not preserved artificially).

## Unmatched and selection bias
The estimand is event-conditional on matched, finalized events. It does not establish unconditional strategy superiority, does not evaluate signals without completed Point-B, and produces no net/fee-aware conclusion. Point-B-only events (305) remain in the funnel; unobserved Point-B outcomes are never imputed. Point-B entry occurs on average ~145 minutes after baseline entry (median 105-115 min) and entry price / risk geometry differ (identical entry price only for 47 LONG / 34 SHORT pairs): the cohorts are matched by underlying event, **not** by identical trade geometry.

## Maturity gates (all passed)
18 753 >= 300 matched events; LONG 8 870 >= 100; SHORT 9 883 >= 100; 112 distinct symbols >= 30; 359.68 h >= 336 h. Calendar-date count (16) is tracked separately and is **not** a substitute for elapsed hours; few independent day clusters remains a limitation.

## Reproducible read-only run (VPS isolated worktree)
```bash
cd /opt/trad_bot_issue169
git fetch origin
git checkout --detach origin/research/htf-point-b-final-paired-oos-verdict-v1

PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
/opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_htf_point_b_final_paired_oos_verdict_v1.py \
  tests/test_htf_point_b_paired_report_v1.py \
  tests/test_htf169_final_paired_adversarial_v1.py --tb=short

# capture: single REPEATABLE READ READ ONLY transaction (refuses non-empty out dir)
/opt/trad_bot/.venv/bin/python -m tools.research.htf_point_b_final_paired_snapshot_and_report_v1 \
  capture --out /tmp/htf_issue169_repro --worktree /opt/trad_bot_issue169 --config-root /opt/trad_bot

# report: fully offline from the frozen snapshot (refuses artifact overwrite)
/opt/trad_bot/.venv/bin/python -m tools.research.htf_point_b_final_paired_snapshot_and_report_v1 \
  report --out /tmp/htf_issue169_repro --reps 10000 --seed 169 \
  --git-research-head <research HEAD> --git-production-head <production HEAD>
```

Snapshot capture uses **REPEATABLE READ READ ONLY** with a read-only SQL guard; `report` is offline and deterministic. Service code, registry, paper/live and research outcomes stay untouched.

## Canonical artifacts and SHA-256 (dir `/tmp/htf_issue169_final_20261021/`, untouched by the P2 repair)
```text
de9c341fde0db1e5f8ffdacf9299dad2a0f13fe7c9689083a4c7c4b0aad95fc0  HTF_POINT_B_FINAL_PAIRED_DAILY_V1.csv
8bbed1f8fb4501bc82315ee825f016493e943e0abbc0fdfc8b4a946720234522  HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.csv
ba6dc66d4886788e49ade6b3b66b3cbd34d3fdefb8de5cc24d44380bcb30182a  HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.json
2ea5d5defc85772d6bf5741899aa55a8bc05011de835375f4928b4d6228aad50  HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.md
7302ff065512622c89ade3e0a744b64363e477f9b4ada6ab9906311ec2094233  HTF_POINT_B_FINAL_PAIRED_SOURCE_MANIFEST_V1.json
541ef48a0a613191b288359807a3a1d3747afe8cd690336b6e91af22028a7de5  HTF_POINT_B_FINAL_PAIRED_SYMBOL_V1.csv
0d156734776a3b1a85fd914d7e5ef22131d546301a7ec26dbdeb68e99a13fa92  HTF_POINT_B_FINAL_PAIRED_UNMATCHED_V1.csv
2f8b0c3ebad56a21a049ec18f08b3efdcc0e7d3c76d6a2e3842fbb4e80a6c66f  SHA256SUMS.txt
```

## P2 repair v2 artifacts (dir `/tmp/htf_issue169_p2_repair_v2_20261021/`)
Recomputed from the same frozen snapshot with `first_exit_v2` classification; exact SHA-256 values are listed in that directory's `SHA256SUMS.txt` and in the PR #171 repair comment (byte-identical Linux/Windows). Set:
`HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.{csv,json,md}`, `HTF_POINT_B_FINAL_PAIRED_{UNMATCHED,DAILY,SYMBOL}_V1.csv`, `HTF_POINT_B_FINAL_PAIRED_SOURCE_MANIFEST_V1.json`, `HTF_POINT_B_P2_REPAIR_IMPACT_V2.{json,md}`, `HTF_POINT_B_P2_REPAIR_CHANGED_ROWS_V2.csv` (556 deterministic audit records), `HTF_POINT_B_P2_REPAIR_VARIANT_AUDIT_V2.csv` (37 919 rows), `SHA256SUMS.txt`. The frozen snapshot copies there remain `snapshot_rows.csv = 53acc6f588eb08992101383657bbccf30d05ad279b367210897a00f3fd481f25`, `snapshot_meta.json = 5c42768c633e8d931e343341a8cf85955ffdbdba9d9933b0f0fe9a2b00cb62ed`.

Exact source revisions: research HEAD `f5d5ea98e121407185b8b49965c556ebab29483f`, production audit baseline `4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba`. Frozen snapshot payload: `snapshot_rows.csv` = `53acc6f588eb08992101383657bbccf30d05ad279b367210897a00f3fd481f25`, `snapshot_meta.json` = `5c42768c633e8d931e343341a8cf85955ffdbdba9d9933b0f0fe9a2b00cb62ed`.

Deterministic reproducibility: the offline `report` mode regenerates JSON and all four CSVs **byte-identical** across Linux and Windows. The regenerated MD differs only in 3 display strings where the archived canonical MD carries a text-encoding artifact (`вЂ”` instead of a dash) inherited from the tool copy that produced it; no number, count, CI or verdict differs (the JSON is byte-identical), and the archived canonical artifacts were not modified.

## Acceptance / caution
- Record as-of timestamp, exact counts and all exclusion reasons. Do not re-use earlier mixed snapshots with drifting N.
- Report LONG and SHORT separately; paired mean difference is `Point-B R - baseline R`.
- Bootstrap full **UTC days** and symbols as clusters, deterministic seed. Only 14 calendar dates were present on October 19; 14 elapsed days since freeze ends **2026-10-20 14:00 UTC**.
- Check adverse concentration / leave-one-day-out, non-independent overlapping positions and inability to infer net tradeability.
- `NO_EDGE` is allowed only when both day and symbol cluster 95% CIs are entirely below zero and elapsed duration gate passed. Otherwise `INCONCLUSIVE`.
- No claim of exchange costs, net edge, executable fills, or generalization to touches without Point-B.

## Provisional 2026-10-19 checkpoint (superseded; earlier snapshot, different N)
- LONG N=6301; delta -0.187342R; day CI [-0.232836, -0.140013].
- SHORT N=7306; delta -0.040648R; day CI [-0.153112, +0.058178].
- Early interpretation (directionally confirmed by the final run): LONG negative incremental; SHORT inconclusive.

**Never deploy to paper/live or mutate the research lifecycle from this Issue.**
