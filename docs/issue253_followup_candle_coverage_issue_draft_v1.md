# Draft follow-up Issue (NOT part of #253 / PR #254)

> **Scope discipline:** this is a *separate* defect class discovered during the
> Issue-#253 entrypoint review. Nothing here is implemented, wired or enabled
> by PR #254. `app/research/candle_coverage_guard_v1.py` remains an untracked
> orphan with **zero importers** and is deliberately **not** added to PR #254.

**Proposed title:** `research: a non-empty but non-covering candle window can
finalize OOS outcomes with default TP/SL`

**Proposed labels:** `research`, `oos-safety`, `needs-operator-decision`
**Blocks:** nothing in #253 · **Related:** #253 (limiter deny path is already
guarded), #225 (`API_BUDGET_NOT_CLEARED`)

---

## 1. Problem

Issue #253 proves that a **denied** fetch can never advance an OOS outcome:
all five generic evaluators guard `if not candles: … continue`
(`app/research/evaluator.py:421`, `app/research/prospective_evaluator.py:484`,
`app/shadow/evaluator.py:314`, `app/shadow/srr_research_evaluator.py:370`,
`app/shadow/v2d_evaluator.py:227`).

A **different and pre-existing** path is *not* covered by that guard: a
**non-empty but non-covering** source. Two concrete shapes:

1. **Truncated latest-N window.** `ProspectiveOOSEvaluator._get_candles`
   (`app/research/prospective_evaluator.py:202-211`) requests
   `min((end-start)//300_000 + 10, 1000)` 5 m bars — a hard **1000-bar
   (≈3.47 day)** ceiling. If the requested window is longer than that, the
   returned list is non-empty, passes `if not candles`, and evaluation runs
   against a window that does not actually cover `[signal_time, now]`.
2. **Silent partial window.** Any consumer whose source stops early (page
   cap, `max_pages`, gap, or a symbol whose history starts after the
   requested `from_time`) produces the same non-empty-but-incomplete list.

Consequence: `app/research/evaluator.py:347-360` can reach finalization with
`mfe/mae = None` and all-false TP/SL defaults → `is_final = TRUE` on an
outcome whose economics were never observed. That is an **incorrect OOS
advancement**, exactly the class Issue #253 was asked to rule out for the
*deny* path — but it is reachable without any deny at all.

## 2. Why this is NOT in Issue #253

* Changing finalization eligibility, coverage rules or window sizing is an
  **OOS policy change** (freeze/checkpoint/registry-adjacent) and requires an
  explicit operator decision — Issue #253 is forbidden from touching it.
* The deny path this issue *does* own is already fail-closed and now covered
  by `tests/test_issue253_limiter_deny_propagation_v1.py`.
* The candidate fixer (`app/research/candle_coverage_guard_v1.py`, which
  classifies `SOURCE_FETCH_ERROR` / `BYBIT_RATE_LIMITER_DENY` at lines
  434-437) is **dead code with zero importers**; wiring it in would silently
  change coverage semantics, so it must not be adopted implicitly.

## 3. Dependency / risk statement for PR #254

* **No dependency:** PR #254 neither introduces nor worsens this defect; it
  is byte-identical on `main` and on the branch.
* **Residual risk carried forward:** after #253, limiter denials become
  *visible* (`BYBIT_API_SKIP`, `SOURCE_BLOCKED`, `SOURCE_UNVERIFIABLE`) while
  the non-empty-truncation path stays **silent**. Operators comparing
  "before" and "after" telemetry must not read the absence of new denials as
  OOS-coverage proof.
* **Do not** treat the presence of `candle_coverage_guard_v1.py` in the
  working tree as mitigation: it is not imported, not tested and not in
  PR #254.

## 4. Proposed acceptance criteria (for the separate issue)

1. A deterministic test per evaluator proving that a **non-covering but
   non-empty** window does NOT finalize (`is_final` stays false, economics
   stay NULL) — parameterised over the 1000-bar ceiling and a symbol whose
   history starts after `signal_time`.
2. A single, explicit, shared definition of "source window covers
   `[signal_time, now]`" used by all five evaluators (no per-copies).
3. Either wire `candle_coverage_guard_v1.py` behind an explicit,
   operator-approved decision **or** delete it — never leave dead
   coverage code in the tree.
4. Reviewers' OOS checklist updated: coverage proof must be asserted from
   the stored window bounds, not from "candles is non-empty".
5. No change to freeze timestamps, eligibility rules, registries or
   existing OOS rows; no backfill.
