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
