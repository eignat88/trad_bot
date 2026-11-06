# ME_LONG_V2_INVERSE_SHORT_V1 — Frozen Protocol & Readiness Report

**Status:** IMPLEMENTED · `NOT_DEPLOYED` (fail-closed until the separate
activation step) · paper-only · prospective-only · no historical backfill.

Independent prospective **SHORT** paper strategy derived from the raw events
of the ORIGINAL detector `MOMENTUM_EXHAUSTION_REVERSE_LONG_V2` (whose own
production direction remains LONG and stays blocked in both directions).

---

## 1. Frozen protocol

| Parameter | Value |
|---|---|
| Strategy ID | `ME_LONG_V2_INVERSE_SHORT_V1` |
| Source scanner | `MOMENTUM_EXHAUSTION_REVERSE_LONG_V2` (source events are `LONG`) |
| Traded direction | `SHORT` |
| Take-profit | **10% of the ACTUAL entry price** (`entry * 0.90`) |
| Stop-loss | **10% of the ACTUAL entry price** (`entry * 1.10`) |
| Max hold | **7200 minutes (120 h)** → `deadline_at = entered_at + 7200 min`, computed ONCE at entry |
| Exit priority | `STOP_LOSS → TAKE_PROFIT_1 → TIMEOUT` (STOP_FIRST on a shared bar) |
| Exit reasons | `STOP_LOSS`, `STOP_LOSS_GAP`, `TAKE_PROFIT_1`, `TIMEOUT` |
| DCA / trailing / breakeven | **OFF** (always) |
| Entry model | `NEXT_OBSERVED_MARKET_PRICE` at the first observed price after capture |
| Fees | taker `0.00055` per side |
| Slippage | `0.0005` per side, adverse on BOTH fills (SHORT sell → fill down; SHORT cover → fill up) |
| Funding | whole elapsed `8 h` intervals, SHORT sign convention (positive rate ⇒ SHORT receives) |
| Mode | prospective-only, paper-only, `max_open_positions` from settings |
| Capture route | `PRE_FILTER_SOURCE` — before direction gate / expectancy filter, which stay untouched |
| **Protocol fingerprint** | `4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c` |

The fingerprint is SHA-256 over the canonical JSON of the frozen spec
(`app/paper/me_long_v2_inverse_short_v1/protocol.py`). It is **pinned in
`tests/test_me_long_v2_inverse_short_v1.py`**: any parameter change fails
that test first, which is the signal to re-review and re-fix the
fingerprint + `freeze_ts` — never to silently deploy a modified protocol.

## 2. Architecture (file map)

```
app/paper/me_long_v2_inverse_short_v1/
  protocol.py    frozen constants, short_geometry(), deadline_for(),
                 execution_spec(), protocol_fingerprint(), describe_protocol()
  identity.py    stable event_key (sha256 of scanner|version|symbol|LONG|tf|
                 signal-candle-open-ms) + namespaced position_key
  models.py      InverseShortEvent / InverseShortPosition / ActivationRecord
  activation.py  two-factor fail-closed activation + strict signal_time > freeze_ts
  capture.py     capture_with_status() — honest statuses, never raises
  store.py       InverseShortStore interface, InMemoryInverseShortStore (tests),
                 PgInverseShortStore (dedicated connection, idempotent writes)
  engine.py      InverseShortPaperEngine — isolated positions, frozen geometry,
                 restart recovery, STOP_FIRST exits, fees/slippage/funding, PnL
  runner.py      InverseShortContour (one cycle) + run_dry_run() readiness check

scanner-side wiring (fail-open, pre-gate)
  app/scanners/orchestrator.py   stats["_me_inverse_short_source_events"] —
                                 published AFTER dedup, BEFORE gate/expectancy
  scanner_runner.py              _capture_me_inverse_short_source() → dedicated
                                 pg8000 store; errors sunk as
                                 ME_INVERSE_SHORT_SOURCE_CAPTURE_ERROR

processes & tools
  me_inverse_short_runner.py                 standalone contour process
                                             (--dry-run / --once / real mode)
  scripts/me_inverse_short_v1_activate.py    --show / --activate --freeze-ts /
                                             --drain fixation tool

storage (migration 085 + schema.sql bootstrap, additive)
  dds.me_long_v2_inverse_short_v1_activation  status + fingerprint + freeze_ts
  dds.me_long_v2_inverse_short_v1_event       append-only source ledger (lineage JSONB)
  dds.me_long_v2_inverse_short_v1_position    isolated SHORT positions + immutability trigger
```

## 3. Requirements → implementation matrix

| # | Requirement | Where |
|---|---|---|
| 1 | Raw events received independently of direction gate + expectancy filter (both unchanged) | `orchestrator.py` `_me_inverse_short_source_events` (pre-gate), `capture.py`, `scanner_runner.py` hook |
| 2 | Stable `event_key` + full lineage to the source signal | `identity.py`, `capture.event_from_candidate` (lineage JSONB: setup_id, signal time, signal-candle ms, features, pipeline fingerprint, protocol fingerprint) |
| 3 | Isolated SHORT position state, no shared `open_trades[symbol]` | `engine.open_positions` keyed `ME_LONG_V2_INVERSE_SHORT_V1:<event_key>`; own tables |
| 4 | Realistic entry/TP/SL/TIMEOUT with fees, slippage, funding | `engine._build_position/_apply_funding/_close` |
| 5 | Entry date + original deadline stored immutably across restarts | `deadline_for()` computed once; store repair + `BEFORE UPDATE` trigger (migration 085) |
| 6 | Existing Paper recovery reads `execution_policy`, `execution_policy_version`, `planned_exit_at` | `app/paper/engine.py::_load_open_trades` + `_parse_planned_exit`, capability-guarded `app/db/repository.py`, migration `084` |
| 7 | Blocks on the original V2 unchanged | audited: `config.yaml` still blocks V2 LONG/SHORT; V2 `FIXED_TP_SL_HORIZON_V1` policy untouched; tests in `test_me_long_v2_inverse_short_source_capture.py` |
| 8 | Old paper trades / OOS results / production trading unchanged | migrations 084/085 additive-only (no UPDATE/DELETE of existing rows); contour never writes `dds.paper_trade` / `dds.scanner_setup` |
| 9 | Tests: TP, SL, TIMEOUT, restart, duplicates, simultaneous TP/SL, missing data, isolation | `tests/test_me_long_v2_inverse_short_v1.py` (50 tests) |
| 10 | Launch only after tests pass AND separate fixation of fingerprint / `freeze_ts` | `scripts/me_inverse_short_v1_activate.py` + runner exit-code-2 fail-closed startup |

## 4. Source-capture audit (requirement 1)

* Capture point sits in `ScannerOrchestrator.scan_all_with_stats` after
  dedup and **before** the direction-gate evaluation — asserted
  textually (index order) and behaviourally in
  `tests/test_me_long_v2_inverse_short_source_capture.py`:
  * the V2 LONG candidate appears in `stats["_me_inverse_short_source_events"]`
    while it is **absent** from the tradeable list (gate BLOCKED),
  * `gate.evaluate(V2, LONG)` is still `allowed=False`,
  * `filter_candidates()` still rejects it on negative expectancy and the
    expectancy records are byte-identical afterwards,
  * `setups_saved == 0` for the scanner in the same cycle.
* The stats key is `_`-prefixed, so `scanner_runner`'s stats loop skips it
  as a counter and only forwards it through
  `_capture_me_inverse_short_source(repository, inv_candidate)`.
* Fail-open contract (tested): flag off ⇒ **zero** DB touch; store
  unavailable ⇒ error sunk to `dds.scanner_error` as
  `ME_INVERSE_SHORT_SOURCE_CAPTURE_ERROR`, never raises; even a failing
  error sink is swallowed. A capture failure can never break a scan cycle,
  and a capture success can never change a trading decision.
* Capture is two-factor: runtime flag `ME_INVERSE_SHORT_V1_ENABLED=1` AND a
  `RUNNING` activation row whose frozen fingerprint matches the running
  code AND a non-null `freeze_ts`. Missing/mismatched ⇒ honest status
  (`DISABLED / NOT_ACTIVE / PRE_FREEZE / …`), nothing written.
* No-backfill by construction: strict `signal_time > freeze_ts` in
  `capture.py`, re-checked at entry in `engine.py`, and enforced by the
  database `CHECK (signal_time > activation_freeze_ts)` (migration 085).

## 5. Isolation audit (requirement 3)

* Contour positions live in `engine.open_positions`, keyed
  `ME_LONG_V2_INVERSE_SHORT_V1:<event_key>` — never in
  `PaperTradingEngine.open_trades[symbol]`; the contour engine does not
  even expose an `open_trades` attribute (asserted).
* Separate tables (`…_event`, `…_position`, `…_activation`), dedicated
  `pg8000` connection — no shared transaction with scanner/paper/research.
* Same-symbol semantics proven side by side in
  `TestIsolationFromProductionPaper`:
  * production engine keeps ONE trade per symbol and its balance/open_trades
    are byte-for-byte untouched by contour entries and exits,
  * the contour opens TWO positions on the same symbol (different source
    events) simultaneously,
  * closing either side never disturbs the other's state or PnL,
  * contour balance is reconstructed exclusively from the contour ledger.
* Production tables are never written: no `INSERT INTO dds.paper_trade` /
  `dds.scanner_setup`, no `ensure_schema()` anywhere in the package
  (asserted per file).

## 6. Execution & PnL model

```
entry_fill   = market_mid * (1 - slippage)              # SHORT sell, adverse
stop         = entry_fill * 1.10      target = entry_fill * 0.90
qty          = min(balance*risk/(0.10*entry), balance*max_exposure/entry)
exit_fill    = exit_ref * (1 + slippage)                # SHORT cover, adverse
gross        = (entry_mid - exit_ref) * qty
net_pnl      = gross - entry_fee - exit_fee - funding_paid - (entry_slip + exit_slip)
balance     += gross - exit_fee - exit_slip   (funding settled per interval)
```

`exit_ref` is `stop_price` (STOP/STOP_GAP), `target_price` (TP) or the
observed mid (TIMEOUT). Simultaneous conditions on one bar resolve
STOP-first; deadline and TP together resolve TP-first
(`STOP → TP → TIMEOUT`), all pinned by tests. Missing market data ⇒ hold
/ skip (never guess a fill); an invalid exit reason raises `INVALID_EXIT_REASON`.

## 7. Restart recovery & immutability

* `entered_at` / `deadline_at` are computed exactly once at entry and are
  persisted before the event is marked `ENTERED`.
* `PgInverseShortStore.UPDATE_POSITION_SQL` deliberately excludes
  `entered_at`, `deadline_at` and every entry column;
  `InMemoryInverseShortStore.update_position` repairs any divergence
  (counter asserted in tests); migration 085 adds a `BEFORE UPDATE`
  trigger that makes a rewrite a hard database error.
* Recovery restores positions verbatim (same entry, same original
  deadline — NOT recomputed from restart time), refuses non-SHORT rows,
  duplicate keys and invalid deadlines, and rebuilds the balance from the
  contour ledger. Covered by `TestRestartRecovery`.

## 8. Requirement 6 — Paper execution-policy recovery

* `PaperTradingEngine._load_open_trades` now reads `execution_policy`,
  `execution_policy_version` and `planned_exit_at` (with
  `_parse_planned_exit` handling aware/naive/`Z`/malformed values without
  ever crashing startup) and logs
  `paper recovery: restored execution_policy=…` for non-DEFAULT rows.
* `ScannerRepository` gained a cached, read-only capability probe against
  `information_schema.columns`: reads/writes use the three columns when
  they exist and keep the exact pre-084 SQL shape when they do not
  (runners never call `ensure_schema`).
* Migration `084_paper_execution_policy_recovery_v1.sql` adds
  `execution_policy_version TEXT NOT NULL DEFAULT ''` and re-states the
  038 columns `IF NOT EXISTS` — **no existing row is touched**, so old
  paper trades keep their data byte-for-byte (they simply read `''` /
  legacy defaults, matching the previous in-memory behaviour).
* Behaviour pinned by `tests/test_paper_execution_policy_recovery_v1.py`:
  the RESTORED `planned_exit_at` drives the `FIXED_HORIZON` exit even when
  `entered_at + hold_minutes` would still be in the future, and a legacy
  row without the columns keeps its exact old behaviour.

## 9. Activation procedure (requirement 10 — separate fixation)

```powershell
# 1) All test suites must pass first (see §10).

# 2) Read-only status of the activation row + current code fingerprint:
python scripts/me_inverse_short_v1_activate.py --show

# 3) Fix the CURRENT protocol fingerprint together with freeze_ts
#    (timezone-aware, never in the future — both validated):
python scripts/me_inverse_short_v1_activate.py --activate --freeze-ts 2026-11-07T00:00:00Z --note "..."

# 4) Enable the runtime flag (e.g. in the contour process environment):
#    ME_INVERSE_SHORT_V1_ENABLED=1

# 5) Start the contour (fail-closed: exits with code 2 unless BOTH gates hold):
python me_inverse_short_runner.py --dry-run     # final readiness check
python me_inverse_short_runner.py               # real contour process

# Drain (stop new captures/entries; open positions keep managing exits):
python scripts/me_inverse_short_v1_activate.py --drain
```

Fail-closed matrix (`activation.py`, all tested):

| Condition | Reason | Effect |
|---|---|---|
| flag unset | `DISABLED_FLAG` | no capture, no entries, no DB touch |
| row missing | `ACTIVATION_ROW_MISSING` | runner exits 2 |
| status ≠ RUNNING | `ACTIVATION_NOT_RUNNING` | runner exits 2 |
| fingerprint ≠ running code | `PROTOCOL_FINGERPRINT_MISMATCH` | capture `NOT_ACTIVE`, runner exits 2 |
| freeze missing / naive | `FREEZE_TS_MISSING` / `FREEZE_TS_INVALID` | runner exits 2 |
| `signal_time <= freeze_ts` | `PRE_FREEZE` | refused at capture AND at entry AND by DB CHECK |

The fixator refuses to overwrite an existing RUNNING activation without an
explicit `--re-freeze`, refuses a `CANCELLED` row, refuses future/naive
freeze timestamps, and its UPDATE touches only the activation table.

## 10. Test & migration inventory

| Suite | Coverage | Count |
|---|---|---|
| `tests/test_me_long_v2_inverse_short_v1.py` | protocol freeze + pinned fingerprint, event identity/lineage, capture statuses, entry model, TP/SL/TIMEOUT + PnL math, simultaneous STOP_FIRST, missing data, restart recovery + immutability, duplicate delivery, isolation vs production Paper, funding sign, fail-closed entries, dry-run | 50 |
| `tests/test_me_long_v2_inverse_short_source_capture.py` | pre-gate capture independence, gate/expectancy/config non-modification, runner fail-open, migration/schema audits, activation fixator, runner wiring | 30 |
| `tests/test_paper_execution_policy_recovery_v1.py` | requirement 6: engine recovery, FIXED_HORIZON honoured after restart, legacy-row defaults, parse variants, repository capability-guarded SELECT/INSERT, additive migrations | 24 |
| **Total (new)** | | **104 — all passing, no PostgreSQL required** |

Regression subsets re-run green (186 + 143 tests): paper fixed-horizon,
paper engine, score bypass, direction gate, expectancy filter, DB
transaction recovery, regime filter, exit-reason/schema contracts,
scanner runtime, ME capture integrations.

Migrations (source-controlled, not applied to production by this task):
`084_paper_execution_policy_recovery_v1.sql`,
`085_me_long_v2_inverse_short_v1.sql`; both mirrored (tables/columns, no
function bodies) into `app/db/schema.sql` for the split-on-`;` bootstrap —
the immutability trigger lives only in migration 085.

## 11. Dry-run evidence (pre-activation readiness)

`python me_inverse_short_runner.py --dry-run` → exit code `0`:

```json
{
  "ok": true,
  "capture": {"DRYTPUSDT": "CAPTURED", "DRYSLUSDT": "CAPTURED", "DRYTOUSDT": "CAPTURED"},
  "entries": 3,
  "closed": [
    {"symbol": "DRYSLUSDT",  "exit_reason": "STOP_LOSS",      "entry_market": 100.0, "stop": 109.945, "target": 89.955, "funding_paid": 0.0,       "net_pnl": -50.8527},
    {"symbol": "DRYTOUSDT",  "exit_reason": "TIMEOUT",       "entry_market": 100.0, "stop": 109.945, "target": 89.955, "funding_paid": -0.799958, "net_pnl": -0.2505},
    {"symbol": "DRYTPUSDT",  "exit_reason": "TAKE_PROFIT_1", "entry_market": 100.0, "stop": 109.945, "target": 89.955, "funding_paid": 0.0,       "net_pnl": 49.2472}
  ],
  "snapshot": {"balance": 9998.14, "starting_balance": 10000.0, "net_pnl": -1.86,
               "closed_trades": 3, "open_positions": 0, "direction": "SHORT"},
  "immutability_repairs": 0
}
```

Notes: the dry-run exercises the REAL capture path, entry sizing, frozen
geometry (stop 109.945 / target 89.955 = ±10% of the actual
slippage-adjusted entry 99.95), all three exit types, fees + slippage on
both sides, one funding settlement (SHORT receives +0.01 % over 16 whole
intervals), and balance reconstruction
`10000 + (−1.856) = 9998.14`. `immutability_repairs == 0` proves
`entered_at` / `deadline_at` were never rewritten during the run.

## 12. Readiness checklist

- [x] Unit/integration tests pass (104 new, DB-less; targeted regressions green)
- [x] Restart-recovery test (entry time + original deadline restored verbatim)
- [x] Source-capture audit (pre-gate, fail-open, two-factor, no-backfill)
- [x] Isolation audit (namespaced state, side-by-side with production Paper)
- [x] Dry-run executed successfully (§11)
- [ ] **Separate fixation of protocol fingerprint / `freeze_ts`** — operator step (§9), `--show` currently reports `NOT_DEPLOYED`
- [ ] Runtime flag + runner start — only after the two items above

Until the last two boxes are ticked by the operator, the contour stays
inert: no capture, no entries, no writes.

## 13. Full-suite regression accounting

`pytest tests` (excluding 5 pre-existing environmental collection errors:
`pwd` / `psycopg2` / missing `tools.research.*` module):

* **239 baseline failures unchanged** — every id listed in
  `baseline_failed_error_ids.txt` that can run still fails exactly as
  before (DB/analytics/OOS suites requiring PostgreSQL);
* **new failures introduced by this task: 0** after one maintenance fix:
  `test_migration_number_is_next_and_not_taken` (migration 082 contract)
  pinned `numbers[-1] == 82` and is now future-proofed to `>= 82` —
  the same convention already used by the migration-078 contract test;
* `test_htf_baseline_short_phase2_isolation_v1.py` (2 tests) and
  `test_htf_baseline_short_phase2_systemd_bybit_wiring_v1.py::
  test_scanner_paper_evaluator_settings_unchanged` are **pre-existing
  red** — they were already failing on 2026-11-05 21:16 (documented delta
  in `new_failed_error_ids.txt` vs `baseline_failed_error_ids.txt`) from
  other tasks' uncommitted work in this shared worktree. They are
  PR-scope guards for the issue-#197 Phase-2 surface (they reject ANY
  scanner/paper/live change outside that PR); this task legitimately
  touches `scanner_runner.py`, `app/scanners/orchestrator.py` and
  `app/paper/engine.py`, so they would need a documented exception of the
  FIX-5/FIX-7 style at PR time. Their other control — byte-identity of the
  frozen `FROZEN_PATHS` (Point-B stack, registry, DB, workflow) — still
  passes, and nothing frozen was modified.
