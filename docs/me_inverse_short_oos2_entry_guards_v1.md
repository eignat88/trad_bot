# ME_INVERSE_SHORT_OOS2_ENTRY_GUARDS_AND_CAPACITY_V1 — design, audit and runbook (Issue #287)

Working branch: `feat/287-me-inverse-short-oos2-entry-guards` (Draft PR #288).
This document is the development audit note and the design/cutover record for
the OOS-2 entry guards.  Mode: **code / tests / docs only** — no production DB
writes, migrations execution, deployment, service restarts, activation, drain,
backfill, trading actions or PR merge are performed by this task.

---

## 1. Baseline audit (Этап 1) — facts found in `main`

Git baseline at audit time: branch `feat/287-me-inverse-short-oos2-entry-guards`
at `c8e433c` (= `origin/main` merge of PR #286 + the #287 development brief).

### 1.1 Where admission / capacity / position creation live today (OOS-2 v1)

| Concern | File | Location |
|---|---|---|
| Entry loop, pending → TTL | `app/paper/me_long_v2_inverse_short_oos2_v1/engine.py` | `open_entries()` (L852), `_open_event()` (L1053), `_sweep_stale_events()` (L769) |
| Position creation (final admission) | `app/paper/me_long_v2_inverse_short_oos2_v1/store.py` | `save_position()` (PG, L1720) → `LOCK_EVENT_FOR_ADMISSION_SQL` → `LOCK_ACTIVATION_FOR_ADMISSION_SQL` → `INSERT_POSITION_SQL` (lock-first, TTL judged at `statement_timestamp()`) |
| Capacity | `engine.py` | in-memory `len(self.open_positions) >= MAX_OPEN_POSITIONS` (12, OOS-2-local, `protocol.py` L92) — **the only cap today**; no DB-level cap, no symbol guard, no cooldown |
| Ambiguous COMMIT | `engine.py` | `_adopt_stored_position()` (L1202), `_entry_facts_match()` (L1156) |
| Partial TP legs | `engine.py` / `store.py` | `_partial_close()` (L2641), `commit_partial_exit()` — exactly-once on `(position_key, leg_seq)` |
| TTL sweep disposition | `store.py` | `mark_event_expired()` (L1641): `UPDATE … WHERE status='CAPTURED' AND NOT EXISTS (position)` |

**Finding (the confirmed problem):** two different `event_key`s of the same
symbol can both pass the in-memory gate and both insert — there is no
`UNIQUE(symbol) WHERE status='OPEN'`, no serialized admission critical section
and no cooldown; the only uniqueness is `event_key` / `position_key`.  A
multi-worker or concurrent-cycle race can create duplicate logical OPEN
positions per symbol and up to 12 per *cycle*, not 12 total.

### 1.2 PostgreSQL tables (OOS-2 v1, migrations 088 + 089)

* `dds.me_long_v2_inverse_short_oos2_v1_activation` — status + `protocol_fingerprint` + `freeze_ts`.
* `dds.me_long_v2_inverse_short_oos2_v1_event` — append-only source events, idempotent `event_key`, four-state disposition (`CAPTURED`/`ENTERED`/`EXPIRED`/`CAPACITY_REJECTED`), lineage JSONB.
* `dds.me_long_v2_inverse_short_oos2_v1_position` — logical positions, namespaced `position_key = 'ME_LONG_V2_INVERSE_SHORT_OOS2_V1:<event_key>'`, immutable entry facts + deadline + original size (trigger `trg_…_oos2_v1_guard`), partial-ladder state (`remaining_size`, `tp1/tp2/tp3_executed`, `realized_legs_net`, `entry_cost_allocated`).
* `dds.me_long_v2_inverse_short_oos2_v1_close_leg` — durable leg ledger, PK `(position_key, leg_seq)` (TP1=1, TP2=2, terminal=3).
* `dds.me_long_v2_inverse_short_oos2_v1_close_outbox` — durable terminal-close staging.
* `dds.me_long_v2_inverse_short_oos2_v1_funding_obligation` — observed-rate funding obligations (migration 089).

### 1.3 Fingerprint / freeze / TTL / recovery / idempotency

* `protocol.py:execution_spec()` → SHA-256 `protocol_fingerprint()`; stored in the activation row; `evaluate_activation()` (`activation.py`) fails closed on mismatch; the runtime feature flag is `ME_INVERSE_SHORT_OOS2_ENABLED`.
* `freeze_ts`: fixed at activation; strict prospective boundary `signal_time > freeze_ts` (also a DB CHECK). Historical `freeze_ts` is never rewritten.
* Entry TTL: 120 minutes, judged **at DB decision time** (`statement_timestamp()` after lock waits) inside `INSERT_POSITION_SQL` and re-checked per event in `_open_event()`.
* Recovery: `_load_open_positions()` at startup, `_adopt_stored_position()` for ambiguous COMMITs, `_resolve_ambiguous_writes()`, `flush_pending_closes()`, journal replay (`journal.py`), funding obligations, dirty-leg retries.  Per-leg exactly-once via `(position_key, leg_seq)`.
* Geometry (frozen, must not change): TP1 `entry*0.95` → 50% original, TP2 `entry*0.93` → 75% of remaining (=37.5% original), TP3 `entry*0.90` → all remaining (12.5%); SL `entry*1.10`; STOP_FIRST; max hold 7200 min; TTL 120 min.

### 1.4 Deployment state — **NOT VERIFIED**

Read-only VPS access is not available in this environment.  Current activation
row, fingerprint, `freeze_ts`, OPEN positions / partial legs and outbox state on
the VPS are **NOT VERIFIED**; the cutover runbook (§6) starts with a mandatory
read-only inventory that captures them.

### 1.5 Risks identified

1. Concurrent same-symbol admissions (the confirmed problem above).
2. No 12h entry cooldown — a symbol can re-enter immediately after a full close.
3. The in-memory cap does not survive multi-process operation; the DB has no cap.
4. Any change to the frozen OOS-2 v1 admission semantics must NOT mutate the
   frozen experiment (fingerprint `1d1e7b4d…`, historical rows, `freeze_ts`).

---

## 2. Design — `ME_LONG_V2_INVERSE_SHORT_OOS2_V2` (versioned fork)

Mirrors the accepted Issue #285 / PR #286 architecture (`ME_LONG_V2_INVERSE_SHORT_V3`):
a **fully isolated, self-contained package fork** — the frozen
`me_long_v2_inverse_short_oos2_v1` runtime is never modified.

### 2.1 Identity and protocol version

* `STRATEGY_ID = EXPERIMENT_ID = "ME_LONG_V2_INVERSE_SHORT_OOS2_V2"`,
  `PROTOCOL_VERSION = "2.0.0"`; owner-facing name
  `ME_INVERSE_SHORT_OOS2_ENTRY_GUARDS_AND_CAPACITY_V1` (Issue #287).
* New namespace `ME_LONG_V2_INVERSE_SHORT_OOS2_V2:<event_key>` for position keys.
* **New fingerprint**: the three guard parameters are added to
  `execution_spec()` (`max_open_positions = 12`, `single_active_symbol = true`,
  `symbol_entry_cooldown_hours = 12`, `guard_scope = "OOS2_V1+OOS2_V2"`), so the
  v2 fingerprint necessarily differs from the frozen v1 fingerprint.  The v1
  fingerprint, activation row and `freeze_ts` are untouched.
* Fresh prospective-only `freeze_ts` at a separately approved cutover. No
  backfill, no migration of v1 rows: v1 positions stay in the v1 ledger and are
  managed by the (EXIT_ONLY) v1 runtime until they close naturally.

### 2.2 The three mandatory guards (OOS-2-local, PostgreSQL-atomic)

Guard scope is the **OOS-2 experiment family** — the legacy v1 ledger is read
**READ-ONLY** during handover (exactly like V3 reads V2):

1. `MAX_OPEN_POSITIONS = 12` — OPEN logical positions counted over
   `oos2_v2_position` + legacy `oos2_v1_position` (a partially closed TP1/TP2
   position is still OPEN and keeps its slot).  V2/V3 positions never count.
   The 13th valid signal keeps the frozen pending → TTL contract
   (`ENTRY_CAP` deferred until a slot frees or the TTL sweep disposes it).
2. `SINGLE_ACTIVE_SYMBOL = true` — a new entry is refused while an OPEN logical
   OOS-2 position (v1 or v2) exists for the symbol; terminal refusal
   `SYMBOL_ALREADY_OPEN`.  Legacy duplicate OPEN rows are tolerated to run out;
   no forced closes; no cross-contour block with V2/V3.
3. `SYMBOL_ENTRY_COOLDOWN = 12h` — admission only when
   `decision_time >= last committed OOS-2 entered_at + interval '12 hours'`
   (v1+v2, ANY status — a CLOSED position still cools its symbol down; the
   boundary is inclusive: exactly 12h is admissible).  Refusals never write
   `entered_at`, so they never extend the timer.  A terminally refused event is
   never replayed; a NEW eligible signal is required after the boundary.

Terminal refusals (`SYMBOL_ALREADY_OPEN`, `SYMBOL_COOLDOWN_12H`) dispose the
event **in the same transaction** (`status = 'SYMBOL_REJECTED'`, `status_reason`
+ `status_at` persisted) — with `event_key`, lineage and reason durable, and
without position, entry fee or any artificial trade.  `ENTRY_CAP` stays
deferred (event `CAPTURED` until the TTL sweep) exactly per the current
contract; `ENTRY_TTL` / activation / fingerprint / source errors keep their
existing separate handling.

### 2.3 Atomic PostgreSQL admission (race-safe)

One serialized admission critical section, fixed lock order (deadlock-free):

1. `pg_advisory_xact_lock(ADMISSION_LOCK_KEY)` — `ADMISSION_LOCK_NAMESPACE =
   "ME_LONG_V2_INVERSE_SHORT_OOS2_V2_ADMISSION"`, deterministic pinned key (SHA-256-derived, masked into the signed-BIGINT range for `pg_advisory_xact_lock`)  Serializes all v2 admissions across workers and restarts.  Closes,
   funding, the leg ledger and the TTL sweep never take it — exits are never
   blocked by admission.
2. Legacy OOS-2 v1 activation status read (READ-ONLY): v2 admits only while the
   legacy contour admits nothing (`DRAINING` / `CANCELLED`); an unreadable or
   still-admitting legacy state FAILS CLOSED for new entries (DEFERRED) — exits
   are unaffected.
3. EVENT row lock (`FOR UPDATE`, `status='CAPTURED'` folded in) →
   ACTIVATION row lock — same order as the TTL sweep and the drain.
4. `ADMISSION_DECISION_SQL` re-checks EVERYTHING at `statement_timestamp()`
   (post-lock decision time): event still CAPTURED, fingerprint/activation/
   strict post-freeze, entry TTL at DB time, then symbol guard → 12h cooldown →
   12-cap (frozen gate order: NOT_ADMISSIBLE → ENTRY_TTL →
   SYMBOL_ALREADY_OPEN → SYMBOL_COOLDOWN_12H → ENTRY_CAP → ADMISSIBLE).
5. `ADMISSIBLE` → guarded `INSERT_POSITION_SQL` (`ON CONFLICT (position_key)
   DO NOTHING`); `rowcount=1` means THIS call committed the row.
6. `SYMBOL_*` refusal → `mark_event_expired`-style disposal **in the same
   transaction** under the held event lock — but never when a position for the
   event is already committed (that resolves to adoption first).
7. Every `rowcount=0` resolves via a committed-row read-back first
   (`ADOPT_EXISTING`) — a refusal (and least of all a cooldown refusal) is
   never inferred from a bare rowcount.  Ambiguous COMMITs adopt the original
   committed row verbatim: no second INSERT, no second fee.

Unavailability of DB/state fails closed **for entries only**; STOP/TP/TIMEOUT,
partial TP legs, funding, close-outbox replay and recovery continue in
DRAINING/EXIT_ONLY.

Schema-level backup (migration 091): partial
`UNIQUE (symbol) WHERE status='OPEN'` on the NEW `oos2_v2_position` table plus
`UNIQUE (position_key, status) WHERE status='OPEN'` and a
`(symbol, entered_at DESC)` cooldown index.  The legacy v1 table is never
altered (its historical duplicate OPEN rows survive untouched; the migration
never fails on them because it never touches them).

### 2.4 What is NOT changed

OOS-2 trade geometry and lifecycle are byte-preserved by the fork and asserted
by parity tests: source (`MOMENTUM_EXHAUSTION_REVERSE_LONG_V2` LONG → inverse
SHORT), TP ladder 5/7/10% with the exact partial quantities (50% / 37.5% /
12.5%), SL `entry*1.10`, STOP_FIRST, max hold 7200 min, ENTRY_TTL 120 min,
deterministic rounding/dust, sizing, fees, slippage, funding on outstanding
qty, per-leg exactly-once, stop/timeout/close/recovery paths.  V1/V2/V3,
production scanner/paper and all historical balances/ledgers are untouched.
PAPER_ONLY, no live orders.

---

## 3. Files added / changed (this PR)

(Filled in the final PR summary; see §7 for the migration map.)

---

## 4. Test plan

Unit + in-memory parity, real PostgreSQL integration (concurrent distinct
event keys of one symbol on different connections incl. ~9 ms spacing, 12/13
capacity, TP1/TP2 do not free slots, symbol exclusivity incl. legacy rows,
cooldown `<12h` refused / `>=12h` admitted / refusals do not extend the timer,
TTL 120 m boundary, DB-decision-time under lock wait, ambiguous COMMIT
adoption, duplicate delivery, restart, deadlock-freedom, no EXPIRED-with-
position, no double fee/leg/funding, migration idempotency, config override
precedence), geometry regression (TP1→TP2→TP3, TP1→STOP, TP2→STOP, STOP_FIRST,
timeout, remaining-qty precision, realized PnL retention, funding on
outstanding only), legacy duplicate OPEN coexistence + DRAINING behaviour, and
full unchanged regression suites for OOS-2 v1 and V1/V2/V3.

---

## 5. Grafana

`monitoring/grafana/dashboards/me-inverse-short-oos-2.json` (UID
`me-inverse-short-oos-2`) gains read-only guard panels — OOS-2 OPEN logical
X/12 (v1+v2), occupied symbols, last accepted entry + cooldown remaining/next
eligible, `SYMBOL_ALREADY_OPEN` / `SYMBOL_COOLDOWN_12H` refusal counts, ENTRY_CAP
pending + TTL expirations, TP stage / outstanding qty, activation / fingerprint
/ freeze, outbox / funding / recovery health.  No V2/V3 data is mixed into the
OOS-2 dashboard; TP legs are never counted as separate trades.
The frozen v1 panel set (ids 1-10) is **semantically pinned** вЂ” the Issue #287
panels are APPENDED after it (ids 11-18: `Guard: OPEN logical X/12 (family
v1+v2)`, `Guard: Occupied symbols (SINGLE_ACTIVE_SYMBOL)`, `Guard: Last
accepted entry / 12h cooldown (family)`, `Guard: Refusals & pending (SYMBOL_*,
ENTRY_CAP, TTL)`, `Guard: TP stage / outstanding qty (family, position rows
only)`, `Activation / Freeze / Fingerprint (family)`, `Outbox / funding /
recovery (family)`, `Capture errors / failures (family)`) вЂ” and
`tests/test_me_inverse_short_oos2_v2_dashboard_v1.py` pins the frozen-10
canonical-JSON SHA-256, so any edit to a v1 panel fails CI while new guard
panels remain welcome.  The v2 protocol fingerprint is never hard-coded into
the dashboard: the activation panel reads it LIVE from the activation table.

---

## 6. Approval-gated cutover and rollback runbook (NOT executed by this task)

**Preconditions (read-only inventory, capture as evidence):** current git HEAD,
OOS-2 v1 activation row (`status`, `protocol_fingerprint`, `freeze_ts`), service
unit and release, OPEN v1 positions with TP-ladder state and deadlines, open
close-outbox / funding obligations, Grafana state.  Any unavailable fact is
recorded as `NOT VERIFIED`.

**Cutover (each step separately approved):**

1. Apply migration `091` (additive only) during a quiet window; verify
   repeatability on a scratch database first.
2. Deploy the v2 release artefacts into the v2 unit's OWN release tree
   `/opt/trad_bot_me_inverse_short_oos2_v2_release` (V3-precedent parity: a
   v2 deploy can never touch the tree the draining v1 service runs from);
   service unit + env, flag
   `ME_INVERSE_SHORT_OOS2_V2_ENABLED=0` — inert).
3. Run `scripts/me_inverse_short_oos2_v2_preflight.py` (read-only) — must pass.
4. Drain the legacy contour with its OWN unchanged tooling
   (`scripts/me_inverse_short_oos2_activate.py --drain`) → v1 `DRAINING`
   (EXIT_ONLY).  Verify: v1 keeps servicing STOP/TP/TIMEOUT, funding and the
   close outbox for its OPEN positions; no new v1 entries; `freeze_ts` and
   fingerprint unchanged.
5. Only then activate v2 with `scripts/me_inverse_short_oos2_v2_activate.py`:
   new experiment row `ME_LONG_V2_INVERSE_SHORT_OOS2_V2`, `status=RUNNING`,
   `freeze_ts = activation UTC`, the CURRENT v2 `protocol_fingerprint()`.
   No replay, no backfill; events before `freeze_ts` are refused by
   construction.
6. Verify: source capture writes v2 events, exactly-one entry per symbol,
   cap/cooldown refusals dispose events with reasons, v1 exits continue,
   Grafana shows the guard panels.

**Rollback (exit-only):** set the v2 activation to `DRAINING` (or stop new
admissions via the v2 `--drain` helper) — v2 stops admitting but continues to
protect every OPEN v2 position (STOP/TP/TP2/TP3 legs, funding, close outbox,
recovery).  The legacy contour is NOT re-enabled for new entries without a
separate decision; its OPEN positions continue to be serviced in EXIT_ONLY
until zero.  No data is deleted; no historical row is rewritten.

---

## 7. Migration map

| # | File | Content |
|---|---|---|
| 088 | `088_me_long_v2_inverse_short_oos2_v1.sql` | (existing, frozen) OOS-2 v1 tables |
| 089 | `089_me_long_v2_inverse_short_oos2_v1_funding_obligation.sql` | (existing, frozen) v1 funding obligations |
| 091 | `091_me_long_v2_inverse_short_oos2_v2.sql` | (new) OOS-2 v2 tables — activation / event (five-state disposition incl. `SYMBOL_REJECTED`) / position (partial-ladder, guard indexes: partial `UNIQUE(symbol) WHERE OPEN`, cooldown lookup) / close_leg / close_outbox / funding_obligation + immutability trigger; additive and idempotent; named terminal-refusal CHECK `me_long_v2_inverse_short_oos2_v2_event_terminal_refusal_check` (every TERMINAL disposition вЂ” incl. `SYMBOL_REJECTED` вЂ” carries cause + `status_at`), idempotent `DROP CONSTRAINT IF EXISTS` + `ADD CONSTRAINT`; never touches v1/V1/V2/V3 tables |

(Migration number 091 is the next free number after `090_me_long_v2_inverse_short_v3.sql`.)
