# ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1

Issue #208 — independent, strictly prospective, observe-only **outcome and
lineage capture** for the ongoing #202 Phase 1 snapshots.

Status: **IMPLEMENTED, NOT DEPLOYED, NOT ACTIVATED.**
Deployment and activation require a separate, explicitly approved checkpoint
(see "Deployment readiness" below).

| Area | Touched |
|---|---|
| SCANNER | NO (read-only consumer of MOMENTUM_EXHAUSTION SHORT candidates) |
| PAPER | NO |
| LIVE | NO |
| DATABASE | YES (new migration 078 + least-privilege grants; nothing existing is modified) |
| GRAFANA | NO |
| DEPLOYMENT | NO in this change |

---

## 1. Why this exists

Production READ-ONLY audit of 2026-10-26 established a concrete blocker for
Issue #206 / draft PR #207:

* `research.me_short_a_fill_feasibility_v1` holds 5 post-freeze snapshots, and
  **all** of them have `source_observation_ids IS NULL`; matched observations,
  outcomes and finalized rows = 0.
* The closed original `ME_SHORT_GEOM_A_EXECUTION_V1` has 380 observations in
  total and **0 observations after** the Phase 1 freeze
  `2026-10-26 12:53:57.708435+00`; its last source signal is
  `2026-10-26 08:28:09.396139+00`.
* PR #207 reported six fields as MISSING from Phase 1 evidence / linked rows:
  `trigger_5m_close`, `entry_zone_low`, `entry_zone_high`,
  `recent_high_window_candles`, `post_signal_candle_highs_240m`,
  `realized_exit_fills`.

The original frozen research completion remains valid **for its assumed
synthetic entry** but does not guarantee market execution. It is not reopened
here and no unrelated observation IDs are joined to it.

This protocol supplies the missing layer **forward only**: new events captured
after its own freeze are linked deterministically across

```
source signal  ->  Phase 1 liquidity snapshot  ->  outcome
```

so the read-only price-alignment counterfactual of #206 / PR #207 becomes
reproducible on a clean population.

---

## 2. Frozen protocol (never retuned)

| Item | Value |
|---|---|
| protocol_version | `ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1` |
| scanner | `MOMENTUM_EXHAUSTION` |
| direction | `SHORT` only |
| entry | `reference_price` — frozen A: max of the last five 5m highs |
| stop | `invalidation_price` (absolute, `> reference_price`) |
| target | `target_1` (absolute, `< reference_price`) |
| `structural_r` | `stop_price - reference_price` |
| max hold | **240m** |
| intrabar policy | **STOP_FIRST** |
| touch basis | `CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL` |
| exit fills | `MODELED_UNKNOWN` (never observed by this collector) |
| partial fills | `UNKNOWN_NOT_INDEPENDENTLY_CAPTURED` |
| fee per side | `0.00055` |
| round-trip cost | `0.0021` normal / `0.0031` elevated (classification only) |
| mode | `OBSERVE_ONLY_NO_ORDERS` |

The protocol hash is `sha256` of the canonical (sorted, compact) JSON payload
and is stored on every captured row, so a verifier can prove a row was produced
by exactly this protocol version.

### 2.1 STOP_FIRST outcome semantics

* Evaluation window: the closed 5m candles whose OPEN time `o` satisfies
  `signal_time < o < signal_time + 240m` — the identical inequality used by the
  frozen `prospective_evaluator`. For a grid-aligned signal that is 47 candles
  (the `+0m` candle is the trigger candle and is excluded, exactly as in the
  frozen protocol); for an off-grid signal it is 48. The required count is
  **derived from the window, never assumed**.
* SHORT: a candle touches the stop when `high >= stop_price`, the target when
  `low <= target_price`.
* An ambiguous candle (both) resolves to the **stop** (`STOP_FIRST`), is flagged
  `ambiguous_intrabar`, and yields exactly `gross_r = -1`.
* Otherwise the **first** touch in the window decides the terminal path:
  `STOP_HIT` (`gross_r = -1`), `TARGET_HIT`
  (`gross_r = (reference_price - target_price) / structural_r`).
* No touch inside the window ⇒ `TIMEOUT_240M`, exit at the close of the last
  in-window candle.
* `gross_r = (reference_price - exit_price) / structural_r`.
* `return_at_240m` follows the **frozen direction-agnostic convention** of
  `research.prospective_outcome` — `(last in-window close − reference_price) /
  reference_price × 100` — so a favourable SHORT move is negative here exactly
  as in the closed frozen OOS cohort it must stay comparable with.
* `mae_240m = (max high − reference_price)/reference_price × 100`,
  `mfe_240m = (reference_price − min low)/reference_price × 100` (frozen SHORT
  definitions).
* `limit_at_reference_touch` is classified on a **candle** basis
  (`high >= reference_price`). It is a level touch, **not** a fill.

---

## 3. Deterministic `event_key`

```
event_key = sha256(symbol|SHORT|<utc isoformat signal_time>|<ref>|<stop>|<target>)
```

with `str(Decimal(str(value)))` for prices and `datetime(...).isoformat()` for
the timestamp.

This is **byte-identical** to the identity frozen in migration 077 and consumed
by PR #207, so all three layers join on one collision-resistant key. A test
asserts parity against the Phase 1 `capture()` formula directly.

Every linkage row stores the identity components plus `event_key_recomputed`,
and `research.me_short_a_event_lineage_v1` verifies symbol, direction, exact
signal timestamp, reference, absolute stop, absolute target, protocol version
and protocol hash. A mismatch is **quarantined** (`link_status = 'QUARANTINED'`
with `quarantine_reason`) — it is never repaired, never linked and never
silently accepted.

---

## 4. Storage model (migration 078, all append-only)

| Object | Role |
|---|---|
| `research.me_short_a_lineage_activation_v1` | lifecycle; seeded `READY_TO_START`, `freeze_ts` NULL |
| `research.me_short_a_source_signal_v1` | source signal detail previously missing from Phase 1 |
| `research.me_short_a_post_signal_candle_v1` | future post-signal 5m OHLC provenance |
| `research.me_short_a_event_lineage_v1` | deterministic `event_key` linkage + verification |
| `research.me_short_a_outcome_observation_v1` | autonomous 240m STOP_FIRST outcome |
| `research.v_me_short_a_lineage_status_v1` | the four output statuses, per event |

All four capture tables:

* carry `CHECK (signal_time > freeze_ts)` — no historical backfill is possible
  by construction;
* are **append-only**: `BEFORE UPDATE OR DELETE` triggers raise, so evidence is
  immutable and a retry can only ever add a new row;
* use `INSERT ... ON CONFLICT (event_key) DO NOTHING` for idempotency.

`research.me_short_a_source_signal_v1` records the fields PR #207 listed as
missing: `trigger_5m_close`, `entry_zone_low`, `entry_zone_high`, the exact
five 5m candles behind `reference_price` (`recent_high_window`, with
`recent_high_window_candles/_start/_end`), `market_context`,
`signal_time_quote`. **Missing or unavailable values are recorded explicitly**
in `missing_fields` / `source_quality_flags`; they are never imputed.

`research.me_short_a_post_signal_candle_v1` stores only **closed** 5m candles
with `candle_close_time <= retrieved_at`, and records
`high_touches_stop` / `low_touches_target` / `high_touches_reference` on a
candle basis.

---

## 5. Source coverage and no-lookahead

* Source signal: `recent_high_window_end <= signal_time` and
  `signal_candle_close_time <= signal_time` are **hard rejects** in code and
  `CHECK`s in the database. A reference window closing after the signal time
  would leak future information into the frozen A `reference_price`.
* Post-signal candles: `candle_open_time > signal_time`, `is_closed` must be
  true, `candle_close_time <= retrieved_at`.
* Outcome: every used candle must close at or before `asof_time`
  (`asof_ok`), and `max_candle_close_time <= asof_time` is a `CHECK`.
* `candles_expected` is derived from the frozen window; `candles_present` must
  equal it (`coverage_complete`); `candles_missing` lists the exact missing
  open times.

---

## 6. Controlled finalization

A row is written **only after the 240m horizon is settled on closed 5m
candles**. There is no premature finalization:

* `OUTCOME_UNMATURED` rows are **never produced** by this collector; the state
  is derived in the status view while the horizon is still open.
* A matured event with incomplete source becomes `SOURCE_INCOMPLETE` and
  carries **no economics at all** — `path_class`, `exit_price`, `gross_r`,
  `net_r_*` and `return_at_240m` are structurally NULL (enforced by `CHECK`),
  so a source-missing row can never be read as a result.
* A complete, ASOF-clean event becomes `MATCHED_FINALIZED` with `is_final`,
  `finalized_at` and `finalization_candle_close_time` (`>= horizon_end_time` and
  `<= asof_time`, both `CHECK`ed).
* Exit market fills stay `MODELED_UNKNOWN` and partial fills stay
  `UNKNOWN_NOT_INDEPENDENTLY_CAPTURED` — both are `CHECK`-pinned, so this
  collector is structurally unable to claim an observed fill.
* Retry/idempotency: replaying an event inserts nothing and returns `False`.

---

## 7. Lineage output statuses (exactly four)

Precedence in `research.v_me_short_a_lineage_status_v1`:

| Status | Meaning | Analysis-eligible |
|---|---|---|
| `MISSING_LINEAGE` | the deterministic chain cannot be verified: missing source signal row, missing/quarantined linkage, or no Phase 1 snapshot for the same `event_key` | no |
| `OUTCOME_UNMATURED` | chain verified, but the 240m horizon has not settled yet | no |
| `SOURCE_INCOMPLETE` | chain verified and matured, but source coverage/ASOF/quality is not analysis-safe (or no outcome was captured at all after maturation) | no |
| `MATCHED_FINALIZED` | chain verified, matured, complete, ASOF-clean, finalized | **yes** |

"No source-missing rows silently included" is enforced twice: in the view
precedence and in the database `CHECK`s that keep every economic field NULL on
`SOURCE_INCOMPLETE` rows.

---

## 8. Freeze, cutover and the new holdout cohort

1. **Migration 078 is applied** (source-controlled here; *not* applied to
   production as part of this task). The activation row is seeded
   `READY_TO_START` with `freeze_ts IS NULL` — capture is fail-closed and
   nothing is recorded.
2. **Least-privilege grants are applied** (see §9).
3. **Explicit freeze / activation checkpoint** (separate, human-approved
   change, out of scope here): transition `READY_TO_START → RUNNING` with an
   explicit immutable `freeze_ts`, and set the runtime flag
   `ME_SHORT_A_LINEAGE_CAPTURE_ENABLED=1`.
4. **Strictly forward only** from that `freeze_ts`: every eligible new
   MOMENTUM_EXHAUSTION SHORT candidate is captured with no post-hoc filter,
   **no backfill** and no retro-fitting of `source_observation_ids` on
   pre-freeze rows.
5. **New holdout cohort**: every event captured strictly after this protocol's
   freeze_ts forms a NEW prospective holdout cohort. It is never pooled with
   the closed 380-row `ME_SHORT_GEOM_A_EXECUTION_V1` research cohort, and the
   closed frozen OOS outputs are never modified.

### Accumulation gate

At the accumulation checkpoint target **≥ 200 `MATCHED_FINALIZED` outcomes over
≥ 20 distinct symbols** (frozen `minimum_n` / `minimum_symbols` — a *minimum
eligibility*, **not** an automatic PASS) the #206 evaluation may be run:
independent executable-entry Net E[R] / PF / CI by notionals 100/500/1000, cost
attribution including the entry fee, no double-count of reference-to-bid drift
and spread, and symbol/day concentration.

---

## 9. Least-privilege grants

`sql/grants/me_short_a_forward_outcome_lineage_least_privilege_v1.sql`
(requires migration 078 first; reviewed separately; **not applied** here):

| Role | Privileges |
|---|---|
| `trad_bot_me_short_a_collector` | `INSERT` on the four capture tables; `SELECT` on `me_short_a_lineage_activation_v1` only (fail-closed gate) |
| `trad_bot_me_short_a_audit` | `SELECT` on all five tables + the status view |

* Collector **INSERT only** — idempotency is `ON CONFLICT DO NOTHING` +
  `rowcount`, so the collector never needs to read the capture tables back.
* The file first **REVOKEs** the broad `research`-schema default privileges
  installed by migrations 049/050 from `trad_bot`, `analytics_agent` and
  `analytics_runner`, then grants narrowly.
* No `UPDATE` / `DELETE` / `TRUNCATE` anywhere, and **no privilege at all** on
  `research.me_short_a_fill_feasibility_v1`,
  `research.me_short_a_fill_activation_v1`,
  `research.prospective_observation`, `research.prospective_outcome`, or any
  scanner/paper/live/direction-gate object.

---

## 10. Compatibility with Issue #206 / draft PR #207

* **Same identity.** `event_key` is byte-identical to the Phase 1 key, so the
  PR #207 lineage chain `signal → Phase 1 snapshot → frozen outcome` extends to
  `signal → Phase 1 snapshot → outcome` without any key migration.
* **Supplies exactly the fields PR #207 reported as missing:**
  `trigger_5m_close`, `entry_zone_low`, `entry_zone_high`,
  `recent_high_window_candles`, `post_signal_candle_highs_240m` (via the
  post-signal candle provenance). `realized_exit_fills` is deliberately
  **not** supplied as an observation: exit fills remain `MODELED_UNKNOWN`, which
  is what PR #207 already assumes.
* **Same path classes.** The 240m `STOP_FIRST` terminal path is entry-independent
  and reproduces the frozen semantics, so PR #207 can reuse it as-is.
* **Same `return_at_240m` sign convention** as `research.prospective_outcome`
  (direction-agnostic), so the counterfactual rebasing in PR #207 needs no sign
  translation.
* **No change to PR #207's sources.** This collector never writes to
  `research.me_short_a_fill_feasibility_v1`, so the audit's read-only SQL and
  its `overlap_if_subtracted_again_pct` double-count guard are unaffected.
* PR #207 remains **DRAFT**; Issue #206 and parent #202 stay open until their
  own gates are verified.

---

## 11. Isolation guarantees (what is NOT touched)

* `research.me_short_a_fill_feasibility_v1` (Phase 1) — never written;
  `source_observation_ids` of the existing rows are **never** filled in
  artificially.
* The closed frozen OOS cohort (`research.prospective_observation`,
  `research.prospective_outcome`, `ME_SHORT_GEOM_A_EXECUTION_V1` and its 380
  results) — never modified, never reopened.
* No historical backfill of any kind.
* Scanner, paper, live and the ME SHORT direction gate are untouched and stay
  BLOCKED; no order pathway is introduced.
* No deployment, no SQL writes on the VPS, no service restart, no paper/live
  activation as part of this task.

---

## 12. Tests

```
python -m pytest -q tests/test_me_short_a_forward_outcome_lineage_v1.py
python -m pytest -q tests/test_me_short_a_forward_outcome_lineage_migration_078_v1.py
```

Covered: `event_key` byte-parity with the Phase 1 formula and cross-module
parity with `me_short_a_fill_feasibility_v1.capture`; determinism and component
collision sensitivity; protocol-hash ↔ registry ↔ migration seed agreement;
identity verification and quarantine per component; frozen window geometry and
its derivation (47 vs 48, never assumed); STOP_FIRST terminal paths including
ambiguity = −1R and first-touch precedence; net-R cost normalization; frozen
`mae`/`mfe`/`return_at_240m` conventions; candle-touch ≠ fill; source coverage,
open candles, ASOF violations and missing candles → `SOURCE_INCOMPLETE` with
zero economics; fail-closed gates (feature flag, activation state machine,
pre-freeze contamination); LONG exclusion; idempotency and rollback; "every
INSERT carries the activation guard" and "bind count == parameter count";
migration 078 SQL contract (freeze `CHECK`s, append-only triggers, activation
state machine, no lookahead, STOP_FIRST `CHECK`s, no-economics-on-incomplete,
status enum, view statuses, INSERT column contracts); least-privilege grant
shape; and the strictly read-only audit replay.

---

## 13. Deployment readiness — **separate from the fact of implementation**

**Implementation: COMPLETE** (code, migration, grants, tests, docs, read-only
audit replay — all in this branch/PR).

**Deployment readiness: NOT READY / NOT STARTED — intentionally.**

| Step | State |
|---|---|
| Migration 078 reviewed and applied on VPS | **not done** (forbidden in this task) |
| Least-privilege grants applied on VPS | **not done** (forbidden in this task) |
| Explicit `freeze_ts` activation checkpoint | **not done** (requires separate human approval) |
| Runtime flag `ME_SHORT_A_LINEAGE_CAPTURE_ENABLED=1` | **not done** (forbidden in this task) |
| Services restarted | **not required** and **not done** |
| Paper / live activation | **forbidden** |
| Direction gate change | **forbidden** |
| Post-deploy health check | **not applicable yet** |

Deployment must be executed as a separate, explicitly approved change with its
own migration application, grant application, freeze record and post-deploy
health check. Until then the collector is fail-closed and records nothing.

### Explicitly forbidden in this task

deploy · SQL writes on the VPS · service restart · paper/live activation ·
direction gate change · historical backfill · retro-fitting
`source_observation_ids` on pre-freeze rows · mutation of the frozen OOS
outputs.
