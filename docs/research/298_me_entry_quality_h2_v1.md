# #298 — H2_RSI_ONLY: delayed-entry RSI confirmation (implementation v1)

**Parent:** #295 · **Research issue:** #298 · **Draft PR:** #311 ·
**Package:** `app/research/me_entry_quality_h2_v1/` · **Status:** RESEARCH_ONLY,
`IMPLEMENTATION_PASS` — **DEPLOY HOLD** (no prospective accumulation).

This document records the PRECISE rules implemented for H2, including the
peak-rule definition and confirmation lag that must be reviewed **before**
any prospective freeze.  It complements the handoff spec
`298_h2_rsi_only_v1_implementation_plan.md` (unchanged).

## 1. Hypothesis

Does ME Inverse SHORT V3 improve if the SHORT is opened not immediately
after the original `RSI >= 65` detector signal, but only after a CONFIRMED
weakening of RSI observed on the subsequently **closed** 5m candles?

Because the source detector (`MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`) requires
`rsi_delta_3 > 0` at detection, H2 can never be a same-event filter; it
tracks the **post-signal** RSI path only.

## 2. Package (pure, isolated)

```text
app/research/me_entry_quality_h2_v1/
    __init__.py          package contract + version
    candle_provider.py   caller-supplied 5m OHLCV: provenance + causal validation
    rsi_confirmation.py  Wilder RSI(14) + causal confirmation rules
    result.py            H2Policy / SourceFeatureSnapshot / H2VariantResult
    pipeline.py          per-(event, variant) delayed-entry state machine
```

* No database, network, filesystem, clock, systemd or trading service is
  referenced anywhere in the package (AST/import/token purity tests);
* **identity logic is REUSED unchanged** from H1-C
  (`me_entry_quality_h1c_v1.event_adapter.adapt_source_events`) — no
  duplicated identity derivation, no new `event_adapter.py`;
* candle provenance is REUSED unchanged from H1-C
  (`HtfSourceIdentity` / `build_source_identity` / `source_payload_hash`);
* nothing in production imports or references H2 (isolation tests).

## 3. Source of truth

`research_me_upstream_coverage.source_event` (#305).  Preserved VERBATIM and
never recomputed: `source_event_key`, `source_identity`, `symbol`,
`signal_time`, `activation_freeze_ts`, `lineage_hash`,
`features.rsi_14`, `features.rsi_delta_3`,
`features.entry_confirmation_rsi_rising`.  The original `rsi_14` snapshot is
volatile first-capture data (possibly from a forming candle) and is kept in a
SEPARATE result field from H2's recomputed closed-candle RSI.

* **Original ASOF = `signal_time`** — `captured_at` is delivery time and is
  never used as a signal-time ASOF (tested: delivery-time shifts leave
  results bit-identical);
* microsecond precision of `signal_time` is preserved losslessly; the ms
  views use the H1-C policy `US_PRESERVED__MS_FLOOR_TOWARD_PAST`.

## 4. 5m candle provider (caller-supplied rows only)

The package never fetches candles.  The caller passes raw OHLCV rows plus a
`CandleSourceIdentity` (source kind, label, fetch time, row count, SHA-256
payload hash).  Validation is fail-closed with stable reason codes:

* UTC 5m grid (`open_ms % 300_000 == 0`), candle timestamp = OPEN time;
* a candle is CLOSED at `evaluation_asof_ms` iff
  `open_ms + 300_000 <= evaluation_asof_ms` (inclusive at the boundary);
* strictly ascending, no duplicates (`DUPLICATE_CANDLE` /
  `CONFLICTING_CANDLE`), no gaps (`CANDLE_GAP`), no off-grid timestamps
  (`UNALIGNED_CANDLE`), valid OHLCV (finite, positive prices,
  `low <= min(open, close) <= max(open, close) <= high`, volume >= 0);
* the unclosed/future suffix is excluded BEFORE OHLCV validation (ASOF
  isolation — corrupt future rows can never affect history) and a closed row
  hidden after the future suffix is rejected (`OUT_OF_ORDER_CANDLE`);
* `row_count` and `payload_hash` are verified unconditionally
  (`SOURCE_PAYLOAD_MISMATCH` on any tamper);
* RSI warmup completeness is enforced (Wilder RSI(14) needs 15 closes;
  `O_0` must itself have a defined RSI).

Reason-code → H2 state: `CANDLE_GAP` / `EMPTY_SERIES` /
`INSUFFICIENT_HISTORY` / `MISSING_LATEST_CLOSED_CANDLE` /
`NO_CLOSED_CANDLES` → `INCOMPLETE_COVERAGE`; everything else →
`INVALID_SOURCE`.  A missing data path is never repaired, gap-filled or
downgraded.

## 5. Wilder RSI(14) parity

`rsi_confirmation.rsi_wilder_last` reproduces
`app.indicators.technical.rsi_wilder` bit-for-bit (initial SMA seed, Wilder
recursive smoothing, `avg_loss == 0` handling); `rsi_wilder_series` computes
every prefix with the identical operation order.  Production code is not
imported into the package; parity is enforced by
`tests/test_me_entry_quality_h2_rsi_parity_v1.py` (exact `==` against the
reference on deterministic sequences, every prefix).  Any mismatch must fail
closed.

## 6. Variants (PRELIMINARY CANDIDATES — NOT a frozen policy)

Observation candles: `O_1, O_2, ...` are the 5m candles closing **strictly
after** the exact `signal_time` instant (microsecond comparison — a close at
the same instant belongs to the BASE, tested at ±1µs); `O_0` is the
immediately preceding closed candle (level reference only — pre-signal
declines can never count as confirmations).  `RSI(.)` below is H2's
recomputed closed-candle Wilder RSI(14).

* **`H2_RSI_TURN_1`** — confirms at the first `k >= 1` with
  `RSI(O_k) < RSI(O_{k-1})` (first strictly decreasing closed post-signal
  RSI; no intra-candle signals).
* **`H2_RSI_TURN_2`** — confirms at the first `k >= 2` with
  `RSI(O_k) < RSI(O_{k-1})` AND `RSI(O_{k-1}) < RSI(O_{k-2})` — two
  consecutive strictly decreasing closed observations, BOTH observed after
  the original signal.
* **`H2_RSI_PEAK_REVERSAL`** — rule `PEAK_RULE_V1` (prespecified,
  parameter-free, causal): `O_{k-1}` is a STRICT local peak
  (`RSI(O_{k-1}) > RSI(O_{k-2})` and `RSI(O_{k-1}) > RSI(O_k)`) and `O_k`
  strictly declines.  Recognition lag is EXACTLY one candle (the right
  neighbour must be closed); there is no future-centered pivot and no
  repainting — the returned `k` never changes when later data arrives.
  With `peak_rule = None` the variant evaluates to **`NOT_READY`** (an
  undefined peak rule is never approximated or tuned).

Observation windows **3 and 6** post-signal closed candles are PRELIMINARY
CANDIDATES, not approved defaults.  Every `H2Policy` carries
`policy_status = PRELIMINARY_CANDIDATE_NOT_FROZEN` and a deterministic
SHA-256 parameter fingerprint (variant, window, `rsi_period`, peak rule, RSI
calc version, policy status).  Nothing may be selected post-hoc against
outcomes.

## 7. Delayed-entry state machine (per event × variant)

```text
PENDING_CONFIRMATION -> CONFIRMED_DELAYED  (rule fires at O_k, k <= window)
                     \-> EXPIRED           (window fully closed, no confirmation)
INVALID_SOURCE / INCOMPLETE_COVERAGE / NOT_READY   (terminal, fail closed)
```

Result contract (every field explicit):

* source event key, variant id, symbol;
* original signal time (`_us` exact + `_ms` floor) and original price —
  labeled `SOURCE_REFERENCE_PRICE_VERBATIM_NOT_EXECUTABLE`;
* confirmation candle open, confirmation close time (entry candidate time),
  recomputed RSI at confirmation (separate from the verbatim `rsi_14`);
* entry delay (`_us` exact + `_ms` floor), measured from the exact
  `signal_time` instant;
* **entry price is explicitly UNDETERMINED** (`UNDETERMINED_NO_EXECUTION_
  MODEL`): no execution is assumed at the confirmation-candle close.  The
  close is carried only as
  `CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE`;
* expiry reason (`OBSERVATION_WINDOW_EXHAUSTED_NO_CONFIRMATION`), observed
  candle count, data coverage status + detail;
* preserved lineage payload (key, identity, `signal_time` us/ms,
  `activation_freeze_ts`, `lineage_hash`), calculation version
  (`H2_1.0.0` / `WILDER_RSI__TECHNICAL_PARITY_V1`) and policy fingerprint.

## 8. Causal guarantees (tested)

* closed-candle boundary `T-1ms` / `T` / `T+1ms` and ±1µs signal boundaries;
* original signal fired on a still-forming candle (its close becomes `O_1`,
  its in-flight state is never used);
* appending future candles — even corrupt ones — never changes a result
  computed at an earlier ASOF;
* a decline visible only after the ASOF can never confirm at that ASOF;
* redelivery of one `source_event_key` is rejected explicitly
  (`DUPLICATE_SOURCE_EVENT`) and recomputation is bit-identical (idempotent);
* results are frozen dataclasses; reruns are deterministic.

## 9. Test inventory (offline; no VPS / PostgreSQL / Bybit)

| file | scope |
| --- | --- |
| `test_me_entry_quality_h2_rsi_parity_v1.py` | Wilder RSI parity vs `technical.rsi_wilder`, every prefix, degenerate cases |
| `test_me_entry_quality_h2_candle_provider_v1.py` | grid/boundaries, gaps, duplicates, order, OHLCV, warmup, provenance/hash tamper, future suffix |
| `test_me_entry_quality_h2_variants_v1.py` | TURN_1 / TURN_2 / PEAK_REVERSAL rules + pipeline, expiry, pending, NOT_READY, INVALID_SOURCE, INCOMPLETE_COVERAGE, entry-price honesty |
| `test_me_entry_quality_h2_causal_asof_v1.py` | T-1ms/T/T+1ms, microsecond boundaries, forming-candle signal, future-append invariance, no lookahead, captured_at isolation |
| `test_me_entry_quality_h2_lineage_idempotency_v1.py` | lineage immutability, ms-floor policy, idempotency, repeatability, policy fingerprint |
| `test_me_entry_quality_h2_purity_v1.py` | AST purity, forbidden tokens, production isolation, no SQL/systemd wiring |

## 10. Explicitly OUT of scope (unchanged)

No scanner / paper / live / scoring / gate changes; no H1 or #305 changes; no
SQL migrations; no systemd or configuration changes; no freeze-timestamp
changes; no backfill; no H2 accumulation.  **H2 is NOT moved to
`PROSPECTIVE_ACCUMULATION`.**  Before any prospective deployment: source
coverage proof (#305), an approved bounded candle source, the outcome policy
(parameter freeze), and an independent research-deployment authorization are
all still required.
