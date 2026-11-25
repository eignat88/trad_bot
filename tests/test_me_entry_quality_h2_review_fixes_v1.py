"""Issue #298 — regression tests for code-review findings (PR #311 review
6055546804):

* [P1] PostgreSQL ``NUMERIC`` (``decimal.Decimal``) original prices are
  accepted exactly — no false ``INVALID_SOURCE`` on real DB rows;
* [P1] serialized outcomes/results keep full candle-source provenance
  (identity, payload hash, row count, coverage, canonical window, provider
  reason);
* [P1] the canonical coverage gate: a partial 120-bucket canonical window
  can never yield an eligible result or a false ``COMPLETE`` status;
* [P2] the #305 activation/freeze temporal contract is enforced at the H2
  boundary for caller-supplied rows (``signal_time > activation_freeze_ts``
  STRICTLY at microsecond precision; ``captured_at`` must not precede the
  freeze) — no pre-freeze event can ever confirm.

Offline: no DB, no network, no filesystem, no clock.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_OFFLINE_SNAPSHOT,
)
from app.research.me_entry_quality_h2_v1.candle_provider import (
    CANDLE_5M_MS,
    build_source_identity,
    provide_5m_candles,
)
from app.research.me_entry_quality_h2_v1.pipeline import (
    REASON_CANONICAL_WINDOW_INCOMPLETE,
    REASON_INSUFFICIENT_RSI_WARMUP,
    REASON_INVALID_SOURCE,
    REASON_INVALID_SOURCE_REFERENCE_PRICE,
    REASON_PRE_FREEZE_CAPTURED_AT,
    REASON_PRE_FREEZE_SIGNAL_TIME,
    _extract_original_price,
    run_h2_event,
)
from app.research.me_entry_quality_h2_v1.result import (
    ENTRY_PRICE_UNDETERMINED,
    H2FeatureError,
    H2Policy,
    ORIGINAL_PRICE_BASIS,
    ORIGINAL_PRICE_PRECISION_POLICY,
    STATE_CONFIRMED_DELAYED,
    STATE_INCOMPLETE_COVERAGE,
    STATE_INVALID_SOURCE,
    VARIANT_TURN_1,
)

BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MS = CANDLE_5M_MS
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

CRAFTED = (
    [100.0 + i for i in range(17)]
    + [118.0, 114.0, 116.5, 113.0, 111.5, 115.0]
)
WARMUP = 106
SIGNAL_US = (BASE_MS + 18 * MS + 60_000) * 1000  # mid-candle idx 18
ASOF_MS = BASE_MS + 19 * MS


def warmup_closes(n):
    return [99.0 - 0.5 * (n - 1 - i) for i in range(n)]


def rows_from_closes(closes, start_index=0, warmup=WARMUP):
    placed = [
        (start_index - warmup + j, c) for j, c in enumerate(warmup_closes(warmup))
    ] + [(start_index + i, c) for i, c in enumerate(closes)]
    rows = []
    for bucket, close in placed:
        open_ms = BASE_MS + bucket * MS
        prev = rows[-1]["close"] if rows else float(close)
        rows.append({
            "open_ms": open_ms,
            "open": prev,
            "high": max(prev, float(close)) + 0.5,
            "low": min(prev, float(close)) - 0.5,
            "close": float(close),
            "volume": 1.0,
        })
    return rows


def event_row(
    signal_us=SIGNAL_US,
    *,
    key="evt-review",
    price=Decimal("123.456789"),
    freeze_us=None,
    captured_us=None,
    signal_value=None,
    captured_value=None,
    features=None,
):
    signal = EPOCH + timedelta(microseconds=signal_us)
    if freeze_us is None:
        freeze_us = signal_us - 3_600_000_000  # 1h before the signal
    if captured_us is None:
        captured_us = signal_us + 20_000_000
    freeze = EPOCH + timedelta(microseconds=freeze_us)
    captured = EPOCH + timedelta(microseconds=captured_us)
    return {
        "source_event_key": key,
        "source_identity": (
            "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|BTCUSDT|LONG|5m|"
            f"{signal_us // 1000 // MS * MS}"
        ),
        "symbol": "BTCUSDT",
        "lineage_hash": "12" * 32,
        "signal_time": signal if signal_value is None else signal_value,
        "captured_at": captured if captured_value is None else captured_value,
        "activation_freeze_ts": freeze,
        "source_fingerprint": "fp-review-1",
        "source_reference_price": price,
        "features": features if features is not None else {
            "rsi_14": 71.25,
            "rsi_delta_3": 4.5,
            "entry_confirmation_rsi_rising": True,
        },
    }


def run(
    closes=CRAFTED,
    *,
    row=None,
    warmup=WARMUP,
    asof_ms=ASOF_MS,
    key="evt-review",
    price=Decimal("123.456789"),
    freeze_us=None,
    captured_us=None,
):
    rows = rows_from_closes(closes, warmup=warmup)
    identity = build_source_identity(
        SOURCE_KIND_OFFLINE_SNAPSHOT, "review-fixes", rows
    )
    return run_h2_event(
        row if row is not None else event_row(
            key=key, price=price, freeze_us=freeze_us, captured_us=captured_us
        ),
        rows_5m=rows,
        source_identity=identity,
        policies=[H2Policy(VARIANT_TURN_1, observation_window=3)],
        evaluation_asof_ms=asof_ms,
    )


# ────────────────────────────────────────────────────────────────────────────
# [P1] PostgreSQL NUMERIC / Decimal original price
# ────────────────────────────────────────────────────────────────────────────
class TestNumericOriginalPrice:
    def test_decimal_price_is_accepted_exact_and_view(self):
        view, exact = _extract_original_price(
            {"source_reference_price": Decimal("123.456789")}
        )
        assert view == pytest.approx(123.456789)
        assert exact == "123.456789"  # exact source value preserved verbatim

    def test_decimal_zero_and_negative_are_rejected(self):
        for bad in (Decimal("0"), Decimal("-1.5"), Decimal("-0.000001")):
            with pytest.raises(H2FeatureError) as exc:
                _extract_original_price({"source_reference_price": bad})
            assert exc.value.reason_code == REASON_INVALID_SOURCE_REFERENCE_PRICE

    def test_decimal_non_finite_is_rejected(self):
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with pytest.raises(H2FeatureError) as exc:
                _extract_original_price({"source_reference_price": bad})
            assert exc.value.reason_code == REASON_INVALID_SOURCE_REFERENCE_PRICE

    def test_bool_string_and_none_follow_the_documented_contract(self):
        # bool / strings are never prices
        for bad in (True, False, "123.45", "", [1.0]):
            with pytest.raises(H2FeatureError):
                _extract_original_price({"source_reference_price": bad})
        # an ABSENT optional price stays absent — never fabricated
        assert _extract_original_price({"source_reference_price": None}) is None
        assert _extract_original_price({}) is None

    def test_int_and_float_views(self):
        assert _extract_original_price({"source_reference_price": 5}) == (5.0, "5")
        view, exact = _extract_original_price({"source_reference_price": 1234.5})
        assert view == 1234.5
        assert exact == "1234.5"
        for bad in (float("nan"), float("inf"), float("-inf"), 0.0, -3.0):
            with pytest.raises(H2FeatureError):
                _extract_original_price({"source_reference_price": bad})

    def test_lineage_fallback_value_is_also_exact(self):
        view, exact = _extract_original_price(
            {"lineage": {"source_reference_price": Decimal("0.1")}}
        )
        assert view == pytest.approx(0.1)
        assert exact == "0.1"

    def test_full_pipeline_with_postgres_numeric_row(self):
        # a real psycopg2 row carries decimal.Decimal for NUMERIC columns
        outcome = run(price=Decimal("123.456789"))
        (result,) = outcome.results
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.original_price == pytest.approx(123.456789)
        assert result.original_price_exact == "123.456789"
        assert result.original_price_basis == ORIGINAL_PRICE_BASIS
        # the source price NEVER becomes the delayed-entry execution price
        assert result.entry_candidate_price is None
        assert result.entry_price_basis == ENTRY_PRICE_UNDETERMINED
        payload = result.to_dict()
        assert payload["original_price_exact"] == "123.456789"
        assert payload["original_price_precision_policy"] == (
            ORIGINAL_PRICE_PRECISION_POLICY
        )

    def test_absent_optional_price_never_becomes_a_fabricated_price(self):
        outcome = run(price=None)
        (result,) = outcome.results
        assert result.original_price is None
        assert result.original_price_exact is None
        assert result.original_price_basis == ""
        assert result.entry_candidate_price is None

    def test_invalid_price_is_invalid_source(self):
        outcome = run(price=Decimal("NaN"))
        (result,) = outcome.results
        assert result.status == STATE_INVALID_SOURCE
        assert result.reason == REASON_INVALID_SOURCE_REFERENCE_PRICE


# ────────────────────────────────────────────────────────────────────────────
# [P1] candle-source provenance survives serialization
# ────────────────────────────────────────────────────────────────────────────
class TestProvenanceSerialization:
    def test_outcome_to_dict_carries_source_identity_and_range(self):
        outcome = run()
        source = outcome.to_dict()["source"]
        assert source["source_kind"] == SOURCE_KIND_OFFLINE_SNAPSHOT
        assert source["source_label"] == "review-fixes"
        assert len(source["payload_hash"]) == 64
        assert source["row_count"] == WARMUP + len(CRAFTED)
        assert source["provider_version"]
        assert source["evaluation_asof_ms"] == ASOF_MS
        assert source["timeframe"] == "5m"
        closed = source["closed_range"]
        assert closed["first_closed_open_ms"] == BASE_MS - WARMUP * MS
        # rows 19..22 are unclosed at ASOF_MS and are excluded (ASOF isolation)
        assert closed["last_closed_open_ms"] == BASE_MS + 18 * MS
        assert closed["latest_closed_open_ms"] == BASE_MS + 18 * MS
        assert closed["dropped_unclosed_count"] == 4
        assert source["coverage"]["expected_buckets"] == 120
        assert source["coverage"]["status"] == "COMPLETE"

    def test_payload_hash_and_row_count_present(self):
        source = run().to_dict()["source"]
        assert "payload_hash" in source and source["payload_hash"]
        assert source["row_count"] > 0

    def test_provenance_survives_confirmed_delayed(self):
        rows = rows_from_closes(CRAFTED)
        identity = build_source_identity(
            SOURCE_KIND_OFFLINE_SNAPSHOT, "review-fixes", rows
        )
        outcome = run()
        (result,) = outcome.results
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.candle_source == outcome.to_dict()["source"]
        # provenance matches the identity computed over the SAME rows
        assert result.candle_source["payload_hash"] == identity.payload_hash
        assert result.candle_source["row_count"] == identity.row_count
        # the canonical window actually used is recorded too
        canonical = result.candle_source["canonical_window"]
        assert canonical["status"] == "COMPLETE"
        assert canonical["expected_buckets"] == 120
        assert canonical["present_buckets"] == 120

    def test_diagnostics_survive_invalid_source(self):
        rows = rows_from_closes(CRAFTED)
        identity = build_source_identity(
            SOURCE_KIND_OFFLINE_SNAPSHOT, "review-fixes", rows
        )
        tampered = rows[:-1]  # row_count / payload mismatch
        outcome = run_h2_event(
            event_row(),
            rows_5m=tampered,
            source_identity=identity,
            policies=[H2Policy(VARIANT_TURN_1, observation_window=3)],
            evaluation_asof_ms=ASOF_MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_INVALID_SOURCE
        assert outcome.reason == REASON_INVALID_SOURCE
        source = outcome.to_dict()["source"]
        assert source["source_ok"] is False
        assert source["source_reason"] == "SOURCE_PAYLOAD_MISMATCH"
        # per-result provenance keeps the same diagnostics
        assert result.candle_source["source_reason"] == "SOURCE_PAYLOAD_MISMATCH"
        assert result.candle_source["payload_hash"] == identity.payload_hash

    def test_provenance_matches_the_candle5m_set(self):
        rows = rows_from_closes(CRAFTED)
        identity = build_source_identity(
            SOURCE_KIND_OFFLINE_SNAPSHOT, "review-fixes", rows
        )
        candle_set = provide_5m_candles(
            rows, evaluation_asof_ms=ASOF_MS, source_identity=identity
        )
        outcome = run()
        source = outcome.to_dict()["source"]
        assert source["payload_hash"] == candle_set.source_identity.payload_hash
        assert source["row_count"] == candle_set.source_identity.row_count
        assert source["fetched_at_ms"] == candle_set.source_identity.fetched_at_ms
        assert source["coverage"] == candle_set.coverage.to_dict()
        assert source["closed_range"]["closed_candle_count"] == len(
            candle_set.candles
        )

    def test_serialization_is_deterministic(self):
        a = json.dumps(run().to_dict(), sort_keys=True)
        b = json.dumps(run().to_dict(), sort_keys=True)
        assert a == b


# ────────────────────────────────────────────────────────────────────────────
# [P1] canonical coverage gate (120-bucket event-anchored window)
# ────────────────────────────────────────────────────────────────────────────
class TestCanonicalCoverageGate:
    def test_15_of_120_canonical_window_is_incomplete(self):
        # exactly 15 closed buckets (0..14) end at O_0 — Wilder RSI(14) is
        # MATHEMATICALLY defined, but the canonical cohort window is not
        # covered -> the event is not eligible
        short_closes = [100.0 + i for i in range(20)]
        sig = BASE_MS + 15 * MS + 60_000  # O_0 = idx 14
        outcome = run(
            short_closes, warmup=0, asof_ms=BASE_MS + 20 * MS,
            row=event_row(sig * 1000),
        )
        (result,) = outcome.results
        assert result.status == STATE_INCOMPLETE_COVERAGE
        assert result.reason == REASON_CANONICAL_WINDOW_INCOMPLETE
        assert not outcome.ok
        canonical = result.candle_source["canonical_window"]
        assert canonical["present_buckets"] == 15
        assert canonical["expected_buckets"] == 120
        assert len(canonical["missing_open_ms"]) == 105
        assert "15/120" in result.detail
        assert result.coverage_detail == REASON_CANONICAL_WINDOW_INCOMPLETE

    def test_math_warmup_and_canonical_window_are_separate(self):
        # base < rsi_period: the Wilder seed itself is undefined -> the
        # MATHEMATICAL warmup reason fires (still INCOMPLETE_COVERAGE)
        short_closes = [100.0 + i for i in range(20)]
        sig = BASE_MS + 1 * MS + 60_000  # O_0 = idx 0
        outcome = run(
            short_closes, warmup=0, asof_ms=BASE_MS + 20 * MS,
            row=event_row(sig * 1000),
        )
        (result,) = outcome.results
        assert result.status == STATE_INCOMPLETE_COVERAGE
        assert result.reason == REASON_INSUFFICIENT_RSI_WARMUP

    def test_119_of_120_canonical_window_is_incomplete(self):
        # one canonical bucket short of the full window (bucket -102 missing)
        outcome = run(warmup=101)
        (result,) = outcome.results
        assert result.status == STATE_INCOMPLETE_COVERAGE
        assert result.reason == REASON_CANONICAL_WINDOW_INCOMPLETE
        canonical = result.candle_source["canonical_window"]
        assert canonical["present_buckets"] == 119
        assert canonical["expected_buckets"] == 120
        assert canonical["status"] == "PARTIAL"
        assert len(canonical["missing_open_ms"]) == 1

    def test_full_120_window_proceeds_to_rsi_evaluation(self):
        outcome = run(warmup=102)  # exactly the 120-bucket window
        (result,) = outcome.results
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.coverage_status == "COMPLETE"

    def test_internal_gap_in_window_is_rejected(self):
        rows = rows_from_closes(CRAFTED)
        del rows[WARMUP + 17]  # remove the O_0 bucket itself
        identity = build_source_identity(
            SOURCE_KIND_OFFLINE_SNAPSHOT, "review-fixes", rows
        )
        outcome = run_h2_event(
            event_row(),
            rows_5m=rows,
            source_identity=identity,
            policies=[H2Policy(VARIANT_TURN_1, observation_window=3)],
            evaluation_asof_ms=ASOF_MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_INCOMPLETE_COVERAGE
        assert result.reason == "CANDLE_GAP"

    def test_missing_latest_closed_candle_is_rejected(self):
        # ASOF after the provided range: the newest closed bucket is absent
        outcome = run(asof_ms=BASE_MS + 24 * MS)
        (result,) = outcome.results
        assert result.status == STATE_INCOMPLETE_COVERAGE
        assert result.reason == "MISSING_LATEST_CLOSED_CANDLE"

    def test_history_prefix_length_is_deterministic(self):
        # same event, different amounts of EXTRA earlier history loaded:
        # identical semantic results (the RSI is seeded at the fixed window)
        payloads = []
        for warmup in (102, 106, 130):
            (result,) = run(warmup=warmup).results
            assert result.status == STATE_CONFIRMED_DELAYED
            payload = result.to_dict()
            payload.pop("candle_source")  # provenance names the supplied payload
            payloads.append(payload)
        assert payloads[0] == payloads[1] == payloads[2]
        assert payloads[0]["h2_rsi_at_confirmation"] == pytest.approx(
            75.3334, abs=1e-3
        )

    def test_confirmed_never_carries_a_false_complete(self):
        # PARTIAL canonical coverage can never produce CONFIRMED_DELAYED
        partial = run(warmup=101).results[0]
        assert partial.status != STATE_CONFIRMED_DELAYED
        assert partial.coverage_status != "COMPLETE"
        # and a CONFIRMED result always documents the full window
        confirmed = run(warmup=102).results[0]
        assert confirmed.status == STATE_CONFIRMED_DELAYED
        assert confirmed.coverage_status == "COMPLETE"
        canonical = confirmed.candle_source["canonical_window"]
        assert canonical["status"] == "COMPLETE"
        assert canonical["present_buckets"] == canonical["expected_buckets"]


# ────────────────────────────────────────────────────────────────────────────
# [P2] #305 activation / freeze temporal boundary at the H2 boundary
# ────────────────────────────────────────────────────────────────────────────
class TestActivationFreezeBoundary:
    def test_signal_before_freeze_is_rejected(self):
        outcome = run(freeze_us=SIGNAL_US + 1)  # freeze AFTER the signal
        (result,) = outcome.results
        assert result.status == STATE_INVALID_SOURCE
        assert result.reason == REASON_PRE_FREEZE_SIGNAL_TIME
        assert not outcome.ok

    def test_signal_exactly_at_freeze_is_rejected(self):
        # the #305 contract is STRICT: signal_time > activation_freeze_ts —
        # an event exactly at the freeze is not retroactively admissible
        outcome = run(freeze_us=SIGNAL_US)
        (result,) = outcome.results
        assert result.status == STATE_INVALID_SOURCE
        assert result.reason == REASON_PRE_FREEZE_SIGNAL_TIME

    def test_signal_one_microsecond_after_freeze_is_admitted(self):
        outcome = run(freeze_us=SIGNAL_US - 1)
        (result,) = outcome.results
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.signal_time_us == SIGNAL_US

    def test_capture_before_freeze_is_rejected(self):
        freeze_us = SIGNAL_US - 3_600_000_000
        outcome = run(captured_us=freeze_us - 1)
        (result,) = outcome.results
        assert result.status == STATE_INVALID_SOURCE
        assert result.reason == REASON_PRE_FREEZE_CAPTURED_AT

    def test_capture_exactly_at_freeze_is_admitted(self):
        freeze_us = SIGNAL_US - 3_600_000_000
        outcome = run(captured_us=freeze_us)
        (result,) = outcome.results
        assert result.status == STATE_CONFIRMED_DELAYED

    def test_invalid_timestamp_is_rejected(self):
        outcome = run(row=event_row(signal_value="not-a-timestamp"))
        assert not outcome.ok
        assert outcome.reason == "SOURCE_EVENT_REJECTED"
        assert outcome.adapter_rejections  # explicit adapter rejection
        assert outcome.results[0].status == STATE_INVALID_SOURCE

        outcome = run(row=event_row(captured_value="garbage"))
        assert not outcome.ok
        assert outcome.reason == "SOURCE_EVENT_REJECTED"

    def test_no_retroactive_admission_of_pre_freeze_events(self):
        # a pre-freeze event whose candle path WOULD confirm must still never
        # receive a valid H2 confirmation
        outcome = run(freeze_us=SIGNAL_US + 1, asof_ms=BASE_MS + 22 * MS)
        assert not outcome.ok
        for result in outcome.results:
            assert result.status == STATE_INVALID_SOURCE
            assert result.status != STATE_CONFIRMED_DELAYED
            assert result.reason == REASON_PRE_FREEZE_SIGNAL_TIME

    def test_boundary_comparisons_use_microsecond_precision(self):
        # signal == freeze at the MILLISECOND view but +1µs apart at the
        # exact instant: admitted (the us value is what counts)
        outcome = run(freeze_us=SIGNAL_US - 1, captured_us=SIGNAL_US - 1)
        (result,) = outcome.results
        assert result.status == STATE_CONFIRMED_DELAYED
        # and the same millisecond with the signal 1µs AT the freeze: rejected
        outcome = run(freeze_us=SIGNAL_US)
        assert outcome.results[0].reason == REASON_PRE_FREEZE_SIGNAL_TIME
