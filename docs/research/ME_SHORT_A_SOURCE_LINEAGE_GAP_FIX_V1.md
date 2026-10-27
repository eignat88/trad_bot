# ME SHORT A Source Lineage Gap Fix V1 (Issue #208 corrective)

**Type:** Bugfix / Research Infrastructure / Data Integrity
**Scope:** strictly research-only, strictly forward-only. No trading-path
behaviour changes. No historical backfill.
**Status:** implemented on branch `fix/me-short-a-source-lineage-gap-v1`;
migration 080 is source-controlled and NOT applied to production as part of
this change. Activation is a separate, explicitly approved step.

---

## 1. Root-cause analysis

### 1.1 Confirmed incident

* **Event:** `TSLAUSDT`, `MOMENTUM_EXHAUSTION` / `SHORT`,
  `signal_time = 2026-10-27 10:43:57.672455+00` (microsecond wall clock),
  `market_regime = TREND_UP`, `run_id 17585`.
* **Phase 1** (`research.me_short_a_fill_feasibility_v1`, event_key
  `ecd13b2e0d73…`) captured the event (`feasibility_status=OK`).
* **Issue #208 chain** never saw it: `dds.scanner_setup` has no row,
  `research.me_short_a_source_signal_v1` / `me_short_a_event_lineage_v1` /
  `me_short_a_outcome_observation_v1` are empty for the event, while runtime
  #208 was `RUNNING` and polling without errors.

### 1.2 Cause chain (verified in code)

1. `MOMENTUM_EXHAUSTION` ∈ `ScannerOrchestrator.SHADOW_CONTROL_SCANNERS`;
   direction-gate-blocked candidates take the shadow path and rejoin `valid`
   (`valid = gate_accepted + shadow_candidates`).
2. `_apply_regime_filter` drops
   `direction == "SHORT" and regime == "TREND_UP"` (generic rule,
   `regime_filter_enabled = True` default). The candidate is routed to
   `stats["_regime_rejected_candidates"]`.
3. `scanner_runner._observe_prospective_with_fill` observes the
   regime-rejected candidate for research and Phase 1 captures it —
   **but nothing writes it to `dds.scanner_setup`**.
4. The #208 runtime discovers source candidates ONLY in `dds.scanner_setup`
   (`DbScannerSetupCandidateSource`), so the event can never enter the
   `source signal -> Phase 1 -> lineage -> outcome` chain. A Phase 1 snapshot
   with an empty lineage chain results.
5. `setups_saved` was computed **before** the regime filter
   (`orchestrator.scan_all_with_stats`), so the funnel reported
   `setups_saved = 1` while `dds.scanner_setup` stayed empty — the loss was
   invisible in telemetry.

### 1.3 Latent precision gap (must be fixed for any real event to pass)

`SetupCandidate.detected_at = ctx.evaluated_at = datetime.now(timezone.utc)`
carries microseconds (the incident: `.672455`). The #208 runtime's
`Candidate.__post_init__` required **whole seconds**
(`signal_time.second or microsecond -> ValueError`), so `_map_row` rejected
every real wall-clock row (`skipped_invalid_candidate`). Even with 1.2 fixed,
no real event could pass runtime intake. The frozen identity
(`sha256(symbol|SHORT|<utc isoformat signal_time>|…)`) is precision
preserving (timestamptz stores microseconds), so accepting sub-second
precision keeps the event_key byte-identical to Phase 1 — and losing a
microsecond would DESYNCHRONISE the key. The check was therefore relaxed.

## 2. Chosen solution and alternatives

**Chosen (Option A): a dedicated research-only source intake, read by the
runtime through a composite candidate source.**

* Scanner-side, fail-open research hook captures the ORIGINAL
  `MOMENTUM_EXHAUSTION` / `SHORT` event at signal time, **before and
  independent of every trading filter**, into the new append-only table
  `research.me_short_a_source_intake_v1` (migration 080), with the
  `capture_route` evidence (`TRADEABLE_FLOW`, `SHADOW_CONTROL`,
  `OBSERVE_ONLY`, `DIRECTION_GATE_BLOCKED`, `EXPECTANCY_FILTER_REJECTED`,
  `REGIME_FILTER_REJECTED`).
* The runtime reads `dds.scanner_setup ∪ research.me_short_a_source_intake_v1`
  through `CompositeCandidateSource` (both READ ONLY) into its **unchanged**
  frozen collector pipeline.
* Identity is one deterministic `event_key`, byte-identical across intake,
  Phase 1 and Issue #208 (delegated to
  `me_short_a_forward_outcome_lineage_v1.event_key`).

**Rejected (Option B): use Phase 1 rows as the intake source.** Couples the
intake to Phase 1's feature flag/activation and cannot answer the §7
observability question "candidates excluded by the regime filter but captured
for research" — Phase 1 rows carry no route metadata. It would also make a
Phase 1 error the intake's failure mode.

**Rejected (Option C): move `setups_saved` / relax the regime filter to let
the candidate into `dds.scanner_setup`.** Explicitly forbidden: no change to
the direction gate / regime filter / scoring / expectancy filter; nothing may
return to the trading flow.

### Why a composite source instead of a second runtime

The frozen runtime protocol payload (`runtime_protocol_payload()`) names its
source as `MOMENTUM_EXHAUSTION_SHORT_CANDIDATES` — a transport-agnostic
label. Changing the transport underneath it does **not** change the frozen
`PROTOCOL_HASH`, `freeze_ts`, or any capture-table semantics. A second
runtime would instead need a second frozen protocol — much larger blast
radius for zero gain.

## 3. Before / after event flow

**Before (confirmed gap):**

```
ME scanner SHORT/TREND_UP
  -> orchestrator: direction gate -> shadow rejoin -> regime filter DROPS
  -> stats[_regime_rejected_candidates]
       -> prospective observe + Phase 1 capture        (research evidence OK)
       -> dds.scanner_setup                            (NEVER written)
  -> #208 runtime reads dds.scanner_setup only         (event invisible)
  -> result: Phase 1 snapshot, empty lineage chain     (silent loss)
```

**After (fix):**

```
ME scanner SHORT/TREND_UP
  -> orchestrator: UNCHANGED gates; regime filter still DROPS from trading
  -> research hook (fail-open, before/independent of filters):
       research.me_short_a_source_intake_v1
         (capture_route=REGIME_FILTER_REJECTED, event_key = frozen identity)
  -> #208 runtime = CompositeCandidateSource(
         dds.scanner_setup  ∪  research.me_short_a_source_intake_v1)
       -> capture_source_signal -> record_event_lineage
       -> post-signal closed 5m candles -> finalize_outcome (240m, STOP_FIRST)
  -> result: source -> Phase 1 -> lineage -> outcome joins on ONE event_key
```

## 4. Invariants kept frozen

* `runtime_protocol_payload()` / `runtime_protocol_hash()` — byte-identical.
* `freeze_ts` of migrations 077/078/079 — untouched; no migration rewrites
  Phase 1 snapshots, the four #208 capture tables' writer, or the closed
  frozen OOS cohort (`prospective_observation` / `prospective_outcome`).
* Outcome semantics — 240-minute horizon, closed 5m candles only, fixed
  stop/target, `STOP_FIRST`, no lookahead, incomplete/unconfirmed
  observations never final (`SOURCE_INCOMPLETE` / `OUTCOME_UNMATURED`).
* Trading behaviour — direction gate, regime filter, scoring, expectancy
  filter, `app/paper`, `app/live`: untouched (see §7 verification).

## 5. No-backfill guarantee (the incident event can never enter)

Three independent layers:

1. **DB:** `CHECK (signal_time > intake_freeze_ts)` on
   `research.me_short_a_source_intake_v1`; `intake_freeze_ts` is set once at
   explicit activation (AFTER the incident) and is immutable
   (guard trigger, migration 080).
2. **Writer:** strict `event_time > intake_freeze_ts`, else the capture is
   refused with `PRE_FREEZE` (logged).
3. **Reader:** the runtime intake source applies
   `max(#208 freeze_ts, intake_freeze_ts)` as the keyset lower bound and is
   fail-closed until the activation row is `RUNNING`.

The original #208 `freeze_ts` is intentionally NOT modified — modifying it
would retroactively change the frozen #208 cohort.

## 6. Idempotency, replay and error isolation

* Every collector write is one atomic
  `INSERT ... SELECT ... WHERE EXISTS (activation RUNNING) ... ON CONFLICT
  (event_key) DO NOTHING` — safe under retry and redelivery after a restart.
* Runtime intake cursors advance only after a fully handled page
  (`HANDLER_RETRY` stalls without advancing); the composite keeps one
  sub-cursor per transport and only records a sub-page as handled when the
  composite page containing it was handled in full. Cross-transport duplicates
  are collapsed by the deterministic `event_key` and, across fetches, by
  `ON CONFLICT DO NOTHING`.
* Research capture errors never affect the scanner: the hook fails open
  (`capture_with_status` returns `ERROR`; the hook rolls back research state
  only), and persistence errors get their own diagnostics —
  `dds.scanner_error.error_type = 'ME_SHORT_A_SOURCE_INTAKE_ERROR'` plus the
  module log — never silent.
* Protection from silent loss after a successful Phase 1 capture: the intake
  write happens BEFORE the Phase 1 call at every route, and
  `sql/research/me_short_a_source_intake_diagnostics_v1.sql` Q5 lists every
  post-activation Phase 1 snapshot without source/lineage evidence.
* Intake reads fail SOFT and lossless (empty page + `error_count`/`last_error`,
  cursor unchanged) so a read problem can never stall the dds transport or
  drop rows.

## 7. Observability (read-only; `sql/research/me_short_a_source_intake_diagnostics_v1.sql`)

| Question | Query |
|---|---|
| source candidates seen / source events saved | Q3 (`source_events_saved`), runtime heartbeat counters (Q7) |
| candidates excluded by the regime filter but captured for research | Q2 `capture_route = 'REGIME_FILTER_REJECTED'`, Q3 |
| Phase 1 snapshots with confirmed link | Q4 |
| `MISSING_LINEAGE` / `OUTCOME_UNMATURED` / `SOURCE_INCOMPLETE` / `MATCHED_FINALIZED` | Q6 (all four always shown) |
| persistence / retry errors | Q7 (heartbeat `errors`/`last_error`, `dds.scanner_error`) |
| silent-loss detector | Q5 |
| event_key integrity across the chain | Q8 |
| historical gaps (incl. the incident), excluded from cohort stats | Q9 |

The `setups_saved` funnel metric is now computed **after** the regime filter
(it reflects what is actually persisted), so a phantom
`setups_saved = 1 / dds.scanner_setup = 0` can no longer hide a capture gap.

## 8. Changed files

* `sql/migrations/080_me_short_a_source_intake_v1.sql` — NEW: intake +
  activation tables, strict forward-only CHECK, append-only guards.
* `sql/grants/me_short_a_source_intake_least_privilege_v1.sql` — NEW:
  `SELECT, INSERT` for `trad_bot`, `SELECT` for runtime/audit roles.
* `app/research/me_short_a_source_intake_v1.py` — NEW: fail-open scanner-side
  writer (`capture` / `capture_with_status`), frozen-identity `compute_event_key`.
* `app/research/me_short_a_forward_outcome_runtime_sources_v1.py` — shared
  `reconstruct_recent_high_window`; NEW `DbSourceIntakeCandidateSource`
  (READ ONLY, keyset `(signal_time, event_key)`, fail-soft reads) and
  `CompositeCandidateSource`.
* `app/research/me_short_a_forward_outcome_runtime_v1.py` — `Candidate`
  accepts full-precision `signal_time` (was: whole seconds only — latent
  gap 1.3); idempotency token is the deterministic `event_key`; typing
  widenings (`market_context`, `signal_time_quote`, page cursor). Frozen
  protocol payload/hash and `COUNTER_NAMES` untouched.
* `app/research/me_short_a_forward_outcome_runtime_runner_v1.py` —
  `build_runtime` wires the composite source (dds + intake) on the READ ONLY
  connection.
* `scanner_runner.py` — NEW `_capture_me_short_a_source` fail-open hook with
  dedicated connection + `ME_SHORT_A_SOURCE_INTAKE_ERROR` diagnostics; call
  sites at all four research routes (`TRADEABLE_FLOW`/`SHADOW_CONTROL`,
  `OBSERVE_ONLY`/`DIRECTION_GATE_BLOCKED`, `EXPECTANCY_FILTER_REJECTED`,
  `REGIME_FILTER_REJECTED`), outside the `prospective_obs` gate and before
  the Phase 1 capture.
* `app/scanners/orchestrator.py` — `setups_saved` computed after the regime
  filter (funnel truth only). No filter/gate/scoring behaviour change.
* `sql/research/me_short_a_source_intake_diagnostics_v1.sql` — NEW: §7.
* Tests: `tests/test_me_short_a_source_intake_v1.py`,
  `tests/test_me_short_a_source_intake_migration_080_v1.py`,
  `tests/test_me_short_a_source_lineage_gap_fix_v1.py`.

## 9. Activation runbook note (SEPARATE approved step)

1. Apply migration 080 and the grants file (they are source-controlled only).
2. Freeze the boundary AFTER deployment, in one explicit transaction:
   `UPDATE research.me_short_a_source_intake_activation_v1
    SET status = 'RUNNING', intake_freeze_ts = NOW()
    WHERE experiment_id = 'ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1'
      AND status = 'READY_TO_START';`
   (the guard trigger enforces `READY_TO_START -> RUNNING` and the set-once
   freeze). Fix code + activation define ONE clean cohort boundary.
3. Set `ME_SHORT_A_SOURCE_INTAKE_ENABLED=1` for the scanner process
   (fail-closed by default: without the flag and the RUNNING row nothing is
   captured).
4. Restart `trad-bot-me-short-a-forward-outcome` to pick up the composite
   candidate source (deployment step; requires the usual approval).
5. Confirm E2E on a NEW real prospective ME SHORT event (see §10).

## 10. Limitations / risks

* **Do NOT close Issue #208 on these tests alone.** Closing requires a
  production E2E confirmation on a NEW real prospective ME SHORT event after
  deployment: intake row → runtime source signal → Phase 1 linkage on the
  identical `event_key` → finalized outcome, verified via Q2/Q4/Q6/Q8.
* The regime-rejected event is captured for research but is still NOT saved
  to `dds.scanner_setup` (by design — the trading filter is unchanged). The
  runtime sees it through the intake transport instead.
* If the scanner-side research capture itself fails (DB outage at signal
  time), the event is reported (`ME_SHORT_A_SOURCE_INTAKE_ERROR`) but not
  retried into the intake — a research-only evidence gap, never a trading
  loss. The incident class is now loud, not silent.
* The closed-window reconstruction in the runtime remains honest: for
  signals whose frozen `reference_price` included a non-closed candle (the
  ME scanner reads `candles_5m[-5:]`, which can include the forming candle),
  the window is dropped with `recent_high_window_reference_mismatch` and the
  event lands in `SOURCE_INCOMPLETE` — reporting, never imputation. The
  source/lineage layer still records the event.
* Pre-existing test failures in `tests/test_oos_reject_dedup_fix.py`
  (`test_no_duplication_when_dedup_keeps_reject`,
  `test_no_duplication_all_reject`) also fail on pristine `origin/main`: they
  expect `setups_saved` to count ME-OOS observe-only rows while
  `test_no_duplication_...::TEST 3` expects the opposite invariant. This
  contradiction predates this change and involves ME OOS routing semantics,
  which are out of scope here.
* Migration 080 is not applied to production by this PR; until activation,
  the intake source is fail-closed and the runtime behaves exactly as today
  (dds transport only).
