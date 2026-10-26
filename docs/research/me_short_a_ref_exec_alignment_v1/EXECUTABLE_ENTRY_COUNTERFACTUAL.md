# Executable Entry Counterfactual — ME_SHORT_A_REFERENCE_TO_EXECUTION_PRICE_ALIGNMENT_AUDIT_V1

Generated: 2026-10-26T13:35:20.970845+00:00. Absolute stop/target prices are identical in both views; only the entry price and its risk denominator change. Path classes (240m STOP_FIRST) are entry-independent and are reused from the frozen outcomes. Unknown exits stay unknown — never zero.

## No double counting
- CONTROL: synthetic entry at reference; pays frozen round-trip costs (0.0021 normal / 0.0031 elevated) in structural R.
- EXECUTABLE: payoff rebased on the modeled bid fill; entry-side reference-to-bid drift + book impact + entry spread are already inside the fill price and are NEVER subtracted again; only exit-side costs (fee + slippage; worst-case adds the spread) are applied.
- The Phase-1 diagnostic cost (A+B+C+2*fee) is reference-anchored; see `overlap_if_subtracted_again_pct` in the CSV.

## Risk recomputation
| event_key | symbol | scenario | path | risk_structural | risk_exec | risk_inflation_% | gross_R_ctrl | net_R_ctrl | gross_R_exec | net_R_exec_frozen_exit | net_R_exec_worst_exit | entry_availability | limit_at_reference_touch |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## Aggregates (base scenario)
{
  "matched_conclusive_rows": 0,
  "symbols": 0,
  "independent_symbol_clusters": 0,
  "clusters": [],
  "control_net_r": {
    "n": 0,
    "insufficient_sample": true,
    "point_estimate": null,
    "net_pf": null,
    "bootstrap": null
  },
  "executable_net_r_frozen_exit": {
    "n": 0,
    "insufficient_sample": true,
    "point_estimate": null,
    "net_pf": null,
    "bootstrap": null
  },
  "executable_net_r_worst_exit": {
    "n": 0,
    "insufficient_sample": true,
    "point_estimate": null,
    "net_pf": null,
    "bootstrap": null
  }
}

## Symbol / cluster concentration
[]

## Verdicts
- alignment_verdict: **INCONCLUSIVE**
- fill_feasibility_verdict: **FILL_FEASIBILITY_INCONCLUSIVE**

## Verdict reasons
{
  "alignment": {
    "verdict": "INCONCLUSIVE",
    "reasons": [
      "matched_conclusive_rows=0 < minimum_n=200",
      "symbols=0 < minimum_symbols=20",
      "no aggregate estimate published below the statistical floor",
      "Phase 1 evidence source unavailable or empty in this environment"
    ]
  },
  "fill_feasibility": {
    "verdict": "FILL_FEASIBILITY_INCONCLUSIVE",
    "n_quote_valid": 0,
    "reasons": [
      "n_quote_valid=0 < 30"
    ],
    "ok_rate": null,
    "min_constraint_feasible_rate": null
  }
}

## Prospective-only plan
1. Keep Phase 1 RUNNING and strictly append-only: continue signal-time capture for every new MOMENTUM_EXHAUSTION SHORT candidate; no backfill, no mutation of existing evidence rows.
2. Register a SEPARATE forward-only append-only supplement (new table via its own migration + PR) for trigger_5m_close, entry_zone_low/high and signal-window candle context; link via event_key, never update me_short_a_fill_feasibility_v1.
3. Accumulate at least minimum_n=200 matched matured outcomes across minimum_symbols=20 symbols (frozen cohort thresholds) before any profitability claim.
4. Re-run this audit (read-only) on the enlarged population; only then may ALIGNMENT_CONFIRMED / ALIGNMENT_INVALID be issued.
5. No paper/live activation, no scanner changes, no Phase 1 freeze/status changes, no production SQL writes as part of this audit.
