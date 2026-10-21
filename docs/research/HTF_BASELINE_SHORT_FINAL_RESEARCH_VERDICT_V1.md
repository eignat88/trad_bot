# FINAL_RESEARCH_VERDICT_V1 — HTF BASELINE SHORT causal observer

**Issue:** [#176](https://github.com/eignat88/trad_bot/issues/176)  
**Draft PR:** [#177](https://github.com/eignat88/trad_bot/pull/177)  
**Disposition:** `HISTORICAL_RESEARCH_DIAGNOSTICS_COMPLETE / PHASE_2_NO_GO / NO_MERGE / NO_DEPLOY`.

## Evidence boundary

Research-only, detached VPS worktree `/tmp/trad_bot_issue176`, historical BTCUSDT and ETHUSDT 5m candles with warm-up. Scoped confirmations: **2026-10-14T00:00:00Z through 2026-10-17T00:00:00Z** (end exclusive). No new prospective OOS, live/paper entries, order fills or net execution study.

Operator VPS at HEAD `2a1e122c`: **15 tests passed**, final readiness report generated; event integrity PASS on each symbol; no production modifications.

## Scope-reconciled counts

| Measure | BTCUSDT | ETHUSDT | Combined |
|---|---:|---:|---:|
| Confirmed events within scoped historical replay | 20 | 17 | **37** |
| Post-eligibility-loss unique confirmed IDs over whole warm-up+replay processing | 15 | 19 | **34** |
| Post-loss confirmations **in the scoped window** | 3 | 3 | **6** |
| Post-loss confirmations **outside the scoped window** | 12 | 16 | **28** |
| Scoped confirmations without a recorded eligibility loss | 17 | 14 | **31** |
| Previously audited unique pending episode IDs affected by loss (all replay history) | 87 | 118 | **205** |
| Previously audited raw eligibility-loss-with-pending records | 116 | 157 | **273** |

The 34 post-loss confirmations **must not** be compared directly with 37 scoped emissions: they have different time boundaries. These are episode/event diagnostic IDs, **not independent trades**. A post-loss confirmation shows unsafe assumptions about unconditional pending deletion; it does not establish that all hypothetical retirement policies would lose it.

Historical selected-once/full-universe **37/37** event ID parity and provisional ASOF batch grouping **24 clusters** (BTC 16 / ETH 8, ambiguous 0) passed preceding bounded audits. Separate-process combined restart passed both symbols at earlier stage. Checkpoint interrupted-write-before-replace test passed for both symbols; this is **not** proven recovery from a corrupted committed checkpoint or a transactional side-effect outbox.

## Final gate assessment

| Gate | Verdict |
|---|---|
| Historical causal chronology/event integrity | **PASS in bounded window** |
| Episode ID set parity across two research modes | **PASS 37/37** |
| Historical replay and combined separate-process restart | **PASS in tested scenario** |
| Provisional atomic-ASOF grouping | **PASS in tested scenario; geometric hypothesis not approved** |
| Pending vs eligibility evidence | **PASS as diagnostic; risk documented** |
| Scope reconciliation (warm-up vs 14–17 Oct) | **PASS** |
| Safe terminal HTF retirement rule | **BLOCKED — not established** |
| Corrupt-committed-checkpoint fallback / concurrent writers / external exactly-once | **NOT PROVEN / NOT IMPLEMENTED** |
| Approved causal level selector and cluster geometry | **NOT APPROVED** |
| Prospective observer readiness | **NO-GO** |
| Independently costed executable edge | **NOT ESTABLISHED; separate Issue #170** |

## Explicit conditions to reopen Phase 2 decision

1. Specify and independently review prospective HTF-level eligibility, invalidation/retirement, re-eligibility, and pending preservation. Reject blanket `NOT_CURRENTLY_ELIGIBLE ⇒ LEVEL_RETIRED`.
2. Design and validate state + event delivery durability and idempotency, including corrupt-checkpoint fallback, writer exclusivity, and recovery without duplicate externally delivered events.
3. Approve/freeze new causal HTF selector, 5m ASOF batching, independent event/cluster identity, 20bps proximity sensitivity and strict post-freeze observation boundary. Preserve immutable protocol and repository commit.
4. Conduct a fresh readiness/security/causality audit and obtain explicit operator authorization before any prospective observer deployment or OOS. No historical records become OOS.
5. Keep net executable-edge research under Issue #170 separate from observer readout.

## GitHub branch disposition

- Active research branch: `research/htf-baseline-short-stateful-episode-v1` → Draft PR #177. **Keep OPEN / DRAFT / UNMERGED**.
- Previous Phase-1 branch: `research/htf-baseline-short-causal-event-coverage-v1` → PR #175 **closed unmerged**; historical diagnostics only.
- Issue #176: **Keep OPEN, BLOCKED FOR PHASE 2**. Historical work package documented as complete; unresolved readiness conditions remain explicit.
- Production `main` and frozen `app/research/htf_keylevel_point_b.py` unchanged. Scanner/config/evaluator/systemd/DB/paper/live and existing Point-B OOS unchanged.

**Final decision: `PHASE_2_NO_GO`.** Do not merge/deploy or close Issue #176 as production-ready.
