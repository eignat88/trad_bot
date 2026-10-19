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
