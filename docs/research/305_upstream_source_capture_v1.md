# Issue #305 — Independent Upstream Source Coverage

**Experiment id:** `ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1`
**Protocol version:** `1.0.0`
**Status:** `#305 IMPLEMENTATION_ONLY` — capture built, **NOT** activated,
**NOT** deployed, **NO** production changes
**Parent:** #295 `ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1` (`OOS_NOT_STARTED`)
**Dependency:** #296 research observer (PR #303 merged; `DEPLOY_HOLD`)

---

## 1. Stage A — Architecture audit and verdict

### Verdict: `NEW_CAPTURE_REQUIRED`

Two existing mechanisms were audited before any code was written:

| Candidate source | Verdict | Reason |
| --- | --- | --- |
| #296 `ResearchObserver` (`app/research/observer.py`) | **Rejected** | It observes the **V3 ledger** (`dds.me_long_v2_inverse_short_v3_event`) *after* V3 scoring — the exact later stage we must measure against. Its `save_observation()` returns `None` for **both** duplicate and error (no honest outcome signal), its `research.research_observation` schema does not exist in production, and it is itself on `DEPLOY_HOLD`. Not an independent source. |
| Existing V1/V2/OOS2/V3 capture hooks (`scanner_runner.py` → `app/paper/me_long_v2_inverse_short_*`) | **Rejected** | They publish from the **post-scoring** `scored` list (`stats["_me_inverse_short_source_events"]` etc.) — after scoring, dedup, geometry, direction gate and regime filter. They capture *filtered* outputs and write into their own contour ledgers. Not detector-level, not independent. |

Consequence: the detector output must be captured by a **new, isolated,
pre-scoring** mechanism (`NEW_CAPTURE_REQUIRED`), wired immediately after
`candidates = scanner.scan(ctx)`, stored in a brand-new research schema.

### Independence contract (what the capture guarantees)

```
scanner.scan(ctx)
      │
      ▼  candidates (raw detector output)
      │
      ├─▶ [NEW] upstream publication (fail-open, best-effort)
      │        stats["_me_upstream_source_events"]
      │        (MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 + LONG only)
      │        │
      │        ▼
      │        scanner_runner._capture_me_upstream_source_v1()
      │        → research_me_upstream_coverage.source_event   (NEW schema)
      │
      ▼
   scoring (source_score / source_state / source_market_regime)
      ▼
   dedup (self.dedup.filter_new)          ─┐
   risk geometry (validate_risk_geometry)  │  UNCHANGED — the capture is
   direction gate (filter_candidates)      │  invisible to all of these
   regime filter (_apply_regime_filter)    │  and to every trading decision
   expectancy filter                       ─┘
      ▼
   V3 capture → dds.me_long_v2_inverse_short_v3_event  (READ-ONLY input
                                                        for the reconciler)
```

* The upstream capture point is **before** scoring, dedup, geometry,
  direction gate, regime filter and the expectancy filter.
* The upstream ledger is a **separate schema**; nothing downstream reads it;
  the only reader is the read-only coverage reconciler.
* The capture is **fail-open towards the scanner** (any capture error is
  swallowed and recorded; the scan cycle continues) and **fail-closed
  towards research** (an unknown/errored capture is never silently treated
  as healthy).
* **Scope note:** capturing after `scanner.scan(ctx)` proves *detector-output
  capture completeness* only. It does **NOT** prove detector correctness or
  market-data completeness.

## 2. Identity (deterministic, protocol-independent)

```
normalized_identity = "<scanner_name>|<symbol>|<direction>|<entry_timeframe>|<signal_candle_open_ms>"
source_event_key    = SHA256("ME_UPSTREAM_SOURCE_V1|" + normalized_identity)
```

* `signal_candle_open_ms` — the explicit `signal_candle_open_time` of the
  candidate, or `detected_at` floored to the 300 000 ms (5m) grid — exactly
  the derivation V3 uses for `lineage->>'source_identity'`.
* The key does **not** depend on V3/OOS2 protocol versions, on scoring
  results, or on capture-specific fields.
* `uq_me_upstream_coverage.uq_me_ups_event_identity UNIQUE(source_identity)`
  guarantees one ledger row per original identity.

### Immutable source fields (conflict set) vs expected variance

Two SEPARATE comparisons (Codex review P1):

* **Redelivery integrity check** — `immutable_source_hash` covers ONLY the
  stable identity + immutable provenance: `source_identity`,
  `source_scanner`, `source_scanner_version`, `source_direction`,
  `source_entry_timeframe`, `source_signal_candle_open_time`,
  `source_pipeline_fingerprint`. A redelivery of one `source_event_key`
  that differs on ANY of these is a REAL integrity conflict →
  `EVENT_KEY_LINEAGE_CONFLICT` → sticky `BLOCKED`.
* **Cross-ledger snapshot comparison** — `comparable_lineage_hash` (the set
  above + price facts / entry-stop-target geometry / the frozen
  9-feature detector subset: `exhaustion_magnitude`, `body_ratio`,
  `rsi_confirmation`, `volume_ratio`, `rr_ratio`, `stop_distance_atr`,
  `rsi_14`, `rsi_delta_3`, `entry_confirmation_rsi_rising`). Drift on the
  volatile subset is reported diagnostically (`snapshot_drifts`); only
  drift on the immutable fields is a `lineage_mismatch` →
  `INTEGRITY_CONFLICT`.

Deliberately **outside** the conflict set: `source_setup_id` and
`source_signal_time` (a fresh `setup_id`/`detected_at` per re-detection)
AND the volatile detector facts — price / geometry / feature values —
which legitimately drift between re-detections while the signal candle is
still forming (`SetupCandidate`: "detected_at and prices may change while
that candle is still forming"). All of that stays an idempotent
`DUPLICATE` with the **first-capture snapshot preserved verbatim**
(acceptance criterion 5).

## 3. Research schema `research_me_upstream_coverage` (migration 094)

| Table | Purpose |
| --- | --- |
| `activation` | One experiment row; `status` (`READY`→`RUNNING`); **immutable** `freeze_ts` / `protocol_version` / `protocol_fingerprint` (triggers reject changes) |
| `source_event` | The upstream ledger — `source_event_key` PK, `source_identity` UNIQUE, `lineage jsonb`, `lineage_hash`, `comparable_lineage_hash`; **append-only** (trigger rejects UPDATE/DELETE); CHECK `signal_time > activation_freeze_ts` (backfill impossible) |
| `rejected_event` | Durable rejection ledger (`PRE_FREEZE`, `INCOMPLETE_FEATURES`, `INVALID_EVENT`, …) — rejected events are observably excluded, never silently dropped |
| `capture_health` | Per-delivery health: `status`, `events_inserted`, `duplicates`, `errors`, `delivery_latency_seconds`, `diagnostics` |
| `coverage_audit` | Persisted reconciliation results (written explicitly, never automatically) |
| `capture_state_event` | Append-only `HEALTHY`/`BLOCKED` state; a lineage conflict makes `BLOCKED` **sticky** until manual recovery |

`deploy/sql/305_upstream_source_role.sql` — least-privilege role
(`tradbot_me_upstream_source`): SELECT on `dds.me_long_v2_inverse_short_v3_event`
(read-only reconciler input), SELECT/INSERT only inside
`research_me_upstream_coverage`, no DDL, no writes to `dds.*`/`research.*`.
`deploy/sql/305_upstream_source_recovery.sql` — manual recovery appends
`RESUMED` state; it **never** resets `freeze_ts` and **never** backfills.

## 4. Activation and prospective-only boundary

* Runtime flag `ME_UPSTREAM_SOURCE_CAPTURE_V1_ENABLED` is checked **first**
  — with the flag off the capture performs **no DB connection and no write**
  (acceptance criterion 1).
* Capture starts only when the activation row is `status='RUNNING'` **and**
  carries a frozen `freeze_ts` matching the code's
  `protocol_fingerprint()`; otherwise `NOT_ACTIVE`.
* Strict boundary: `signal_time > freeze_ts` (no equality), enforced in
  Python **and** by a DB CHECK. No backfill path exists anywhere.
* `freeze_ts` is immutable (DB triggers + admin script refuses to overwrite);
  it can never auto-reset.
* Boundary semantics: `signal_time` is the **detection time**
  (`detected_at`), matching the V3/OOS2 contours' established rule. An event
  detected after `freeze_ts` whose signal candle opened just before it is
  admitted (it was detectable only after activation); historical scanning is
  impossible by construction.

## 5. Capture outcome vocabulary (fail-open / fail-closed)

`DISABLED`, `NOT_ACTIVE`, `PRE_FREEZE`, `INVALID_SCANNER`, `INVALID_EVENT`,
`CAPTURED`, `DUPLICATE`, `INTEGRITY_CONFLICT`, `BLOCKED`, `ERROR` — every
delivery produces observable diagnostics (`capture_health` /
`rejected_event` / `capture_state_event`). The scanner never sees an
exception; research never sees a hidden loss (an errored capture is
`ERROR`/`BLOCKED`, and the reconciler treats unknown activity as
`SOURCE_COVERAGE_UNKNOWN`, never as PASS).

## 6. Coverage reconciliation (read-only)

`reconcile_coverage(connection, as_of, grace_seconds=900)` matches
`research_me_upstream_coverage.source_event` ↔
`dds.me_long_v2_inverse_short_v3_event` on the normalized original identity,
over the **intersection of confirmed activity periods**, reporting:
upstream/V3/matched counts, upstream-only (lag vs gap after grace), V3-only,
duplicate identities, lineage mismatches, capture errors, delivery latency
and unprocessed-within-grace.

**Window basis (two distinct clocks — do not conflate):**

* **Scope (the audited window)** is judged on the **original event time**
  (`signal_time`) — identical on both ledgers and part of the signal's
  identity. `window_start = max(freeze_ts, first-event-time-upstream,
  first-event-time-v3)`; rows with `signal_time < window_start` are
  outside-window evidence — never gaps, never PASS evidence. Ingestion
  times (`captured_at`/`created_at`) deliberately do **not** move the
  window: they differ between the capture routes by construction (capture
  latency, retries, recovered re-deliveries) and using them would exclude
  exactly the delayed events the grace window exists to tolerate.
* **Grace (lag vs gap)** is judged on **ingestion time**: a counterpart had
  `grace_seconds` to land after the event reached the delivering ledger
  ("delays accounted"), never from raw detection time.

Verdict precedence (strict):

```
INTEGRITY_CONFLICT > COVERAGE_GAP > SOURCE_COVERAGE_UNKNOWN > COVERAGE_LAG > COVERAGE_PASS
```

* `COVERAGE_PASS` requires matched ≥ 1 (`SUFFICIENT_EVIDENCE`) and no
  confirmed gaps/mismatches.
* `COVERAGE_LAG` — unmatched upstream events still inside the 900 s grace.
* `COVERAGE_GAP` — unmatched events past grace (confirmed miss).
* `SOURCE_COVERAGE_UNKNOWN` — no activation / no freeze / capture
  `BLOCKED`-free but no confirmed activity / unknown activity period.
* `INTEGRITY_CONFLICT` — capture state `BLOCKED` or lineage mismatches.

An absent event is **not** an error until activation, grace delays and
capture state are accounted for. Saving an audit row is a separate explicit
step (`save_coverage_audit`) — reconciliation itself never writes.

## 7. Tests

| Suite | Scope | Result (this machine) |
| --- | --- | --- |
| `tests/test_me_upstream_source_identity_v1.py` | identity determinism, contracts, lineage + immutable-source hashes | PASS (52) |
| `tests/test_me_upstream_source_capture_v1.py` | flag/activation/freeze/idempotency/redelivery-variance (forming-candle drift)/conflict/fail-open (acceptance criteria 1–12) + PgUpstreamSourceStore SQL contract | PASS (48) |
| `tests/test_me_upstream_source_coverage_v1.py` | matching + verdicts LAG/GAP/UNKNOWN/INTEGRITY_CONFLICT (pure + SQL path doubles) + activity-window-basis regressions (ingestion skew, delayed recovery, mismatched activity periods) + snapshot-drift vs immutable-mismatch split | PASS (49) |
| `tests/test_me_upstream_source_wiring_v1.py` | static audit: capture placement before scoring/gates; runtime publication; runner hook fail-open; connection recovery (invalidate/bounded backoff/reconnect); regression of all 5 existing capture hooks; migration 094 audits; deploy safety | PASS (31) |
| `tests/test_me_upstream_source_pg_integration_v1.py` | acceptance criteria 1–12 + activity-window regressions + forming-candle-drift duplicate on **real PostgreSQL**, isolated scratch DB `me_ups305_it_*`, migration 094 + stand-in V3 table | prepared; **skipped** here (no local PostgreSQL server; opt-in `ME_UPS305_PG_INTEGRATION=1`) — 26 scenarios |

Static audits prove: publication is before scoring/dedup/geometry/gates/
regime/expectancy; the capture package imports nothing from `app.paper` /
`app.db` / exchange code; no trading module references the new mechanism.

## 8. Rollout plan (NOT executed by this PR)

1. Merge PR (after review) — code stays inert: flag default `0`.
2. Apply `sql/migrations/094_me_upstream_source_capture_v1.sql` to the
   research DB (separate approved change; not part of this PR).
3. Apply `deploy/sql/305_upstream_source_role.sql`.
4. On a **separately approved** GO only:
   `ME_UPSTREAM_SOURCE_CAPTURE_V1_ENABLED=1` +
   `scripts/me_upstream_source_v1_admin.py --activate --freeze-ts <now>`.
5. Wait for prospective events; run `--audit` / `reconcile_coverage`;
   interpret with the scope note of §1.

## 9. Rollback plan

1. Set `ME_UPSTREAM_SOURCE_CAPTURE_V1_ENABLED=0` (capture stops; scanner is
   unaffected either way — fail-open).
2. Optional: `deploy/sql/305_upstream_source_recovery.sql` appends
   `RESUMED`/`MANUAL_ADMIN_RECOVERY` state — never resets `freeze_ts`.
3. The schema/ledger is research data; dropping it (if ever required) is an
   explicit approved DB change. Nothing in trading reads it.

## 10. Known limitations

* Detector-output completeness only — not detector correctness, not
  market-data completeness (§1 scope note).
* `SOURCE_INDEPENDENCE` remains **UNPROVEN** until a separate activation
  yields prospective events and a clean coverage audit.
* PostgreSQL integration tests require a test PostgreSQL server
  (`ME_UPS305_PG_IT_*`, opt-in) — they are skipped in environments without
  one and must run in CI/DB-capable staging before deployment.

## 11. SOURCE_ONLY routing defect (2026-11-25) — root cause, fix, gap

### Defect

With `ME_INVERSE_SHORT_V2_SOURCE_ONLY=1` the `source_only` branch of
`scanner_runner.run_scan_cycle` rebuilt `symbol_stats` keeping only
`_me_inverse_short_source_events` and **silently dropped**
`_me_upstream_source_events`. The #305 delivery loop then read an empty
list every cycle: the upstream capture was ACTIVE (`RUNNING`, frozen
`freeze_ts = 2026-11-24 13:31:53.365952+00`) but received zero events
(audit 2026-11-25 06:23 UTC: `SOURCE_COVERAGE_UNKNOWN /
NO_UPSTREAM_ACTIVITY`, capture errors = 0 — fail-open hid nothing, the
events never reached the hook).

Full data-flow audit confirmed this is the **only** loss point: the
orchestrator publishes `stats["_me_upstream_source_events"]` immediately
after `scanner.scan(ctx)` (pre-scoring, pre-dedup, pre-geometry, pre-gates,
fail-open); no other assignment to `symbol_stats` exists and every
downstream consumer reads via `.get(key, [])`.

### Fix (research routing only)

The `source_only` rebuild now preserves BOTH research source streams
(`_me_inverse_short_source_events` + `_me_upstream_source_events`) and
nothing else; `candidates = []` and all other suppressions are unchanged.
SOURCE_ONLY semantics — no ordinary setup routing, no paper/live side
effects — are intact. Regression scenarios A–E live in
`tests/test_me_inverse_short_v2_source_only.py` (scenario A reproduces the
loss on pre-fix code).

### Deployment-gap accounting (prospective integrity)

* `freeze_ts` is **unchanged** (`2026-11-24 13:31:53.365952+00`); no
  backfill is possible or attempted.
* The interval `(freeze_ts, fix_deploy_ts)` is a period of **potentially
  incomplete capture** (the routing defect). It is recorded explicitly via
  the reconciler's `unverified_intervals` parameter — pure diagnostics,
  persisted in `coverage_audit.diagnostics`. The `COVERAGE_PASS` formula,
  the historical-exclusion rules and every verdict precedence are
  byte-identical.
* The gap can NOT be validated retroactively by construction: pre-window
  rows are `outside_window_*` (never PASS evidence, never gaps), `PASS`
  requires `matched >= 1` inside the audited window, and "no errors" never
  yields a validated verdict (there is no `SOURCE_VALIDATED` state).
* `fix_deploy_ts` is captured at deployment time and passed as
  `unverified_intervals=[(freeze_ts, fix_deploy_ts)]` on the first
  post-deploy audit. Recommended: run one read-only reconcile BEFORE and
  AFTER the fix deploy and archive both results.

### Post-deploy verification checklist (VPS)

1. Record `fix_deploy_ts`; confirm git HEAD contains the fix.
2. New detector-level events appear in
   `research_me_upstream_coverage.source_event` (prospective, post-fix).
3. Read-only reconciliation over the post-fix window: `COVERAGE_PASS`
   (matched >= 1) or an honest `COVERAGE_LAG/GAP` — never UNKNOWN hidden
   as success; integrity conflicts = 0.
4. Persist one `coverage_audit` row with
   `unverified_intervals=[(freeze_ts, fix_deploy_ts)]` documenting the
   gap.
5. #296 stays `DEPLOY_HOLD` until a separate GO decision.
