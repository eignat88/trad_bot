# ME_R_V3_PROTOCOL_FREEZE_APPROVAL_V1

Issue: #150
Experiment: ME_R_LONG_EARLY_MAE_EXIT_OOS_V3
Base revision: 8fea9d96b64ac03c3146fb1a21cf786d8bb5f814
Protocol version: 3.0.0
Scanner: ME_R_LONG_CLOSED_V1
Scanner version: 1.0.0

## 1. Scope

This record freezes the research strategy specification only.

It does not authorize deployment, migration, capture activation,
historical backfill, scanner changes, paper trading, or live trading.

## 2. Source and temporal semantics

- Direction: LONG.
- Frozen V1 detector rules on completed candles.
- Required timeframes: 5m, 15m, 1h.
- Maximum source delay: 300 seconds.
- Source availability must not precede candle close.
- Duplicate, missing, future or incomplete source candles fail closed.
- Production runtime fetch provenance is not established by
  synthetic detector-parity tests alone.

## 3. Entry and baseline

- Entry model: NEXT_OPEN_5M.
- Entry boundary: first strictly later 5m open after eligibility.
- Entry candle must be completed before retrospective confirmation.
- Entry expiry: 240 minutes.
- SL: 2.5% of entry price.
- TP: 3.0% of entry price.
- Baseline horizon: 240 minutes from entry.
- Intrabar policy: STOP_FIRST.
- Ambiguous SL/TP collision remains explicitly marked.
- Timeout: first completed 5m close at or after deadline,
  subject to complete coverage.

## 4. MAE variants

| Variant | Window | Threshold | Role |
|---|---:|---:|---|
| MAE_10m_040 | 10m | 0.40R | SECONDARY |
| MAE_10m_050 | 10m | 0.50R | SECONDARY |
| MAE_15m_040 | 15m | 0.40R | SECONDARY |
| MAE_15m_050 | 15m | 0.50R | PRIMARY |
| MAE_15m_060 | 15m | 0.60R | SECONDARY |
| MAE_15m_075 | 15m | 0.75R | SECONDARY |
| MAE_30m_050 | 30m | 0.50R | SECONDARY |

Variants are paired against the same source-event baseline.
Only MAE_15m_050 is confirmatory.

HORIZON_END_CONDITIONAL_MAE_EXIT observes the complete variant
window. Baseline terminal SL/TP takes precedence.
Immediate MAE first-touch exit is forbidden.

The completed horizon-boundary 5m CLOSE is a research valuation
proxy, not proof of executable fill at that price.

## 5. Costs

- Taker fee: 0.055% per side.
- Normal slippage: 0.05% per side.
- Elevated slippage: 0.10% per side.
- Funding: NOT_MODELED.
- Normal net paired Delta E[R] is the primary research estimand.
- Elevated costs are sensitivity analysis.

## 6. Immutable specification

Artifact:
docs/research/ME_R_V3_PROTOCOL_3_0_0_IMMUTABLE_SPEC.json

Specification SHA256:
b626700a04b679d67fa4db029e4968dd9feba62123d5145f428e22a87c2e5ed3

Hash algorithm:
SHA256 over canonical JSON
(sort_keys=True, separators=(",", ":"), ensure_ascii=True).

The immutable specification excludes activation_at and frozen_at.

This specification fingerprint is distinct from the existing
activation-bound runtime protocol fingerprint.

## 7. Change control

- Protocol parameter changes require a new version and a new
  prospective cohort.
- No retroactive rewriting of existing source observations.
- No primary-variant promotion using the same holdout.
- Rollback disables future capture without mutating finalized
  historical evidence.
- Runtime activation requires separate authorization and verification.
- Existing V2 and ME_REVERSE_LONG_V3 remain independent.

## 8. Verification and limitations

Issue #149 positive detector parity: PASS.
Previous targeted regression suite: 38 PASS.

Immutable specification tests: PASS (4 tests).
Broader regression suite: PASS (159 tests total, including immutable specification tests).

Runtime capture remains disabled by default.
Production fetch provenance was not verified.
Statistical CI-selection and stopping decisions are governed by
the separate statistical protocol, not by this specification freeze.

## 9. Approval boundary

Status: FREEZE_CANDIDATE_PENDING_FINAL_VALIDATION.

Protocol specification may be marked APPROVED only after:
- immutable hash and constant consistency checks PASS;
- broader relevant regression tests PASS;
- operator reviews and approves the final artifact.

Activation approval: NOT GRANTED.
No V3 OOS accumulation may be claimed by this issue.
