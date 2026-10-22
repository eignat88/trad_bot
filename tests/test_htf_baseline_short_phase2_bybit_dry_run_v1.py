"""Issue #181 review FIX-2 regression matrix: fail-closed dry-run verdict.

Covers the mandatory review scenarios (dry-run readiness verdict):

13. all symbols valid                       -> PASS;
14. one symbol SOURCE_BLOCKED               -> FAIL (even when run_once()==EXIT_OK);
15. one symbol QUARANTINED                  -> FAIL;
16. one symbol with incomplete coverage     -> FAIL;
17. run_once()==EXIT_OK but source_errors>0 -> FAIL;
18. broken checkpoint/restart equivalence   -> FAIL;
19. exceeded API request budget             -> FAIL;
20. missing verified evaluation ASOF        -> FAIL.

All tests are deterministic and never call the Bybit API.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "research"))

from app.models import Candle
from app.research.htf_baseline_short_phase2_observer_runner_v1 import EXIT_OK
from htf_baseline_short_phase2_bybit_live_dry_run_v1 import (
    build_symbol_status,
    evaluate_readiness,
    main,
)

BAR = 300_000
HOUR = 3_600_000
HOUR0 = 1_759_996_800_000
SYMBOLS = ("BTCUSDT", "ETHUSDT")


def make_candles() -> list[Candle]:
    """Same causal shape as the runner fixture: touches then two contact runs."""
    highs = [100.0] * 68
    for i in (44, 52):
        highs[i] = 109.9
    highs[56] = 110.0
    for i in (61, 62, 65, 66):
        highs[i] = 110.0
    candles: list[Candle] = []
    for hour_index, high in enumerate(highs):
        for bar_index in range(12):
            ts = HOUR0 + hour_index * HOUR + bar_index * BAR
            touch_hour = high >= 109.5
            candles.append(
                Candle(ts, 100.0, high, 99.0, 100.0 if not touch_hour else 109.5, 1.0)
            )
    return candles


class FakeMultiClient:
    """Per-symbol deterministic Bybit v5 fake; ``blocked`` symbols always fail."""

    def __init__(self, candles, blocked=(), server_time_ms=None):
        self.rows = {int(c.timestamp): row_of(c) for c in candles}
        self.server_time_ms = int(
            server_time_ms if server_time_ms is not None else candles[-1].timestamp + BAR
        )
        self.blocked = set(blocked)
        self.calls: list[tuple] = []

    def _public_get(self, endpoint: str, **params):
        self.calls.append((endpoint, dict(params)))
        symbol = params.get("symbol")
        if endpoint == "/v5/market/time":
            return {"retCode": 0, "result": {}, "time": self.server_time_ms}
        if symbol in self.blocked:
            raise RuntimeError(f"bybit retCode=10001 for {symbol}")
        start, end = int(params["start"]), int(params["end"])
        out = [self.rows[o] for o in sorted(self.rows) if start <= o <= end]
        out.reverse()
        return {"retCode": 0, "result": {"list": out}, "time": self.server_time_ms}


def row_of(candle: Candle) -> list[str]:
    return [
        str(int(candle.timestamp)),
        str(candle.open),
        str(candle.high),
        str(candle.low),
        str(candle.close),
        str(candle.volume),
    ]


def ok_status(**overrides):
    entry = {
        "status": "OK",
        "processed": True,
        "coverage": "COMPLETE",
        "cursor_last_processed_open_ms": HOUR0,
        "reason": None,
    }
    entry.update(overrides)
    return entry


def base_inputs():
    symbol_status = {s: ok_status() for s in SYMBOLS}
    metrics = {
        "source_errors": 0,
        "symbols_blocked": 0,
        "quarantine_bars": 0,
        "coverage_gap_bars": 0,
    }
    audits = {
        "causal_asof": {"pass": True, "failures": []},
        "bounded_requests": {"pass": True, "failures": []},
        "checkpoint_restart_equivalence": {"pass": True, "failures": []},
    }
    return symbol_status, metrics, audits


def codes(blockers):
    return [b["code"] for b in blockers]


def run_tool(tmp_path, *, blocked=(), extra_args=()):
    candles = make_candles()
    report_path = tmp_path / "report.json"
    client = FakeMultiClient(candles, blocked=blocked)
    argv = [
        "--symbols", ",".join(SYMBOLS),
        "--min-history-bars", "600",
        "--warmup-bars", "400",
        "--evaluation-asof-ms", str(candles[-1].timestamp + BAR),
        "--checkpoint", str(tmp_path / "state.json"),
        "--report", str(report_path),
        "--log-level", "ERROR",
        *extra_args,
    ]
    exit_code = main(argv, client_factory=lambda: client)
    return exit_code, json.loads(report_path.read_text(encoding="utf-8")), client


# ----------------------------------------------------------------- scenario 13
def test_all_symbols_valid_passes(tmp_path):
    symbol_status, metrics, audits = base_inputs()
    verdict, blockers = evaluate_readiness(
        requested_symbols=SYMBOLS,
        symbol_status=symbol_status,
        metrics=metrics,
        audits=audits,
        evaluation_asof_ms=HOUR0 + 68 * HOUR,
    )
    assert verdict == "PASS"
    assert blockers == []

    exit_code, report, client = run_tool(tmp_path)
    assert exit_code == EXIT_OK
    assert report["overall_verdict"] == "PASS"
    assert report["verdict"] == "PASS"
    assert report["readiness_blockers"] == []
    assert report["source_errors"] == 0
    assert report["symbols_blocked"] == 0
    assert report["quarantine_bars"] == 0
    assert all(report["symbol_status"][s]["status"] == "OK" for s in SYMBOLS)
    assert report["coverage_status"]["complete"] == list(SYMBOLS)
    for key in ("symbol_status", "source_errors", "symbols_blocked", "quarantine_bars",
                "coverage_status", "recovery_status", "readiness_blockers", "overall_verdict"):
        assert key in report, key


# ----------------------------------------------------------------- scenario 14
def test_one_source_blocked_symbol_fails(tmp_path):
    exit_code, report, client = run_tool(tmp_path, blocked={"ETHUSDT"})
    assert report["cycle1_exit_code"] == EXIT_OK  # run_once()==EXIT_OK must NOT hide it
    assert exit_code == 1
    assert report["overall_verdict"] == "FAIL"
    assert "SOURCE_BLOCKED_ETHUSDT" in codes(report["readiness_blockers"])
    assert report["symbols_blocked"] == 1
    assert report["source_errors"] == 1
    assert report["symbol_status"]["BTCUSDT"]["status"] == "OK"
    assert report["symbol_status"]["ETHUSDT"]["status"] == "SOURCE_BLOCKED"

    symbol_status, metrics, audits = base_inputs()
    symbol_status["ETHUSDT"] = ok_status(
        status="SOURCE_BLOCKED", coverage="INCOMPLETE", reason="BYBIT_API_ERROR"
    )
    metrics["source_errors"] = 1
    metrics["symbols_blocked"] = 1
    verdict, blockers = evaluate_readiness(
        requested_symbols=SYMBOLS,
        symbol_status=symbol_status,
        metrics=metrics,
        audits=audits,
        evaluation_asof_ms=HOUR0,
    )
    assert verdict == "FAIL"
    assert "SOURCE_BLOCKED_ETHUSDT" in codes(blockers)


# ----------------------------------------------------------------- scenario 15
def test_one_quarantined_symbol_fails():
    symbol_status, metrics, audits = base_inputs()
    symbol_status["ETHUSDT"] = ok_status(
        status="QUARANTINED", coverage="INCOMPLETE", reason="RESUME_GAP_SOURCE_WINDOW"
    )
    metrics["quarantine_bars"] = 1
    verdict, blockers = evaluate_readiness(
        requested_symbols=SYMBOLS,
        symbol_status=symbol_status,
        metrics=metrics,
        audits=audits,
        evaluation_asof_ms=HOUR0,
    )
    assert verdict == "FAIL"
    assert "QUARANTINED_ETHUSDT" in codes(blockers)
    assert "QUARANTINE_BARS_PRESENT" in codes(blockers)


# ----------------------------------------------------------------- scenario 16
def test_incomplete_coverage_fails():
    symbol_status, metrics, audits = base_inputs()
    symbol_status["ETHUSDT"] = ok_status(coverage="INCOMPLETE")
    verdict, blockers = evaluate_readiness(
        requested_symbols=SYMBOLS,
        symbol_status=symbol_status,
        metrics=metrics,
        audits=audits,
        evaluation_asof_ms=HOUR0,
    )
    assert verdict == "FAIL"
    assert "INCOMPLETE_COVERAGE_ETHUSDT" in codes(blockers)


# ----------------------------------------------------------------- scenario 17
def test_exit_ok_with_source_errors_fails():
    symbol_status, metrics, audits = base_inputs()
    metrics["source_errors"] = 1  # run_once()==EXIT_OK but errors were recorded
    verdict, blockers = evaluate_readiness(
        requested_symbols=SYMBOLS,
        symbol_status=symbol_status,
        metrics=metrics,
        audits=audits,
        evaluation_asof_ms=HOUR0,
    )
    assert verdict == "FAIL"
    assert "SOURCE_ERRORS_PRESENT" in codes(blockers)


# ----------------------------------------------------------------- scenario 18
def test_broken_restart_equivalence_fails():
    symbol_status, metrics, audits = base_inputs()
    audits["checkpoint_restart_equivalence"] = {
        "pass": False,
        "failures": ["bars reprocessed on restart"],
    }
    verdict, blockers = evaluate_readiness(
        requested_symbols=SYMBOLS,
        symbol_status=symbol_status,
        metrics=metrics,
        audits=audits,
        evaluation_asof_ms=HOUR0,
    )
    assert verdict == "FAIL"
    assert "RESTART_EQUIVALENCE_BROKEN" in codes(blockers)


# ----------------------------------------------------------------- scenario 19
def test_request_budget_exceeded_fails():
    symbol_status, metrics, audits = base_inputs()
    audits["bounded_requests"] = {"pass": False, "failures": ["request_count 900 > budget"]}
    verdict, blockers = evaluate_readiness(
        requested_symbols=SYMBOLS,
        symbol_status=symbol_status,
        metrics=metrics,
        audits=audits,
        evaluation_asof_ms=HOUR0,
    )
    assert verdict == "FAIL"
    assert "REQUEST_BUDGET_EXCEEDED" in codes(blockers)


# ----------------------------------------------------------------- scenario 20
def test_missing_verified_asof_fails():
    symbol_status, metrics, audits = base_inputs()
    verdict, blockers = evaluate_readiness(
        requested_symbols=SYMBOLS,
        symbol_status=symbol_status,
        metrics=metrics,
        audits=audits,
        evaluation_asof_ms=None,
    )
    assert verdict == "FAIL"
    assert "MISSING_VERIFIED_ASOF" in codes(blockers)


# ------------------------------------------------- build_symbol_status coverage
def test_build_symbol_status_never_merges_symbol_outcomes():
    metrics = {"source_errors": 1, "symbols_blocked": 1}
    status = build_symbol_status(
        SYMBOLS,
        metrics,
        coverage_reports=[{"symbol": "ETHUSDT", "action": "SOURCE_BLOCKED", "reason": "RATE_LIMITED"}],
        cursors={"BTCUSDT": {"last_processed_open_ms": HOUR0}},
        symbols_state={"BTCUSDT": {}},
    )
    assert status["BTCUSDT"]["status"] == "OK"
    assert status["BTCUSDT"]["coverage"] == "COMPLETE"
    assert status["ETHUSDT"]["status"] == "SOURCE_BLOCKED"
    assert status["ETHUSDT"]["coverage"] == "INCOMPLETE"
    assert status["ETHUSDT"]["reason"] == "RATE_LIMITED"
