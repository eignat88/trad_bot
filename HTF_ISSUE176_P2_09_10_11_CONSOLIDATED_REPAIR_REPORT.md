# HTF Issue #176 — P2-09/10/11 Consolidated Repair

**Task:** `HTF_ISSUE176_P2_09_11_CONSOLIDATED_REPAIR_V1`
**Target review:** Codex review of commit `be8c441dbd21c0ba97f6e5e4457e84b8bc5806b3` on `research/htf-baseline-short-stateful-episode-v1`
**Scope:** research-only, local candidate — no commit / push / merge / deploy / Phase-2.
**Frozen detector `_find_reaction_end` (`app/research/htf_keylevel_point_b.py`): NOT modified.**

---

## 1. Findings addressed

| ID | Defect | File (root cause) | Status |
|----|--------|-------------------|--------|
| **P2-09** | Single-level replay suppressed a reaction confirmation whenever the confirmation candle also touched the level (gate `if level is not None and not touch`). Diverged from the multi-level active-vs-recontact semantics (P2-06). | `tools/research/htf_baseline_short_stateful_replay_v1.py` | FIXED |
| **P2-10** | A same-step re-contact replaced the pending episode before the reaction confirmation was validated, so the confirmation was mis-attributed to the re-contact episode and the prior episode was orphaned in `CONTACT`. `EpisodeState.advance` was not atomic. | `app/research/htf_baseline_short_stateful_episode_v1.py`, `app/research/htf_baseline_short_multi_level_lifecycle_v1.py` | FIXED |
| **P2-11** | Global `seen_prices` dedup suppressed independent same-price swing formations (not just one tied plateau); it also marked the price "seen" before the eligibility (touch-count) test, so an ineligible candidate could hide a later eligible one. | `app/research/htf_baseline_short_causal_level_adapter_v1.py` | FIXED |

## 2. Root-cause fixes (no detector weakening)

### P2-09 — single-level confirmation gate (`htf_baseline_short_stateful_replay_v1.py`)
The confirmation evaluation is now gated on **continuation vs re-contact**, identical to the multi-level rule:
```python
if level is not None and (not touch or level.level_id in state.active):
```
- `not touch` → confirm allowed (unchanged).
- `touch` and `key in state.active` → **continuation** of an open contact run → confirm allowed (the P2-09 defect is closed: a touching confirmation bar is no longer dropped).
- `touch` and `key not in state.active` → **re-contact** → the confirmation is handled by the state machine as a supersession and does **not** confirm the superseded pending episode (matches non-regressable `test_replay_recontact_does_not_confirm_superseded_pending`).

### P2-10 — episode identity + atomicity (`stateful_episode_v1.py`, `multi_level_lifecycle_v1.py`)
Unified causal semantics (identical in single-level and multi-level):
- **Continuation** (`touch` and `key in active`) → confirmation allowed and bound to the pending episode.
- **Re-contact** (`touch` and `key not in active`) → the prior pending episode is superseded to `UNRESOLVED` / `RECONTACT_SUPERSEDED_PENDING` (never orphaned in `CONTACT`), a new episode is created, and a **same-step confirmation is rejected** (neither emitted nor attributed to the re-contact episode). This matches the non-regressable P2-06 test, which mandates *no* confirmation on a re-contact bar.
- **No touch** → confirmation allowed.

Atomicity: `EpisodeState.advance` now applies the step to a `deepcopy` and commits only on success (`self.__dict__.update(working.__dict__)`), exactly as `MultiLevelEpisodes.advance` does. A rejected/invalid step never corrupts recoverable state; retry is deterministic; checkpoint/restart is equivalent.

### P2-11 — tied-price dedup restricted to one plateau (`causal_level_adapter_v1.py`)
The global `seen_prices` set was replaced with **run-based** dedup over a maximal run of equal highs:
- A contiguous tied swing-high **plateau** (consecutive equal highs) collapses to exactly one representative level (P2-07 preserved).
- Independent same-price swings (separate formations separated by lower highs) are **never** suppressed for sharing a price; each mints its own level.
- The run is marked "represented" only after a candidate actually passes eligibility, so an ineligible candidate no longer blocks a later eligible one (Codex example). Level ordering remains deterministic `(price, formation, level_id)`.

## 3. Test evidence

### 3.1 Failing reproductions added BEFORE the fix (new file `tests/test_htf_baseline_short_p2_09_10_11_repair_v1.py`)
All six targeted reproductions FAILED on `be8c441` for the correct reasons, and PASS after the repair:

| Test | Defect | Pre-fix result |
|------|--------|----------------|
| `test_single_level_replay_confirms_reaction_on_touching_bar` | P2-09 | FAIL (confirmation omitted) |
| `test_episode_state_recontact_same_step_rejects_confirmation` | P2-10 | FAIL (mis-attributed emission, orphaned CONTACT) |
| `test_multi_level_recontact_same_step_rejects_confirmation` | P2-10 | FAIL (mis-attributed event) |
| `test_single_and_multi_level_consistent_on_recontact_confirmation` | P2-10 | FAIL (`['CONFIRMED','CONTACT'] != ['CONFIRMED','UNRESOLVED']`) |
| `test_separate_same_price_swings_form_independent_levels` | P2-11 | FAIL (`1 == 2`) |
| `test_prior_ineligible_swing_does_not_suppress_later_eligible_same_price_swing` | P2-11 | FAIL (`0 >= 1`) |

Guard / preservation tests (pass before **and** after): re-contact does not confirm; invalid-confirmation rollback + retry determinism; checkpoint/restart equivalence; tied plateau → one level; plateau identity stable across window shift; deterministic ordering. Single/multi consistency and checkpoint/restart equivalence are covered explicitly.

### 3.2 HTF regression suite
```
python -m pytest (Get-ChildItem tests -Filter "test_htf*.py").FullName -q
→ 225 passed   (213 prior + 12 new)
```
All P2-06/07/08 tests pass — no regression.

### 3.3 No-regression (whole repo, `--continue-on-collection-errors`)
| | failed | passed | skipped | errors |
|---|---|---|---|---|
| base `be8c441` | 77 | 3924 | 30 | 57 |
| fixed (candidate) | 77 | **3936** | 30 | 57 |

**Identical 77 failures and 57 errors** (pre-existing: DB-dependent `pg8000` / `canonical_analytics_db` / `migration_013` suites needing live PostgreSQL, and the known `srr_short_r_expansion_*` collection errors). The only delta is **+12 passed** = the 12 new reproductions. No previously-passing test fails.

### 3.4 P2-07 fixture refinement (transparency)
`test_tied_htf_swing_highs_form_one_resistance_level` and `test_tied_plateau_mints_single_id_even_after_original_swing_eviction` originally used a fixture that conflated the tied plateau (indices 9,10) with a genuinely **separate** index-4 swing; under correct P2-11 these are two independent formations (2 levels). Their fixtures were refined to isolate the plateau (leading 100s moved outside the `SWING_RIGHT=4` window as touch-count builders), preserving P2-07's exact intent ("a contiguous tied plateau forms one level") while not asserting the now-forbidden global collapse of independent same-price swings. `test_level_id_is_stable_when_rolling_window_shifts_but_swing_remains` was left intact (it tests multi-peak id stability and already passed). This is fixture refinement, not a P2-07 regression.

## 4. Analogous-path trace (mandatory #9)
The three defect classes were traced across all `not touch` / `_find_reaction_end` / `seen*` dedup / touch+confirm state-machine call sites. They are **confined to the stateful-episode pipeline**:
- **P2-09** (`not touch` confirmation gate): only the two replay drivers — single-level (fixed) and multi-level (already correct via P2-06).
- **P2-10** (touch+confirm state machine): only `EpisodeState` and `MultiLevelEpisodes` (both fixed).
- **P2-11** (price-formation dedup): `seen_prices` existed only in `causal_resistance_universe` (fixed); all other `seen`/`seen.add` are timestamp/event-identity dedups, not price-formation suppression.

Reviewed and **not** affected by the same defects:
- `htf_keylevel_baseline_short_causal_observer_v1.py` — single-shot detector, no episode state machine, no `not touch` confirmation gate, no price dedup.
- `htf_keylevel_baseline_short_causal_replay_v1.py` — simple touch / no-touch replay, no state machine, no gate, no price dedup.
- `htf_keylevel_baseline_short_reaction_mask_audit_v1.py` — mask audit, no state machine.
- `_find_htf_key_levels` (`htf_keylevel_point_b.py`) — single-level selector, returns one level, no price-based dedup.
- `htf_baseline_short_episode_identity_hardening_v1.py` and various `seen` dedups (`me_r_long_*`, `srr_*`) — event/candle identity dedup, correct immutable-identity pattern.

## 5. BTCUSDT / ETHUSDT historical A/B + restart validation
_Isolated candidates in `/tmp/htf176_ab/` (never `/opt/trad_bot`); window `2026-10-14T00:00:00Z` → `2026-10-17T00:00:00Z`; data `/tmp/htf_coverage_174`._
- **base** tree verified = `be8c441` (P2-09 `not touch:` gate, no `deepcopy`, no `recontacted`, `seen_prices`).
- **fixed** tree verified = candidate (all four fix markers present).

**RESULTS** (base = `be8c441`, fixed = candidate; window `2026-10-14T00:00:00Z`→`2026-10-17T00:00:00Z`; run 2026-10-21 10:18→10:45 UTC; all four `.err` empty).

### 5.1 Single-level replay (`ab_single.py`) — P2-09 + P2-10
| | BTCUSDT | ETHUSDT |
|---|---|---|
| `new_confirmations` base→fixed | **6 → 7 (+1)** | 6 → 6 |
| `emissions` base→fixed | **6 → 7 (+1)** | 6 → 6 |
| `contacts` / `decisions` | 50 / 864 (unchanged) | 28 / 864 (unchanged) |
| `all_episodes` | 171 (unchanged) | 225 (unchanged) |
| `unselected_pending_level_prefixes` | 2978 → 3120 | 3403 → 3409 |

- **BTCUSDT +1 confirmation (P2-09)** — exactly the defect: one reaction confirmation whose candle also touched the level (a continuation of an open contact run) was dropped by the base `not touch:` gate and is now correctly captured. The frozen detector (`_find_reaction_end`) still validates the reaction (`candidate[0] == len(prefix)-1`), so this is a genuine reaction end, not a spurious emission. ETHUSDT has no such bar in this window (fix dormant there).
- `all_episodes` unchanged (171/225): P2-10 supersession changes episode **state** (orphaned `CONTACT` → `UNRESOLVED`/`RECONTACT_SUPERSEDED_PENDING`), not episode count. The single-level probe exposes emissions and episode count (not per-episode state histograms); the P2-10 single-level state semantics are verified by unit tests (`test_episode_state_recontact_same_step_rejects_confirmation`, `test_single_and_multi_level_consistent_on_recontact_confirmation`). This is a reporting-surface limitation of the probe, not an untested condition.
- `unselected_pending_level_prefixes` shifts (BTC +142, ETH +6): a **diagnostic** counter (it tracks the known gap "non-selected HTF levels and pending reactions need independent lifecycle"), not a decision signal. It moves because the corrected confirmation path retires the selected level's pending reaction at a different time. The actual decision signal (emissions) differs only by the single P2-09 confirmation on BTCUSDT.

### 5.2 Multi-level replay (`ab_driver.py`) — P2-10 + P2-11
`multi_base.json == multi_fixed.json` (byte-identical) for both symbols. Expected and correct:
- **P2-11 dormant on this window**: `levels_known` = 24 (BTC) / 28 (ETH) unchanged — there are no independent same-price swing formations here, so restricting the tied-price dedup to one plateau changes nothing. `test_separate_same_price_swings_form_independent_levels` / `test_prior_ineligible_swing_does_not_suppress_later_eligible_same_price_swing` prove the fix activates when the pattern occurs.
- **P2-10 multi-level is defensive**: the multi-level driver's P2-06 gate (`not touch or key in state.active`) already prevented same-step re-contact confirmations from reaching `MultiLevelEpisodes.advance`, and supersession (`RECONTACT_SUPERSEDED_PENDING` = 198 BTC / 223 ETH) already existed in `MultiLevelEpisodes`. The `recontacted` rejection therefore hardens the state machine for direct calls without altering historical driver output.

| multi-level (both symbols) | base | fixed |
|---|---|---|
| `episode_state_counts` (CONFIRMED/UNRESOLVED) | 109/203 (BTC), 137/227 (ETH) | identical |
| `unresolved_reason_counts` | RECONTACT_SUPERSEDED 198/223, EVICTED 5/4 | identical |
| `restart_emissions_match` / `restart_counters_match` | True / True | True / True |

### 5.3 Restart validation
`restart_emissions_match = True` and `restart_counters_match = True` for **both** base and fixed, **both** symbols. Checkpoint/restart equivalence holds (the fixed `deepcopy` atomicity additionally guarantees no partial-state corruption on a rejected step — unit-tested by `test_invalid_confirmation_rolls_back_and_is_retryable`).

### 5.4 Explanation of every historical difference
1. Single-level BTCUSDT `new_confirmations`/`emissions` +1 → **P2-09** (touching-bar confirmation correctly captured). Intended correction.
2. Single-level `unselected_pending_level_prefixes` (BTC +142, ETH +6) → diagnostic side-effect of (1); not a signal.
3. Multi-level: **no differences** → P2-11 dormant on this window + P2-10 multi-level already gated/superseded. Intended (no behavioral change where the defects do not manifest).

No unexplained differences.

## 6. Remaining limitations
- The historical A/B quantifies the frequency of each corrected behavior on the BTCUSDT/ETHUSDT 2026-10-14→17 window. Where a defect class does not manifest in this window, the base/fixed delta for that counter is 0 (the fix is dormant when the triggering pattern is absent); the unit reproductions prove each fix is active when the pattern occurs.
- The three fixes are causal-semantics corrections in the research-only HTF baseline stateful-episode pipeline; they do not touch scanner / paper / live / PostgreSQL / Grafana.

---

## 7. Verdict

**`READY_FOR_REVIEW`**

Rationale:
- All three findings (P2-09, P2-10, P2-11) are fixed at the root cause; the frozen detector `_find_reaction_end` is untouched.
- Failing reproductions for all three were added and confirmed to FAIL on `be8c441` before the fix, and PASS after.
- Entire HTF regression suite: **225 passed** (213 prior + 12 new). All P2-06/07/08 tests pass — no regression.
- Whole-repo no-regression: base `be8c441` (77 failed / 3924 passed / 57 errors) vs fixed (77 failed / 3936 passed / 57 errors) — identical failures + errors, delta exactly **+12 passed** (the new reproductions). No previously-passing test fails.
- Independent single-level and multi-level consistency tests pass.
- BTCUSDT/ETHUSDT historical A/B + restart validation complete; every historical difference is explained (§5.4); no unexplained differences.
- Analogous defect-class call sites traced (§4); confined to the stateful-episode pipeline.

Remaining limitations (not blocking):
- P2-11 and the P2-10 multi-level hardening are dormant on the 2026-10-14→17 window (no independent same-price swings; the P2-06 driver gate already prevented same-step re-contact confirmations). Their correctness is established by unit reproductions, not by a historical delta.
- The single-level A/B probe exposes emissions and episode counts but not per-episode state histograms; P2-10 single-level state semantics are covered by unit tests.

Not done here (per task constraints): no commit / push / merge / deploy / Phase-2 activation.
