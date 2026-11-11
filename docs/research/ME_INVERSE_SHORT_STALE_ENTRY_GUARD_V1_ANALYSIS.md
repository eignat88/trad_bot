# ME_INVERSE_SHORT_STALE_ENTRY_GUARD_V1 — TECHNICAL ANALYSIS (STOP AFTER ANALYSIS)

> **Stage:** branch + investigation + FIX DESIGN only. **No implementation in this document's
> branch state.** The trading implementation, PostgreSQL, systemd, frozen policy / fingerprint /
> `freeze_ts`, positions and source events are NOT modified by this task. No PR is created.

## 0. Metadata

| Field | Value |
|---|---|
| Repository path | `C:\py_pro\trad_bot_inverse_clean` (worktree of `C:\py_pro\trad_bot_issue231\.git`, remote `https://github.com/eignat88/trad_bot.git`) |
| Base branch | `main` → `origin/main` |
| Branch | `fix/me-inverse-short-stale-entry-guard-v1` |
| Base commit SHA (branch created from) | `537ad743a68a93fa7493c1483fec27e862bb7ee4` (then fast-forwarded) |
| Git HEAD (analysis performed at) | `6bc7ea777d606a3068e7ff62aef2b8d771be19a4` — `monitoring: show open ME inverse SHORT paper positions in Grafana (#274)`, == current `origin/main` |
| Working tree status | Clean for tracked files; pre-existing **untracked** files preserved: `.tmp/`, `baseline_repo/`, `reports/issue268_*.txt` (4 files). No tracked file modified by this task. |
| Changed files (this task) | `docs/research/ME_INVERSE_SHORT_STALE_ENTRY_GUARD_V1_ANALYSIS.md` (new, this document) |
| Production runtime | `/opt/trad_bot_me_inverse_short_release`, systemd `trad-bot-me-inverse-short-runtime.service` (`ExecStart=... production_runtime --interval 10`, `deploy/systemd/trad-bot-me-inverse-short-runtime.service:32`) |
| Audit facts (as of 10.11.2026, supplied by task) | Activation `RUNNING / FULL`; `freeze_ts = 2026-11-09 16:44:40 UTC`; 24 source events; 3 OPEN SHORT positions (`KORUUSDT`, `XAUUSDT`, `QQQUSDT`); 21 `CAPTURED`; entry refusal `ENTRY_CAP`; `max_open_positions = 3`; `PIEVERSEUSDT` events registered but no new trades created |

Branch creation notes: local `main` was 36 commits behind `origin/main` (fast-forward only, no
divergence); `git fetch` initially denied by sandbox (`.git` lives outside the session workspace)
and was re-run with approval; the branch was fast-forwarded from `537ad74` to the freshly fetched
`6bc7ea7` before any analysis (branch had zero own commits — no history reset performed).
Untracked files were checked for path collisions against `origin/main` before checkout (none).

---

## 1. ROOT CAUSE

**The entry path of `ME_LONG_V2_INVERSE_SHORT_V1` has NO event-age admission control. An event
stays eligible for entry forever while its status is `CAPTURED`, and the `ENTRY_CAP` capacity
refusal is non-terminal and telemetry-only. The moment a position slot frees, the oldest
`CAPTURED` event — regardless of how old its `signal_time` is — is entered at the then-current
market price with a brand-new `entered_at`.**

Concrete code facts (HEAD `6bc7ea7`):

1. **Capacity check is the only queue gate.** `InverseShortPaperEngine.open_entries()`
   `app/paper/me_long_v2_inverse_short_v1/engine.py:275-377`:
   - `max_positions = int(self.settings.max_open_positions)` — `engine.py:331`;
   - `if len(self.open_positions) >= max_positions: return opened` — `engine.py:332-333`
     (returns **before** even listing events);
   - inside the entry loop: `if len(self.open_positions) >= max_positions: ... break` —
     `engine.py:342-346`.
   Nowhere in this method (or anywhere else in the contour) is `event.signal_time` compared
   against the current clock. Verified by exhaustive grep over
   `app/paper/me_long_v2_inverse_short_v1/`: the only `signal_time` uses in the entry path are
   (a) ordering in `list_events()` and (b) the **lower** bound `is_post_freeze(signal_time,
   freeze_ts)` (`engine.py:413-419`, `capture.py:255-263`). There is no **upper** bound.

2. **FIFO backlog survives indefinitely.** `PgInverseShortStore.list_events()`
   `store.py:815-828` returns `ORDER BY signal_time, event_key` (lines 818, 823; in-memory
   mirror `store.py:223-227` sorts by `(signal_time, event_key)`). Nothing ever removes old
   rows from the `CAPTURED` set: no TTL sweep, no expiry status, no age filter in
   `_gather_symbols()` (`production_runtime.py:399-432`) or `pending_events()`
   (`engine.py:1529-1530`).

3. **Slot release immediately unlocks the stale head of the queue.** `check_exits()` frees the
   slot with `self.open_positions.pop(position_key, None)` (`engine.py:740`); the very same
   `run_cycle()` (`runner.py:116-182`, exits at line 129 → entries at line 131-132) then calls
   `open_entries()`, which enters the oldest `CAPTURED` event **in the same cycle**.

4. **The fill is taken at the current price, not at the signal's price.**
   `market_price = prices.get(event.symbol)` (`engine.py:421`), refused only when missing/≤0
   (`engine.py:422-424`); `_build_position()` derives
   `entry_price = market_price * (1 - slippage_percent)` (`engine.py:622-624`) and
   `entered_at = self._clock()` (`engine.py:651`), `deadline_at = entered_at + 7200 min`
   (`engine.py:652`, `protocol.py:98-102`). The event's own `source_reference_price`,
   entry zone and invalidation price (stored in `lineage`) are **never** consulted for
   admissibility — a 5m LONG-setup signal inverted to SHORT can be acted on hours or days
   after the setup's entry window has passed.

5. **Capacity refusal is silent and non-terminal.** While the cap is full the engine returns
   at `engine.py:332-333` without even reading the events; the only trace is the telemetry
   refusal code `ENTRY_CAP` appended in `ProductionRuntime._entry_refusals()`
   (`production_runtime.py:456-459`). No row, status or lineage field records that an event
   was skipped because of capacity.

Why this is a defect, not a protocol feature:

- The frozen entry model is `ENTRY_MODEL = "NEXT_OBSERVED_MARKET_PRICE"` — *"the first
  observed market price AFTER the source event was captured"* (`protocol.py:59-61`). Under a
  10-second cycle (`--interval 10`, systemd unit line 32; `settings.position_monitor_interval
  = 10`, `config.yaml:66`) and a 30-second price-freshness window
  (`DEFAULT_PRICE_MAX_AGE_S = 30.0`, `market_data.py:79-81`), the model's nominal latency is
  **seconds**. Entering after a capacity backlog of hours/days violates the intent of that
  model even though each individual fill uses an observed (never reconstructed) price.
- The source pipeline itself treats a signal as live only for a bounded window (see §4 TTL
  justification): the contour is the ONLY component that never expires one.
- Statistically, capacity-gated late entry is also an adverse-selection channel: which event
  gets the freed slot is determined by how fast existing positions terminated, i.e. the
  entering signals are systematically selected by the outcome path of previous trades.

**Not the root cause (explicitly ruled out):** market-data staleness is already guarded
(prices 30s / bars 600s / funding 3600s, `market_data.py:79-93`, env example
`deploy/env/me-inverse-short-runtime.env.example:30-34`); pre-freeze backfill is guarded
(`activation.py:189-198` + migration 085 CHECK line 114); duplicate delivery is guarded
(`identity.py:66-89` + `INSERT ... ON CONFLICT DO NOTHING`, `store.py:349-360`); ambiguous
COMMIT is guarded (`engine.py:404-411, 430-443, 502-614`). None of these bounds the **age** of
a pending event at entry time.

---

## 2. CURRENT BEHAVIOR

### 2.1 Full path `CAPTURED → ENTERED`

```
ScannerOrchestrator.scan_all_with_stats
  └─ publishes PRE-DEDUP MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 / LONG candidates
     under stats["_me_inverse_short_source_events"]   (orchestrator.py:275-279)
       └─ scanner_runner._capture_me_inverse_short_source()  (scanner_runner.py:873-939)
            gate: ME_INVERSE_SHORT_V1_ENABLED=1      (scanner_runner.py:905-907)
            └─ capture_with_status()                 (capture.py:195-284)
                 · activation row RUNNING + fingerprint match (capture.py:220-231)
                 · NEW_ENTRIES permission / not DRAINING     (capture.py:232-243)
                 · event_from_candidate(): signal_time = detected_at (UTC)  (capture.py:129)
                 · post-freeze: signal_time > freeze_ts      (capture.py:255-263)
                 · store.save_event(): INSERT ... WHERE EXISTS(activation RUNNING)
                   ON CONFLICT(event_key) DO NOTHING         (store.py:349-360, 762-806)
                 → status = CAPTURED (default, models.py:85; DB default, migration 085:107)

ProductionRuntime.run_cycle()                       (production_runtime.py:464-557)
  ├─ activation refresh (fail closed)               (production_runtime.py:470-488)
  ├─ _gather_symbols(): prices for open ∪ CAPTURED-event symbols (FULL mode only)
  │                                              (production_runtime.py:399-432)
  ├─ market-data snapshot (stale/missing excluded)  (production_runtime.py:496-501)
  └─ InverseShortContour.run_cycle()                (runner.py:116-182)
       ├─ refresh_activation()                      (runner.py:123)
       ├─ engine.check_exits()  → STOP/TP/TIMEOUT; frees slots (engine.py:679-743)
       └─ if activated and not exit_only: engine.open_entries(prices)  (runner.py:131-132)
            gates (in order): activation (278-283) → EXIT_ONLY (284-294) →
            balance-recovery retry + durability risk (300-309) → accounting trust (310-316) →
            dirty funding (317-329) → CAPACITY (331-333, 342-346)
            └─ store.list_events(status='CAPTURED')  ORDER BY signal_time, event_key
                                                       (engine.py:340; store.py:815-828)
                 └─ _open_event(event, prices)       (engine.py:379-463)
                      · direction check              (384-389)
                      · duplicate-in-memory check    (391-393)
                      · unfinished-close-per-symbol  (394-403)
                      · has_position → ADOPT committed row (404-411 → 502-614)
                      · post-freeze re-check          (413-419)   ← only time bound: LOWER bound
                      · market_price = prices.get(symbol) (421-424)
                      · _build_position(): entered_at = clock() (651),
                                           deadline = entered_at + 7200 min (652)
                      · store.save_position(): ON CONFLICT DO NOTHING (430; store.py:362-366)
                      · store.mark_event_entered(): UPDATE ... WHERE status='CAPTURED'
                                                    (445-452; store.py:392-396, 830-839)
                      → status = ENTERED, position_key set
```

### 2.2 Where `max_open_positions` is applied

| Location | Behavior |
|---|---|
| `engine.py:331-333` | hard early return while `len(open_positions) >= max_open_positions` — **events are not even read** |
| `engine.py:342-346` | per-cycle loop `break` once the cap is reached mid-loop |
| `production_runtime.py:456-459` | telemetry refusal code `ENTRY_CAP` |
| `config.yaml:25` / `settings.py:205` | `max_open_positions: 3` (shared `Settings`, also used by main Paper engine) |

### 2.3 What happens after a slot is freed

Same cycle: `check_exits()` pops the closed position (`engine.py:740`) → `open_entries()` sees
`len < max` → lists **all** `CAPTURED` events FIFO by `(signal_time, event_key)` → the oldest
one is entered at the current observed price with a fresh `entered_at` (`engine.py:651`).
After a restart the same applies: open positions are restored (`_load_open_positions`,
`engine.py:156-221`), and the `CAPTURED` backlog is re-read from PostgreSQL every cycle.

### 2.4 Can an old `CAPTURED` event reach `ENTERED`? — YES

Unconditionally: the only time-related gate is the **lower** bound
`is_post_freeze(signal_time, freeze_ts)` (`engine.py:413-419`). An event captured 1 second or
10 days ago is treated identically. With `max_open_positions = 3`, `MAX_HOLD_MINUTES = 7200`
(5 days, `protocol.py:49`) and the audit backlog of 21 `CAPTURED` events behind 3 open
positions, every slot release is a stale-entry opportunity.

### 2.5 Stale-event protection outside the reviewed code — NONE

Exhaustively checked (`grep` for `ttl|stale|expir|max_age|age` across the contour package and
its consumers):

- `market_data.py` freshness windows guard **prices/bars/funding**, never events;
- `production_runtime.py` reports `market_data_age_ms` (line 533) — price age only;
- the main Paper engine's setup expiry (`_ENTRY_TIMEOUT_BASE`, `app/paper/engine.py:36-43`;
  `_is_expired`, `engine.py:1431-1437`) and `repo.expire_stale_setups(max_age_minutes=120)`
  (`app/db/repository.py:1684-1703`, called from `paper_runner.py:478-484` and
  `app/paper/cli.py:345-347`) belong to the **production paper pipeline** and are never
  invoked by this contour;
- `ScannerOrchestrator.check_expiration()` (`orchestrator.py:663-681`) exists but is **not
  called anywhere** in the codebase (dead code) — and even if called, it would only filter
  candidates at scan time, not pending contour events;
- `DeduplicationEngine.cleanup(max_age_seconds=7200)` (`deduplication.py:57-65`) only prunes
  the scanner's in-memory dedup cache;
- `scripts/me_inverse_short_v1_preflight.py` checks activation/fingerprint/freeze only
  (lines 83-107) — no event-age check;
- Grafana `me-inverse-short-oos.json` has no age/staleness panel (only counts and lists).

### 2.6 How `entered_at` and the entry price are computed

- `entered_at = self._clock()` at entry instant — `engine.py:651` (production clock =
  `datetime.now(timezone.utc)`, `production_runtime.py:315`). Immutable afterwards: excluded
  from `UPDATE_POSITION_SQL` (`store.py:368-390`) and enforced by DB trigger
  `trg_me_long_v2_inverse_short_v1_guard` (migration 085, lines 201-243); the in-memory store
  repairs drift (`store.py:243-255`).
- `deadline_at = deadline_for(entered_at)` = `entered_at + 7200 minutes` — `engine.py:652`,
  `protocol.py:98-102`.
- Entry price: current observed mid `prices.get(symbol)` (`engine.py:421`), minus adverse
  slippage for a SHORT fill: `entry_price = market_price * (1 - slippage_percent)`
  (`engine.py:622-624`); STOP/TP = ±10% of that (`short_geometry`, `protocol.py:85-95`).
  Missing/≤0 price → no entry, event stays `CAPTURED` (`engine.py:422-424`).

### 2.7 Events after a runtime restart

Events are durable rows, never in-memory state: after a restart the engine re-reads all
`CAPTURED` events per cycle (`engine.py:340`), open positions are restored verbatim
(`engine.py:156-221`), unfinished closes are replayed from the outbox (migration 086,
`store.py:987-1009`). **A restart does not age, drop or expire anything** — a backlog that
accumulated during downtime is fully eligible (and stale) afterwards. This amplifies the
risk: any outage ≥ TTL converts the whole backlog into stale-entry candidates.

### 2.8 Idempotency / recovery after ambiguous COMMIT (current)

- **Event identity:** deterministic `event_key` = SHA-256 of
  `strategy|scanner|version|symbol|direction|entry_timeframe|signal_candle_open_ms`
  (`identity.py:66-89`; 5m grid `CANDLE_GRID_MS = 300_000`, line 34) → redelivery is
  `DUPLICATE`, one row ever (`store.py:349-360`, `save_event` `store.py:762-806`).
- **Position identity:** namespaced `position_key = <strategy>:<event_key>`
  (`identity.py:92-96`), `ON CONFLICT DO NOTHING` (`store.py:362-366`).
- **Ambiguous INSERT:** `save_position` returning rowcount 0 → read back and **adopt** the
  committed row (`engine.py:430-443` → `_adopt_stored_position` 502-614) with structural
  verification `_entry_facts_match` (465-500); committed facts are adopted verbatim, never
  recomputed; entry costs are booked exactly once via `reconcile_balance()`.
- **Ambiguous event mark:** `mark_event_entered` failure is fail-open — `has_position()`
  keeps delivery idempotent next cycle (`engine.py:445-452`); the SQL is inherently
  idempotent (`WHERE status='CAPTURED'`, `store.py:392-396`).
- **Ambiguous terminal close:** stage exact snapshot to outbox → flip row → verify
  (`engine.py:1147-1248`, flush `1262-1374`), fail-closed accounting gates meanwhile.
- **Connection loss:** dedicated self-healing connection, idempotent statement retried as one
  execute+commit unit (`store.py:640-721`).

**Design constraint derived from §2.8:** any future age guard must be evaluated **after** the
`has_position → adopt` branch in `_open_event()` — an already-committed position must always
be adopted (protective STOP/TP/TIMEOUT must never be blocked by event age). The guard belongs
next to the post-freeze check (`engine.py:413-419`), i.e. on the *new-entry* path only.

### 2.9 Runtime settings actually used by the isolated OOS

Read directly from the code (grep of `settings.` over `app/paper/me_long_v2_inverse_short_v1/`),
loaded via `load_settings()` (`production_runtime.py:813`; `app/config/settings.py:787` —
`config.yaml` + `.env` of the release CWD `/opt/trad_bot_me_inverse_short_release`):

| Setting | Used at | Role in the contour |
|---|---|---|
| `initial_balance` | `engine.py:145,149,1421`; `production_runtime.py:276` | ledger base |
| `max_open_positions` (=3) | `engine.py:331`; `production_runtime.py:457` | **capacity gate (subject of this fix)** |
| `risk_per_trade` | `engine.py:629` | sizing |
| `max_symbol_exposure` | `engine.py:632` | sizing cap |
| `taker_fee` | `engine.py:642,1104` | entry/exit fees |
| `slippage_percent` | `engine.py:622,1097` | fill model |
| `paper_funding_interval_hours` (=8) | `engine.py:782,980` | funding settlement |
| `position_monitor_interval` (=10) | `production_runtime.py:316-319` | default cycle interval — **overridden to 10 s by `--interval 10` in the systemd unit (line 32)** |
| `db_host/port/name/user/password` | `production_runtime.py:271-275` | dedicated store connection |

Contour-specific environment (NOT main-paper settings):
`ME_INVERSE_SHORT_V1_ENABLED` (`activation.py:38,108-111`),
`ME_INVERSE_SHORT_MD_PRICE_MAX_AGE_S` / `..._BAR_MAX_AGE_S` / `..._FUNDING_MAX_AGE_S` /
`..._MD_BAR_INTERVAL` (`market_data.py:79-93`, env example lines 30-34),
`EnvironmentFile=/etc/trad-bot/me-inverse-short-runtime.env` (systemd line 30).

**Deliberately NOT used by the isolated OOS** (must not be substituted in): direction gate,
expectancy filter, safety gates (daily loss limit, consecutive-loss cooldown,
`max_consecutive_losses`), `maker_fee`, DCA / trailing / breakeven / expiry policy,
`setup_ttl_multiplier` (the main engine's entry-expiry multiplier), `dds.paper_trade` /
`dds.scanner_setup` / position-monitor machinery. The contour is isolated by construction
(`TestIsolationContract`, `tests/test_me_long_v2_inverse_short_production_runtime_v1.py:1540+`).

**Gap confirmed:** the main Paper engine expires a 5m-entry setup after
`_ENTRY_TIMEOUT_BASE["5m"] = 12` bars × 5 min × `setup_ttl_multiplier = 2.0` → **120 minutes**
(`app/paper/engine.py:36-43`, `1431-1437`; `settings.py:298`), but this contour uses none of
that — it has no entry expiry at all.

---

## 3. EXPECTED BEHAVIOR

1. **Bounded entry latency.** An event may transition `CAPTURED → ENTERED` only while
   `now − signal_time ≤ ENTRY_MAX_AGE` (TTL, see §4). Older events are **never** entered.
2. **Terminal, honest dispositions.** Every `CAPTURED` event eventually becomes exactly one
   of: `ENTERED`, `EXPIRED` (age limit reached for a non-capacity reason), or
   `CAPACITY_REJECTED` (age limit reached while capacity was the binding refusal). Rows are
   never deleted; the full source-capture history (all 24 audit events and everything after)
   is preserved with its lineage.
3. **Capacity refusals become visible.** While the cap is full and fresh events wait,
   telemetry keeps reporting `ENTRY_CAP`; events that die *because* capacity blocked them are
   distinguishable from events that died for other reasons (missing price, accounting gate,
   outage).
4. **Same-cycle freshness.** In normal operation (slots free) behavior is unchanged: entry at
   the first observed price within seconds of capture — the frozen
   `NEXT_OBSERVED_MARKET_PRICE` model.
5. **Ordering preserved.** Eligible events keep FIFO order `(signal_time, event_key)`.
6. **Adoption never blocked.** An already-committed position row is always adopted regardless
   of event age (protective exits always attach).
7. **Restart-safe.** Dispositions are durable; after a restart the sweep resumes from the
   stored statuses; no event is resurrected by redelivery (`ON CONFLICT DO NOTHING` keeps the
   terminal status).
8. **Fail-closed for entries, fail-soft for bookkeeping.** A failing sweep write never blocks
   protective exits and never crashes the cycle; it is logged and retried next cycle (the
   entry loop itself keeps its existing fail-closed semantics).
9. **No backfill, no protocol drift.** `signal_time > freeze_ts` stays the only lower bound;
   no historical rows are created; the frozen protocol artifacts (`execution_spec`,
   `protocol_fingerprint`, `PROTOCOL_VERSION`, `freeze_ts`, activation row) are unchanged
   under the recommended Option A (§5).
10. **EXIT_ONLY (DRAINING) unchanged.** No entry-event query and no status mutation in
    DRAINING mode (existing contract `engine.py:284-294`, `runner.py:133-141,152-154`).

---

## 4. FIX DESIGN

### 4.1 TTL — value and justification (NOT arbitrary)

**Recommended: `ENTRY_MAX_AGE = 120 minutes` (= 7 200 s = 24 × 5m bars), pre-registered here
before implementation.**

Derivation from the *current mechanics of the 5m signals* and the *frozen execution model*:

| # | Ground | Evidence | Value |
|---|---|---|---|
| 1 | Frozen entry model implies near-instant entry: cycle 10 s, price freshness 30 s | `protocol.py:59-61`; systemd `--interval 10`; `market_data.py:81` | nominal latency ≈ 40 s → TTL only governs *abnormal* waits (capacity / outages) |
| 2 | Source entry timeframe is 5m (`entry_timeframe="5m"`, 5m signal-candle grid) | `capture.py:183`; `identity.py:34,46-55`; V2 scanner `momentum_exhaustion_reverse_long_v2.py:242` | signal information is a 5m-candle artifact; latency budget must be expressed in 5m bars |
| 3 | **The production Paper engine's own setup-alive window for 5m entries** = 12 bars × 5 min × `setup_ttl_multiplier(2.0)` | `app/paper/engine.py:36-43` (`"5m": 12`), `1431-1437`; `settings.py:298` | **120 min** |
| 4 | Repo-wide stale-setup expiry used by the paper pipeline | `app/db/repository.py:1684-1703`; `paper_runner.py:480`; `app/paper/cli.py:346` | **120 min** (`max_age_minutes=120`) |
| 5 | Source scanner's live-candidate memory window | `app/scanners/deduplication.py:57-65` | **7 200 s = 120 min** |
| 6 | Alternative (looser) cross-check: 15m-setup expiration convention `8 candles × 15 min × 2.0`; frozen research precedent `ENTRY_EXPIRY_MINUTES` | `orchestrator.py:35-40`; `app/research/me_r_long_early_mae_v3/protocol.py:30` (`= 240`) | 240 min |
| 7 | Frozen `MAX_HOLD_MINUTES = 7200` is the **exit** horizon | `protocol.py:49,98-102` | explicitly **not** reusable as an entry-staleness bound |

Grounds 3-5 independently converge on **120 minutes** for exactly this signal class (5m entry
timeframe), which makes it the least arbitrary choice: it is the same admissibility horizon
the repo already applies to the same source signals in other pipelines. Ground 6 documents the
240-minute alternative if the owner prefers alignment with the 15m-setup convention. Both are
derived; a value must be ratified before implementation (OPEN QUESTION Q1).

Placement rule: the constant must live **outside** `execution_spec()`
(`protocol.py:105-130`) — recommended in a new module `entry_policy.py` (or a clearly
commented engine-level constant) with an explicit note *"NOT part of execution_spec; does not
affect `protocol_fingerprint()`"*. The pinned-fingerprint test
(`tests/test_me_long_v2_inverse_short_v1.py:290-298`) is the guard.

### 4.2 New event statuses (disposition model)

Extend `models.py:30-32`:

```python
EVENT_STATUS_CAPTURED        = "CAPTURED"          # eligible (age <= TTL)
EVENT_STATUS_ENTERED         = "ENTERED"           # has a position row
EVENT_STATUS_EXPIRED         = "EXPIRED"           # terminal: age > TTL (generic cause)
EVENT_STATUS_CAPACITY_REJECTED = "CAPACITY_REJECTED"  # terminal: age > TTL, capacity was the binding refusal
EVENT_STATUSES = (CAPTURED, ENTERED, EXPIRED, CAPACITY_REJECTED)
```

Semantics:

- **`EXPIRED` ≠ `CAPACITY_REJECTED`.** `EXPIRED` = the event aged out for any reason other
  than a full book (e.g. long market-data outage, accounting gate down, DRAINING window).
  `CAPACITY_REJECTED` = the event aged out while `max_open_positions` was the binding
  condition — the signal was *lost to capacity*, which is the specific defect this task fixes
  and must be measurable separately for the OOS report.
- Both are **terminal**: `mark_event_entered` already guards `WHERE status='CAPTURED'`
  (`store.py:392-396`), so a terminal event can never be flipped to `ENTERED` afterwards;
  redelivery stays `DUPLICATE` and cannot resurrect it.
- Rows are never deleted or rewritten beyond the single `status`(+reason) transition — full
  `lineage` JSON and `signal_time` history preserved (requirement: сохранение полной истории
  source capture).

Determination rule (baseline, deterministic per sweep):

```
age = clock() − event.signal_time
if age > ENTRY_MAX_AGE:
    status ← CAPACITY_REJECTED if capacity currently binding (len(open_positions) >= max)
                                       else EXPIRED
```

Refinement (recommended, listed as Q5): persist the *first* capacity-block moment per event
into `lineage.capacity_blocked_from` (one idempotent UPDATE `WHERE lineage->>'capacity_blocked_from'
IS NULL` on the first refusal cycle), so classification stays accurate across restarts instead
of reflecting only the state at sweep time. Without the refinement, an event that spent 119
minutes waiting for capacity and one more minute during an accounting outage is classified
`EXPIRED` — acceptable but less precise.

### 4.3 Where the guard goes (exact code plan — NOT implemented yet)

1. **Stale sweep at the start of `open_entries()`** (`engine.py:275`), *after* the activation
   and EXIT_ONLY gates (lines 278-294) and *before* the capacity early-return (line 331) and
   the fail-closed durability/accounting gates (lines 300-329):
   - reads `list_events(status='CAPTURED')` once (the same read the entry loop needs),
   - marks every event with `age > ENTRY_MAX_AGE` via a new idempotent
     `store.mark_event_expired(event_key, status)` (`UPDATE ... SET status=%s WHERE event_key=%s
     AND status='CAPTURED'`), **fail-soft**: on store error log + continue to the normal gates
     (bookkeeping must never block exits or crash the cycle);
   - placing the sweep *before* the cap early-return is essential: today `open_entries`
     returns at line 333 without touching events while the cap is full — the backlog must age
     out even during long full-cap periods, not only at slot release;
   - the sweep runs only in FULL mode (EXIT_ONLY keeps its "no entry-event query at all"
     contract).
2. **Defensive re-check inside `_open_event()`**, placed with (after) the post-freeze check at
   `engine.py:413-419`, i.e. **after** the `has_position → adopt` branch (lines 404-411):
   adoption of an already-committed position must never be refused by age; only a genuinely
   new entry is refused when `age > ENTRY_MAX_AGE` (belt-and-braces if the sweep write
   failed).
3. **Store layer**: `mark_event_expired()` on the interface (`store.py:115-119` area),
   in-memory mirror (status transition like `mark_event_entered`, `store.py:229-234`) and PG
   implementation next to `MARK_EVENT_ENTERED_SQL` (`store.py:392-396, 830-839`).
4. **Telemetry** (`production_runtime.py:434-462`): keep `ENTRY_CAP`; add refusal codes
   `ENTRY_EXPIRED` / `CAPACITY_REJECTED` (and counters `expired_events`,
   `capacity_rejected_events` in the cycle record, `production_runtime.py:522-551`). The
   existing prefix allow-list assertion must be extended (see §7).
5. **Ordering/FIFO**: unchanged — sweep does not reorder; the entry loop still iterates
   `ORDER BY signal_time, event_key` (`store.py:818/823`).
6. **`pending_events()` / `_gather_symbols()`** automatically shrink as events leave
   `CAPTURED` — Grafana's "pending" panel and the runtime's symbol set follow suit with no
   extra code.
7. **Signal-level prospective observer (independent, optional but designed-in):** with
   terminal dispositions in the event ledger, a read-only observer can compute purely
   signal-level metrics — eligible-signal count, entry lateness distribution
   (`entered_at − signal_time` via `position.event_key` join), expiry rates split by cause,
   capacity-loss rate — completely independently of position-level PnL accounting. This
   matches the contour's existing "capture independent of direction gate / expectancy"
   philosophy (`capture.py:1-28`) and can be delivered as SQL/Grafana panels or a later
   `research/` observer without touching trading code.
8. **Explicit non-goals:** no change to `protocol.py`, `execution_spec()`,
   `protocol_fingerprint()`, `PROTOCOL_VERSION`, activation row, `freeze_ts`, exit logic,
   funding, outbox, sizing, positions or source events; no backfill; no new service.

### 4.4 Behavior of the accumulated audit state under the fix (for design completeness)

- The 3 open positions (`KORUUSDT`, `XAUUSDT`, `QQQUSDT`): untouched — rows, `entered_at`,
  deadlines, STOP/TP/TIMEOUT all frozen as-is (immutability trigger, migration 085:201-243).
- The 21 `CAPTURED` events: at first sweep after deploy, every event older than the TTL is
  transitioned once to `EXPIRED`/`CAPACITY_REJECTED` (rows and lineage preserved); any event
  still within TTL remains eligible and may enter normally. Exact ages must be measured with
  a read-only `SELECT` at implementation time (not run during this analysis — PostgreSQL is
  out of scope for this stage).
- `PIEVERSEUSDT` events: behave per the same rule (fresh ones can enter when a slot frees;
  stale ones become terminal).

---

## 5. V1 VS V2 DECISION

### Option A — fix `ME_LONG_V2_INVERSE_SHORT_V1` in place (recommended)

Implementation = engine/store/models/telemetry code + one status-CHECK migration (§6).
**No change to `execution_spec()` → `protocol_fingerprint()` and `PROTOCOL_VERSION` stay
byte-identical → the existing activation row (`RUNNING`, fingerprint, `freeze_ts =
2026-11-09 16:44:40 UTC`) remains valid; no re-freeze.**

| Criterion | Assessment |
|---|---|
| Frozen-protocol compatibility | **Compatible under the "data-admissibility guard" classification**: the fingerprint covers `execution_spec()` (`protocol.py:105-138`), not engine code — the repo precedent is that engine-level fail-closed guards (price freshness 30 s, pre-freeze, durability/accounting gates) were added across PR #267 review rounds without re-freezing. The guard is in the same class: it only *rejects inadmissible fills*, never selects among admissible ones. **Residual risk:** if the owner classifies entry TTL as an *execution parameter*, Option A would violate the freeze contract → switch to Option B (Q2). |
| OOS-purity risk | Low–moderate: a post-freeze tightening that only removes entries; cannot improve measured performance by selection; must be documented with deploy timestamp + pre-registered TTL (§9). |
| 21 accumulated `CAPTURED` events | Kept in place; stale ones marked terminal on first sweep (history intact), fresh ones remain eligible. No data rewrite. |
| 3 open positions | Fully preserved (rows, immutable entry facts, exits continue). |
| PostgreSQL | One additive migration (CHECK widening, optional columns). No backfill, no data loss, backward compatible (§6). |
| Grafana | Minor: "CAPTURED" panel semantics narrows to *eligible pending*; add disposition panels. Dashboard file only. |
| Rollback | Revert code; widened CHECK is backward compatible so the migration can stay; behavior returns to status quo ante (stale-entry risk returns — documented). Terminal statuses set during the fixed period remain (rows unaffected, old code simply ignores them). |
| Migrations | 1 (`087`), reversible in effect (old code writes only `CAPTURED`/`ENTERED`, still valid). |

### Option B — new version `ME_LONG_V2_INVERSE_SHORT_V2` (new fingerprint, clean holdout)

New package `app/paper/me_long_v2_inverse_short_v2/`, new `STRATEGY_ID`, new
`execution_spec()` **including** the TTL field → new fingerprint → new activation row with a
new `freeze_ts`; new tables (event/position/activation/outbox) via a new migration; new
namespace CHECK; Grafana panels; either a second systemd service or a reconfigured one.

| Criterion | Assessment |
|---|---|
| Frozen-protocol compatibility | Perfect — nothing frozen is touched; V1 stays byte-identical forever. |
| OOS-purity risk | Cleanest: a genuinely fresh prospective holdout under a fully pre-registered spec. |
| 21 accumulated `CAPTURED` events | Stay in V1's ledger; they can only ever be entered by the V1 runtime — which would keep the stale-entry defect alive unless V1 is additionally fixed or drained. Handling them requires either the Option A fix on V1 anyway, or flipping V1 to `DRAINING` (EXIT_ONLY: no new entries at all, `activation.py:126-170`). |
| 3 open positions | Must finish under V1 (up to 7200 min = 5 days): V1 stays deployed in EXIT_ONLY or FULL until they close → **dual-runtime period**, doubled operational surface, split history across two experiment IDs. |
| PostgreSQL | New tables + activation row + namespace/trigger migration; V1 tables untouched. |
| Grafana | New panels/dashboard for V2; V1 dashboard stays for the drain period. |
| Rollback | Stop/disable V2 service; V1 unaffected. Clean, but the clean holdout is then wasted. |
| Migrations | 1 large (new schema objects) + activation seeding; more moving parts, more review. |

### Recommendation

**Option A — fix V1 in place**, conditioned on two explicit owner decisions (Q1, Q2):

1. TTL ratified at **120 minutes** (pre-registered in §4.1);
2. the guard formally classified as a **data-admissibility / fail-closed entry guard** (same
   class as price freshness and pre-freeze checks), *not* an `execution_spec` parameter —
   hence no fingerprint change.

Rationale: the defect is an implementation gap against the frozen model's intent (prospective
= near-signal entry), not a deliberate protocol parameter; Option A preserves every frozen
artifact and the OOS run's continuity, is rollback-cheap, and requires only an additive
migration. Option B's cleanliness does not remove the need to also neutralize the V1 backlog
(the 21 events), so it doubles the work while adding dual-runtime risk. **If the owner rules
the TTL to be an execution-spec parameter, stop and switch to Option B — do not implement A.**

---

## 6. DB MIGRATION IMPACT

### Option A — migration `087_me_long_v2_inverse_short_v1_event_disposition.sql` (proposed)

Scope: **only** `dds.me_long_v2_inverse_short_v1_event`:

```sql
BEGIN;
-- 1) widen the status CHECK (migration 085 lines 107-108: status IN ('CAPTURED','ENTERED'))
ALTER TABLE dds.me_long_v2_inverse_short_v1_event
  DROP CONSTRAINT IF EXISTS me_long_v2_inverse_short_v1_event_status_check;  -- verify actual name via pg_constraint at implementation time
ALTER TABLE dds.me_long_v2_inverse_short_v1_event
  ADD CONSTRAINT me_long_v2_inverse_short_v1_event_status_check
  CHECK (status IN ('CAPTURED','ENTERED','EXPIRED','CAPACITY_REJECTED'));

-- 2) optional but recommended audit columns (both nullable, additive)
ALTER TABLE dds.me_long_v2_inverse_short_v1_event
  ADD COLUMN IF NOT EXISTS status_reason text,
  ADD COLUMN IF NOT EXISTS status_at timestamptz;
COMMIT;
```

- **No data rewrite / no backfill:** existing 24 rows untouched; only future single-row status
  transitions use the new values.
- **Backward compatible both directions:** old code writes only `CAPTURED`/`ENTERED` (still
  valid under the widened CHECK) and never reads the new values; new code degrades to a
  logged sweep failure if the migration is missing (fail-soft, §4.3.1) — but deployment order
  is migration-first anyway.
- **Indexes:** the sweep reuses `idx_me_inverse_short_v1_event_status (status, signal_time)`
  (migration 085 lines 120-121) — no new index strictly required; add
  `(status, signal_time)` partial ordering only if profiling shows otherwise (tiny table).
- **Untouched:** position table, outbox (086), immutability trigger, activation table,
  `signal_time > activation_freeze_ts` CHECK (085 line 114), all `dds.paper_*` /
  `scanner_setup` / research tables (085 "WHAT THIS MIGRATION NEVER DOES" contract holds).
- **Scripts:** `me_inverse_short_v1_activate.py` and `me_inverse_short_v1_preflight.py`
  touch only the activation row (`scripts/...:64,83-91`) — unaffected.
- **Tests asserting migration 085 text** (`tests/test_me_long_v2_inverse_short_source_capture.py:
  396-416,451-454`) remain valid — 085 is not edited; new assertions move to 087.

### Option B — additional impact

New migration creating `..._v2_activation / _event / _position / _close_outbox` (with the
widened status CHECK built in), seeded `READY_TO_START` activation row (fail-closed),
new namespace `CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_V2:%')`, new guard trigger,
new indexes; plus Grafana dashboard/panels for the new tables. V1 schema untouched. No
cross-table writes; no data migration between V1 and V2 (by design — clean holdout).

### Grafana impact (both options)

`monitoring/grafana/dashboards/me-inverse-short-oos.json`:

- line 337 `... WHERE status='CAPTURED'` — under A this panel becomes "eligible pending
  events" (terminal dispositions drop out of the count). Rename/annotate; optionally add
  `EXPIRED`/`CAPACITY_REJECTED` stat panels next to it;
- line 590 event list shows the new `status` values (no query change needed);
- lines 125 (total events) and 506 (integrity) are disposition-agnostic — unchanged;
- position panels (167/251/674) — unchanged.

---

## 7. TEST PLAN

All tests run against the in-memory store with a scripted clock unless noted; PG-store
variants where the store semantics matter. Existing suite must stay green except the
deliberate contract updates listed in §7.3.

### 7.1 Required cases (from the task)

1. **Entry immediately after the signal.** Fresh event (`age ≈ 0`), slot free → entered in the
   same cycle at the observed price; statuses `CAPTURED → ENTERED`; telemetry `entries=1`,
   no expiry codes. (Regression of current behavior — mirrors
   `test_event_without_price_stays_pending_then_enters`, `tests/...v1.py:694-707`.)
2. **Signal while the cap is full.** 3 open positions + fresh event → no entry, event stays
   `CAPTURED`, telemetry contains `ENTRY_CAP`; **no status mutation**; `pending_events` count
   unchanged. Advance clock in small steps (< TTL) → still `CAPTURED`.
3. **Slot release after TTL expiry.** Event captured at T; advance `> 120 min`; close a
   position (or restart with a free slot) → sweep marks the event `EXPIRED` (or
   `CAPACITY_REJECTED` when capacity was binding), **no position is opened for it**, and a
   second, fresh event captured meanwhile enters instead. Assert exactly one terminal
   transition (idempotency: repeated sweeps don't rewrite).
4. **FIFO backlog.** Events E1(old, stale), E2(middle, fresh), E3(new, fresh) with a free slot
   → E1 skipped/marked terminal; E2 then E3 enter in `(signal_time, event_key)` order; assert
   order and that E1's row/lineage survive.
5. **Event redelivery.** Re-deliver an `EXPIRED` event through `capture_with_status` →
   `SAVE_DUPLICATE`, status stays `EXPIRED` (no resurrection; `ON CONFLICT DO NOTHING`
   semantics, `store.py:349-360`). Re-deliver a fresh event → `DUPLICATE`, still eligible.
   (Extend `tests/test_me_inverse_short_source_redelivery_v1.py`.)
6. **Restart with a `CAPTURED` queue.** Build a mixed-age backlog, restart (new contour +
   engine objects over the same store), run cycles → sweep resumes from durable statuses,
   open positions restored verbatim (`entered_at`/`deadline_at` unchanged), stale events
   become terminal, fresh ones enter, balance reconciliation unaffected.
7. **Ambiguous COMMIT / adoption.** Position INSERT committed with lost ACK while the event
   stays `CAPTURED`; advance clock **past TTL**; next cycle → the committed row is still
   **adopted** (age check sits *after* `has_position`, §4.3.2), no second INSERT,
   `mark_event_entered` retried, no expiry refusal, protective exits active. Mirror
   `tests/test_me_long_v2_inverse_short_ambiguous_commit_v1.py` and `..._adoption_costs_v1.py`.
8. **No backfill.** (a) pre-freeze events still refused at capture and at entry
   (`capture.py:255-263`, `engine.py:413-419`); (b) sweep never inserts rows (row counts
   unchanged); (c) migration 085 CHECK `signal_time > activation_freeze_ts` still enforced;
   (d) sweep is a no-op in EXIT_ONLY/DRAINING and while the contour is not activated.
9. **STOP / TP / TIMEOUT preserved.** Full exit suite unaffected: STOP_FIRST on shared bars,
   gap detection, deadline immutability, funding settlement, dirty-funding retry,
   pending-close replay — run the existing exit/funding/durability tests unchanged; add one
   test that a sweep failure (store error) does not block or alter exits.
10. **Frozen fingerprint preserved.** `test_protocol_fingerprint_is_pinned`
    (`tests/...v1.py:290`) and `test_frozen_parameters` (line 282) stay green;
    `protocol_fingerprint()` before/after the change identical; activation row still resolves
    `RUNNING/FULL` with the same `freeze_ts`; `execution_spec()` byte-identical (assert
    explicit snapshot comparison in the new test).

### 7.2 Additional recommended tests

- Sweep placement: cap full + stale backlog + fail-closed accounting gate → events still age
  out (sweep precedes gates), entries still refused.
- Clock skew: `signal_time` slightly in the future (negative age) → treated as eligible, no
  crash, no spurious expiry.
- `mark_event_expired` rowcount semantics: concurrent/idempotent second call is a no-op.
- Telemetry: new refusal codes present in `entry_refusals` and counted; JSON-serializable
  cycle record.
- PG integration (`test_me_long_v2_inverse_short_production_runtime_pg_it_v1.py` style):
  migration 087 applies on a DB with 085+086; CHECK accepts new values, rejects garbage.
- Classification: cap-bound at sweep → `CAPACITY_REJECTED`; not cap-bound → `EXPIRED`
  (and, if Q5 approved, lineage-stamp accuracy across a restart).

### 7.3 Existing tests that encode the OLD "wait forever" contract (must be updated)

| Test | Line | Why it breaks / how to update |
|---|---|---|
| `tests/test_me_long_v2_inverse_short_ambiguous_commit_v1.py::...` | **905** (`pending_events == 1` after `clock.advance(hours=8)`) | XRP event is 8 h old at the assertion → would now be terminal. Re-capture closer to the cycle or assert the new disposition. |
| `tests/test_me_long_v2_inverse_short_production_runtime_v1.py::test_entry_refusal_reasons_are_reported` | **1529-1534** | allow-list of refusal prefixes must include the new codes (`ENTRY_EXPIRED`, `CAPACITY_REJECTED`, …). |
| Any test asserting `pending_events` after large `clock.advance(...)` before an entry | audit during implementation (`funding_draining` 473-484 is EXIT_ONLY → unaffected; `adoption_costs:365` 15 min < TTL → unaffected) | update expectations to the new contract. |

---

## 8. DEPLOY / ROLLBACK PLAN

> Execution of this section is a **separate, approved implementation stage** — not part of
> this analysis task.

### Pre-deploy (read-only + approvals)

1. Owner ratifies Q1 (TTL = 120 min) and Q2 (classification → Option A). Record both in this
   document before any code change.
2. Read-only sizing of the backlog: `SELECT status, min(signal_time), max(signal_time),
   count(*) ... GROUP BY status` on `dds.me_long_v2_inverse_short_v1_event`; also compute
   `entered_at - signal_time` for the 3 open positions (documents whether any *existing*
   position was itself entered late — positions are never modified either way).
3. Merge review; CI green (`me-inverse-short-preflight-ci.yml` + full inverse-short suite).

### Deploy order (Option A)

1. **Migration first:** apply `087_...` to PostgreSQL (source-controlled migration process;
   widen-CHECK only — compatible with the still-running old code).
2. Build/install the new release into `/opt/trad_bot_me_inverse_short_release` (this unit's
   own release dir — never the scanner's), preserve `config.yaml` + `.env`.
3. `systemctl restart trad-bot-me-inverse-short-runtime.service` (implementation stage only).
4. Verify: activation still `RUNNING / FULL`, fingerprint unchanged, `freeze_ts` byte-equal;
   3 open positions unchanged (keys, `entered_at`, `deadline_at`); first cycles show fresh
   entries entering normally, backlog entries transitioning to `EXPIRED`/`CAPACITY_REJECTED`
   exactly once; telemetry `entry_refusals` carries the new codes; heartbeat/log healthy;
   Grafana panels render.
5. Record the deploy timestamp as the OOS segment boundary (§9).

### Rollback (Option A)

1. `git revert` / redeploy previous release + restart the unit → status quo ante (stale-entry
   risk returns; documented as an accepted temporary condition).
2. Migration 087 **stays**: widened CHECK + nullable columns are backward compatible; old code
   never reads or writes the new values; already-terminal events simply remain non-enterable
   (rows preserved; old code's `list_events('CAPTURED')` correctly excludes them — this part
   of the fix is effectively irreversible-but-harmless).
3. No data cleanup, no position/activation changes, no systemd unit changes beyond the restart.
4. If a partial deploy is detected (migration applied, code not), nothing breaks — the sweep
   simply doesn't exist yet.

### Rollback/exit for Option B (if chosen instead)

V1 → `scripts/me_inverse_short_v1_activate.py --drain` (preserves fingerprint/freeze,
EXIT_ONLY keeps managing the 3 positions to closure); start V2 with its own activation +
freeze; retire V1 after its last position closes; V1 history stays queryable forever.

---

## 9. OOS VALIDITY IMPACT

**Option A:**

- The experiment continues under the **same** fingerprint, `freeze_ts` and activation row;
  no re-freeze, no backfill, no history rewrite, positions untouched.
- The guard is a **post-freeze tightening that only removes entries**: it cannot select a
  "better" subset of admissible trades, so it cannot flatter the result by construction.
  Residual validity costs, to be disclosed in the eventual OOS report:
  1. a rule change happened mid-run — the deploy timestamp must be recorded as a segment
     boundary, and results reported with pre/post segments (at minimum: count of entries
     entered under each regime);
  2. the 21 pre-existing `CAPTURED` events are dispositioned under a rule that did not exist
     when they were captured — their terminal statuses are *censoring decisions*, and the
     sensitivity of the outcome to them must be reportable. The disposition ledger makes this
     possible: every event keeps `signal_time`, final status and reason, so a reviewer can
     reconstruct "what the old rule would have done" (enter-all) vs "the fixed rule";
  3. classification risk (Q2): if a reviewer later rules the TTL an execution-spec parameter,
     Option A's run would be considered protocol-divergent from that date → the fallback is
     Option B with a fresh holdout.
- Statistical power impact: with `max_open_positions = 3` and 5-day max holds, capacity
  rejection may censor a large share of signals — this is *honest censoring* (the signal was
  untradeable within its validity window) rather than silent contamination, and it is now
  measurable (`CAPACITY_REJECTED` rate) for the first time.

**Option B:** cleanest validity (fully pre-registered spec, fresh freeze, clean prospective
holdout) but the current run's 3 positions + 21 events still require the V1 disposition (or a
V1 drain), so option B does not by itself remove the need for an Option-A-style decision on
the backlog.

---

## 10. OPEN QUESTIONS

| # | Question | Options / recommendation |
|---|---|---|
| Q1 | Ratify the TTL value before implementation. | **Recommended 120 min** (§4.1 grounds 3-5). Alternative 240 min (ground 6). Any other value needs a written derivation. |
| Q2 | Classification: is the entry-age guard a *data-admissibility guard* (Option A, no fingerprint change) or an *execution_spec parameter* (Option B, new fingerprint)? | **Recommended: admissibility guard** — precedent: price freshness/pre-freeze/fail-closed gates are outside the fingerprint; the guard only rejects. Owner decision is mandatory; B is the fallback. |
| Q3 | Should `CAPACITY_REJECTED` be terminal (recommended) or return the event to `CAPTURED` for later retry? | Terminal — a signal that missed its validity window is not tradeable; returning it re-creates the defect. |
| Q4 | Should the sweep run while fail-closed gates (durability/accounting/dirty funding) block entries? | Yes (recommended): bookkeeping is fail-soft and independent; events must age out even during outages. |
| Q5 | Classification accuracy: current-state rule vs durable `lineage.capacity_blocked_from` stamp (restart-proof)? | Baseline current-state rule is sufficient for v1 of the fix; lineage stamp is a small additive improvement — decide during implementation. |
| Q6 | Do we add an independent signal-level prospective observer (SQL/Grafana now, `research/` observer later)? | Recommend Grafana panels + SQL in this fix; full observer as a follow-up task. |
| Q7 | Immediate disposition of the 21 audit `CAPTURED` events at deploy (mark all older than TTL on first sweep — recommended)? | Confirm after the read-only backlog sizing (§8 pre-deploy step 2). |
| Q8 | Grafana: rename the `status='CAPTURED'` panel to "eligible pending events" and add disposition panels? | Yes (dashboard-only change). |
| Q9 | Migration `087` approval + actual constraint name verification (`pg_constraint` lookup) at implementation time. | Standard migration review; do not guess the constraint name in the script — resolve it dynamically or verify. |
| Q10 | If any of the 3 existing positions turns out to have been entered late (`entered_at − signal_time` > TTL), what is the reporting treatment? | Positions are never modified (task constraint); document as pre-fix regime in the OOS report. |

---

## Appendix A — Answers to the analysis questions (task §4)

1. **Full `CAPTURED → ENTERED` path** — §2.1 (with file/line for every hop).
2. **Where `max_open_positions` applies** — §2.2 (`engine.py:331-333,342-346`;
   `production_runtime.py:456-459`; `config.yaml:25`).
3. **After a slot frees** — §2.3: same-cycle FIFO entry of the oldest `CAPTURED` event at the
   current observed price (`engine.py:740` → `131-132` → `340-366`).
4. **Can an old `CAPTURED` become `ENTERED`?** — §2.4: **yes, unconditionally** (only a
   lower-bound post-freeze check exists, `engine.py:413-419`).
5. **Stale protection elsewhere?** — §2.5: **none** (market-data freshness guards prices only;
   paper-engine expiry machinery is never invoked by this contour; `check_expiration` is dead
   code).
6. **`entered_at`** — §2.6: `engine.py:651` (`self._clock()`), immutable via
   `store.py:368-390` + migration 085 trigger (lines 201-243); `deadline_at = entered_at +
   7200 min` (`engine.py:652`, `protocol.py:98-102`).
7. **Entry price** — §2.6: current observed `prices.get(symbol)` minus SHORT adverse
   slippage (`engine.py:421-424,622-624`); missing price → no entry.
8. **Events after restart** — §2.7: durable rows re-read every cycle; restart neither ages
   nor drops anything; outages convert backlogs into stale-entry candidates.
9. **Idempotency / ambiguous COMMIT** — §2.8: deterministic `event_key`/`position_key`,
   `ON CONFLICT DO NOTHING`, read-back adoption with fact verification, guarded
   `mark_event_entered`, outbox-staged closes, idempotent reconnect-retry unit
   (`store.py:640-721`). Derived constraint: the age guard must sit **after** adoption.
10. **What changes affect the frozen protocol/fingerprint** — anything touching
    `execution_spec()` (`protocol.py:105-130`, incl. `PROTOCOL_VERSION` at line 109) changes
    `protocol_fingerprint()` (lines 133-138) and invalidates the activation
    (`activation.py:152-154`). Engine/store/telemetry/migration changes do **not**
    (Option A boundary). §5.

## Appendix B — Evidence index (primary files)

| File | Key regions (HEAD `6bc7ea7`) |
|---|---|
| `app/paper/me_long_v2_inverse_short_v1/engine.py` | `open_entries` 275-377; capacity 331-333/342-346; `_open_event` 379-463; adoption 404-411/502-614; post-freeze 413-419; price 421-424; `_build_position` 616-674 (`entered_at` 651); exits/slot-free 679-743; recovery 156-221; `pending_events` 1529-1530 |
| `app/paper/me_long_v2_inverse_short_v1/store.py` | `INSERT_EVENT_SQL` 349-360; `INSERT_POSITION_SQL` 362-366; `UPDATE_POSITION_SQL` 371-390; `MARK_EVENT_ENTERED_SQL` 392-396; `list_events` 815-828; `mark_event_entered` 830-839; reconnect 640-721; in-memory mirror 223-234 |
| `app/paper/me_long_v2_inverse_short_v1/runner.py` | `run_cycle` 116-182 (exits 129, entries 131-132, pending count 152-163) |
| `app/paper/me_long_v2_inverse_short_v1/production_runtime.py` | `_gather_symbols` 399-432; `_entry_refusals` 434-462 (`ENTRY_CAP` 456-459); `run_cycle` 464-557; interval 316-320; `load_settings` 813 |
| `app/paper/me_long_v2_inverse_short_v1/capture.py` | `capture_with_status` 195-284; `signal_time` 129; post-freeze 255-263; pre-dedup redelivery contract 8-15 |
| `app/paper/me_long_v2_inverse_short_v1/activation.py` | `evaluate_activation` 126-170; `is_post_freeze` 189-198; permissions 46-52 |
| `app/paper/me_long_v2_inverse_short_v1/protocol.py` | frozen params 47-57; `ENTRY_MODEL` 59-61; `MAX_HOLD_MINUTES` 49; `deadline_for` 98-102; `execution_spec` 105-130; `protocol_fingerprint` 133-138 |
| `app/paper/me_long_v2_inverse_short_v1/models.py` | statuses 30-32; event defaults 85-87 |
| `app/paper/me_long_v2_inverse_short_v1/identity.py` | 5m grid 34; `event_key` 66-89; `position_key` 92-96 |
| `app/paper/me_long_v2_inverse_short_v1/market_data.py` | freshness windows 79-93; exclusion logic 254-360 |
| `sql/migrations/085_me_long_v2_inverse_short_v1.sql` | status CHECK 107-108; prospective CHECK 114; indexes 120-127; immutability trigger 201-243 |
| `sql/migrations/086_me_long_v2_inverse_short_v1_close_outbox.sql` | durable close staging |
| `deploy/systemd/trad-bot-me-inverse-short-runtime.service` | `--interval 10` line 32; `EnvironmentFile` line 30; release dir lines 15-17 |
| `deploy/env/me-inverse-short-runtime.env.example` | activation flag 15; MD freshness 30-34 |
| `monitoring/grafana/dashboards/me-inverse-short-oos.json` | CAPTURED panel 337; event list 590; integrity 506 |
| TTL precedents | `app/paper/engine.py:36-43,1431-1437`; `app/config/settings.py:298`; `app/db/repository.py:1684-1703`; `paper_runner.py:478-484`; `app/paper/cli.py:345-347`; `app/scanners/orchestrator.py:35-40,663-681`; `app/scanners/deduplication.py:57-65`; `app/research/me_r_long_early_mae_v3/protocol.py:30` |
| Tests encoding current contract | `tests/test_me_long_v2_inverse_short_v1.py:282-313,694-707,794-836`; `..._ambiguous_commit_v1.py:905`; `..._production_runtime_v1.py:1520-1534`; `..._funding_draining_v1.py:468-486`; `..._source_redelivery_v1.py` |

---

## Appendix C — IMPLEMENTATION ADDENDUM (stage: IMPLEMENTATION + LOCAL TESTS ONLY)

> **Scope of this stage:** code + tests only. No PR, no deploy, no VPS /
> PostgreSQL / systemd / paper / live / OOS-registry change was made; migration
> 087 is source-controlled only and has NOT been applied to any database.
> Owner decisions applied: **Q1 = 120-minute entry TTL** living inside the
> frozen V2 `execution_spec()` (Q2 → variant B: the TTL is part of the hashed
> protocol payload, not an out-of-band guard), and **Q2 = variant B** — a NEW
> experiment `ME_LONG_V2_INVERSE_SHORT_V2` with its own fingerprint, its own
> activation row and its own clean prospective holdout; V1 is untouched.

### C.1 Delivered artifacts (branch `fix/me-inverse-short-stale-entry-guard-v1`)

| # | Artifact | Path |
|---|---|---|
| 1 | V2 protocol — frozen spec + `entry_max_age_minutes: 120` + derivation | `app/paper/me_long_v2_inverse_short_v2/protocol.py` |
| 2 | Four-state event model + refusal audit fields (`EXPIRED`, `CAPACITY_REJECTED`, `status_reason`, `status_at`) | `.../models.py` |
| 3 | Exactly-once disposition transitions (CAS on `status`; `ENTERED` clears the reason) | `.../store.py` — `mark_event_expired(event_key, status, *, reason, decided_at) -> bool` |
| 4 | Backlog sweep + per-event age gate | `.../engine.py` — `_sweep_stale_events()` (line 290), called from `open_entries()` (line 403) **before** the capacity early-return and the fail-closed gates; second age check in `_open_event()` (lines 536-542) **after** the `has_position → adopt` branch |
| 5 | Read-only signal-level prospective observer | `.../observer.py` — `SignalLevelProspectiveObserver.observe()` / `.dispositions()` |
| 6 | Activation / capture / runner / identity / market-data / production-runtime | V2-renamed copies — flag `ME_INVERSE_SHORT_V2_ENABLED`, namespace `ME_LONG_V2_INVERSE_SHORT_V2:`, lock/heartbeat `me_inverse_short_v2_runtime*` |
| 7 | Additive migration (source-controlled, NOT applied) | `sql/migrations/087_me_long_v2_inverse_short_v2.sql` |
| 8 | Grafana dashboard — 12 panels (captured / entered / expired / capacity-rejected / open / finalized + dispositions table) | `monitoring/grafana/dashboards/me-inverse-short-v2-oos.json` |
| 9 | Activation + preflight tooling | `scripts/me_inverse_short_v2_activate.py`, `scripts/me_inverse_short_v2_preflight.py` |
| 10 | New tests — 55 tests in two suites | `tests/test_me_long_v2_inverse_short_v2_entry_ttl_v1.py` (behaviour), `tests/test_me_long_v2_inverse_short_v2_isolation_v1.py` (isolation/observer) |
| 11 | Test-contract updates forced by migration 087 (mechanical, cross-PR) | `tests/test_srr_short_fill_feasibility_migration_082_v1.py` (newest-number pin 086 → 087), `tests/test_srr_short_fill_pre_activation_hardening_v1.py` (stale pin repaired), `tests/test_htf_baseline_short_phase2_isolation_v1.py` (allowlist) + `tests/test_htf_baseline_short_phase2_scope_routing_v1.py` (routing expectation for the two external paths) |

### C.2 Frozen protocol fingerprints

| Experiment | `protocol_fingerprint()` |
|---|---|
| **V2 (new)** | `5a9047ca3621cefb4d53b45275ad6348887440568cb055f843f85e2eb1abdacc` |
| **V1 (unchanged)** | `4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c` |

`execution_spec()` gained exactly one field — `entry_max_age_minutes: 120` —
so the V2 fingerprint moved exactly once, for exactly the owner-approved
reason (Q2 → B). Both values are pinned in
`tests/test_me_long_v2_inverse_short_v2_isolation_v1.py`; a mutation test
proves the TTL is inside the hashed payload.

### C.3 Acceptance criteria → evidence

| Acceptance criterion | Evidence (all local, in-memory) |
|---|---|
| age < 120 min may enter (other gates intact) | `test_fresh_event_enters_same_cycle`, `test_event_just_under_ttl_still_enters` (119 m 59 s), `test_fresh_event_without_price_stays_pending` |
| age ≥ 120 min can NEVER open a new position | `test_event_at_exactly_ttl_never_enters` (exactly 120 m), `test_event_past_ttl_never_enters`, `test_expired_event_is_terminal_across_cycles_and_redelivery` |
| headline: slot frees after TTL → no stale entry | `test_slot_release_never_opens_a_stale_backlog_event` (BTC TP frees a slot; stale `XRPUSDT` → `EXPIRED`/`ENTRY_TTL`, fresh `DOGEUSDT` takes the slot) |
| `ENTRY_CAP` blocks new positions | `test_entry_cap_blocks_a_fresh_signal_without_disposing_it` (fresh stays `CAPTURED`), `test_stale_event_while_book_is_full_is_capacity_rejected` |
| `EXPIRED` ≠ `CAPACITY_REJECTED`, both with cause + timestamp | sweep classification (`ENTRY_TTL` vs `ENTRY_CAP`, `status_at = decision clock`), `test_sweep_runs_even_while_fail_closed_gates_block_entries`, `test_observer_dispositions_expose_reason_and_timestamp` |
| duplicate delivery / restart / ambiguous COMMIT preserved | `test_expired_event_is_terminal_across_cycles_and_redelivery` (redelivery → `CAPTURE_DUPLICATE`, row count unchanged), `test_restart_with_captured_backlog_resumes_the_sweep`, `test_ambiguous_commit_adoption_is_never_age_gated` (committed row adopted verbatim past TTL, exactly one position row), `test_duplicate_position_suppression_still_holds_with_ttl` |
| FIFO backlog order | `test_fresh_backlog_enters_in_signal_time_order`, `test_mixed_ages_stale_head_is_skipped_fresh_tail_enters_in_order` |
| STOP / TP / TIMEOUT exits unchanged | `test_stop_loss_exit_is_preserved` (intrabar → `STOP_LOSS`), `test_take_profit_exit_is_preserved`, `test_timeout_exit_is_preserved_past_the_ttl` |
| no backfill | `test_pre_freeze_event_is_refused_at_capture`, `test_capture_is_disabled_without_the_v2_flag`, `test_engine_double_checks_pre_freeze_events`, migration `CHECK (signal_time > activation_freeze_ts)` |
| sweep fail-soft / DRAINING never sweeps | `test_sweep_read_failure_fails_closed_without_raising`, `test_sweep_write_failure_keeps_event_capTUREd_but_refuses_entry`, `test_draining_never_sweeps_and_never_enters` |
| observer: read-only, no trades when cap full, no look-ahead | `test_observer_is_read_only_and_creates_no_trades_when_cap_full`, `test_observer_source_is_structurally_read_only`, `test_observer_has_no_lookahead_across_clock_moves`, `test_observer_after_expiry_reports_expired_disposition` |
| V1 fingerprint / freeze / tables / results unchanged | `test_v1_fingerprint_is_still_the_frozen_v1_value`, `test_v1_contour_still_enters_and_is_two_state` (V1 still enters a 10-hour-old event — old contract intact), `git diff` vs merge-base: **no V1 file modified** |
| V2 own fingerprint / activation / namespace | `TestV2Fingerprint`, `TestActivationIsolation`, `TestStoreIsolation` (4-state, namespaced keys, cross-version keys differ) |
| migration 087 additive, V2-only | `TestMigration087` (4 `CREATE TABLE`, no `_V1_` in executable statements, no `DROP`/`TRUNCATE`/`ALTER TABLE`, four-state CHECK + refusal audit, fail-closed seed) |
| Grafana panels observable | `TestGrafanaDashboard` (all queries hit `*_v2_*` tables, all four dispositions queryable) |

### C.4 Local test results (this stage)

| Suite | Result |
|---|---|
| New V2 suites (`..._entry_ttl_v1.py` + `..._isolation_v1.py`) | **55 passed** |
| All inverse-short suites (14 files: V1 + V2 + ambiguous-commit + production-runtime + funding/draining + redelivery) | **354 passed, 4 skipped** (skips = PostgreSQL-backed tests; no `TEST_DB_*` locally) |
| Full local suite, clean-HEAD baseline (archive of HEAD, same excludes) | 77 failed / 6268 passed / 81 skipped / 168 errors |
| Full local suite, this implementation | **73 failed / 6327 passed / 81 skipped / 168 errors** |
| Failures unique to this implementation vs baseline | **0** (baseline-minus-final = 3 git-artifact failures of the `.git`-less archive copy + 1 stale pin repaired here: `test_migration_083_is_next_and_unique`) |

The remaining 73 failures are byte-identical to the clean-HEAD baseline set:
PostgreSQL-dependent tests (no local `TEST_DB_*`), platform/dependency tests
(`pwd`, `uid`, `scramp 1.5.0` vs `pg8000`, missing `psycopg2`/`requests`/`tools`),
config-drift tests (prospective registry count, `PAPER_DISABLED_SCANNERS`),
and the pre-existing `test_new_files_are_confined_to_phase2_surface` (it fails
at HEAD itself: the committed analysis doc is an `A` entry outside the
Phase-2 allowlist, plus pre-existing untracked workspace artifacts). Six
further test modules cannot even be collected locally (Unix-only `pwd`,
`psycopg2`, `requests`, `tools.research`) and are excluded from both runs —
same exclusions on both sides.

### C.5 Migration 087 — plan (NOT applied in this stage)

`sql/migrations/087_me_long_v2_inverse_short_v2.sql` creates, inside the
existing `dds` schema, four brand-new objects mirroring 085+086 with V2
identity: `me_long_v2_inverse_short_v2_activation` (seeded `READY_TO_START`,
fingerprint/freeze NULL → fail-closed), `..._event` (four-state CHECK,
`status_reason`/`status_at` with `status NOT IN ('EXPIRED','CAPACITY_REJECTED')
OR (status_reason IS NOT NULL AND status_at IS NOT NULL)`, prospective
`CHECK (signal_time > activation_freeze_ts)`), `..._position`
(namespaced `position_key`, entry-facts/deadline immutability trigger),
`..._close_outbox` (durable terminal snapshot, contract of 086). It contains
no `ALTER`/`DROP`/`TRUNCATE` of anything existing, never references a `_v1_`
object in executable statements, and never writes the V1 activation row.

Deploy-stage application sequence (separately approved): schema review →
apply on target PostgreSQL inside a transaction (already `BEGIN/COMMIT`) →
verify `pg_constraint` names dynamically (Q9) → run preflight → freeze the
fingerprint (`5a9047…`) + fresh `freeze_ts` via
`scripts/me_inverse_short_v2_activate.py --activate` → only then set
`ME_INVERSE_SHORT_V2_ENABLED=1`. Rollback = drop the four new V2 objects
only; V1 is never in the blast radius.

### C.6 Deploy prerequisites (deliberately NOT done here)

1. `deploy/env/me-inverse-short-v2-runtime.env.example` + systemd unit copy
   (`EnvironmentFile` for the V2 flag, `me_inverse_short_v2_runtime` lock and
   heartbeat paths) — V1 unit/env stay byte-identical.
2. Apply migration 087 to the target database (approved stage).
3. `python scripts/me_inverse_short_v2_preflight.py --env-file
   /etc/trad-bot/me-inverse-short-v2-runtime.env` (flag must be `0` at this
   point; both V1 rows/tables verified present and untouched).
4. Fixation: `me_inverse_short_v2_activate.py --activate --freeze-ts
   <FRESH_UTC>` — writes ONLY `dds.me_long_v2_inverse_short_v2_activation`.
5. Flip `ME_INVERSE_SHORT_V2_ENABLED=1`, start the V2 runtime; confirm the
   first events have `signal_time > freeze_ts` (no backfill).
6. Import `monitoring/grafana/dashboards/me-inverse-short-v2-oos.json`.

### C.7 Known risks / limitations

1. **Migration 087 has not been executed against a live PostgreSQL** in this
   stage (forbidden): SQL is reviewed and unit-tested textually only; the
   PostgreSQL-backed tests skip locally (no `TEST_DB_*`).
2. **Runtime modules are renamed copies of V1** (`production_runtime.py`,
   `market_data.py`, `runner.py`): future V1 fixes must be ported manually —
   drift risk inherent to the variant-B isolation choice.
3. **Grafana JSON is validated structurally** (parses, panel/SQL assertions)
   but has not been rendered in a live Grafana.
4. **Sweep classification uses the current-state rule** (book full at
   decision time → `CAPACITY_REJECTED`); the Q5 lineage stamp
   (`lineage.capacity_blocked_from`) was accepted as a follow-up, so an event
   rejected while the book was full earlier and evaluated when a slot is free
   is classified `EXPIRED` — disposition remains terminal either way.
5. **Activate/preflight scripts are compile-checked, not executed** (they
   need a database).
6. **Pre-existing local failures remain** (identical to clean-HEAD baseline,
   see C.4) — none reference V1/V2 inverse-short code.
7. `event_key` for V2 embeds the V2 experiment id (namespaced identity), so
   V1 and V2 rows for the same source candle intentionally differ — the
   idempotency mechanism is preserved, keys are not shared across ledgers.

### C.8 Verdict

**READY_FOR_REVIEW** — implementation complete in isolation (V1 byte-identical),
migration/dashboard/tooling delivered, acceptance criteria each mapped to a
passing local test, **0 test regressions vs clean-HEAD baseline**, all
inverse-short suites green (354 passed, 4 environment-skipped) and both new
suites green (55 passed). Deploy remains a separately approved stage.
