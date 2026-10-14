# ME_R_EARLY_MAE_V3 Open Operator Decisions

**Status:** DESIGN-APPROVED DRAFT — approved design values below are not frozen; pending items require explicit operator approval before freeze.

| ID | Decision | Approved or pending decision | Status | Further approval required |
|---|---|---|---|---|
| OD-01 | Source detector | Option A, closed-candle V1 rules | APPROVED | No — already approved |
| OD-02 | Scanner identity | `ME_R_LONG_CLOSED_V1`, `1.0.0-DRAFT` | APPROVED for design; final collision audit pending | Yes, collision audit only |
| OD-03 | Primary entry policy | `NEXT_OPEN_5M`, strictly after actual signal availability | APPROVED | No |
| OD-04 | Entry expiry | 240 minutes | APPROVED | No |
| OD-05 | Stop geometry | 2.5% below entry | APPROVED | No |
| OD-06 | Target geometry | 3.0% above entry | APPROVED | No |
| OD-07 | Baseline max hold | 240 minutes | APPROVED | No |
| OD-08 | Normal slippage | 0.05% per side | APPROVED; elevated sensitivity remains separate | No |
| OD-09 | Elevated slippage sensitivity | 0.10% per side | APPROVED as preregistered sensitivity analysis | No |
| OD-10 | Taker fee | 0.055% per side | APPROVED | No |
| OD-11 | STOP_FIRST policy | Conservative stop-first | APPROVED | No |
| OD-12 | Timeout policy | First completed 5m close at or after deadline | APPROVED | No |
| OD-13 | Seven-variant matrix | Approved seven rows | Matrix approved for design, still unfrozen | No for design; freeze requires separate approval |
| OD-14 | Primary MAE variant | `MAE_15m_050` | APPROVED as sole PRIMARY; evidence claim still requires prospective data | No for design; freeze requires separate approval |
| OD-15 | Variant threshold exit | `HORIZON_END_CONDITIONAL_MAE_EXIT` at completed horizon-boundary 5m CLOSE | APPROVED as research proxy; causal executability verification remains pending | Yes, causal/executability verification |
| OD-16 | Processing latency allowance | Maximum 300 seconds after signal-candle close; later or incomplete signals rejected | APPROVED maximum; implementation verification pending | Yes, implementation verification only |
| OD-17 | Minimum operational sample | At least 30 filled events, 10 distinct symbols, 5 distinct OOS days | APPROVED (operator, 2026-10-14); operational integrity checkpoint only | No for thresholds; protocol freeze separately required |
| OD-18 | Statistical verdict sample | At least 100 filled events, 20 distinct symbols, 20 distinct OOS days | APPROVED (operator, 2026-10-14); first statistical evaluation checkpoint, not an automatic edge verdict | No for thresholds; analysis methodology pending |
| OD-19 | Multiple-comparison handling | Paired Net Delta E[R] against shared baseline; only `MAE_15m_050` PRIMARY; other six variants exploratory | PRIMARY/SECONDARY roles APPROVED (operator, 2026-10-14); CI procedure and multiple-comparison adjustment remain PENDING | Yes — CI methodology and multiple-comparison specification |
| OD-20 | Freeze timestamp | None assigned | PENDING — operator approval required | Yes |
| OD-21 | Activation timestamp | None assigned | PENDING — operator approval separately from freeze | Yes |
| OD-22 | Deployment target | Dedicated V3 service only | PENDING — deployment approval required | Yes |
| OD-23 | Grafana metrics | Optional, new-schema only | PENDING — must not alter existing research dashboards | Yes |
| OD-24 | Retention of invalid rows | Retain diagnostic violations; never synthesize history | PENDING — storage and privacy policy approval required | Yes |
| OD-25 | Statistical stopping rules | No early positive edge verdict before the approved statistical checkpoint; no automatic positive verdict at threshold | EARLY-POSITIVE-VERDICT BAN APPROVED (operator, 2026-10-14); final stopping/negative/inconclusive rules PENDING | Yes — full stopping rules before freeze |

## Required operator sequence

1. Confirm approved design values.
2. Resolve the final identity/collision audit.
3. Approve causal execution verification for the horizon-end proxy.
4. Approve sample-size checkpoints, stopping rules, and multiple-comparison methodology.
5. Approve implementation branch and deterministic tests.
6. Approve migration separately.
7. Approve deployment and dedicated service separately.
8. Assign and approve freeze timestamp.
9. Assign and approve activation timestamp separately.
10. Enable capture only after all invariants pass.

## Operator statistical decisions — 2026-10-14 (research-only record)

The operator explicitly approved the following **design-stage statistical commitments**, without approving protocol freeze, migrations, production deployment, service installation, capture, or OOS activation:

- **Operational integrity checkpoint (OD-17):** at least **30 filled source events**, **10 distinct symbols**, and **5 distinct prospective OOS days**. Audit candle and outcome coverage, all seven variants per eligible filled source, identity integrity, duplicates, maturity, finalization and ambiguity. This is **not** a positive-edge statistical verdict.
- **First statistical evaluation checkpoint (OD-18):** at least **100 filled source events**, **20 distinct symbols**, and **20 distinct prospective OOS days**. All three thresholds must be met. The sample is not a claim of statistical power or an automatic positive verdict.
- **One confirmatory comparison (OD-19):** `MAE_15m_050` is the only PRIMARY, compared against the same source-event baseline using paired **Net Delta E[R]** under the normal cost model. The other six predeclared variants are **SECONDARY / exploratory** and cannot independently establish an edge claim. Elevated-cost outcomes remain a sensitivity analysis.
- **No early positive verdict (OD-25):** no positive-edge conclusion before all statistical-checkpoint thresholds have been satisfied. Reaching them does **not** itself imply positive expectancy, statistical significance, or executable trading performance. The complete stopping/negative/inconclusive-verdict policy is still pending preregistration.

**Outstanding decisions:** formal confidence-interval / resampling methodology, dependence and concentration handling, multiple-comparison policy, complete stopping criteria, causal executability limitation of the completed-5m-close MAE proxy, fingerprint, freeze and activation timestamps, schema migration approval, and dedicated-service deployment approval.

**Invariant:** protocol remains `3.0.0-DRAFT`; `approved_for_activation=false`; `frozen_at`, `activation_at`, and approved fingerprint stay unset. No paper/live effect, no historical backfill, no production SQL or runtime modifications are authorized by this record.

Related implementation: [PR #145](https://github.com/eignat88/trad_bot/pull/145) (already merged). This is a subsequent documentation-only decision record, not an amendment to that merged pull request.
