# Issue #297 — ME Inverse SHORT V3 / H1 HTF Trend Alignment (research-only)

**Package:** `app/research/me_entry_quality_h1_v1/`
**Hypothesis H1** (parent #295 `ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1`): part of
the losing `ME_LONG_V2_INVERSE_SHORT_V3` SHORT signals arise while the
higher-timeframe (4H / 1D) structure is still bullish. Classifying the HTF
regime CAUSALLY at the original signal time allows an OOS comparison of SHORT
outcomes under `BULLISH` / `BEARISH` / `NEUTRAL` HTF structure.

**This is research.** It does not change any trading decision and must never
block a signal. H1-C / H1-D (event adapter, historical HTF source, prospective
accumulation) are explicitly out of scope for this iteration.

## 1. Scope and isolation guarantees

* Pure functions only: no I/O, no network, no database, no clock reads
  (enforced by `tests/test_me_entry_quality_h1_classifier_v1.py::
  test_package_sources_are_pure_no_clock_no_network_no_db_no_io`).
* Not wired into any scanner, orchestrator, trading gate, systemd unit, SQL
  migration or registry (enforced by
  `test_package_is_not_wired_into_any_production_surface`).
* No production file is modified by the #297 change set.
* Missing data is NEVER converted into a market mode: every data problem
  fails closed as `INVALID_SOURCE` with an explicit reason code — `NEUTRAL`
  is reserved for a valid source with genuinely mixed structure.

## 2. Modules

| Module | Task | Contents |
|---|---|---|
| `htf_candles.py` | H1-A.1 | `validate_htf_candles`, UTC 4H/1D grid, causal ASOF truncation, explicit reason codes |
| `structure.py` | H1-A.2 | `detect_swings`, `usable_swings`, `pair_relation`, `detect_bos_events` |
| `classifier.py` | H1-A.3/A.4 | `classify_timeframe`, `classify_combined`, `TimeframeState`, 9-combination contract |

## 3. Timeframes and UTC alignment

* `4H` — `TIMEFRAME_4H_MS = 14_400_000`; on-grid iff `open_ms % 14_400_000 == 0`
  (00/04/08/12/16/20 UTC).
* `1D` — `TIMEFRAME_1D_MS = 86_400_000`; on-grid iff `open_ms % 86_400_000 == 0`,
  i.e. the daily open is **exactly 00:00:00 UTC**. The grid is derived from
  epoch-millisecond integer arithmetic only — there is no local calendar-day
  boundary and no DST handling anywhere in the package (verified by
  `test_1d_candles_align_to_utc_midnight_not_local_day` and
  `test_result_does_not_depend_on_system_timezone`).
* Candle timestamp = candle **open** time, UTC epoch milliseconds.

## 4. H1-A.1 candle validation (`validate_htf_candles`)

Availability (causal, inclusive at the boundary): a candle is usable at
`evaluation_asof_ms` iff `open_ms + timeframe_ms <= evaluation_asof_ms` —
available exactly at its close, never before. `evaluation_asof_ms` is the
moment of the ORIGINAL event; the later computation time is never consulted.

Rejection / truncation policy (all explicit, all fail-closed):

* unclosed / future candles are never used — they are truncated from the
  usable series and reported in `dropped_unclosed`; their payload (including
  malformed OHLCV or off-grid opens) cannot invalidate a past ASOF; integrity
  checks apply to the closed prefix available at that ASOF;
* unsorted input → `OUT_OF_ORDER_CANDLE` (never silently reordered);
* duplicate opens → `DUPLICATE_CANDLE` (identical) / `CONFLICTING_CANDLE`;
* missing bucket between the first and last closed candle → `CANDLE_GAP`
  (never gap-filled — automatic filling is forbidden);
* off-grid open → `UNALIGNED_CANDLE`; non-integer/negative timestamp →
  `INVALID_TIMESTAMP`;
* non-finite / non-positive prices, inconsistent OHLC → `INVALID_OHLC`;
  non-finite / negative volume → `INVALID_VOLUME`;
* source tail missing the newest closed bucket at the ASOF →
  `MISSING_LATEST_CLOSED_CANDLE` (incomplete source, overridable only via the
  explicit `require_latest_closed=False` diagnostic parameter);
* fewer than `min_closed_candles` (default `MIN_CLOSED_CANDLES = 7`) →
  `INSUFFICIENT_HISTORY`.

## 5. H1-A.2 swing structure (`structure.py`)

`swing_lookback = k` (research default **3**). A **Swing High** at
representative index `i`: `high[i] >= high[i +/- j]` for all `j = 1..k`, with
at least one strict `high[i - j] < high[i]` on the left and one strict
`high[i + j] < high[i]` on the right (Swing Low symmetric on `low`). The
strictness clauses forbid swings inside flat regions.

Timestamps:

* `formed_at_ms` = open time of the representative extremum candle;
* `confirmed_at_ms` = **close time of the k-th right candle** =
  `timestamp[i + k] + timeframe_ms`.

A swing is unusable before `confirmed_at_ms`
(`usable_swings`: `confirmed_at_ms <= evaluation_asof_ms`).

Equal-extremum policy (deterministic tie-breaking, defined before freeze):

1. a maximal run of consecutive candles with exactly equal highs (lows) is
   ONE formation: only the **first qualifying** index of the run is kept as
   the representative; later qualifying indices of the same run are
   suppressed (no repeated confirmation of one level);
2. independent swings separated by strictly lower/higher prints are separate
   formations even at exactly equal prices (P2-11 parity) — never suppressed
   for sharing a price;
3. `pair_relation` equality: `EQUAL` iff
   `abs(a - b) <= 1e-9 * max(abs(a), abs(b))`. The tolerance absorbs
   float-representation noise only and is far below one market tick; it is
   NOT a market "same level" tolerance. `EQUAL` never counts as HH/HL/LH/LL.

Structure relations (`HH`, `LH`, `EQH` / `HL`, `LL`, `EQL`) are taken over
the last two confirmed swings of each kind in `(confirmed_at_ms,
formed_at_ms)` order.

## 6. H1-A.3 regime and BOS (`classifier.py`)

Per timeframe, independently:

| Regime | Condition |
|---|---|
| `BULLISH` | last two confirmed Swing Highs form **HH** and last two confirmed Swing Lows form **HL** |
| `BEARISH` | last two confirmed Swing Highs form **LH** and last two confirmed Swing Lows form **LL** |
| `NEUTRAL` | valid, sufficient source but mixed/undefined structure (incl. `EQH`/`EQL`) |
| `INVALID_SOURCE` | insufficient data or data-integrity violation (`INSUFFICIENT_HISTORY`, `INSUFFICIENT_CONFIRMED_STRUCTURE`, or any candle-source reason code) |

`INSUFFICIENT_CONFIRMED_STRUCTURE` (fewer than 2 confirmed Swing Highs or 2
confirmed Swing Lows at the ASOF) is an explicit `INVALID_SOURCE` — a trend is
never invented.

BOS is a **separate diagnostic** and never overrides the regime in v1:

* `BULLISH_BOS` — a candle **closes** strictly above a previously confirmed
  Swing High level; `BEARISH_BOS` — a candle closes strictly below a
  previously confirmed Swing Low level. Wick-only excursions are not breaks
  (close-confirmation protocol);
* causality: `level_confirmed_at_ms < break_confirmed_at_ms <=
  evaluation_asof_ms`;
* each confirmed level breaks at most once (first close-beyond candle); one
  candle breaks at most one level per direction — the nearest one (highest
  Swing High below the close / lowest Swing Low above the close);
* every event stores `level_confirmed_at_ms` and `break_confirmed_at_ms`;
* the diagnostic label is the most recent event; the pathological same-close
  double break is resolved deterministically (more recently confirmed level
  wins, then a fixed `BULLISH_BOS` rank).

## 7. H1-A.4 combined 4H/1D contract

`classify_combined(candles_4h, candles_1d, evaluation_asof_ms=...)` returns:

```python
{
    "h1_version": "1.0.0",
    "evaluation_asof_ms": 0,
    "htf_4h": {"regime": "...", "bos": "...", "coverage_status": "...",
               "reason": "...", "detail": "..."},
    "htf_1d": {...},
    "combined_regime": "BULLISH/BEARISH",
    "combined_eligible": True,
    "swing_lookback": 3,
    "canonical_lookback": 60,
}
```

* all nine `<4H>/<1D>` combinations are preserved verbatim
  (`VALID_COMBINED_REGIMES`) — conflicting HTF modes are never merged into
  one averaged feature;
* if either timeframe is `INVALID_SOURCE`, the other keeps its own valid
  state, but `combined_eligible` is `False`;
* `coverage_status` is `VALID` only when the candle source passed H1-A.1
  validation; an incomplete source can never report `VALID`.

## 8. Causal / anti-repaint invariants

* `confirmed_at_ms <= evaluation_asof_ms` for every swing used;
* `break_confirmed_at_ms <= evaluation_asof_ms` and
  `level_confirmed_at_ms < break_confirmed_at_ms` for every BOS used;
* analysis runs over a FIXED canonical window — the most recent
  `canonical_lookback` (60) closed candles — so older history can never
  change a fixed-ASOF result;
* appending future candles never changes an earlier ASOF result;
* results are deterministic across reruns (pure functions, no clock reads).

## 9. Frozen-candidate parameters (NOT approved for OOS)

| Parameter | Value | Status |
|---|---|---|
| `swing_lookback` | 3 (parameterizable 2/3/5 for offline sensitivity) | research default, **not frozen** |
| `canonical_lookback` | 60 closed candles | research default, **not frozen** |
| `EQUAL_REL_TOL` | 1e-9 (relative) | float-noise policy, **not frozen** |
| `MIN_CLOSED_CANDLES` | 7 = 2*3+1 | derived from lookback, **not frozen** |

Sensitivity runs at lookback 2/3/5 exist on synthetic data
(`test_swing_lookback_sensitivity_is_explicit_and_deterministic`); **no
winning parameter is selected** and no prospective holdout is used. Offline
real-data sensitivity belongs to the later H1-C/H1-D analysis phase.

## 10. Test matrix

90 deterministic unit tests (`tests/test_me_entry_quality_h1_candles_v1.py`,
`..._structure_v1.py`, `..._classifier_v1.py`) cover H1-B scenarios 1-20 of
Issue #297 — no PostgreSQL, no Bybit, no network.

## 11. Known limitations

* Unit-test success is **not** evidence of real predictive value for H1;
* no historical HTF data source and no event adapter exist yet (H1-C/H1-D);
* `source_event` for #305 was empty at audit time (`source_event = 0`,
  `capture_health = 0`) — this development does **not** confirm #305 source
  coverage and does not treat that source as `SOURCE_VALIDATED`;
* BOS has no expiry policy (label = most recent break in the canonical
  window) — deliberate v1 simplification for later research;
* sensitivity on real offline data is deferred to the analysis phase.
