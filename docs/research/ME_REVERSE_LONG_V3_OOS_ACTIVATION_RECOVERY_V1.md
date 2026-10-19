# ME reverse LONG V3 — activation recovery review (19 Oct 2026)

**Experiment:** `MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS` (not `ME_R_LONG_EARLY_MAE_EXIT_OOS_V3`).

## Reason and evidence

An operator-supplied audit dated 14 Oct reports NATURAL_E2E_VALIDATED / CLEAN_OOS_ACCUMULATING at the original freeze `2026-10-13T12:37:03Z`. Live read-only SQL on 19 Oct showed `research.prospective_experiment.status=RUNNING` with `started_at=2026-10-13T12:37:03Z`. There were 73 observations in 21 symbols and 73/73 FINALIZED shadow execution outcomes (52 PASS, 21 REJECT); last observation 17 Oct 02:43 UTC. The current tracked registry was INACTIVE with null freeze and start boundaries.

The scanner's `ProspectiveOOSObserver._load_active_registry` skips INACTIVE experiments, and the V3 dedicated startup evaluator checks an exact registry/DB boundary match. The patch restores **only** the V3 registry entry to RUNNING with both timestamps equal to the existing DB timestamp.

## Approval boundary

**This branch and draft PR do not authorize production activation.** Prior to merging or deploying, the operator must confirm the original 13 Oct approval and audit provenance and explicitly approve activation recovery, per `docs/research/ME_REVERSE_LONG_V3_RELEASE_GATES.md`.

No SQL migration, UPDATE, historical backfill, paper/live routing, or production restarts are performed by this patch. No thresholds, geometry, cost model, entry rules, or existing outcomes are changed.

## Validation / go-no-go checklist

1. Confirm the approved initial boundary from operator evidence, and independently verify DB status and boundary.
2. Verify GitHub CI and the new `tests/test_me_reverse_long_v3_oos_activation_recovery.py` plus existing activation, evaluator, observer and prospective registry tests. If any regression expects V3 INACTIVE by design, amend the test to distinguish the historical default from the now-authorized activated release rather than suppress it.
3. Verify the exact diff touches only the V3 registry entry, this test and this review document.
4. Obtain **separate explicit approval** to merge/deploy; after that, controlled deployment of main and scanner/evaluator restart only with operator authorization. Verify logged V3 evaluator activation and scanner observer routing.
5. READ-ONLY before/after DB audit: original 73 outcomes unchanged, no duplicate IDs, no pre-freeze contamination, and no backfill of missing periods (17–19 Oct). New observations must be naturally arriving, strictly post-freeze, and FINALIZED only with complete 48-candle coverage.
6. Check other OOS health. Separately investigate pg8000 connection EOF failures; they are not fixed by this branch.

**Rollback:** if activation checks fail, do not alter the DB boundary or rewrite outcomes; preserve the original cohort and halt further rollout for diagnosis. Any lifecycle state changes or service restarts require explicit operator authorization.
