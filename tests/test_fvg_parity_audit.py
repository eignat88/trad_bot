"""Tests for FVG production structural parity audit + CLI."""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

from app.scanners.fvg_parity_audit import run_parity_audit, print_parity_report, main


# --- Structural audit ---

class TestParityAudit:
    def test_empty_setup_returns_empty(self):
        class Repo:
            class _conn:
                def cursor(self): return self
                def execute(self, sql, params): self._rows = []
                def fetchall(self): return self._rows
            _conn = _conn()
        result = run_parity_audit(Repo(), limit=10)
        assert result["summary"]["total"] == 0

    def test_matched_setup(self):
        features = json.dumps({
            "fvg_created_at": 1000, "confirmation_at": 2000,
            "entry_price": 100.0, "sl_price": 98.0, "tp_price": 106.0,
            "bars_to_touch": 2, "fvg_atr": 0.25, "c2_body_ratio": 0.75, "rr": 3.0,
        })
        class FC:
            def __init__(self):
                self._rows = [("id-1", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                    "LONG", "5m", "5m", None, 1500, 100.0, 100.0, 100.0,
                    98.0, 106.0, None, 80.0, "RANGE", None, features)]
            def execute(self, sql, params): pass
            def fetchall(self): return self._rows
        class R:
            def __init__(self): self._conn = type("C", (), {"cursor": lambda s: FC()})()
        result = run_parity_audit(R(), limit=10)
        assert result["summary"]["total"] == 1
        assert result["summary"]["matched"] == 1

    def test_tp_mismatch(self):
        features = json.dumps({
            "fvg_created_at": 1000, "confirmation_at": 2000,
            "entry_price": 100.0, "sl_price": 98.0, "tp_price": 105.0,
            "bars_to_touch": 2, "fvg_atr": 0.25, "c2_body_ratio": 0.75, "rr": 3.0,
        })
        class FC:
            def __init__(self):
                self._rows = [("id-2", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                    "LONG", "5m", "5m", None, 1500, 100.0, 100.0, 100.0,
                    98.0, 105.0, None, 80.0, "RANGE", None, features)]
            def execute(self, sql, params): pass
            def fetchall(self): return self._rows
        class R:
            def __init__(self): self._conn = type("C", (), {"cursor": lambda s: FC()})()
        result = run_parity_audit(R(), limit=10)
        assert result["summary"]["mismatched"] >= 1
        assert "TP mismatch" in str(result["records"][0]["mismatch_reason"])

    def test_missing_features(self):
        features = json.dumps({})
        class FC:
            def __init__(self):
                self._rows = [("id-3", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                    "LONG", "5m", "5m", None, 1500, 100.0, 100.0, 100.0,
                    98.0, 106.0, None, 80.0, "RANGE", None, features)]
            def execute(self, sql, params): pass
            def fetchall(self): return self._rows
        class R:
            def __init__(self): self._conn = type("C", (), {"cursor": lambda s: FC()})()
        result = run_parity_audit(R(), limit=10)
        assert result["summary"]["mismatched"] >= 1

    def test_print_no_crash(self):
        print_parity_report({"records": [], "summary": {"total": 0, "matched": 0,
            "mismatched": 0, "unverified": 0, "duplicates": 0}})

    def test_duplicates(self):
        features = json.dumps({
            "fvg_created_at": 1000, "confirmation_at": 2000,
            "entry_price": 100.0, "sl_price": 98.0, "tp_price": 106.0,
            "bars_to_touch": 2, "fvg_atr": 0.25, "c2_body_ratio": 0.75, "rr": 3.0,
        })
        class FC:
            def __init__(self):
                self._rows = [
                    ("id-a", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                     "LONG", "5m", "5m", None, 1500, 100.0, 100.0, 100.0,
                     98.0, 106.0, None, 80.0, "RANGE", None, features),
                    ("id-b", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT",
                     "LONG", "5m", "5m", None, 1500, 100.0, 100.0, 100.0,
                     98.0, 106.0, None, 80.0, "RANGE", None, features),
                ]
            def execute(self, sql, params): pass
            def fetchall(self): return self._rows
        class R:
            def __init__(self): self._conn = type("C", (), {"cursor": lambda s: FC()})()
        result = run_parity_audit(R(), limit=10)
        assert result["summary"]["duplicates"] >= 1


# --- CLI ---

class TestCLIEntrypoint:
    def test_main_invokes_and_closes(self):
        close_calls = []
        mock_repo = MagicMock()
        mock_repo.close = lambda: close_calls.append(1)
        audit_result = {"records": [], "summary": {"total": 0}}

        with patch("app.config.load_settings", create=True) as ms, \
             patch("app.db.repository.ScannerRepository", create=True) as mr, \
             patch("sys.argv", ["fvg_parity_audit"]), \
             patch("app.scanners.fvg_parity_audit.run_parity_audit", return_value=audit_result) as ma, \
             patch("app.scanners.fvg_parity_audit.print_parity_report") as mp:
            ms.return_value = MagicMock()
            mr.return_value = mock_repo
            main()

        ma.assert_called_once()
        mp.assert_called_once_with(audit_result)
        assert close_calls == [1]

    def test_main_closes_on_error(self):
        close_calls = []
        mock_repo = MagicMock()
        mock_repo.close = lambda: close_calls.append(1)

        with patch("app.config.load_settings", create=True) as ms, \
             patch("app.db.repository.ScannerRepository", create=True) as mr, \
             patch("sys.argv", ["fvg_parity_audit"]), \
             patch("app.scanners.fvg_parity_audit.run_parity_audit", side_effect=RuntimeError("db fail")), \
             patch("app.scanners.fvg_parity_audit.print_parity_report"):
            ms.return_value = MagicMock()
            mr.return_value = mock_repo
            try:
                main()
            except (SystemExit, RuntimeError):
                pass

        assert close_calls == [1]

    def test_default_args(self):
        mock_repo = MagicMock()
        mock_repo.close = MagicMock()

        with patch("app.config.load_settings", create=True) as ms, \
             patch("app.db.repository.ScannerRepository", create=True) as mr, \
             patch("sys.argv", ["fvg_parity_audit"]), \
             patch("app.scanners.fvg_parity_audit.run_parity_audit") as ma, \
             patch("app.scanners.fvg_parity_audit.print_parity_report"):
            ms.return_value = MagicMock()
            mr.return_value = mock_repo
            ma.return_value = {"records": [], "summary": {}}
            main()

        args = ma.call_args[0]
        assert args[1] == "FVG_REACTION_LONG_LOCAL_STRUCT_V1"
        assert args[2] == 100
