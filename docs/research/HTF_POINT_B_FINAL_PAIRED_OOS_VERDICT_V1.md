# HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1 — Issue #169

## Objective
Evaluate **incremental Point-B uplift** compared with BASELINE on the identical underlying `setup_event_id` per direction. This is a **gross, event-conditional** research comparison, not a portfolio backtest or a deploy-to-trading authorization.

## Frozen source
- Point-B: `HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE`
- Control: `HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE`
- Strict freeze: `signal_time > 2026-10-06T14:00:00Z`
- Trade path: `SL=1R / TP=2R / STOP_FIRST / 240m`
- The evaluator's `return_at_240m` is a **raw, unsigned-for-direction price percentage return**. SHORT TIMEOUT R changes sign.
- If frozen policy or target 2R differs, **reject the row** with a diagnostic reason; never silently retrofit geometry.
- `is_final` does **not** independently establish contiguous market candle coverage, a known research limitation.

## Event selection and attrition
Matching requires exact event ID, direction, symbol, baseline time <= Point-B time and both outcomes finalized. Reject duplicates; report Point-B-only, baseline-only, identity and time-order violations. The paired estimand **conditions on Point-B completion**. Missing completion and asymmetric observation counts are essential funnel data, not evidence of causality.

## Reproducible read-only run (VPS isolated worktree)
```bash
cd /opt/trad_bot
git fetch origin
cd /opt/trad_bot_issue169
git fetch origin
git checkout --detach origin/research/htf-point-b-final-paired-oos-verdict-v1
/opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_htf_point_b_final_paired_oos_verdict_v1.py \
  tests/test_htf_point_b_paired_report_v1.py --tb=short

/opt/trad_bot/.venv/bin/python -m tools.research.htf_point_b_paired_report_v1 \
  --output-prefix /tmp/htf_point_b_paired_169 --reps 10000
```

Outputs `/tmp/htf_point_b_paired_169.md`, `.json`, `.csv` on VPS only; no Git artifact is claimed until those files are reviewed. Snapshot uses **REPEATABLE READ READ ONLY**; service code, registry, paper/live and research outcomes stay untouched.

## Acceptance / caution
- Record as-of timestamp, exact counts and all exclusion reasons. Do not re-use earlier mixed snapshots with drifting N.
- Report LONG and SHORT separately; paired mean difference is `Point-B R - baseline R`.
- Bootstrap full **UTC days** and symbols as clusters, deterministic seed. Only 14 calendar dates were present on October 19; 14 elapsed days since freeze ends **2026-10-20 14:00 UTC**.
- Check adverse concentration / leave-one-day-out, non-independent overlapping positions and inability to infer net tradeability.
- `NO_EDGE_INCREMENTAL_POINT_B` is allowed only when both day and symbol cluster 95% CIs are entirely below zero and elapsed duration gate passed. Otherwise `INCONCLUSIVE`.
- No claim of exchange costs, net edge, executable fills, or generalization to touches without Point-B.

## Provisional 2026-10-19 checkpoint (NOT final; different snapshots)
- LONG N=6301; delta −0.187342R; day CI [−0.232836, −0.140013].
- SHORT N=7306; delta −0.040648R; day CI [−0.153112, +0.058178].
- Early interpretation: LONG negative incremental; SHORT inconclusive. These are not final logged outputs of the new reproducible CLI.

**Never deploy to paper/live or mutate the research lifecycle from this Issue.**
