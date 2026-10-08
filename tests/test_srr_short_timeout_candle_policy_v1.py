from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.research.policies import (
    SRR_EXPERIMENT_ID,
    SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1,
    evaluate_srr_short_timeout_candle_path,
)


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "audit_db" / "srr_short_r_expansion_candle_path_integrity_audit_v1_20261008"
SNAPSHOT = BASE / "srr_short_r_expansion_snapshot_clean_utf8.csv"
CANDLES = BASE / "offline_replay_01" / "srr_short_r_expansion_candle_path_integrity_audit_v1_candles.json"

SIGNAL = int(datetime(2026, 10, 7, 10, 2, 30, tzinfo=timezone.utc).timestamp() * 1000)
SIGNAL_OPEN = SIGNAL // 300_000 * 300_000
CUTOFF = SIGNAL + 120 * 60_000


def _candle(
    offset_minutes: int | None,
    *,
    open: float = 100.0,
    high: float = 100.2,
    low: float = 99.8,
    close: float = 100.0,
    base: int = SIGNAL_OPEN,
):
    return {
        "open_time_ms": base + int(offset_minutes) * 60_000,
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def _candles(*, signal_touched=False, cutoff_touched=False):
    candles = []
    for minute in range(0, 125, 5):
        high = 100.2
        low = 99.8
        if minute == 0 and signal_touched:
            high = 101.5
            low = 99.0
        if minute == 120 and cutoff_touched:
            high = 101.5
            low = 99.0
        candles.append(_candle(minute, high=high, low=low))
    return candles


def _evaluate(**overrides):
    kwargs = {
        "signal_time_ms": SIGNAL,
        "entry": 100.0,
        "stop": 101.0,
        "target": 99.25,
        "structural_r": 0.5,
        "execution_r": 1.0,
        "max_hold_minutes": 120,
        "cutoff_time_ms": SIGNAL + 120 * 60_000,
        "candles": _candles(),
        "evaluation_asof_ms": SIGNAL + 240 * 60_000,
        "source_provenance": {
            "source_kind": "OFFLINE_TEST_FIXTURE",
            "source_id": "synthetic-policy-test",
        },
    }
    kwargs.update(overrides)
    return evaluate_srr_short_timeout_candle_path(**kwargs)


def _load_rows():
    import csv

    with SNAPSHOT.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {int(row["observation_id"]): row for row in rows}


def _load_candles():
    return json.loads(CANDLES.read_text(encoding="utf-8"))


def _row_geometry(row):
    entry = float(row["reference_price"])
    invalidation = float(row["invalidation_price"])
    structural = abs(entry - invalidation)
    return {
        "entry": entry,
        "structural_r": structural,
        "execution_r": 2.0 * structural,
        "stop": entry + 2.0 * structural,
        "target": entry - 1.5 * structural,
    }


def _policy_for_row(row, symbol_candles, *, evaluation_asof_ms):
    signal_dt = datetime.fromisoformat(row["signal_time"].replace("Z", "+00:00"))
    signal_ms = int(signal_dt.timestamp() * 1000)
    cutoff_ms = int((signal_dt + timedelta(minutes=120)).timestamp() * 1000)
    geometry = _row_geometry(row)
    return _evaluate(
        signal_time_ms=signal_ms,
        cutoff_time_ms=cutoff_ms,
        **geometry,
        candles=symbol_candles,
        evaluation_asof_ms=evaluation_asof_ms,
        source_provenance={
            "source_kind": "OFFLINE_AUDIT_ARCHIVE",
            "source_id": (
                "offline_replay_01/"
                "srr_short_r_expansion_candle_path_integrity_audit_v1_candles.json"
            ),
        },
    )


class TestSyntheticEligibility:
    def test_fully_eligible_candle_is_included(self):
        result = _evaluate(candles=[_candle(5, high=101.2)])
        assert result.status == "RESOLVED"
        assert result.path_class == "SL_FIRST"
        assert result.eligible_candle_count == 1
        assert result.first_eligible_candle_ms == SIGNAL_OPEN + 300_000
        assert result.last_eligible_candle_ms == SIGNAL_OPEN + 5 * 60_000

    def test_candle_starting_before_signal_is_excluded(self):
        result = _evaluate(candles=[_candle(-5)])
        assert result.eligible_candle_count == 0
        assert result.status == "INCOMPLETE_COVERAGE"
        assert result.net_r is None
    def test_candle_closing_after_cutoff_is_excluded(self):
        result = _evaluate(candles=[_candle(120, high=101.2)])
        assert result.selected_timeout_candle_ms is None
        assert result.selected_timeout_close_ms is None
        assert result.path_class == "CUTOFF_BOUNDARY_UNCERTAIN"
        assert result.status == "CUTOFF_BOUNDARY_UNCERTAIN"
        assert result.net_r is None
        assert result.finalization_eligible is False

    def test_candle_closing_exactly_at_cutoff_is_included(self):
        signal = SIGNAL_OPEN
        cutoff = signal + 120 * 60_000
        candles = [
            _candle(minute, base=signal)
            for minute in range(0, 125, 5)
        ]
        result = _evaluate(
            signal_time_ms=signal,
            cutoff_time_ms=cutoff,
            max_hold_minutes=120,
            candles=candles,
        )
        assert result.selected_timeout_close_ms == cutoff
        assert result.eligible_candle_count == 24
        assert result.status == "RESOLVED"

    def test_candle_ending_after_evaluation_asof_is_not_available(self):
        result = _evaluate(
            evaluation_asof_ms=SIGNAL_OPEN + 120 * 60_000 - 1,
        )
        assert result.status == "INCOMPLETE_COVERAGE"
        assert result.reason_code == "CANDLE_NOT_YET_AVAILABLE_AT_EVALUATION_ASOF"
        assert result.net_r is None
        assert result.finalization_eligible is False


class TestSyntheticOutcomeSemantics:
    @pytest.mark.parametrize("field", ["low", "high"])
    def test_signal_candle_touch_is_uncertain(self, field):
        candles = _candles(signal_touched=True)
        result = _evaluate(candles=candles)
        assert result.status == "FIRST_CANDLE_BOUNDARY_UNCERTAIN"
        assert result.reason_code == "FIRST_CANDLE_BOUNDARY_UNCERTAIN"
        assert result.signal_boundary_uncertain is True
        assert result.net_r is None
        assert result.finalization_eligible is False
    @pytest.mark.parametrize("field", ["low", "high"])
    def test_cutoff_candle_touch_is_uncertain_when_no_earlier_event(self, field):
        candles = _candles(cutoff_touched=True)
        result = _evaluate(candles=candles)
        assert result.status == "CUTOFF_BOUNDARY_UNCERTAIN"
        assert result.reason_code == "CUTOFF_BOUNDARY_UNCERTAIN"
        assert result.cutoff_boundary_uncertain is True
        assert result.net_r is None
        assert result.finalization_eligible is False

    def test_earlier_resolved_event_prevents_cutoff_uncertainty(self):
        candles = _candles(cutoff_touched=True)
        candles[1] = _candle(5, high=101.2)
        result = _evaluate(candles=candles)
        assert result.status == "RESOLVED"
        assert result.path_class == "SL_FIRST"
        assert result.cutoff_boundary_uncertain is False

    def test_eligible_tp_only(self):
        result = _evaluate(candles=[_candle(5, low=99.0)])
        assert result.path_class == "TP_FIRST"
        assert result.gross_r == pytest.approx(0.75)
        assert result.net_r == pytest.approx(0.54)

    def test_eligible_sl_only(self):
        result = _evaluate(candles=[_candle(5, high=101.2)])
        assert result.path_class == "SL_FIRST"
        assert result.gross_r == pytest.approx(-1.0)
        assert result.net_r == pytest.approx(-1.21)

    def test_eligible_tp_and_sl_same_candle_uses_stop_first(self):
        result = _evaluate(candles=[_candle(5, low=99.0, high=101.5)])
        assert result.path_class == "SL_FIRST"
        assert result.reason_code == "TP_SL_SAME_CANDLE_STOP_FIRST"
        assert result.gross_r == pytest.approx(-1.0)
        assert result.net_r == pytest.approx(-1.21)

    def test_clean_path_uses_last_eligible_close_for_timeout(self):
        result = _evaluate()
        assert result.path_class == "TIMEOUT"
        assert result.selected_timeout_close_ms == SIGNAL_OPEN + 120 * 60_000
        assert result.selected_timeout_close_ms == SIGNAL_OPEN + 120 * 60_000
        assert result.return_at_120m == pytest.approx(0.0)
        assert result.net_r == pytest.approx(-0.21)

    def test_missing_interval_is_not_timeout(self):
        result = _evaluate(candles=[_candle(0), _candle(5)])
        assert result.status == "INCOMPLETE_COVERAGE"
        assert result.path_class is None
        assert result.net_r is None
        assert result.selected_timeout_candle_ms is None

    def test_duplicate_timestamp_is_invalid_input(self):
        result = _evaluate(candles=[_candle(0), _candle(0), _candle(5)])
        assert result.status == "INVALID_INPUT"
        assert result.reason_code == "DUPLICATE_CANDLE_TIMESTAMP"

    def test_unordered_candles_are_rejected_deterministically(self):
        result = _evaluate(candles=[_candle(5), _candle(0)])
        assert result.status == "INVALID_INPUT"
        assert result.reason_code == "CANDLES_NOT_SORTED"

    @pytest.mark.parametrize("overrides", [
        {"high": 99.0},
        {"low": 101.0},
        {"high": 98.0, "low": 102.0},
    ])
    def test_incorrect_ohlc_is_invalid_input(self, overrides):
        result = _evaluate(candles=[_candle(5, **overrides)])
        assert result.status == "INVALID_INPUT"
        assert result.reason_code == "INVALID_CANDLE_DATA"

    @pytest.mark.parametrize("provenance", [None, {"source_id": ""}])
    def test_missing_provenance_reduces_source_confidence(self, provenance):
        result = _evaluate(candles=[_candle(5, low=99.0)], source_provenance=provenance)
        assert result.status == "RESOLVED"
        assert result.source_confidence == "UNPROVEN"
        assert result.source_provenance["source_id"] == ""

    @pytest.mark.parametrize("kwargs", [
        {"entry": 0.0},
        {"stop": 99.0},
        {"target": 101.0},
        {"structural_r": 0.0},
        {"execution_r": 0.0},
        {"experiment_id": "OTHER"},
        {"direction": "LONG"},
        {"intrabar_policy": "ORDER_FIRST"},
    ])
    def test_invalid_inputs_are_rejected(self, kwargs):
        overrides = {}
        for key in ("experiment_id", "direction", "intrabar_policy"):
            if key in kwargs:
                overrides[key] = kwargs.pop(key)
        overrides.update(kwargs)
        result = _evaluate(**overrides)
        assert result.status == "INVALID_INPUT"
        assert result.net_r is None
        assert result.finalization_eligible is False


class TestHistoricalFixtures:
    @pytest.mark.parametrize("observation_id,expected_status,expected_path", [
        (26938, "CUTOFF_BOUNDARY_UNCERTAIN", "CUTOFF_BOUNDARY_UNCERTAIN"),
        (22228, "RESOLVED", "TIMEOUT"),
        (18944, "RESOLVED", "TIMEOUT"),
        (15928, "FIRST_CANDLE_BOUNDARY_UNCERTAIN", "FIRST_CANDLE_BOUNDARY_UNCERTAIN"),
        (20207, "FIRST_CANDLE_BOUNDARY_UNCERTAIN", "FIRST_CANDLE_BOUNDARY_UNCERTAIN"),
        (28709, "FIRST_CANDLE_BOUNDARY_UNCERTAIN", "FIRST_CANDLE_BOUNDARY_UNCERTAIN"),
    ])
    def test_key_historical_verdicts(self, observation_id, expected_status, expected_path):
        rows = _load_rows()
        candles_by_symbol = _load_candles()
        result = _policy_for_row(
            rows[observation_id],
            candles_by_symbol[rows[observation_id]["symbol"]],
            evaluation_asof_ms=int(
                datetime.fromisoformat(
                    rows[observation_id]["evaluated_240m_at"].replace("Z", "+00:00")
                ).timestamp() * 1000
            ),
        )
        assert result.status == expected_status
        assert result.path_class == expected_path
        if expected_status != "RESOLVED":
            assert result.net_r is None
            assert result.finalization_eligible is False

    @pytest.mark.parametrize("observation_id,expected_close", [
        (22228, 0.07356),
        (18944, 90.17),
    ])
    def test_timeout_close_selection(self, observation_id, expected_close):
        rows = _load_rows()
        candles_by_symbol = _load_candles()
        result = _policy_for_row(
            rows[observation_id],
            candles_by_symbol[rows[observation_id]["symbol"]],
            evaluation_asof_ms=int(
                datetime.fromisoformat(
                    rows[observation_id]["evaluated_240m_at"].replace("Z", "+00:00")
                ).timestamp() * 1000
            ),
        )
        cutoff_ms = result.cutoff_time_ms
        assert result.status == "RESOLVED"
        assert result.path_class == "TIMEOUT"
        assert result.selected_timeout_close == pytest.approx(expected_close)
        assert result.selected_timeout_close_ms <= cutoff_ms


def test_tp_after_missing_prior_candles_is_not_resolved():
    r = _evaluate(candles=[_candle(60, low=99.0)])
    assert r.status == "INCOMPLETE_COVERAGE"
    assert r.path_class is None
    assert r.finalization_eligible is False
    assert r.gross_r is None
    assert r.net_r is None
    assert r.net_r_normal is None
    assert r.net_r_elevated is None
    assert r.data_quality["missing_eligible_open_times_ms"]


def test_sl_after_missing_prior_candles_is_not_resolved():
    r = _evaluate(candles=[_candle(60, high=101.2)])
    assert r.status == "INCOMPLETE_COVERAGE"
    assert r.path_class is None
    assert r.finalization_eligible is False
    assert r.net_r is None


def test_stop_first_after_missing_prior_candles_is_not_resolved():
    r = _evaluate(candles=[_candle(60, high=101.2, low=99.0)])
    assert r.status == "INCOMPLETE_COVERAGE"
    assert r.path_class is None
    assert r.finalization_eligible is False
    assert r.net_r is None


def test_asof_candle_close_boundary_is_enforced():
    r = _evaluate(
        signal_time_ms=SIGNAL_OPEN,
        cutoff_time_ms=SIGNAL_OPEN + 120 * 60_000,
        candles=[_candle(0, low=99.0)],
        evaluation_asof_ms=SIGNAL_OPEN + 4 * 60_000,
    )
    assert r.status != "RESOLVED"
    assert r.path_class != "TP_FIRST"
    assert r.finalization_eligible is False
    assert r.net_r is None


def test_asof_candle_close_boundary_allows_confirmed_tp():
    r = _evaluate(
        signal_time_ms=SIGNAL_OPEN,
        cutoff_time_ms=SIGNAL_OPEN + 120 * 60_000,
        candles=[_candle(0, low=99.0)],
        evaluation_asof_ms=SIGNAL_OPEN + 5 * 60_000,
    )
    assert r.status == "RESOLVED"
    assert r.path_class == "TP_FIRST"
    assert r.finalization_eligible is True
    assert r.gross_r == pytest.approx(0.75)
    assert r.net_r_normal == pytest.approx(0.54)
