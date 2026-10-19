# Issue #174 — causal BASELINE SHORT event coverage (Phase 1)

Research-only diagnostic prototype. **No production integration, DB writes, trading orders, or OOS evidence.**

## Why
All 7,331 historical BASELINE SHORT observations were recorded after Point-B. Their historical `signal_time` is not executable evidence. The earlier causal observer prototype detected no fresh confirmations in its bounded BTC test. Need isolate warmup/window effects, first-seen coverage, and whether fresh reactions genuinely occur.

## Files
- `tools/research/htf_baseline_short_causal_coverage_v1.py`: offline CSV replay by successive ASOF prefixes, closed hourly aggregation, 200/400 execution-window sensitivity; JSON output with prefix counts and distinct first-seen event IDs.
- `tests/test_htf_baseline_short_causal_coverage_v1.py`: closed-hour, missing-hour, as-of, no-duplicate-event and gapped-source guards.
- Contains prior unmerged causal observer prototype as a base. Nothing activated.

## Boundaries and limitations
- Source CSV must be independently fetched/verified. Neither the script nor its tests fetch historical Bybit candles.
- The full hourly source uses all previously closed bars, capped to 200. Window sensitivity changes 5m execution window only; not a controlled independent sample.
- All emitted candidate statistics are diagnostic only, NOT OOS, not profitability, not unique trades unless explicitly `unique_events`.
- `known_at_ms` is the model confirmation time, not proof of exchange fill or actual observer persistence.
- Missing historical candles reject the entire symbol CSV; hourly coverage additionally requires 12 contiguous component 5m bars.
- The first-seen ledger is in-memory for this offline run. Deployment needs a persistent dedup key/transaction and strict new freeze boundary.

## VPS read-only test workflow
```bash
cd /opt/trad_bot
git fetch origin research/htf-baseline-short-causal-event-coverage-v1
git worktree add --detach /tmp/trad_bot_issue174 origin/research/htf-baseline-short-causal-event-coverage-v1
cd /tmp/trad_bot_issue174
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_keylevel_baseline_short_causal_observer_v1.py tests/test_htf_baseline_short_causal_coverage_v1.py --tb=short
```

Example, after placing vetted CSV input under `/tmp/htf_coverage_174/`:
```bash
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_causal_coverage_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_174_coverage.json
```

## GO/NO-GO
Pending reproducible BTC/ETH source verification and runner execution. **Not deployable.** No threshold changes, main merge, freeze, systemd, config, evaluator, scanner or paper modifications.

## Window divergence found (2026-10-19)

BTCUSDT at 2026-10-14 12:35 UTC: 200-bar window chose HTF resistance 83260.1, touch 2026-10-14 10:10; 400-bar window chose resistance 82910.8, touch 2026-10-13 03:20. Both contained many eligible touch candidates. Root cause is source-window dependence of initial close, ATR initialization, level eligibility and first-touch ordering. In the initial run BTC events 10 vs 1; ETH 7 vs 7 with mostly differing timestamps/touches.

### Diagnostic normalization patch (not Phase-2 implementation)

The research-only causal observer now restricts inputs to the **last 200 closed 5m candles**, irrespective of an upstream 200/400 supply. Tests check that extending the earlier prefix does not change the observer's output. This freezes comparison semantics for the same rolling 200 context; it does NOT make HTF levels immutable across later live scans, and it does NOT establish globally stable setup identity. The touch-level canonicalization, event lifecycle and independent prospective detection/persistence remain unimplemented. No change was made to the original frozen Point-B detector.

**Expected:** comparing runner windows 200 and 400 will now match because both evaluate the same trailing execution window. This is a deliberate normalization, **not** evidence that window sensitivity is scientifically resolved or that an execution edge exists. Do not merge or deploy based only on equal counts.

Existing historical replay results and 25-record timestamp audit remain archived as pre-patch observations, not validated prospective OOS. Run the new tests and BTC/ETH replay again, and record complete results before Phase-1 GO/NO-GO.

## Phase-1 event ledger candidate (not a production dedup gate)

The offline runner now enriches each emitted first-seen signal with `event_id`, `level_id` (symbol, direction, formation/availability timestamp and price), touch time, selected 5m level-contact episode, left-censor flag, reaction time and first-seen time. An episode is a **maximal contiguous run** of 5m candles meeting the same frozen proximity/touch predicate for the **same selected HTF level**. This is a narrow, falsifiable operational definition — not proof that separated episodes are statistically independent setups.

Output includes `unique_htf_levels`, `unique_touch_episodes`, `same_episode_excess_signals` and `episode_groups`; raw `unique_events` remains the original number of emitted distinct signal IDs. An event linked to a touch episode whose start is outside the 200-bar window may be left-censored or misclassified. HTF level reselection between as-of decisions can fragment a true physical touch; levels with different IDs might be geometrically equivalent. **The ledger makes these differences visible but cannot fully repair them yet.**

After VPS tests, replay the original October 14–17 BTC/ETH CSV input to `/tmp/htf_174_coverage_episode_ledger.json`, retaining the pre-patch file as baseline. Report the actual count of contact episodes and signals per episode separately for each symbol. Do not describe this count as proven independent events or OOS trades. No merge, no deploy, no edge verdict before reviewing replay and remaining event-identity sensitivity.

## EPISODE_IDENTITY_HARDENING_V1 — follow-up (pending VPS)

An offline diagnostics module now computes the **first confirmation per exact episode_id**, counts subsequent signals suppressed by that rule, and fails closed for left-censored episodes, contradictory episode starts and collisions across levels. A second diagnostic tracks, for each signal, when its episode start exits the rolling 200-bar execution window. These outputs are in each window's `identity_hardening` and `rolling_visibility` JSON fields; no signal emission or trading is altered.

**Scope constraint:** sliding visibility is NOT repeated live-style recomputation of eligible HTF levels, contacts and `episode_id` at all ASOF points. Same-level/price reactions with noncontiguous contacts can still be dependent; changing level selection can fragment episodes. Do not claim these counts as proven independent setups or approve Phase 2 on this diagnostic alone. Further full rolling-prefix recomputation and cross-level collision audit may be necessary.

VPS validation (isolated worktree only):
```bash
git fetch origin research/htf-baseline-short-causal-event-coverage-v1
git checkout --detach origin/research/htf-baseline-short-causal-event-coverage-v1
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_htf_keylevel_baseline_short_causal_observer_v1.py tests/test_htf_baseline_short_causal_coverage_v1.py tests/test_htf_baseline_short_episode_ledger_v1.py tests/test_htf_baseline_short_episode_identity_hardening_v1.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m tools.research.htf_baseline_short_causal_coverage_v1 --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z --out /tmp/htf_174_hardening_v1.json
```
