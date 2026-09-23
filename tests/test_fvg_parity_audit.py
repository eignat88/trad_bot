"""Tests for FVG production ↔ research parity audit."""
from __future__ import annotations

import json
from app.scanners.fvg_parity_audit import run_parity_audit, print_parity_report


class TestParityAudit:
    def test_empty_setup_returns_empty(self):
        class Repository:
            class _conn:
                def cursor(self):
                    return self
                def execute(self, sql, params):
                    self._rows = []
                def fetchall(self):
                    return self._rows
            _conn = _conn()

        result = run_parity_audit(Repository(), limit=10)
        assert result["summary"]["total"] == 0
        assert result["summary"]["matched"] == 0

    def test_matched_setup(self):
        """Setup with correct parameters should be MATCHED."""
        features = json.dumps({
            "fvg_created_at": 1000,
            "confirmation_at": 2000,
            "entry_price": 100.0,
            "sl_price": 98.0,
            "tp_price": 106.0,
            "bars_to_touch": 2,
            "fvg_atr": 0.25,
            "c2_body_ratio": 0.75,
            "rr": 3.0,
        })

        class FakeCursor:
            def __init__(self):
                # SQL: setup_id[0], scanner[1], symbol[2], direction[3],
                # setup_tf[4], entry_tf[5], detected_at[6], signal_candle_open_time[7],
                # reference_price[8], entry_zone_low[9], entry_zone_high[10],
                # invalidation[11], target_1[12], target_2[13],
                # score[14], market_regime[15], reasons[16], features[17]
                self._rows = [
                    ("id-1", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                     "LONG", "5m", "5m",
                     None, 1500,
                     100.0, 100.0, 100.0,
                     98.0, 106.0, None,
                     80.0, "RANGE", None, features),
                ]
            def execute(self, sql, params):
                pass
            def fetchall(self):
                return self._rows

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        class Repository:
            def __init__(self):
                self._conn = FakeConn()

        result = run_parity_audit(Repository(), limit=10)
        assert result["summary"]["total"] == 1
        assert result["summary"]["matched"] == 1

    def test_tp_mismatch_detected(self):
        """TP that doesn't match 3R should be flagged."""
        features = json.dumps({
            "fvg_created_at": 1000,
            "confirmation_at": 2000,
            "entry_price": 100.0,
            "sl_price": 98.0,
            "tp_price": 105.0,  # wrong: should be 106 (3R)
            "bars_to_touch": 2,
            "fvg_atr": 0.25,
            "c2_body_ratio": 0.75,
            "rr": 3.0,
        })

        class FakeCursor:
            def __init__(self):
                self._rows = [
                    ("id-2", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                     "LONG", "5m", "5m",
                     None, 1500,
                     100.0, 100.0, 100.0,
                     98.0, 105.0, None,
                     80.0, "RANGE", None, features),
                ]
            def execute(self, sql, params):
                pass
            def fetchall(self):
                return self._rows

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        class Repository:
            def __init__(self):
                self._conn = FakeConn()

        result = run_parity_audit(Repository(), limit=10)
        assert result["summary"]["mismatched"] >= 1
        assert "TP mismatch" in str(result["records"][0]["mismatch_reason"])

    def test_missing_frozen_params_flagged(self):
        """Missing frozen parameters should be flagged."""
        features = json.dumps({})  # empty features

        class FakeCursor:
            def __init__(self):
                self._rows = [
                    ("id-3", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                     "LONG", "5m", "5m",
                     None, 1500,  # detected_at, signal_candle_open_time
                     None, 100.0, 100.0,  # reference_price, entry_zone_low, high
                     98.0, 106.0, None,  # invalidation, target_1, target_2
                     80.0, "RANGE", None, features),
                ]
            def execute(self, sql, params):
                pass
            def fetchall(self):
                return self._rows

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        class Repository:
            def __init__(self):
                self._conn = FakeConn()

        result = run_parity_audit(Repository(), limit=10)
        assert result["summary"]["mismatched"] >= 1
        reasons = result["records"][0]["mismatch_reason"]
        assert "missing" in reasons

    def test_print_report_does_not_crash(self):
        """print_parity_report should not raise."""
        result = {
            "records": [],
            "summary": {"total": 0, "matched": 0, "mismatched": 0, "unverified": 0, "duplicates": 0},
        }
        print_parity_report(result)

    def test_duplicate_detection(self):
        """Same (symbol, tf, ts) should be flagged as duplicate."""
        features = json.dumps({
            "fvg_created_at": 1000,
            "confirmation_at": 2000,
            "entry_price": 100.0, "sl_price": 98.0, "tp_price": 106.0,
            "bars_to_touch": 2, "fvg_atr": 0.25, "c2_body_ratio": 0.75, "rr": 3.0,
        })

        class FakeCursor:
            def __init__(self):
                self._rows = [
                    ("id-a", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                     "LONG", "5m", "5m", None, None, 1500,
                     None, 100.0, 100.0, 98.0, 106.0, None,
                     80.0, "RANGE", None, features),
                    ("id-b", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                     "LONG", "5m", "5m", None, None, 1500,
                     None, 100.0, 100.0, 98.0, 106.0, None,
                     80.0, "RANGE", None, features),
                ]
            def execute(self, sql, params):
                pass
            def fetchall(self):
                return self._rows

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        class Repository:
            def __init__(self):
                self._conn = FakeConn()

        result = run_parity_audit(Repository(), limit=10)
        assert result["summary"]["duplicates"] >= 1
