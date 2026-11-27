# #324 H1-E — OOS Readiness Protocol V1 (PROPOSAL ONLY)

**Contract:** `H1E_OOS_READINESS_V1` (`app/research/me_entry_quality_h1e_v1/oos_readiness.py`)
**Status:** `PROPOSAL_ONLY_NOT_FROZEN / OUTCOME_COMPUTATION=FORBIDDEN_IN_PHASE_A`
**Gate:** separately reviewed + frozen policy and a separate owner GO are
required before ANY accumulation or evaluation.

This document + the generated template pre-register the SHAPE of the future
H1-E OOS comparison.  It contains **no results** and the module computes
**no outcomes** (structurally: no evaluator, no statistics, no data-tuned
thresholds).

## 1. Cohorts (forward-only)

| Cohort | Inclusion |
| --- | --- |
| `H1_BULLISH` | prospective events (`signal_time > H1-E freeze_ts`) with eligible H1-E classification and combined regime `BULLISH/*`-level semantics as recorded verbatim |
| `H1_BEARISH` | ditto, `BEARISH` |
| `H1_NEUTRAL` | ditto, `NEUTRAL` |

Plus all nine verbatim 4H×1D combinations
(`BULLISH/BULLISH` … `NEUTRAL/NEUTRAL`) as retained strata — never merged,
never averaged.

**Exclusions (pre-registered):** pre-freeze events; #305 UNVERIFIED window
events (`2026-11-24T13:31:53.365952+00:00 → 2026-11-25T07:14:41.230516+00:00`);
the historical 2026-11-25 SOXSUSDT QA example; deferred / non-eligible
classifications; identity conflicts.

## 2. Unchanged control

* definition: `UNCHANGED_BASELINE_CONTROL`;
* pairing: identical `source_event_key` (paired design — every cohort event
  is compared against the control evaluated on the SAME event);
* tuning: **FORBIDDEN** — the control is never re-fit, re-tuned or
  replaced.

## 3. Pre-registered thresholds & execution model

All `TBD_FROZEN` placeholders to be fixed BEFORE accumulation:
min events per cohort / per combination, min distinct symbols, min distinct
days, uncertainty policy (CI method), entry model, fees (bps), slippage
model, TP-SL timeout, follow-up windows.

## 4. Metrics & strata (pre-declared)

* metrics: `MAE_R`, `MFE_R`, `MISSED_WINNERS`, `TIME_TO_PEAK`,
  `ACCEPTED_RATE`, `REJECTED_RATE`;
* strata: accepted / rejected-with-reason-code / failure strata (HTF
  incomplete, API/limiter denial, identity conflicts);
* evidence retention: per-event H1-E ledger rows + `H1D_HTF_SNAPSHOT_2`
  provenance hashes for the experiment lifetime (offline replay stays
  byte-identical).

## 5. Hold gates

`phase_bc_hold`: Phase B (VPS read-only preflight) and Phase C (isolated
deploy / H1-E freeze) each require a SEPARATE OWNER GO; accumulation and
evaluation require the frozen policy + GO; merge/deploy is not authorized
by this template.  No OOS verdict is issued by this contour.
