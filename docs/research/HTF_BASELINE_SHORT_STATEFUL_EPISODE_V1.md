# Issue #176 — State machine prototype (research only)

This is Phase-A scaffolding, **not** a complete causal observer, historical signal pipeline or production-eligible implementation.

## Implemented
- Deterministic closed-5m cursor and gap/duplicate rejection.
- Stateful `level_id` and contiguous contact runs; a new recontact creates a new ID.
- Persistable JSON snapshot with old episode IDs, last candle cursor and first confirmations.
- Pending reaction confirmations can arrive after a contact ends.
- Explicit rejection of a contact with a not-yet-available level; synthetic regressions for restart, eviction and level replacement.

## Explicit blockers
1. HTF level builder and touch/reaction detector are **not wired**. The caller currently provides `LevelSnapshot`, `touch`, and `reaction_confirmed`; values must later be generated from strict closed 1h/5m ASOF data and frozen causal geometry.
2. Pending reaction handling has no complete termination/invalidation semantics, and a new same-level contact can supersede a pending unconfirmed reaction. Event lifecycle definition and ambiguity audit required.
3. JSON snapshot is a research fixture format, not transactional durable storage or exactly-once production delivery. File atomicity, concurrent writers, process recovery, overlap processing and fault injection still required.
4. The state machine rejects duplicate bars rather than treating them as idempotent inputs. Production replay scheduler should gate by persisted cursor.
5. No real BTC/ETH rolling-ASOF replay, independent-prefix audit or Phase-2 acceptance has been run on this branch. Past 17 raw / 14 touch episodes are **historical diagnostic** numbers from #174, not new stateful results.
6. Does not change frozen Point-B geometry, trade execution, main, DB or services.

## Isolated VPS tests
```bash
cd /opt/trad_bot
git fetch origin research/htf-baseline-short-stateful-episode-v1
git worktree add --detach /tmp/trad_bot_issue176 origin/research/htf-baseline-short-stateful-episode-v1
cd /tmp/trad_bot_issue176
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_stateful_episode_v1.py --tb=short
```

## Gate
Remain **RESEARCH_ONLY / PHASE_2_NO_GO**. Follow #176 acceptance criteria and obtain explicit separate approval before any deployment or clean prospective capture.

## New integration prototype (unvalidated on VPS)
- `app/research/htf_baseline_short_causal_level_adapter_v1.py`: a proposed as-of level selector using only fully closed 1h bars, confirmed 4-right swing, frozen touch count/tolerances, and the latest closed 5m reference close instead of `execution_candles[0].close`. **This changes level-selection semantics and is research-only, not frozen production policy.**
- `tools/research/htf_baseline_short_stateful_replay_v1.py`: sequential one-closed-5m-at-a-time driver, 200-bar reaction evidence, existing frozen `_find_reaction_end`, state-machine `advance`, bounded BTC/ETH JSON output. Warm-up from first available archive candle; full prefix and truncated HTF are known at each decision.
- `tests/test_htf_baseline_short_stateful_replay_v1.py`: synthetic source-boundary and runner tests.

### Critical limitations
- It selects a single nearest eligible HTF resistance **at each ASOF**. A previously active level can become unselected; pending confirmation continuity and full level retirement are not solved. JSON counters expose unselected pending levels.
- The provisional contact run ends on the first non-contact 5m bar. This is not yet a frozen definition of an independent market setup; multiple unconfirmed episodes can supersede pending entries.
- Reaction lookback capped at 200 candles. Pending start outside this window is explicitly counted and left unconfirmed; re-entry/persistence protocol is still to be designed.
- No durable file/DB state, transactional cursor, actual restart replay or independent prefix-equivalence evidence. Do not merge/deploy or classify signals as clean OOS.

### VPS validation — isolated worktree
```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_stateful_episode_v1.py tests/test_htf_baseline_short_stateful_replay_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_stateful_replay_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_176_stateful_replay.json
```

## Pending-level lifecycle repair prototype — unverified on VPS

Added research-only parallel implementation `app/research/htf_baseline_short_multi_level_lifecycle_v1.py`, driver `tools/research/htf_baseline_short_multi_level_replay_v1.py` and regression tests. Existing single-level runner left in place as baseline. Multi-level replay tracks **all levels historically selected at least once** and independently checks their pending reactions on each closed 5m bar. This directly addresses the prior per-ASOF selected-level-only blind spot.

Termination policy is intentionally conservative: CONFIRMED is emitted once; SOURCE_WINDOW_EVICTED and RECONTACT_SUPERSEDED_PENDING are explicit UNRESOLVED reasons. No arbitrary timeouts or unproven INVALIDATED, EXPIRED, LEVEL_RETIRED judgments. Such terminal semantics remain future work and pending states can accumulate.

**Important**: selected-once level universe is not proof of complete simultaneous HTF eligibility. Causal 1h level creation for all available levels, alternate-level selection, invalidation and lifecycle retirement remain unfrozen. This replay is diagnostic, NOT OOS or evidence of executable edge. Run only in /tmp isolated worktree.

```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_multi_level_lifecycle_v1.py tests/test_htf_baseline_short_stateful_episode_v1.py tests/test_htf_baseline_short_stateful_replay_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_multi_level_replay_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_176_multi_level_replay.json
```

## MULTI_LEVEL_EVENT_CLUSTER_AND_RESTART_AUDIT_V1

Additional research-only script `tools/research/htf_baseline_short_event_cluster_restart_audit_v1.py` compares uninterrupted historical replay with a JSON checkpoint/restore **in the same process** at midpoint. Comparison covers emissions, counters, episode-status totals, pending totals and integrity checks. This is an initial deterministic restart equivalence audit, **not** independent process failure or transactional persistence testing.

Event records now carry `level_price_at_touch`; candidate near-collision pairs are detected if confirmation time differs by at most 5m and resistance prices are within 20 bps. These are **exploratory diagnostic thresholds, not a dedup/trade protocol**. Do not claim clustered pairs are duplicate market reactions or that events outside clusters are independent.

```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_multi_level_lifecycle_v1.py tests/test_htf_baseline_short_multi_level_terminal_policy_v1.py tests/test_htf_baseline_short_stateful_episode_v1.py tests/test_htf_baseline_short_stateful_replay_v1.py tests/test_htf_baseline_short_event_cluster_restart_audit_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_event_cluster_restart_audit_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_176_event_cluster_restart.json
```

Phase-2 remains NO-GO until all simultaneously eligible levels and independent external restart / prefix audits are verified.

## Reaction-cluster identity + separate-invocation disk restart (pending VPS verification)

`tools/research/htf_baseline_short_reaction_cluster_identity_v1.py` builds a deterministic connected-component identity for candidate pairs (≤5m, ≤20bps), preserving **all** event IDs, episode IDs, level IDs, first confirmed timestamp and first event. This cluster ID depends on all members and is therefore *retrospective, diagnostic only*; it MUST NOT be used as a prospective first-seen or trading dedup key. Transitive edges can combine events further than direct time/price tolerance.

`tools/research/htf_baseline_short_multi_level_replay_v1.py` now accepts `stop_at_ms`, `save_checkpoint` and `resume_checkpoint`; checkpoint stores episode state, known levels and counters. The CLI `tools/research/htf_baseline_short_disk_restart_audit_v1.py` supports baseline, stage1, stage2 and compare in **separate Python invocations** with local JSON. This is a synthetic process restart test, not transactional crash safety or concurrency verification.

The original 37 events remain a bounded historical diagnostic count, **not an OOS or independent trade sample**. Level universe is still only levels selected at least once. Frozen Point-B detector unchanged.

Validation:
```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_multi_level_lifecycle_v1.py tests/test_htf_baseline_short_multi_level_terminal_policy_v1.py tests/test_htf_baseline_short_stateful_episode_v1.py tests/test_htf_baseline_short_stateful_replay_v1.py tests/test_htf_baseline_short_event_cluster_restart_audit_v1.py tests/test_htf_baseline_short_reaction_identity_disk_restart_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_event_cluster_restart_audit_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_176_cluster_identity.json
```

## FULL_CAUSAL_LEVEL_UNIVERSE_AND_FIRST_SEEN_CLUSTER_V1 (VPS pending)

New as-of function `causal_resistance_universe` lists all resistance candidates satisfying the *proposed* causal swing, touch-count, price-above-last-close and available-at-ASOF rules. Existing nearest-level method delegates to this list. `full_universe=True` in the **research-only** replay retains every candidate seen at any past ASOF, not just the selected one; original default mode remains available for direct comparison.

**Not yet full production-level lifecycle:** levels remain in the known registry after they cease satisfying the current reference-price eligibility. A complete causal level retirement/invalidation policy has NOT been proven. Expanding the observable universe also changes touch counts and may increase unresolved episodes. Historical Point-B remains frozen and untouched.

`FirstSeenReactionClusters` creates immutable cluster IDs anchored to the **first received event** and attaches subsequent confirmations using only already-known anchors and a provisional (5m/20bps) threshold. Bridges matching two existing anchors are left `AMBIGUOUS_BRIDGE` rather than merging/rekeying old clusters. This is a *proposal*, not validated independence or approved OOS event dedup. Retrospective connected-component cluster IDs must not be substituted as first-seen IDs.

Audit script: `tools/research/htf_baseline_short_full_universe_cluster_audit_v1.py`, produces JSON comparing selected-once and full-universe counts and provisional anchors. Tests: `tests/test_htf_baseline_short_full_universe_first_seen_v1.py`.

VPS:
```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_full_universe_first_seen_v1.py tests/test_htf_baseline_short_multi_level_lifecycle_v1.py tests/test_htf_baseline_short_multi_level_terminal_policy_v1.py tests/test_htf_baseline_short_stateful_episode_v1.py tests/test_htf_baseline_short_stateful_replay_v1.py tests/test_htf_baseline_short_event_cluster_restart_audit_v1.py tests/test_htf_baseline_short_reaction_identity_disk_restart_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_full_universe_cluster_audit_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_176_full_universe_first_seen.json
```

Gate stays **PHASE_2_NO_GO**, Draft PR only, no deployment.

## FIRST_SEEN_CLUSTER_BRIDGE_RESOLUTION_V1 — diagnostic audit

`tools/research/htf_baseline_short_bridge_parity_v1.py` compares exact emitted `event_id` sets across selected-once and full-universe modes and captures each fail-closed ambiguous first-seen bridge with complete causally observed confirmation and both competing anchor metadata. CLI: `tools/research/htf_baseline_short_bridge_parity_cli_v1.py`.

No ambiguity resolution is automatically applied. Two ETH historical ambiguous bridges remain to be investigated on VPS. Counts alone (20/20 BTC, 17/17 ETH) do not prove equality of event sets. Clustering remains provisional, not an execution filter or proven independence.

```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_bridge_parity_v1.py tests/test_htf_baseline_short_full_universe_first_seen_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_bridge_parity_cli_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_176_bridge_parity.json
```

## EPISODE_ID_PARITY_ROOT_CAUSE_AND_ASOF_BATCH_CLUSTER_V1 — VPS pending

Research-only evidence tools:
- `htf_baseline_short_episode_id_parity_root_cause_v1.py` matches mismatched IDs by level_id + confirmation ASOF + level_price; compares first_touch timestamp. This is an attribution diagnostic: matching geometry does NOT prove trade independence or full episode semantic equivalence.
- `htf_baseline_short_asof_batch_cluster_audit_v1.py` groups confirmations by identical ASOF, calculates within-batch candidate edges before processing and checks matches to anchors existing **before** that batch. It DOES NOT change production cluster allocation, automatically merge bridges or resolve sequence-dependence.
- `htf_baseline_short_parity_batch_cli_v1.py` generates a bounded read-only report for BTC/ETH.
- `tests/test_htf_baseline_short_episode_id_parity_asof_batch_v1.py` includes synthetic parity and batch cases.

Research only, no changes to frozen Point-B, production, OOS registry or scanners. Full-universe level retirement and prospective batch allocation remain **NOT IMPLEMENTED**. Phase 2 NO-GO.

```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_episode_id_parity_asof_batch_v1.py tests/test_htf_baseline_short_bridge_parity_v1.py tests/test_htf_baseline_short_full_universe_first_seen_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_parity_batch_cli_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_176_parity_batch.json
```

## ASOF_ATOMIC_CLUSTER_ASSIGNMENT_AND_EPISODE_ID_STABILITY_V1

Research-only changes: `MultiLevelEpisodes` episode digest now uses `symbol|SHORT|level_id|first_touch_open_ms`, not the local `sequence`; retains sequence for compatibility with earlier checkpoint reports but it no longer determines an event ID. An identical causal first-touch identity is rejected rather than silently overwritten. **Historic hashed IDs deliberately change; do not mix old and new identity namespaces.** No stored production outcomes changed.

`AtomicAsofClusters` first groups all events at a closed 5m ASOF by a *provisional* 20bps connected-component relation. Only clusters committed at a STRICTLY EARLIER ASOF may act as prior anchors. Same-ASOF groups cannot influence each other. Multiple earlier anchors matching a component cause `AMBIGUOUS_PRIOR_ANCHORS` with no reassignment or anchor merger. Anchored IDs are still research proposals, not validated independent reactions or execution keys.

Historical full vs selected-once parity and new atomic grouping CLI:
```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_atomic_asof_identity_v1.py tests/test_htf_baseline_short_multi_level_lifecycle_v1.py tests/test_htf_baseline_short_multi_level_terminal_policy_v1.py tests/test_htf_baseline_short_reaction_identity_disk_restart_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_atomic_asof_parity_cli_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_176_atomic_asof_parity.json
```

No OOS, production, frozen detector, scanner, paper or main changes. **PHASE_2_NO_GO** until VPS validation, full-universe lifecycle retirement and proposed batch dedup protocol review.

## HTF_LEVEL_RETIREMENT_AND_ATOMIC_RESTART_VALIDATION_V1 (research-only; VPS pending)

`LevelEligibilityAudit` tracks `DISCOVERED`, `NOT_CURRENTLY_ELIGIBLE` (reason `BELOW_OR_AT_REFERENCE` or `NOT_IN_CURRENT_ELIGIBLE_SET`), and `RE_ELIGIBLE` across consecutive closed 5m ASOFs. Eligibility loss is **NOT** proven `LEVEL_RETIRED`/`LEVEL_INVALIDATED`; the audit does not change episode pending or known-level registry. The bounded historical CLI currently audits only the requested time window, so discovery at the window boundary is not full-warmup history. Pending indicators in this standalone eligibility audit are not wired to replay pending and should not be used as evidence of safe retirement.

`AtomicAsofClusters.checkpoint/restore` stores symbol, existing clusters, assignments, and last ASOF. `htf_baseline_short_combined_restart_v1.py` tests full-universe episode replay state plus cluster state through **separate Python invocations** (baseline/stage1/stage2/compare) with JSON on local disk. The stage2 process continues episodes from their checkpoint and only delivers event IDs not already assigned by the cluster checkpoint. This is not atomic cross-file transaction or crash-safe recovery. Historical IDs use the new first-touch hash namespace. Do not reuse checkpoints from earlier commits.

VPS tests and replay must pass before any conclusion. Do not deploy or merge. Phase 2 remains NO-GO.

## HTF_PENDING_LIFECYCLE_AND_CRASH_SAFE_RECOVERY_V1 — combined research gate

This version introduces `pending_impact=True` in the full-universe historical replay: the loss of present ASOF eligibility is cross-referenced with real `MultiLevelEpisodes.pending` **before** advancing the candle. It records pending episode IDs at eligibility loss and subsequent confirmations for those same episodes. No level is retired, invalidated, or deleted automatically. This is read-only historical evidence; missing a confirmation later is not proof a retirement rule is safe. The impact structure lives inside the replay checkpoint for deterministic continuity.

`htf_baseline_short_atomic_checkpoint_v1.py` writes a single schema-versioned JSON envelope containing episode state, HTF levels, cursor/counters and atomic cluster state. It hashes canonical JSON with SHA-256, writes/flushed/fsyncs a temporary file, atomically `os.replace`s it, and fsyncs parent directory. It can fail before replace for fault injection. Validation rejects a changed hash or schema. **No claim of universal hardware durability, dual-generation fallback, safe concurrent writer lease, nor atomic linkage to any external execution side effects.** An already corrupt *committed* checkpoint intentionally raises instead of silently recovering, pending a verified backup generation policy.

`htf_baseline_short_pending_atomic_gate_v1.py` supports baseline, stage1, fault (attempt interrupted replace), stage2 and compare across separate Python processes in the detached research worktree. Tests and historical VPS replay pending. No production modification / OOS.

```bash
cd /tmp/trad_bot_issue176
git fetch origin research/htf-baseline-short-stateful-episode-v1
git checkout --detach origin/research/htf-baseline-short-stateful-episode-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_baseline_short_pending_atomic_gate_v1.py tests/test_htf_baseline_short_level_eligibility_combined_restart_v1.py tests/test_htf_baseline_short_atomic_asof_identity_v1.py --tb=short
for symbol in BTCUSDT ETHUSDT; do
  for phase in baseline stage1 fault stage2 compare; do
    PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_pending_atomic_gate_v1 --phase "$phase" --data-dir /tmp/htf_coverage_174 --symbol "$symbol" --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --prefix "/tmp/htf_176_atomic_pending_${symbol}" || break
  done
done
```

**Phase 2 NO-GO**, pending combined VPS gate, independently reviewed retirement semantics, and final protocol readiness audit.
