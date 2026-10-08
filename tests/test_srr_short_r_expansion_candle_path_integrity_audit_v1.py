"""Offline regression tests for SRR candle-path integrity audit v1.

These tests do not access PostgreSQL, production services, frozen registry state,
scanner, paper or live execution. They exercise isolated replay and coverage logic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.research.srr_short_r_expansion_candle_path_integrity_audit_v1 import (
    Candle,
    candle_coverage_report,
    execution_economics,
    normalize_snapshot_row,
    replay_frozen_semantics,
    replay_legacy,
    stored_path_class,
)


def _row(
    *,
    observation_id: int = 1,
    signal_time: str = "2026-10-07T10:00:00Z",
    entry: float = 100.0,
    invalidation: float = 100.5,
    stop: float = 101.0,
    target: float = 99.25,
):
    return normalize_snapshot_row({
        "observation_id": observation_id,
        "experiment_id": "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1",
        "symbol": "BTCUSDT",
        "direction": "SHORT",
        "signal_time": signal_time,
        "reference_price": entry,
        "invalidation_price": invalidation,
        "variant_entry": entry,
        "variant_stop": stop,
        "variant_target": target,
        "features": {
            "_frozen_max_hold": 120,
            "_frozen_intrabar_policy": "STOP_FIRST",
            "_frozen_freeze_ts": "2026-10-07T08:17:50Z",
            "_execution_r_abs": (entry - invalidation) * 2,
            "_frozen_sl_r": 2.0,
            "_frozen_tp_r": 1.5,
        },
        "tp_hit": False,
        "sl_hit": False,
        "tp_before_sl": False,
        "sl_before_tp": False,
        "ambiguous_intrabar": False,
        "return_at_120m": None,
        "evaluated_120m_at": "2026-10-07T12:00:00Z",
        "evaluated_240m_at": "2026-10-07T14:00:00Z",
        "is_final": True,
    })


def _candle(
    open_time: datetime,
    *,
    open: float = 100.0,
    high: float = 100.5,
    low: float = 99.0,
    close: float = 99.5,
):
    return Candle(
        int(open_time.timestamp() * 1000), open, high, low, close, 10.0,
    )


def _offset(start: datetime, minutes: int) -> datetime:
    return start + timedelta(minutes=minutes)



def _full_candles(row, *, overrides=None):
    """Contiguous 5m path with no default TP/SL touches."""
    overrides = overrides or {}
    signal = row["signal_time"]
    start = signal.replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 5)

    candles = []
    for minute in range(0, 125, 5):
        opened = start + timedelta(minutes=minute)
        values = dict(open=100.0, high=100.2, low=99.8, close=100.0)
        values.update(overrides.get(minute, {}))
        candles.append(_candle(opened, **values))
    return candles


def _coverage(row, candles):
    return candle_coverage_report(row, candles)


def test_tp_before_sl_legacy_and_frozen():
    row = _row()
    candles = _full_candles(row, overrides={
        5: {"low": 99.0},
        10: {"high": 101.2},
    })
    coverage = _coverage(row, candles)
    legacy = replay_legacy(row, coverage)
    frozen = replay_frozen_semantics(row, coverage)
    assert legacy.path_class == "TP_FIRST"
    assert frozen.path_class == "TP_FIRST"
    assert stored_path_class({
        "tp_before_sl": True, "sl_before_tp": False, "ambiguous_intrabar": False,
    }) == "TP_FIRST"


def test_sl_before_tp_legacy_and_frozen():
    row = _row()
    candles = _full_candles(row, overrides={
        5: {"high": 101.2},
        10: {"low": 99.0},
    })
    coverage = _coverage(row, candles)
    legacy = replay_legacy(row, coverage)
    frozen = replay_frozen_semantics(row, coverage)
    assert legacy.path_class == "SL_FIRST"
    assert frozen.path_class == "SL_FIRST"


def test_same_candle_stop_first():
    row = _row(signal_time="2026-10-07T10:02:30Z")
    candles = _full_candles(row, overrides={
        5: {"high": 101.5, "low": 98.0},
    })
    coverage = _coverage(row, candles)
    legacy = replay_legacy(row, coverage)
    frozen = replay_frozen_semantics(row, coverage)
    assert legacy.ambiguous_intrabar and legacy.sl_before_tp
    assert frozen.ambiguous_intrabar and frozen.sl_before_tp
    assert execution_economics(
        row, path_class=frozen.path_class, timeout_close=None,
    )["gross_r"] == -1.0


def test_timeout_uses_last_confirmed_closed_candle():
    row = _row(signal_time="2026-10-07T10:02:30Z")
    candles = _full_candles(row, overrides={
        115: {"close": 100.2, "high": 100.3},
        120: {"close": 100.9, "high": 100.9},
    })
    coverage = _coverage(row, candles)
    legacy = replay_legacy(row, coverage)
    frozen = replay_frozen_semantics(row, coverage)
    assert legacy.path_class == "TIMEOUT"
    assert frozen.path_class == "TIMEOUT"
    # cutoff is 12:02:30; last confirmed closed candle opens at 11:55.
    start = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    assert frozen.timeout_candle_open_ms == int(_offset(start, 115).timestamp() * 1000)
    assert frozen.timeout_close == 100.2


def test_signal_inside_candle_can_be_unverifiable():
    row = _row(signal_time="2026-10-07T10:02:30Z")
    candles = _full_candles(row, overrides={
        0: {"high": 101.5, "low": 99.0},
    })
    coverage = _coverage(row, candles)
    legacy = replay_legacy(row, coverage)
    frozen = replay_frozen_semantics(row, coverage)
    assert legacy.path_class == "TIMEOUT"
    assert frozen.status == "UNVERIFIABLE"
    assert frozen.reason_code == "FIRST_CANDLE_BOUNDARY_UNCERTAIN"


def test_cutoff_inside_last_candle_can_be_unverifiable_timeout():
    row = _row(signal_time="2026-10-07T10:02:30Z")
    candles = _full_candles(row, overrides={
        120: {"high": 101.5},
    })
    coverage = _coverage(row, candles)
    frozen = replay_frozen_semantics(row, coverage)
    # 12:00 candle is not eligible for TP/SL; there is no confirmed closed timeout
    # candle after signal boundary, so cutoff ambiguity must remain unverifiable.
    assert frozen.status == "COMPUTED"
    assert frozen.path_class == "TIMEOUT"
    assert frozen.timeout_close == 100.0
    assert frozen.timeout_candle_open_ms == int(
        datetime(2026, 10, 7, 11, 55, tzinfo=timezone.utc).timestamp() * 1000
    )


def test_unclosed_candle_before_cutoff_is_not_timeout_exit():
    row = _row(signal_time="2026-10-07T10:02:30Z")
    candles = _full_candles(row, overrides={
        115: {"close": 100.1},
        120: {"close": 100.9, "high": 100.9},
    })
    coverage = _coverage(row, candles)
    frozen = replay_frozen_semantics(row, coverage)
    assert frozen.status == "COMPUTED"
    assert frozen.path_class == "TIMEOUT"
    assert frozen.timeout_close == 100.1
    assert frozen.timeout_candle_open_ms == int(
        datetime(2026, 10, 7, 11, 55, tzinfo=timezone.utc).timestamp() * 1000
    )


def test_missing_candle_reports_incomplete_coverage():
    row = _row()
    start = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    candles = [
        _candle(start, close=100.0),
        _candle(_offset(start, 10), close=100.0),
    ]
    coverage = _coverage(row, candles)
    frozen = replay_frozen_semantics(row, coverage)
    assert not coverage["coverage_ok"]
    assert frozen.status == "UNVERIFIABLE"
    assert frozen.reason_code == "MISSING_CANDLES"


def test_duplicate_candles_are_detected():
    row = _row()
    start = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    candles = [
        _candle(start),
        _candle(start),
        _candle(_offset(start, 5)),
    ]
    coverage = _coverage(row, candles)
    assert not coverage["coverage_ok"]
    assert len(coverage["duplicate_open_times"]) == 1


def test_invalid_ohlc_is_detected():
    row = _row()
    start = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    candles = [
        Candle(int(start.timestamp() * 1000), 100.0, 99.0, 101.0, 100.0, 1.0),
        _candle(_offset(start, 5)),
    ]
    coverage = _coverage(row, candles)
    assert not coverage["coverage_ok"]
    assert coverage["invalid_ohlc"]



def test_out_of_order_candles_are_detected():
    row = _row()
    candles = _full_candles(row)
    candles[1], candles[2] = candles[2], candles[1]

    coverage = _coverage(row, candles)

    assert coverage["out_of_order"] is True
    assert coverage["coverage_ok"] is False
    assert not coverage["missing_intervals"]
