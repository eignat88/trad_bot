"""Issue #314 / H1-D — real event classification tests
(``H1D_REAL_EVENT_CLASSIFICATION_V1``).

Canonical first event: the #305 SOXSUSDT event
(``source_event_key=d2ce270406e63cde2e7546d74344dc7bd5d0f5109893631a2387a5e2cc6a5829``,
``source_identity=MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|SOXSUSDT|LONG|5m|1795612200000``,
``signal_time=2026-11-25T13:11:00.804844+00:00``).

**The HTF payload rows in these tests are SYNTHETIC_TEST_FIXTURE data** —
they demonstrate the classification machinery OFFLINE and are NEVER a claim
of real market coverage for the event.  Real-data coverage is UNKNOWN until
the separately-approved real fetch runs.

Covers: per-TF regime/BOS + combined state + per-TF coverage + eligibility
+ diagnostics, ASOF = signal_time (never captured_at), fail-closed
59/60 / INVALID_SOURCE never becoming a market mode, H1-C contract reuse,
determinism, and provenance-replay parity.

Deterministic and offline: no PostgreSQL, no Bybit, no network.
"""
from __future__ import annotations

import hashlib

import pytest

from app.research.me_entry_quality_h1_v1.classifier import (
    BOS_NOT_EVALUATED,
    COMBINED_REGIME_SEPARATOR,
    REGIME_BEARISH,
    REGIME_BULLISH,
    REGIME_INVALID_SOURCE,
    REGIME_NEUTRAL,
)
from app.research.me_entry_quality_h1c_v1.event_adapter import (
    adapt_source_events,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    EMPTY_PAYLOAD_HASH,
    SOURCE_KIND_OFFLINE_SNAPSHOT,
    HtfSourceIdentity,
    build_source_identity,
)
from app.research.me_entry_quality_h1d_v1.classification import (
    H1D_CLASSIFICATION_VERSION,
    classify_event,
)
from app.research.me_entry_quality_h1d_v1.coverage import (
    COVERAGE_COMPLETE,
    COVERAGE_INCOMPLETE,
    COVERAGE_INVALID,
)
from app.research.me_entry_quality_h1d_v1.provenance import (
    FIXTURE_LABEL_SYNTHETIC,
    build_snapshot,
    canonical_output_bytes,
    replay_classification,
)

# ─── the canonical #305 SOXSUSDT event (verbatim lineage) ──────────────────

EVENT_ROW = {
    "source_event_key":
        "d2ce270406e63cde2e7546d74344dc7bd5d0f5109893631a2387a5e2cc6a5829",
    "source_identity":
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|SOXSUSDT|LONG|5m|1795612200000",
    "symbol": "SOXSUSDT",
    # synthetic stand-in for the #305 lineage hash (real value UNKNOWN here)
    "lineage_hash": hashlib.sha256(
        b"SYNTHETIC_TEST_FIXTURE_LINEAGE").hexdigest(),
    "signal_time": "2026-11-25T13:11:00.804844+00:00",
    "captured_at": "2026-11-25T13:11:01.500000+00:00",
    "activation_freeze_ts": "2026-11-24T13:31:53.365952+00:00",
}

SIGNAL_TIME_US = 1795612260804844     # 2026-11-25T13:11:00.804844Z (µs)
SIGNAL_TIME_MS = 1795612260804        # floor policy
FREEZE_MS = 1795527113365             # 2026-11-24T13:31:53.365952Z floored

TF = 14_400_000
DAY = 86_400_000


def _path(i):
    """Deterministic upward zigzag (triangle wave + drift)."""
    phase = i % 8
    tri = phase if phase <= 4 else 8 - phase
    return 100.0 + 0.3 * i + 5.0 * tri


def _zigzag_rows(open0, n, tf):
    rows = []
    for i in range(n):
        o, c = _path(i - 1), _path(i)
        rows.append({
            "open_ms": open0 + i * tf,
            "open": o, "close": c,
            "high": max(o, c) + 1.0, "low": min(o, c) - 1.0,
            "volume": 1.0,
        })
    return rows


def _event_record(**overrides):
    row = dict(EVENT_ROW)
    row.update(overrides)
    batch = adapt_source_events([row])
    assert not batch.rejections, batch.rejections
    return batch.records[0]


def _payloads(record, n_4h=60, n_1d=60):
    asof_us = record.signal_time_us
    latest_4h = ((asof_us // 1000) - TF) // TF * TF
    latest_1d = ((asof_us // 1000) - DAY) // DAY * DAY
    rows_4h = _zigzag_rows(latest_4h - (n_4h - 1) * TF, n_4h, TF)
    rows_1d = _zigzag_rows(latest_1d - (n_1d - 1) * DAY, n_1d, DAY)
    return rows_4h, rows_1d


def _identity(label, rows):
    if not rows:
        # an empty payload gets the canonical empty hash — it is still
        # reported as EMPTY_SERIES and can never become a market mode
        return HtfSourceIdentity(
            source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT, source_label=label,
            fetched_at_ms=None, row_count=0, payload_hash=EMPTY_PAYLOAD_HASH)
    return build_source_identity(SOURCE_KIND_OFFLINE_SNAPSHOT, label, rows)


def _classify(record, rows_4h, rows_1d):
    return classify_event(
        record, rows_4h, rows_1d,
        source_4h=_identity("SYNTHETIC_TEST_FIXTURE:SOXSUSDT:4H", rows_4h),
        source_1d=_identity("SYNTHETIC_TEST_FIXTURE:SOXSUSDT:1D", rows_1d))


def _full_classification():
    record = _event_record()
    rows_4h, rows_1d = _payloads(record)
    return record, rows_4h, rows_1d, _classify(record, rows_4h, rows_1d)


# ─── first-event handling: SOXSUSDT offline, SYNTHETIC fixture ─────────────


class TestSpecLiteralReportFields:
    """Issue #314 §3.5 per-event field names, top level (audit D3)."""

    def test_spec_fields_are_top_level_and_correct(self):
        _, _, _, classification = _full_classification()
        report = classification.to_dict()
        result = classification.result
        assert report["source_event_key"] == EVENT_ROW["source_event_key"]
        assert report["signal_time"] == "2026-11-25T13:11:00.804844+00:00"
        assert report["regime_4h"] == result.htf_4h.regime
        assert report["bos_4h"] == result.htf_4h.bos
        assert report["regime_1d"] == result.htf_1d.regime
        assert report["bos_1d"] == result.htf_1d.bos
        assert report["combined_state"] == result.combined_regime
        assert report["coverage_4h"]["status"] == COVERAGE_COMPLETE
        assert report["coverage_1d"]["status"] == COVERAGE_COMPLETE
        assert report["eligible"] is True
        assert report["eligibility_reason"] == "ELIGIBLE"

    def test_spec_fields_stay_present_on_fail_closed_result(self):
        record = _event_record()
        rows_4h, rows_1d = _payloads(record, n_4h=59)
        classification = _classify(record, rows_4h, rows_1d)
        report = classification.to_dict()
        result = classification.result
        assert report["eligible"] is False
        for name in ("source_event_key", "signal_time", "regime_4h", "bos_4h",
                     "coverage_4h", "regime_1d", "bos_1d", "coverage_1d",
                     "combined_state", "eligible", "eligibility_reason"):
            assert name in report
        if result is None:
            assert report["combined_state"] is None
        else:
            assert report["combined_state"] == result.combined_regime


class TestFirstEventSyntheticFixture:
    def test_event_lineage_is_verbatim(self):
        record = _event_record()
        assert record.source_event_key == EVENT_ROW["source_event_key"]
        assert record.source_identity == EVENT_ROW["source_identity"]
        assert record.symbol == "SOXSUSDT"
        assert record.signal_time_us == SIGNAL_TIME_US
        assert record.signal_time_ms == SIGNAL_TIME_MS
        assert record.activation_freeze_ts_ms == FREEZE_MS

    def test_asof_is_signal_time_never_captured_at(self):
        _, _, _, classification = _full_classification()
        assert classification.asof_us == SIGNAL_TIME_US
        assert classification.asof_ms == SIGNAL_TIME_MS
        assert classification.asof_ms != 1795612261500  # captured_at ms

    def test_captured_at_cannot_change_the_output(self):
        record_a = _event_record(
            captured_at="2026-11-25T13:11:01.500000+00:00")
        record_b = _event_record(
            captured_at="2026-12-01T00:00:00.000000+00:00")
        rows_4h, rows_1d = _payloads(record_a)
        out_a = canonical_output_bytes(_classify(record_a, rows_4h, rows_1d))
        out_b = canonical_output_bytes(_classify(record_b, rows_4h, rows_1d))
        assert out_a == out_b

    def test_full_report_shape(self):
        _, _, _, classification = _full_classification()
        assert classification.classification_version == H1D_CLASSIFICATION_VERSION
        assert classification.result is not None
        result = classification.result
        # per-TF regime / BOS
        for summary in (result.htf_4h, result.htf_1d):
            assert summary.regime in (
                REGIME_BULLISH, REGIME_BEARISH, REGIME_NEUTRAL)
            assert summary.bos != BOS_NOT_EVALUATED
            assert summary.coverage_status == "VALID"
        # combined state — one of the nine <4H>/<1D> combinations
        expected_combined = (
            f"{result.htf_4h.regime}{COMBINED_REGIME_SEPARATOR}"
            f"{result.htf_1d.regime}")
        assert result.combined_regime == expected_combined
        # per-TF coverage + eligibility + diagnostics
        assert classification.coverage_4h.status == COVERAGE_COMPLETE
        assert classification.coverage_1d.status == COVERAGE_COMPLETE
        assert classification.eligible is True
        assert classification.eligibility_reason == "ELIGIBLE"
        for key in ("coverage_4h_status", "coverage_1d_status",
                    "coverage_4h_missing_open_ms",
                    "coverage_1d_missing_open_ms",
                    "h1c_pipeline_reason", "combined_regime"):
            assert key in classification.diagnostics

    def test_synthetic_fixture_is_labeled_never_real(self):
        # the ONLY snapshot label honest for these rows
        record = _event_record()
        rows_4h, rows_1d = _payloads(record)
        snapshot = build_snapshot(
            record=record, rows_4h=rows_4h, rows_1d=rows_1d,
            source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT,
            source_label="SYNTHETIC_TEST_FIXTURE:SOXSUSDT:htf",
            fixture_label=FIXTURE_LABEL_SYNTHETIC)
        assert snapshot.fixture_label == "SYNTHETIC_TEST_FIXTURE"
        assert "SYNTHETIC" in snapshot.payload_4h.source_label

    def test_regimes_are_reported_for_the_zigzag(self):
        _, _, _, classification = _full_classification()
        # the deterministic upward zigzag classifies as BULLISH on both TFs
        assert classification.result.htf_4h.regime == REGIME_BULLISH
        assert classification.result.htf_1d.regime == REGIME_BULLISH
        assert classification.result.combined_regime == "BULLISH/BULLISH"


# ─── fail closed: coverage problems are never a market mode ────────────────


class TestFailClosed:
    def test_59_of_60_is_not_eligible(self):
        record = _event_record()
        rows_4h, rows_1d = _payloads(record)
        classification = _classify(record, rows_4h[:-1], rows_1d)
        assert classification.eligible is False
        assert "INCOMPLETE_COVERAGE:4H" in classification.eligibility_reason
        assert classification.coverage_4h.status == COVERAGE_INCOMPLETE
        assert classification.coverage_4h.present_buckets == 59
        # diagnostic classification is kept — but never eligible
        assert classification.result is not None
        assert classification.coverage_4h.missing_open_ms

    def test_gap_is_invalid_source_not_a_trend(self):
        record = _event_record()
        rows_4h, rows_1d = _payloads(record)
        broken = list(rows_1d)
        del broken[20]
        classification = _classify(record, rows_4h, broken)
        assert classification.eligible is False
        assert "INVALID_SOURCE:1D:CANDLE_GAP" in classification.eligibility_reason
        assert classification.coverage_1d.status == COVERAGE_INVALID
        # H1-C contract: the failed source NEVER becomes a market mode
        assert classification.result.htf_1d.regime == REGIME_INVALID_SOURCE

    def test_invalid_ohlc_is_invalid_source(self):
        record = _event_record()
        rows_4h, rows_1d = _payloads(record)
        broken = [dict(row) for row in rows_4h]
        broken[10]["high"] = 0.0
        classification = _classify(record, broken, rows_1d)
        assert classification.eligible is False
        assert "INVALID_SOURCE:4H:INVALID_OHLC" in classification.eligibility_reason
        assert classification.result.htf_4h.regime == REGIME_INVALID_SOURCE
        assert classification.result.htf_4h.bos == BOS_NOT_EVALUATED

    def test_empty_payload_is_invalid_source(self):
        record = _event_record()
        _, rows_1d = _payloads(record)
        classification = _classify(record, [], rows_1d)
        assert classification.eligible is False
        assert "INVALID_SOURCE:4H:EMPTY_SERIES" in classification.eligibility_reason

    def test_future_rows_are_excluded_without_error(self):
        record = _event_record()
        rows_4h, rows_1d = _payloads(record)
        with_future = rows_4h + [{
            "open_ms": rows_4h[-1]["open_ms"] + TF,
            "open": 999.0, "high": 1.0, "low": -3.0,
            "close": float("nan"), "volume": -1.0,
        }]
        baseline = _classify(record, rows_4h, rows_1d)
        classification = _classify(record, with_future, rows_1d)
        # corrupt FUTURE rows can never influence a historical classification
        assert classification.eligible == baseline.eligible
        assert classification.coverage_4h.status == COVERAGE_COMPLETE
        assert classification.coverage_4h.dropped_unclosed_opens == (
            rows_4h[-1]["open_ms"] + TF,)


# ─── H1-C contract reuse + determinism ─────────────────────────────────────


class TestH1CReuseAndDeterminism:
    def test_lineage_passes_through_h1c_result(self):
        _, _, _, classification = _full_classification()
        lineage = classification.result.lineage_payload()
        assert lineage["source_event_key"] == EVENT_ROW["source_event_key"]
        assert lineage["source_identity"] == EVENT_ROW["source_identity"]
        assert lineage["signal_time_ms"] == SIGNAL_TIME_MS
        assert lineage["signal_time_us"] == SIGNAL_TIME_US
        assert lineage["activation_freeze_ts_ms"] == FREEZE_MS
        assert lineage["lineage_hash"] == EVENT_ROW["lineage_hash"]

    def test_deterministic_output_bytes(self):
        _, _, _, first = _full_classification()
        _, _, _, second = _full_classification()
        assert canonical_output_bytes(first) == canonical_output_bytes(second)

    def test_replay_matches_direct_classification(self):
        record, rows_4h, rows_1d, direct = _full_classification()
        snapshot = build_snapshot(
            record=record, rows_4h=rows_4h, rows_1d=rows_1d,
            source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT,
            source_label="SYNTHETIC_TEST_FIXTURE:SOXSUSDT:htf",
            source_label_4h="SYNTHETIC_TEST_FIXTURE:SOXSUSDT:4H",
            source_label_1d="SYNTHETIC_TEST_FIXTURE:SOXSUSDT:1D",
            fixture_label=FIXTURE_LABEL_SYNTHETIC)
        replayed = replay_classification(snapshot)
        assert canonical_output_bytes(replayed) == canonical_output_bytes(direct)

    def test_unknown_source_identity_type_rejected(self):
        record = _event_record()
        rows_4h, rows_1d = _payloads(record)
        with pytest.raises(ValueError):
            classify_event(record, rows_4h, rows_1d,
                           source_4h="not-an-identity", source_1d="x")

    def test_non_record_rejected(self):
        rows_4h, rows_1d = _payloads(_event_record())
        identity = build_source_identity(
            SOURCE_KIND_OFFLINE_SNAPSHOT, "x", rows_4h)
        with pytest.raises(ValueError):
            classify_event({"not": "a record"}, rows_4h, rows_1d,
                           source_4h=identity, source_1d=identity)
