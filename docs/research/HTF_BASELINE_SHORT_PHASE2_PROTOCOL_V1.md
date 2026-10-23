# HTF_BASELINE_SHORT_PHASE2_PROTOCOL_V1

**Status: `FROZEN` (Issue #197) — six parameter values operator-approved on 2026-10-23 in #186 and unchanged by the freeze; the freeze timestamp, activation and observe-only startup remain separate gates. `FROZEN` authorizes nothing by itself.**

| Field | Value |
|---|---|
| Issue | #176 (predecessor #174); operator approval #186; freeze mechanism #197 |
| Approved parameter decisions | 6/6 on 2026-10-23; no additional conditions |
| Freeze timestamp | `None` in Git — chosen at the operator GO as an operator-approved **freeze record** (timestamp authority V1, see `HTF_BASELINE_SHORT_PHASE2_ISSUE197_FROZEN_TIMESTAMP_AUTHORITY_V1.md`) |
| Study family | `HTF_BASELINE_SHORT_CAUSAL_EPISODE_STATEFUL_OBSERVER_V1` |
| Experiment id | `HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1` |
| Direction | `SHORT` (observe-only) |
| Base commit | `4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba` |
| Machine-readable source | `app/research/htf_baseline_short_phase2_protocol_v1.py` |
| Canonical fingerprint, FROZEN payload (SHA-256) | `22fa0c08eed92df7bb6e94576987305343a71083f3129f3ce4acd07c287cb3f0` — pinned in `FROZEN_PROTOCOL_FINGERPRINT`, regression-verified |
| Canonical fingerprint, reviewed candidate (audit trail) | `39accac9ddddfd1495202cd708d7dc95b6c6b93ac79d819ff3d5a02beec60de1` — reproducible from the FROZEN payload by restoring `status = PROTOCOL_NOT_FROZEN` (the only delta) |
| Delivery guarantee | **AT-LEAST-ONCE + IDEMPOTENT writes** (exactly-once NOT claimed) |

The fingerprint is computed over the canonical JSON serialization
(`sort_keys`, separators `(",", ":")`) of `PROTOCOL_V1` in the module above.
Any semantic change to that module changes the fingerprint and requires a new
protocol version, a new experiment version and a new freeze.

---

## 1. Scope

Phase 2 is a **prospective, observe-only** measurement of the funnel

`1H resistance level formation → first touch → causal SHORT reaction confirmation (FIRST_CONFIRM) → forward behavior`

on **closed** candles only. Phase 2 does **not** establish an executable edge,
does **not** route orders, does **not** evaluate trade expectancy
(out of scope, Issue #170) and does **not** consume or modify the frozen
Point-B OOS protocol.

## 2. Causal data contract

* Time base: epoch milliseconds UTC; `close_time = open_time + duration`;
  a bar is usable iff `close_time <= decision_asof`.
* Decision time = close of the latest **closed** 5m bar. Forming candles are
  forbidden at every stage.
* 5m series: aligned to 300000 ms, strictly contiguous, no duplicates
  (identical **or** conflicting), no out-of-order bars, last close must equal
  the decision time (no stale prefix). Violations are **fail-closed**.
* 1H level series: an hour is usable iff it contains exactly 12 contiguous
  closed 5m bars (or a closed provider 1H bar) and is closed at decision time.
  Partial/missing hours are **excluded and reported** — never silently dropped.

## 3. Level contract (unified S/R + ATR)

* Strict swing resistance: 4 closed 1H bars lower-or-equal on each side with at
  least one strictly lower on each side (`SWING_LEFT = SWING_RIGHT = 4`).
* A contiguous tied-high plateau collapses to **one** representative level
  (P2-07); independent same-price swings are **separate** levels (P2-11).
* `formation_time` = plateau first-bar close; `available_at` = formation +
  4 closed hours (right confirmation). A level is causally unusable before
  `available_at`.
* Touch count from closed 1H bars **strictly before** formation, tolerance
  `max(price*0.003, decision_atr*0.25)` where `decision_atr` is the causal ATR
  of the frozen Point-B implementation evaluated **at the decision bar**
  (unified ATR contract; the fallback-ATR call pattern of the old observer is
  not reproduced).
* Eligibility at decision: `available_at <= decision_time` AND
  `price > reference` (last closed 5m close) AND `touch_count >= KEY_LEVEL_MIN_TOUCHES`.
* Touch predicate: `high >= level` and min ATR-normalized distance ≤
  `KEY_LEVEL_TOUCH_TOLERANCE_ATR` (frozen constant), gated by `available_at`.
* **Level identity (window-independent):**
  `sha256(symbol|direction|formation_close_ms|available_ms|price:.12g)`.

## 4. Episode identity and lifecycle

* **Episode identity:** `sha256(experiment_id|symbol|direction|level_id|first_touch_open_ms)`.
  Mutable counters, checkpoint generations, restart counts, rolling-window
  indices and wall-clock time are **forbidden** in identity.
* Event identity: `episode_id + ":FIRST_CONFIRM"`; at most one FIRST_CONFIRM
  per episode.
* States: `CONTACT → CONFIRMED | UNRESOLVED{RECONTACT_SUPERSEDED_PENDING, SOURCE_WINDOW_EVICTED}`.
* A new touch after the contact run broke supersedes the pending episode
  (P2-10); a same-step recontact rejects the same-step confirmation (never
  emitted, never attributed). A confirmation on a touching bar is allowed only
  while the run is active (P2-06/P2-09).
* Pending may confirm after the run ends (FIRST_CONFIRM). No timeouts.
* Terminal episodes are never re-attributed; a post-terminal confirmation
  raises. Each 5m step is all-or-nothing.
* **Terminal level-retirement policy:** eligibility loss is **non-terminal**
  (diagnostic only) — marked `OPERATOR_APPROVED`; any alternative
  requires a new protocol version.

## 5. Freeze boundary

* Phase 2 has its **own** freeze. Point-B's `2026-10-06T14:00:00Z` must never
  be reused; experiment IDs must never be reused.
* Eligible iff `signal_time > freeze_ts` **and** `first_touch_time > freeze_ts`
  (both strict `>`).
* Episodes whose first touch is at/before the freeze, warmup-origin episodes,
  and episodes restored from a pre-freeze checkpoint are **not** eligible.
* Freeze sources (protocol, registry, database) must agree exactly; a missing
  or mismatched freeze rejects everything (fail-closed). Historical events are
  never eligible observations.

## 6. Persistence

* Dedicated tables `research.htf_baseline_short_phase2_experiment` /
   `research.htf_baseline_short_phase2_observation` (migration
  `074_htf_baseline_short_phase2_registration.sql`).
* Observations are append-only immutable (trigger rejects UPDATE/DELETE);
  durable dedup on `UNIQUE (experiment_id, source_event_id)` with
  `ON CONFLICT DO NOTHING`.
* The insert function re-enforces the freeze strictly at the DB boundary and
  refuses writes unless the experiment is `RUNNING` with a freeze and
  fingerprint.
* Registration starts `PENDING`, `freeze_ts = NULL`, `observer_enabled = FALSE`.
  Activation is only possible through
  `research.htf_baseline_short_phase2_activate()` — never automatic.

## 7. Checkpoint / restart

* Versioned checksummed envelope (`HTF_P2_STATE_V1`, SHA-256) with atomic
  tempfile + fsync + `os.replace`. Interrupted writes never clobber the last
  valid checkpoint; corrupt/truncated/legacy checkpoints raise and are never
  reinterpreted (`HTF_176_COMBINED_CHECKPOINT_V2` explicitly rejected).
* Single-writer lease with heartbeat; stale (crashed) lease takeover only with
  explicit `allow_stale_takeover`. A second live writer is refused.
* Checkpoints record the freeze boundary and protocol fingerprint; a checkpoint
  from a different boundary is rejected on resume.

## 8. Metrics separation

* **Observability (health):** emitted confirmations, duplicate suppressions,
  coverage gaps, checkpoint writes, heartbeat, pending episode count.
* **Readout (diagnostic only):** confirmation counts by symbol, forward path
  statistics, episode funnel.
* **Execution/expectancy/trade metrics: out of scope** (Issue #170), except if
  and when trade expectancy is explicitly evaluated under its own protocol.
* Retrospective PnL parameter tuning is **prohibited**; parameter changes after
  freeze require a new experiment version and a new freeze.

## 9. Maturity gates (readout before maturity is diagnostic only)

| Gate | PROPOSED value | Status |
|---|---|---|
| minimum confirmations | 30 | `OPERATOR_APPROVED` |
| minimum symbols | 5 | `OPERATOR_APPROVED` |
| minimum active days | 14 | `OPERATOR_APPROVED` |
| geometry proximity | 300000 ms | `OPERATOR_APPROVED` |
| geometry price band | 20 bps (0/10/20/30/40 sensitivity) | `OPERATOR_APPROVED` |

## 10. Authorization

`PHASE_2_GO` requires operator approval of this protocol **after** the repeat
readiness audit passes. Local test success alone never yields `PHASE_2_GO`.
Merging code must not activate Phase 2.
