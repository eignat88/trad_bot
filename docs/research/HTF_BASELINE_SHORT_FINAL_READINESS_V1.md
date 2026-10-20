# Issue #176 — FINAL_PROTOCOL_FREEZE_AND_READINESS_AUDIT_V1

**Decision:** `PHASE_2_NO_GO`. Research-only, no merge/deployment/prospective OOS authorization.

**Evidence boundary:** isolated VPS research worktree, historical Bybit BTCUSDT and ETHUSDT 5m candles with warm-up, replay ASOF 2026-10-14 00:00 UTC–2026-10-17 00:00 UTC. Data is historical, NOT a fresh prospective holdout or executable P&L.

## Frozen-to-document research proposal (NOT an approved deployment freeze)

- Source levels: proposed causal closed-1h swing resistance universe and closed-5m reference; selected-once/full-universe historical comparisons.
- Close and chronology: only closed 5m decisions; available level before first touch; confirmation observable no earlier than 5m bar close; closed 1h inputs; no future bars.
- Episode identity: SHA256 of `symbol|SHORT|level_id|first_touch_open_ms`. Older hash namespace containing sequence is incompatible.
- Lifecycle: CONTACT, pending, CONFIRMED, UNRESOLVED on recontact supersession or source-window eviction; rejection of candle overlap/gaps.
- Level eligibility: `NOT_CURRENTLY_ELIGIBLE` does NOT imply terminal retirement, and MUST NOT on its own delete pending. The diagnostic universe currently retains formerly seen levels, even when newly ineligible, so full prospective policy remains unapproved.
- Reaction identities: provisional 5m ASOF batch connected components using 20bps level-price proximity; only strictly earlier ASOF clusters qualify as pre-existing anchors. Multiple prior anchors → fail-closed ambiguity. The 20bps threshold and independent event definition remain research assumptions.
- State: single schema/hash-verified JSON checkpoint of episode state, known levels, counters/cursor, eligibility impact and ASOF cluster state. Temporary write + file fsync + replace + directory fsync, plus pre-replace fault injection. This does NOT provide database/outbox exactly-once or corrupt-committed-generation recovery.

## Historical VPS evidence supplied by operator

| Gate | Observed |
|---|---|
| Prior event ID parity | BTC 20/20, ETH 17/17; no unmatched IDs |
| Atomic ASOF grouping | BTC 16, ETH 8 provisional clusters; ambiguous 0 |
| Prior combined process restart | BTC 20 events PASS, ETH 17 PASS |
| Latest isolated tests | 13 PASS on HEAD 3211dbbe |
| Interrupted-write-before-replace | PASS both symbols |
| Combined baseline vs resumed | PASS both symbols |
| Observed raw confirmations | BTC 20, ETH 17 |
| Eligibility changes in pending-impact replay | BTC 167, ETH 217 |
| Eligibility-loss-while-pending records | BTC 116, ETH 157 |
| Post-loss confirmation records | BTC 15, ETH 19 |
| Pending at end | BTC 1, ETH 1 |
| Event integrity violations | 0 each |

**Accounting caution:** 116/157 counts are transition records, not unique affected episodes. 15/19 are records potentially requiring unique-ID checks. The eligibility-loss audit before this enhancement was standalone; its pending flag was not linked to episodes. Current `pending_impact` does cross-reference actual state; do not infer eventual confirmation of every impacted episode or use an eligibility change as a safe retirement trigger.

## BLOCKING readiness gates

1. **No approved terminal HTF retirement policy:** the historical replay documents potential lost confirmations following eligibility loss; a level is retained while continuity is needed. Permanent retirement would require separately evidenced conditions, including pending protection, re-eligibility and source expiry semantics.
2. **Persistence not end-to-end transactional:** the single JSON file is file-level atomic in tested scenario but not a crash-safe event delivery/outbox transaction. No multi-generation backup/recovery for an already corrupted committed checkpoint, writer leasing or external consumer replay exactly-once proof.
3. **Prospective causal protocol not approved:** level selector is newly proposed, cluster proximity 20bps is provisional; neither independently signed off as a prospective frozen policy.
4. **No net executable-edge proof:** historical observer confirmations and group counts do not constitute independent trades, fills, or executable expectancy. Execution research stays separately scoped in Issue #170.
5. **No prospective Phase-2 holdout or deployment permission.** Existing production Point-B OOS is an independent pipeline; nothing here modifies it.

## Read-only acceptance tool and rerun

```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
git status --short
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
/opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider \
 tests/test_htf_baseline_short_final_readiness_audit_v1.py \
 tests/test_htf_baseline_short_pending_atomic_gate_v1.py \
 tests/test_htf_baseline_short_atomic_asof_identity_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 \
/opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_final_readiness_audit_v1 \
 --btc /tmp/htf_176_atomic_pending_BTCUSDT.resumed.json \
 --eth /tmp/htf_176_atomic_pending_ETHUSDT.resumed.json \
 --out /tmp/htf_176_final_readiness.json
```

This consumes previously generated operator VPS JSON; it does NOT reexecute the historical replay. Rerun the independent combined-gate replay first if data provenance is uncertain. Test results for this **new HEAD are pending** until operator executes the commands.

**Lifecycle disposition:** Keep Issue #176 and Draft PR #177 OPEN / UNMERGED / NOT DEPLOYED with clear NO-GO readiness note. Final diagnostic work package is complete as a documented assessment; promotion must be a **new explicit approval** after resolving blockers. No work on frozen `app/research/htf_keylevel_point_b.py`, production `main`, DB/scanner/evaluator/systemd/paper/live.
