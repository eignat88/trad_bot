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
