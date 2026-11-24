# ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1
## Phase 1: Source Observer Protocol

Issue: #296
Parent: #295
PR: #303
Version: 1.0.0
Mode: RESEARCH_ONLY
Deployment status: DEPLOY_HOLD
Prospective activation: NOT STARTED (requires a separate GO decision)

### Source

dds.me_long_v2_inverse_short_v3_event

Source scanner:
MOMENTUM_EXHAUSTION_REVERSE_LONG_V2

Source direction: LONG
Capture route: PRE_FILTER_SOURCE

Source access: SELECT ONLY.

### Source independence limitation (review P2)

This observer is an INDEPENDENT CONSUMER of the V3 producer ledger, but
it is NOT a scanner-level source capture. Its completeness depends on
the V3 producer (`me_long_v2_inverse_short_v3_event` writes from the
V3 runtime capture contour).

Two coverage dimensions are tracked SEPARATELY and never conflated:

- `V3_LEDGER_COVERAGE_VALIDATED` — reconciliation of the research
  ledger against the AVAILABLE V3 ledger passed;
- `UPSTREAM_SOURCE_COVERAGE_UNPROVEN` — the independent completeness of
  the upstream (scanner-level) capture is NOT proven by ledger
  agreement alone.

Consequences:

- a match between V3 ledger and research records NEVER upgrades to a
  full `SOURCE_VALIDATED` claim;
- the issued coverage status stays `SOURCE_COVERAGE_UNKNOWN` until an
  independent upstream verification exists;
- the final OOS verdict is never issued by this contour.

Technical options for independent upstream verification WITHOUT
touching the working trading filters (for a future, separately
approved phase — none implemented in Issue #296):

1. Read-only replication/dump comparison of the pre-filter detector
   output against `dds.scanner_setup` / scanner telemetry snapshots on
   a fixed schedule (SELECT-only, separate role).
2. A separate standalone capture process fed by read-only market data
   that re-derives the detector events independently and compares
   event keys (pure research consumer; no trading writes).
3. Producer-side signed counters/manifests published by the V3 capture
   contour (requires a separately approved change to the V3 runtime —
   explicitly OUT OF SCOPE for #296).

If none of these can be delivered within the Issue #296 constraints,
the deployment status remains `DEPLOY_HOLD` with this document listing
the blocker explicitly (see "Current decision").

### Identity

Preserve original V3 event_key.
Never regenerate identity from observation time.

One event_key = one source event.

### Prospective boundary

New independent freeze_ts.

Eligible events require:
- signal_time > freeze_ts
- source created_at >= freeze_ts
- source captured_at >= freeze_ts
- complete source lineage

No backfill into the prospective cohort.

`freeze_ts` is immutable after first fixation (DB trigger
`IMMUTABLE_FREEZE_TS`); manual recovery NEVER resets it.

### Source event validation (review P1)

Type/value validation — key presence alone is not enough. Deterministic
`reason_code` per violation; original feature values are preserved
verbatim (never recalculated or substituted).

Required features (`lineage.source_features`):

- `rsi_14`: finite number, `0 <= rsi_14 <= 100`.
  Rejects: null (`FEATURE_RSI14_NOT_FINITE`), NaN/Infinity
  (`FEATURE_RSI14_NOT_FINITE`), strings and booleans
  (`FEATURE_RSI14_INVALID_TYPE`), out-of-range values
  (`FEATURE_RSI14_OUT_OF_RANGE`).
- `rsi_delta_3`: finite number (`FEATURE_RSI_DELTA_3_NOT_FINITE` /
  `FEATURE_RSI_DELTA_3_INVALID_TYPE`). No additional bounds: there is
  no confirmed scanner contract constraining this value further.
- `entry_confirmation_rsi_rising`: a REAL boolean (the V3 source stores
  a JSON boolean). No implicit string/number coercion
  (`FEATURE_CONFIRMATION_FLAG_INVALID_TYPE`).

Identity/lineage fields must be non-empty and well-formed:
`source_identity`, `source_pipeline_fingerprint` (non-empty strings),
`source_signal_time` (ISO timestamp equal to row signal_time),
`source_signal_candle_open_time` (positive integer, matching the row),
`event_key` (64 hex chars), `symbol`, `entry_timeframe`.

### Processing

- Append-only source_event
- Durable processed_event ledger (ACCEPTED / REJECTED + reason_code)
- Idempotent restart (no duplicates after restart or redelivery)
- Transactional batch commit (a failed batch writes NOTHING)
- PostgreSQL advisory transaction lock
- Explicit rejection reasons (deterministic reason codes)
- Periodic source integrity and coverage audit (below)

### Source integrity audit (review P1)

Runs inside the regular runtime contour, every `--audit-interval`
seconds (default 300), read-only.

Statuses (kept separate):

| Status | Meaning |
|---|---|
| `SOURCE_COVERAGE_VALIDATED` | reconciliation with the AVAILABLE V3 ledger passed (scope `V3_LEDGER_COVERAGE_VALIDATED` only) |
| `COVERAGE_LAG` | unprocessed eligible events exist but are younger than the grace window — eventual consistency, transient, non-blocking |
| `COVERAGE_GAP` | unprocessed eligible events persist beyond `--coverage-gap-grace` seconds (default 900) |
| `INTEGRITY_CONFLICT` | a source/ledger integrity violation exists |
| `SOURCE_COVERAGE_UNKNOWN` | coverage not established (no frozen activation) — and always: independent upstream capture completeness unproven |

Audit checks (all read-only, no repair/backfill/modify):

- unprocessed eligible source events (lag vs persistent gap);
- original V3 lineage vs research lineage mismatches
  (lineage / symbol / signal_time / direction / scanner drift);
- `ACCEPTED` ledger rows without a corresponding `source_event`;
- `REJECTED` rows wrongly present in the eligible cohort;
- research `source_event` rows without an original V3 event;
- `experiment_id` mismatches in the processing ledger;
- conflicting reprocessing payloads (duplicate delivery with a
  different lineage → `EVENT_KEY_LINEAGE_CONFLICT`).

Escalation rules (explicit and deterministic):

| Condition | Action |
|---|---|
| audit `INTEGRITY_CONFLICT` | BLOCK contour immediately |
| audit `COVERAGE_GAP` | BLOCK contour immediately |
| audit `COVERAGE_LAG` | log + continue capture |
| audit `SOURCE_COVERAGE_UNKNOWN` | log + continue capture, no validation claim |
| any invalid source event in a batch | record rejections, admit nothing, BLOCK contour |
| `SourceIntegrityError` on save | BLOCK contour (deterministic conflict code) |

Results are logged and persisted to `source_health` (diagnostics JSON),
including the per-category conflict counters and the verdict gate.

### Fail-closed semantics (review P1)

- An invalid source event is stored in the rejection ledger
  (`processed_event` = `REJECTED`) with its concrete `reason_code`;
  it NEVER enters the eligible OOS cohort.
- A batch containing invalid events admits NOTHING (fail-closed);
  its valid siblings remain pending for post-recovery processing.
- The research contour transitions to `BLOCKED` (append-only
  `observer_state_event` ledger). Strictly limited to this observer:
  no activation change, no V3 source change, no backfill, no freeze
  reset. Activation writes belong to a separate administrative
  contour; the observer DB role has SELECT-only on activation and
  no UPDATE/DELETE anywhere.
- `SOURCE_VALIDATED` and a final OOS verdict are NEVER issued while
  source integrity is violated or upstream coverage is unproven
  (deterministic `verdict_gate()` in `integrity.py`).
- Rejection diagnostics are committed atomically WITH the block and
  survive in `source_health` and `processed_event`.
- systemd never loops on deterministic violations: the runner exits
  with code 5 and the unit has `RestartPreventExitStatus=5`
  (`StartLimitIntervalSec=600`, `StartLimitBurst=5` bound transient
  crash loops).

Runner exit codes: 0 OK · 1 transient cycle failure · 2 config/DB
connection · 3 not activated / not running / missing freeze_ts ·
4 another instance holds the lock (--once) · 5 BLOCKED (fail-closed).

### Manual recovery (administrative contour)

`deploy/sql/296_observer_recovery.sql` — executed ONLY by a PostgreSQL
administrator (the observer cannot resume itself). It appends a
`RESUMED` state row after diagnostics are investigated and the cause
remediated. It never changes `freeze_ts`, never backfills, never
touches `source_event` / `processed_event` / V3 data. After the
resume the unit is started explicitly
(`systemctl start trad-bot-me-entry-quality-observer.service`).

### Isolation

No modification to:
- scanner
- V3 runtime
- trading gates
- paper/live execution
- existing OOS

No restart or reload of existing services.

### Evaluation boundary

Source capture does not imply valid outcome evaluation.

H1, H2, H3 and H4 require separately frozen:
- feature definitions
- causal ASOF / closed candle rules
- control and paired comparison
- TP/SL/timeout
- fees and slippage
- minimum N and uncertainty policy

Incomplete source or candle lineage is excluded
from final OOS verdicts.

### Test coverage

Unit tests (`tests/test_me_entry_quality_source_observer_v1.py`,
`tests/test_me_entry_quality_observer_runtime_v1.py`,
`tests/test_me_entry_quality_integrity_audit_v1.py`) and opt-in
PostgreSQL integration tests
(`tests/test_me_entry_quality_pg_integration_v1.py`, isolated scratch
database, `ME_EQ296_PG_INTEGRATION=1`) cover the review scenario
table: valid event, null/NaN/Infinity/string/boolean RSI values,
RSI range, confirmation flag, lineage drift, ACCEPTED without source,
missing source event, coverage lag vs persistent gap, restart
idempotency, batch atomicity, fail-closed with diagnostics, upstream
coverage unknown (no SOURCE_VALIDATED), BLOCKED capture stop, and
manual recovery without freeze reset or backfill.

### Current decision

V3_LEDGER_COVERAGE_VALIDATED is achievable and audited.
UPSTREAM_SOURCE_COVERAGE_UNPROVEN — explicitly BLOCKER-listed.

DEPLOY_HOLD until:
1. isolated deployment acceptance, AND
2. an approved independent upstream capture verification (options
   above) — a full SOURCE_VALIDATED is unobtainable without it.

Status: DEPLOY_HOLD. No prospective accumulation before a separate GO.
