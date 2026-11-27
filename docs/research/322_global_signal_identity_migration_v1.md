# Global Signal-Identity Migration V1 — Design (Issue #322)

Status: **DESIGN + DEFAULT-OFF CODE ONLY**. Nothing in this document is enabled,
wired, or executed in production. Historical data is never rewritten. Every cutover
requires explicit owner approval (§8).

Scope: a *separate, backward-compatible* global migration of signal identity for all
scanner families that still use the unstable identity documented in
`reports/trackC_identity_audit.md` (Track C audit). The completed ME_SHORT-specific
implementation (PR #323, `app/research/scanner_signal_integrity_observer.py`,
`issue322:candle:v1`) is the reference pattern and stays untouched.

Reference implementation module (new, default-OFF, zero production wiring):
`app/research/scanner_signal_identity_migration_v1.py`.
Offline tests: `tests/test_issue322_global_identity_migration_*.py`.

---

## 1. Problem statement (from the audit)

The frozen `app/research/prospective_observer.py::_make_source_key()`
(`prospective_observer.py:239-244`) derives `source_signal_id` from Python `hash()`:

* `hash()` of `str` is **PYTHONHASHSEED-randomized** → the same signal gets a different
  `source_signal_id` after every scanner restart;
* the hashed string embeds `signal_time = candidate.detected_at`, which every production
  scanner sets from `ctx.evaluated_at` (cycle wall clock) → the same candle re-detected in
  a later scan cycle gets yet another identity, even within one process.

Because `research.prospective_observation` dedups on
`(experiment_id, source_signal_id)` (`ON CONFLICT ... DO NOTHING`,
`prospective_observer.py:813-814, 950, 1087, 1263, 1373, 1440, 1548`), both instabilities
defeat dedup and **duplicate observations** under rescan and/or restart.

Families affected (audit §2): MOMENTUM_EXHAUSTION LONG, MOMENTUM_EXHAUSTION_R,
MOMENTUM_EXHAUSTION_REVERSE_LONG_V1, SRR short/long, BREAKOUT_RETEST long, VC short,
LR long, FVG reaction long, TREND_PULLBACK_V3, ME_R_LONG_CLOSE_LOCATION (prospective
path), plus the cycle-dependent `event_key` of the ME_SHORT_A intake/fill captures.
ME_SHORT is already stable via PR #323; HTF key-level and ME V3 have their own stable
SHA-256 keys inside the frozen base.

## 2. Stable identity derivation (§a)

### 2.1 Canonical key format

```
issue322:<family-slug>:v1
```

Domain separation is **per family**: the namespace string is the first element of the
hashed payload, so two families can never collide even if all other fields match.
The version suffix (`v1`) makes any future derivation change an explicit, new namespace
instead of a silent key-space mutation.

### 2.2 Canonical payload

The payload is a JSON array with fixed element order, compact separators, UTF-8:

```python
json.dumps(
    [
        "issue322:<family-slug>:v1",   # domain separator (namespace)
        scanner_name,                   # str, non-empty
        symbol,                         # str, non-empty
        direction,                      # "LONG" | "SHORT"
        entry_timeframe,                # str, non-empty (e.g. "5m")
        signal_candle_open_time,        # int > 0, ms since epoch (µs-precision field set below)
    ],
    separators=(",", ":"),
    ensure_ascii=False,
).encode("utf-8")
```

`source_signal_id = -(2**31 + 1 + int.from_bytes(sha256(payload).digest()[:8], "big") % (2**62 - 2**31))`

This is exactly the PR #323 fold (`scanner_signal_integrity_observer.py:127-131`):
negative, strictly below `-2**31`, inside signed BIGINT, disjoint from the legacy
31-bit `hash()` band and from the HTF 31-bit band. Deterministic across processes and
PYTHONHASHSEED values (SHA-256, no `hash()` anywhere).

### 2.3 Timestamps: µs precision

Identity keys on `signal_candle_open_time` (ms, the documented stable identity component,
`app/scanners/models.py:86-89`) — never on `detected_at`. All *stored* timestamps in
migration-owned records use ISO-8601 with **microsecond** precision
(`datetime.isoformat(timespec="microseconds")`, tz-aware UTC). Rationale: PostgreSQL
`timestamptz` is µs-precision; a migration that round-trips ms-only strings would silently
lose the precision the DB already stores, and cross-ledger joins would degrade. The µs
rule applies to `detected_at`, `observed_at`, `alias_created_at`, etc. — never to the
candle open time, which stays an integer ms epoch.

### 2.4 Family slugs (initial registry, frozen constants)

| family-slug | scanner_name / scope | notes |
|---|---|---|
| `candle` | MOMENTUM_EXHAUSTION / SHORT | **already shipped** by PR #323 as `issue322:candle:v1` — adopted verbatim, never re-keyed |
| `me-long` | MOMENTUM_EXHAUSTION / LONG | new |
| `mer` | MOMENTUM_EXHAUSTION_R | new |
| `merl-v1` | MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 | new |
| `srr` | SUPPORT_RESISTANCE_REACTION | new (short + long share the slug; direction is a payload field) |
| `breakout-retest` | BREAKOUT_RETEST | new |
| `vc` | VOLATILITY_COMPRESSION | new |
| `lr` | LIQUIDITY_REVERSAL | new |
| `fvg` | FVG_REACTION_LONG_LOCAL_STRUCT_V1 | new |
| `tp-v3` | TREND_PULLBACK_V3 | new |
| `me-cl` | ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 | new |

Deliberately **excluded** from re-keying (their identities are already stable and part of
frozen lineage — they get *alias entries*, not new keys):

* HTF key-level (`_make_htf_source_key`, `prospective_observer.py:246-260`) — frozen
  protocol lineage;
* ME Reverse Long V3 (`v3_source_key`, `me_reverse_long_v3/identity.py:10-37`) — frozen
  V3 contract;
* ME_SHORT candle key (`issue322:candle:v1`) — completed PR #323 work.

## 3. Backward compatibility (§b)

### 3.1 The legacy key space is permanent

Existing `research.prospective_observation.source_signal_id` values came from
`-abs(hash(key_str)) % (2**31)` under whatever PYTHONHASHSEED each historical process
had. They are **unreadable as derivable keys** — the original hash seed is gone. They
remain valid *identifiers* forever: frozen OOS lineage (frozen observers, frozen
registries, completed experiments) keeps reading them byte-identically. The frozen
`prospective_observer.py` stays byte-identical; the migration never edits it.

### 3.2 Alias / dual-read mapping

`SignalIdentityAlias` (in-memory; persistence hooks ABSTRACT ONLY in V1):

```
(family, stable_key) <-> {legacy_keys...}
```

* **dual-read**: a lookup resolves a stable key to its known legacy keys and vice versa.
  A signal observed under either key space is found by any consumer that consults the
  alias map.
* aliases are **append-only**: a legacy key is never removed, never re-pointed;
  one stable key may legitimately own many legacy keys (one per historical process seed).
* alias creation is *observation-driven*, not migration-driven: when a signal is captured
  under the stable key and a concurrently-existing legacy row for the same candle is
  matched by (experiment_id, scanner_name, symbol, direction, entry_timeframe,
  signal_candle_open_time), an alias edge is recorded. This is a **mapping**, not a row
  rewrite: the legacy row's `source_signal_id` column never changes.

### 3.3 Dual-write shadow phase (PHASE-1)

New captures record **both** identities:

* `source_signal_id` — continues to hold the legacy-family value exactly as today
  (for ME_SHORT: the #323 candle key, unchanged), so `ON CONFLICT
  (experiment_id, source_signal_id)` behaviour is bit-identical during the shadow phase;
* `stable_source_signal_id` (shadow column / side table — schema change is a
  **separate migration**, never part of this default-OFF code) — records the new
  canonical key;
* plus `identity_version` (`issue322:<slug>:v1`).

Nothing consumes the shadow key in PHASE-1: no dedup, no join, no report.

### 3.4 `ON CONFLICT` interaction — the critical trap

Dedup lives on `(experiment_id, source_signal_id)`
(`prospective_observer.py:813-814 et al.`). **Changing the key space silently defeats
dedup unless the migration is explicit**: a row written under a stable key never
conflicts with its legacy twin → duplicates instead of dedup. Therefore:

* PHASE-1 dual-write keeps the **legacy key as the conflict target** — dedup semantics
  are unchanged;
* PHASE-2 read-migration verifies, per family, that dual-read resolves both key spaces to
  the same logical signal **before** any dedup surface reads the stable key;
* PHASE-3 cutover (owner-approved, per family) switches the conflict target to the stable
  key **only together with** an explicit alias backfill decision (itself separately
  approved — default: none; legacy rows stay in the legacy space and remain readable via
  aliases). A cutover that changes the conflict target without the alias plan is a
  rejected design.

### 3.5 Frozen-OOS lineage preservation

* `app/research/prospective_observer.py` — byte-identical, always (frozen contract).
* `app/research/scanner_signal_integrity_observer.py` — untouched (completed #323).
* `app/db/repository.py` — untouched in all phases except a future, separately-approved
  PR for the shadow column; the terminal-setup guard
  (`repository.py:839-841`, `:888-898`) stays exactly as is.
* Historical `run_id`, `observation_id`, `source_observation_id` values never change.
* Legacy identity remains readable forever (dual-read).

### 3.6 Legacy identity resolution — review B1 (no legacy id from missing/invalid data)

A dual-identity capture must record the **legacy** id that the frozen observer
actually uses for this signal. Exactly two sources are accepted
(`resolve_legacy_identity` in the reference module):

1. **Confirmed original legacy id** (`legacy_source_signal_id=`) — the persisted
   `source_signal_id` of an existing (possibly frozen) row, used **verbatim**.
   Historical values came from an unknown `PYTHONHASHSEED` and can never be
   re-derived; re-minting them would silently fabricate identity.
2. **Exact frozen key inputs** (`legacy_signal_time=`) — a validated
   `signal_time` (timezone-aware `datetime` = `detected_at`/`ctx.evaluated_at`,
   or a non-empty ISO-8601 timestamp string), re-derived byte-compatibly with
   `_make_source_key()` (`-abs(hash(key_str)) % (2**31)` over
   `f"{scanner}:{symbol}:{direction}:{signal_time}"`, raw input formatting).

Everything else is **fail-closed**: `None`, empty/whitespace strings, malformed
strings, naive timestamps and non-timestamp objects (ints/floats/bools/…)
raise `IdentityError` **before anything is written** — no alias, no record.
No fallback is ever substituted (never `now()`, never `0`, never an epoch
default): a fabricated-but-plausible legacy id is exactly the defect this rule
removes. Off-scope captures (default-OFF, disabled family, unknown candle
lineage) keep their existing silent-`None` behaviour — nothing is minted there
at all.

### 3.7 Atomic persistence contract — review B2 (ALL_OR_NOTHING)

One capture is **one logical change**: an `IdentityBatch` containing the
dual-identity record and its mandatory alias edge(s), delivered through a
**single** `IdentityPersistenceHook.save_identity_batch(batch)` call:

* **ALL_OR_NOTHING** — alias edges and the record commit together or not at
  all; a failed batch leaves no trace (no persisted row, no in-memory alias).
  Split `save_alias()`/`save_dual_record()`-style hooks are **outside the
  contract** and are rejected at core construction (`TypeError`), so a partial
  write cannot exist even by accident.
* **No orphans, both directions** — `IdentityBatch.validate()` rejects an alias
  edge without its record (`ORPHAN_ALIAS_EDGE`) and a record without its
  mandatory matching alias edge (`BATCH_WITHOUT_ALIAS`,
  `RECORD_WITHOUT_MATCHING_ALIAS`).
* **Idempotent redelivery** — re-delivering the same signal yields the same
  `IdentityBatch.idempotency_key`
  (`identity_version:canonical_key_hex:legacy_id`); a hook MUST treat a
  committed key as a no-op, and the in-memory alias map is append-idempotent.
* **Concurrent capture** — the commit path is serialized per core
  (`threading.Lock`) and the in-memory append-only constraint is pre-checked
  before any write, so concurrent delivery of the same signal converges on one
  identity and a conflicting edge is rejected before it can be persisted.
* **Crash/restart safety** — the single batch is the only commit point; after a
  crash the store is pre- or post-batch, never partial. Identity is re-derived
  deterministically after restart and alias edges are replayed from the store
  (`SignalIdentityAliasMap.add_alias`), so recovery changes nothing.
* Historical/frozen alias edges are never re-pointed (`AliasIntegrityError`),
  including under replay.

Verified by fault-injection tests
(`tests/test_issue322_global_identity_migration_atomic_persistence_v1.py`):
alias-write failure, dual-record-write failure, crash-before-commit, repeated
redelivery, concurrent same-signal capture (single core and shared store),
restart recovery, batch-invariant and split-write-hook rejection.

## 4. Phased rollout (§c)

| Phase | What happens | Consumes stable key? | DB writes | Default |
|---|---|---|---|---|
| **PHASE-0** | migration-capable identity core shipped as **all-new, default-OFF** module + offline tests; zero production wiring | no | none | OFF |
| **PHASE-1** | shadow dual-identity capture per family: both keys recorded (`source_signal_id` legacy + `stable_source_signal_id` shadow), nothing consumed | no | shadow column/side-table only (separate approved migration) | OFF per family |
| **PHASE-2** | read-migration with verification: reports/joins dual-read via aliases; parity checks legacy vs stable resolution over live captures; no write-path change | read-only | none | OFF per family |
| **PHASE-3** | per-family cutover of the dedup/conflict target to the stable key | yes | write path change | **ONLY with explicit owner approval, per family** |

Family enablement is a per-family constant in a frozen dataclass
(`MigrationConfig`, default `families_enabled = ()`, `phase = PHASE_0`,
`dual_write = False`, `read_migration = False`, `cutover = False`) — **not** env wiring
into `scanner_runner.py`. Enabling a phase for a family is a code-reviewed constant
change, never a runtime toggle.

## 5. Rollback (§d)

| Phase | Rollback |
|---|---|
| PHASE-0 | delete the module + tests; nothing references them |
| PHASE-1 | stop writing the shadow column (config constant off); legacy `source_signal_id` writes never changed, so the ledger is already in the pre-phase state; shadow rows/columns may be dropped by a separate approved cleanup or left inert |
| PHASE-2 | stop reading stable keys (constant off); no data changed |
| PHASE-3 | revert the conflict-target change per family (revert PR); rows written under stable keys during the cutover window remain readable through aliases; legacy rows were never rewritten, so the legacy key space is intact. No destructive step exists in any rollback |

## 6. Explicitly forbidden (§e)

* **Historical rewrites** — no UPDATE of any existing `source_signal_id`,
  `observation_id`, `run_id`, or frozen row, ever.
* **Backfill** — no retroactive population of stable keys for historical rows.
* **Retroactive re-keying** — no re-derivation of legacy identities; the `hash()` keys stay
  exactly as written.
* **Cutover without approval** — PHASE-3 constants stay `False` until the owner approves
  per family.
* **Changes to trading gates / scoring / risk / frozen protocol parameters** — out of scope
  of every phase.
* **Any edit to frozen files** (`prospective_observer.py`,
  `scanner_signal_integrity_observer.py`, `repository.py`, `scanner_runner.py`,
  `app/scanners/*`) in PHASE-0/1 code delivery.

## 7. Per-family rollout table (§f)

| Family | Slug | Priority | Risk | Prerequisites |
|---|---|---|---|---|
| SRR short/long | `srr` | P1 — RUNNING experiment, active dup exposure | Medium: live experiment rows | PHASE-1 shadow column migration approved; alias matcher on (experiment_id, candle key) |
| ME_R_LONG_CLOSE_LOCATION | `me-cl` | P1 — registry-active | Medium: dual ledgers (prospective + dedicated spool) | dedicated-ledger dedup semantics established (currently UNKNOWN in audit) |
| VC short | `vc` | P2 — READY_TO_START | Low: not yet capturing | PHASE-1 migration; no historical rows to alias |
| LR long | `lr` | P2 — READY_TO_START | Low | same as VC |
| BREAKOUT_RETEST long | `breakout-retest` | P2 — READY_TO_START | Low | same |
| FVG reaction long | `fvg` | P2 — READY_TO_START | Medium: `detected_at` is wall-clock `now()` (audit §2.9) — alias matcher must not key on it | alias matcher on candle key only |
| TREND_PULLBACK_V3 | `tp-v3` | P2 — READY_TO_START | Low | same |
| MOMENTUM_EXHAUSTION LONG | `me-long` | P3 — no active experiment for LONG | Low | same |
| MOMENTUM_EXHAUSTION_R | `mer` | P3 | Low | same |
| MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 | `merl-v1` | P3 | Low | same |
| ME_SHORT (MOMENTUM_EXHAUSTION SHORT) | `candle` | **P0 — DONE (PR #323)** | — | none; adopted verbatim as the canonical `issue322:candle:v1` |
| HTF Point-B / BASELINE | — | excluded | — | frozen `_make_htf_source_key` preserved; alias entries only |
| ME V3 | — | excluded | — | frozen `v3_source_key` preserved; alias entries only |
| ME_SHORT_A intake/fill `event_key` | (own namespace) | P4 — separate ledger | Medium: key embeds `signal_time` (cycle-dependent) | own design review; out of the prospective key space |

## 8. Acceptance criteria (§g)

1. **Same signal + rescan + restart → no duplicate observation under BOTH identity
   spaces**: capturing the same candle twice (including from a fresh process with a
   different PYTHONHASHSEED) produces exactly one logical observation; the legacy space
   dedups as today (unchanged conflict target), the stable space resolves to the same
   logical signal via the alias map.
2. **New candle → new identity**: different `signal_candle_open_time` ⇒ different
   canonical key (and different legacy key), for every family.
3. **Terminal setups protected**: `dds.scanner_setup` upsert terminal guard
   (`repository.py:839-841`, `:888-898`) untouched and still verified by its existing
   tests; a re-detection never rewrites `EXECUTED`/`INVALIDATED`/`EXPIRED` rows.
4. **Historical run_id intact**: no UPDATE/DELETE touches historical rows; `run_id`,
   `observation_id`, `source_observation_id` are immutable (asserted by isolation tests).
5. **Paper trade uniqueness intact**: no change to `save_paper_trade`/trade dedup
   surfaces (`repository.py:1351`).
6. **Frozen files byte-identical**: `git diff` shows zero modified production files
   (the migration core module is the only allowed change) in PHASE-0;
   frozen-observer tests keep passing.
7. **Default-OFF**: constructing the migration core without explicit config performs no
   capture, no write, no import side effect on production modules.
8. **Determinism**: canonical keys are identical across processes and PYTHONHASHSEED
   values (subprocess-tested), µs-precision timestamps round-trip exactly.
9. **Legacy identity integrity (review B1)**: a wrong legacy id can never be created or
   stored from incomplete source data — missing/empty/naive/malformed `legacy_signal_time`
   is rejected explicitly before any write; a confirmed persisted legacy id is used
   verbatim; no fallback value is ever substituted.
10. **Atomic persistence (review B2)**: alias and dual-record are one ALL_OR_NOTHING
    transactional batch — after a failure, redelivery, concurrency or crash/restart the
    store is consistent (no orphan aliases, no records without their alias, no
    duplication, no conflicting identities) and historical frozen identities are
    unchanged.

## 9. Open questions for the owner

1. Approve the shadow-column (or side-table) schema migration for PHASE-1? (separate PR)
2. Alias backfill policy at PHASE-3: none (default) vs best-effort candle-match aliasing
   of legacy rows?
3. Should the ME_SHORT_A intake/fill `event_key` (cycle-dependent `signal_time` component)
   be brought into this migration namespace, or fixed in its own issue?
4. Priority confirmation: SRR + ME_R_LONG_CLOSE_LOCATION first (audit-driven), as in §7?
