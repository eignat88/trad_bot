"""Issue #308 / H1-C.4 — H1 result contract + pipeline tests.

Covers: all nine HTF combinations, explicit eligibility + reasons,
verbatim lineage, protocol version, ASOF-at-signal_time guard (never
``captured_at``), no-lookahead / future-append invariance, stale candles,
partial coverage, invalid-source propagation per timeframe, rerun
determinism.

Deterministic and offline: no PostgreSQL, no Bybit, no network.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h1_v1.classifier import (
    CANONICAL_LOOKBACK_CANDLES,
    REGIME_BEARISH,
    REGIME_BULLISH,
    REGIME_INVALID_SOURCE,
    REGIME_NEUTRAL,
    VALID_COMBINED_REGIMES,
)
from app.research.me_entry_quality_h1_v1.htf_candles import (
    REASON_CANDLE_GAP,
    REASON_MISSING_LATEST_CLOSED_CANDLE,
    TIMEFRAME_1D_MS,
    TIMEFRAME_4H_MS,
)
from app.research.me_entry_quality_h1c_v1.event_adapter import (
    adapt_source_events,
)
from app.research.me_entry_quality_h1c_v1.h1_result import (
    H1C_PROTOCOL_VERSION,
    HtfStateSummary,
    REASON_ELIGIBLE,
    REASON_INVALID_4H,
    build_h1_result,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    REASON_SOURCE_PAYLOAD_MISMATCH,
    build_source_identity,
)
from app.research.me_entry_quality_h1c_v1.pipeline import (
    REASON_INVALID_SOURCE,
    REASON_OK,
    run_h1_event,
)

TF4 = TIMEFRAME_4H_MS
DAY = 86_400_000
BASE_MS = 31 * DAY  # 1970-02-01T00:00:00Z
N = 17
ASOF = BASE_MS + N * DAY  # UTC midnight — closes both the 4H and 1D grids

# index geometry: exactly 2 swing highs + 2 swing lows (Issue #297 fixtures)
BULL_HIGH = [(0, 20), (3, 24), (6, 21), (10, 27), (13, 22), (16, 23)]
BULL_LOW = [(0, 11), (6, 6), (10, 10), (13, 8), (16, 9)]        # HH + HL
BEAR_HIGH = [(0, 20), (3, 27), (6, 22), (10, 24), (13, 21), (16, 22)]
BEAR_LOW = [(0, 16), (6, 11), (10, 14), (13, 8), (16, 10)]      # LH + LL
NEUT_HIGH = BULL_HIGH                                            # HH
NEUT_LOW = BEAR_LOW                                              # LL -> mixed

FIXTURES = {
    REGIME_BULLISH: (BULL_HIGH, BULL_LOW),
    REGIME_BEARISH: (BEAR_HIGH, BEAR_LOW),
    REGIME_NEUTRAL: (NEUT_HIGH, NEUT_LOW),
}


def _zigzag(points, n):
    xs = [p[0] for p in points]
    ys = [float(p[1]) for p in points]
    out = []
    for i in range(n):
        if i <= xs[0]:
            out.append(ys[0])
            continue
        if i >= xs[-1]:
            out.append(ys[-1])
            continue
        for j in range(len(xs) - 1):
            if xs[j] <= i <= xs[j + 1]:
                span = xs[j + 1] - xs[j]
                t = (i - xs[j]) / span
                out.append(ys[j] + (ys[j + 1] - ys[j]) * t)
                break
    return out


def _rows(regime, *, tf, asof=ASOF, n=N):
    high_pts, low_pts = FIXTURES[regime]
    highs = _zigzag(high_pts, n)
    lows = _zigzag(low_pts, n)
    out = []
    for i in range(n):
        open_ms = asof - (n - i) * tf
        mid = (highs[i] + lows[i]) / 2.0
        out.append(
            {
                "open_ms": open_ms,
                "open": mid,
                "high": highs[i],
                "low": lows[i],
                "close": mid,
                "volume": 1.0,
            }
        )
    return out


def _record(signal_time_ms=ASOF, **overrides):
    row = {
        "source_event_key": "evt-1",
        "source_identity": "ME_UPSTREAM_SOURCE_V1::scanner::v3",
        "symbol": "BTCUSDT",
        "signal_time": signal_time_ms,
        "captured_at": signal_time_ms + 3_000,
        "activation_freeze_ts": signal_time_ms - DAY,
        "lineage_hash": "b" * 64,
    }
    row.update(overrides)
    return adapt_source_events([row]).records[0]


def _identity(rows, label):
    return build_source_identity("OFFLINE_SNAPSHOT", label, rows)


def _scrub(result_dict):
    """Result dict without payload provenance (classification fields only)."""
    out = dict(result_dict)
    out["htf_4h"] = {k: v for k, v in out["htf_4h"].items() if k != "source"}
    out["htf_1d"] = {k: v for k, v in out["htf_1d"].items() if k != "source"}
    return out


def _run(regime_4h, regime_1d, *, record=None, asof=ASOF):
    rows_4h = _rows(regime_4h, tf=TF4, asof=asof)
    rows_1d = _rows(regime_1d, tf=DAY, asof=asof)
    rec = record if record is not None else _record(signal_time_ms=asof)
    return run_h1_event(
        rec,
        rows_4h,
        rows_1d,
        source_4h=_identity(rows_4h, "test:4h"),
        source_1d=_identity(rows_1d, "test:1d"),
    )


class TestNineCombinations:
    @pytest.mark.parametrize("regime_4h", [REGIME_BULLISH, REGIME_BEARISH, REGIME_NEUTRAL])
    @pytest.mark.parametrize("regime_1d", [REGIME_BULLISH, REGIME_BEARISH, REGIME_NEUTRAL])
    def test_all_nine_combinations_preserved_verbatim(self, regime_4h, regime_1d):
        outcome = _run(regime_4h, regime_1d)
        assert outcome.ok is True
        assert outcome.reason == REASON_OK
        result = outcome.result
        assert result.combined_regime == f"{regime_4h}/{regime_1d}"
        assert result.combined_regime in VALID_COMBINED_REGIMES
        assert result.combined_eligible is True
        assert result.eligible is True
        assert result.eligibility_reason == REASON_ELIGIBLE
        assert result.htf_4h.regime == regime_4h
        assert result.htf_1d.regime == regime_1d

    def test_combined_regimes_are_never_averaged(self):
        outcome = _run(REGIME_BULLISH, REGIME_BEARISH)
        assert outcome.result.combined_regime == "BULLISH/BEARISH"
        assert outcome.result.combined_regime != "NEUTRAL"


class TestResultContract:
    def test_protocol_version_and_params(self):
        result = _run(REGIME_BULLISH, REGIME_NEUTRAL).result
        assert result.protocol_version == H1C_PROTOCOL_VERSION
        assert result.swing_lookback == 3
        assert result.canonical_lookback == CANONICAL_LOOKBACK_CANDLES

    def test_lineage_preserved_verbatim(self):
        record = _record()
        result = _run(REGIME_BULLISH, REGIME_BULLISH, record=record).result
        lineage = result.lineage_payload()
        assert lineage == {
            "source_event_key": "evt-1",
            "source_identity": "ME_UPSTREAM_SOURCE_V1::scanner::v3",
            "signal_time_ms": ASOF,
            "activation_freeze_ts_ms": ASOF - DAY,
            "lineage_hash": "b" * 64,
        }
        assert lineage == record.lineage_payload()

    def test_to_dict_shape(self):
        d = _run(REGIME_BULLISH, REGIME_NEUTRAL).result.to_dict()
        assert set(d) == {
            "protocol_version",
            "evaluation_asof_ms",
            "lineage",
            "symbol",
            "htf_4h",
            "htf_1d",
            "combined_regime",
            "combined_eligible",
            "eligible",
            "eligibility_reason",
            "swing_lookback",
            "canonical_lookback",
        }
        assert set(d["htf_4h"]) == {
            "timeframe",
            "regime",
            "bos",
            "coverage_status",
            "reason",
            "detail",
            "evaluation_asof_ms",
            "source",
        }
        assert d["htf_4h"]["source"]["source_kind"] == "OFFLINE_SNAPSHOT"

    def test_evaluation_asof_is_signal_time(self):
        record = _record(signal_time_ms=ASOF)
        result = _run(REGIME_BULLISH, REGIME_BULLISH, record=record).result
        assert result.evaluation_asof_ms == ASOF
        assert result.evaluation_asof_ms != record.captured_at_ms

    def test_result_is_immutable(self):
        result = _run(REGIME_BULLISH, REGIME_BULLISH).result
        with pytest.raises(Exception):
            result.eligible = False


class TestAsofGuard:
    def test_state_classified_at_captured_at_is_rejected(self):
        record = _record(signal_time_ms=ASOF)
        summary = HtfStateSummary(
            timeframe="4H",
            regime=REGIME_BULLISH,
            bos="NO_BOS",
            coverage_status="VALID",
            reason="OK",
            detail="",
            evaluation_asof_ms=record.captured_at_ms,  # WRONG instant
        )
        with pytest.raises(ValueError):
            build_h1_result(
                record, summary_4h=summary, summary_1d=summary
            )

    def test_captured_at_never_changes_classification(self):
        rows_4h = _rows(REGIME_BULLISH, tf=TF4)
        rows_1d = _rows(REGIME_NEUTRAL, tf=DAY)
        rec_a = _record(captured_at=ASOF + 1)
        rec_b = _record(captured_at=ASOF + 9 * DAY)
        out_a = run_h1_event(
            rec_a, rows_4h, rows_1d,
            source_4h=_identity(rows_4h, "s4"), source_1d=_identity(rows_1d, "s1"),
        )
        out_b = run_h1_event(
            rec_b, rows_4h, rows_1d,
            source_4h=_identity(rows_4h, "s4"), source_1d=_identity(rows_1d, "s1"),
        )
        assert out_a.result.to_dict() == out_b.result.to_dict()

    def test_unclosed_candle_at_boundary_is_excluded(self):
        rows_4h = _rows(REGIME_BULLISH, tf=TF4) + [
            {"open_ms": ASOF, "open": 30.0, "high": 31.0,
             "low": 29.0, "close": 30.0, "volume": 1.0}
        ]
        rows_1d = _rows(REGIME_NEUTRAL, tf=DAY)
        outcome = run_h1_event(
            _record(),
            rows_4h,
            rows_1d,
            source_4h=_identity(rows_4h, "s4"),
            source_1d=_identity(rows_1d, "s1"),
        )
        assert outcome.result.htf_4h.regime == REGIME_BULLISH
        assert len(outcome.candle_set_4h.dropped_unclosed) == 1

    def test_future_append_invariance(self):
        rows_4h = _rows(REGIME_BULLISH, tf=TF4)
        rows_1d = _rows(REGIME_NEUTRAL, tf=DAY)
        before = run_h1_event(
            _record(), rows_4h, rows_1d,
            source_4h=_identity(rows_4h, "s4"), source_1d=_identity(rows_1d, "s1"),
        )
        rows_4h_ext = rows_4h + [
            {"open_ms": ASOF + i * TF4, "open": 40.0, "high": 41.0,
             "low": 39.0, "close": 40.0, "volume": 1.0}
            for i in range(3)
        ]
        rows_1d_ext = rows_1d + [
            {"open_ms": ASOF + i * DAY, "open": 40.0, "high": 41.0,
             "low": 39.0, "close": 40.0, "volume": 1.0}
            for i in range(2)
        ]
        after = run_h1_event(
            _record(), rows_4h_ext, rows_1d_ext,
            source_4h=_identity(rows_4h_ext, "s4"),
            source_1d=_identity(rows_1d_ext, "s1"),
        )
        # the CLASSIFICATION is unchanged; payload provenance legitimately
        # differs (different rows were fetched), so strip source identity
        assert _scrub(before.result.to_dict()) == _scrub(after.result.to_dict())

    def test_rerun_is_identical(self):
        first = _run(REGIME_BEARISH, REGIME_NEUTRAL).result.to_dict()
        second = _run(REGIME_BEARISH, REGIME_NEUTRAL).result.to_dict()
        assert first == second


class TestFailurePropagation:
    def test_stale_4h_source_yields_explicit_invalid_state(self):
        rows_4h = _rows(REGIME_BULLISH, tf=TF4)[:-1]  # stale tail
        rows_1d = _rows(REGIME_NEUTRAL, tf=DAY)
        outcome = run_h1_event(
            _record(), rows_4h, rows_1d,
            source_4h=_identity(rows_4h, "s4"), source_1d=_identity(rows_1d, "s1"),
        )
        assert outcome.ok is False
        assert outcome.reason == REASON_INVALID_SOURCE
        result = outcome.result
        assert result.htf_4h.regime == REGIME_INVALID_SOURCE
        assert result.htf_4h.reason == REASON_MISSING_LATEST_CLOSED_CANDLE
        assert result.htf_4h.bos == "NOT_EVALUATED"
        # the OTHER timeframe keeps its own valid state
        assert result.htf_1d.regime == REGIME_NEUTRAL
        assert result.combined_eligible is False
        assert result.eligible is False
        assert result.eligibility_reason.startswith(REASON_INVALID_4H)

    def test_partial_coverage_still_classifies_available_window(self):
        rows_4h = _rows(REGIME_BULLISH, tf=TF4)  # 17 << 60 canonical
        rows_1d = _rows(REGIME_NEUTRAL, tf=DAY)
        outcome = run_h1_event(
            _record(), rows_4h, rows_1d,
            source_4h=_identity(rows_4h, "s4"), source_1d=_identity(rows_1d, "s1"),
        )
        assert outcome.ok is True
        assert outcome.candle_set_4h.coverage.status == "PARTIAL"
        assert outcome.candle_set_4h.coverage.present_buckets == 17
        assert len(outcome.candle_set_4h.coverage.missing_open_ms) == 43
        assert outcome.result.htf_4h.regime == REGIME_BULLISH

    def test_gap_in_source_is_invalid_not_neutral(self):
        rows_4h = _rows(REGIME_BULLISH, tf=TF4)
        del rows_4h[4]
        rows_1d = _rows(REGIME_NEUTRAL, tf=DAY)
        outcome = run_h1_event(
            _record(), rows_4h, rows_1d,
            source_4h=_identity(rows_4h, "s4"), source_1d=_identity(rows_1d, "s1"),
        )
        assert outcome.result.htf_4h.regime == REGIME_INVALID_SOURCE
        assert outcome.result.htf_4h.reason == REASON_CANDLE_GAP
        assert outcome.result.combined_eligible is False

    def test_tampered_source_is_invalid(self):
        rows_4h = _rows(REGIME_BULLISH, tf=TF4)
        rows_1d = _rows(REGIME_NEUTRAL, tf=DAY)
        ident_4h = _identity(rows_4h, "s4")
        tampered = [dict(r) for r in rows_4h]
        tampered[3] = dict(tampered[3], close=tampered[3]["close"] + 1.0)
        outcome = run_h1_event(
            _record(), tampered, rows_1d,
            source_4h=ident_4h, source_1d=_identity(rows_1d, "s1"),
        )
        assert outcome.result.htf_4h.regime == REGIME_INVALID_SOURCE
        assert outcome.result.htf_4h.reason == REASON_SOURCE_PAYLOAD_MISMATCH

    def test_both_sources_invalid(self):
        rows_4h = _rows(REGIME_BULLISH, tf=TF4)[:-1]
        rows_1d = _rows(REGIME_NEUTRAL, tf=DAY)[:-1]
        outcome = run_h1_event(
            _record(), rows_4h, rows_1d,
            source_4h=_identity(rows_4h, "s4"), source_1d=_identity(rows_1d, "s1"),
        )
        result = outcome.result
        assert result.htf_4h.regime == REGIME_INVALID_SOURCE
        assert result.htf_1d.regime == REGIME_INVALID_SOURCE
        assert result.combined_eligible is False
        assert result.eligibility_reason == "HTF_BOTH_INVALID_SOURCE"

    def test_outcome_to_dict_shape(self):
        d = _run(REGIME_BULLISH, REGIME_NEUTRAL).to_dict()
        assert set(d) == {
            "pipeline_version",
            "ok",
            "reason",
            "detail",
            "record",
            "result",
        }
