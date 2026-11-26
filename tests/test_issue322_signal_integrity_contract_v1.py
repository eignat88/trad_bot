"""Issue #322 — signal-integrity regression contracts.

No real database, network, scanner or production state.

Covered here:

* scanner_setup terminal protection (Task C) — the PostgreSQL UPSERT keeps the
  original ``run_id`` and never resurrects terminal ``EXECUTED`` / ``INVALIDATED``
  / ``EXPIRED`` setups; the JSONL backend mirrors the same protection;
* the ME_SHORT candle identity is restart-stable (deterministic SHA-256);
* the candle-lineage ContextVar never leaks across sequential or concurrent
  ``observe()`` calls.

The identity is scoped to ``MOMENTUM_EXHAUSTION`` / ``SHORT`` (Task B).  Every
other family keeps the frozen legacy identity and is covered by
``test_issue322_scope_isolation_v1.py``.
"""

import os
import re
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.db.repository import ScannerRepository, read_records
from app.research.scanner_signal_integrity_observer import (
    ScannerSignalIntegrityObserver,
)
from app.research.scanner_signal_integrity_observer import _CANDLE_LINEAGE
from app.scanners.models import SetupCandidate


# ============================================================
# Stable ME_SHORT candle identity (restart-safe)
# ============================================================

_IDENTITY_ARGS = ("MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", 1795680000000)


def _candle_identity_in_process(seed: str) -> int:
    code = (
        "from app.research.scanner_signal_integrity_observer import "
        "ScannerSignalIntegrityObserver\n"
        "print(ScannerSignalIntegrityObserver._make_candle_source_key("
        '"MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", 1795680000000,'
        "))\n"
    )
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = seed
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return int(result.stdout.strip())


def test_candle_identity_same_within_one_process():
    assert (
        ScannerSignalIntegrityObserver._make_candle_source_key(*_IDENTITY_ARGS)
        == ScannerSignalIntegrityObserver._make_candle_source_key(*_IDENTITY_ARGS)
    )


def test_candle_identity_survives_restart():
    """Same signal candle keeps identity across hash seeds / restarts."""
    keys = {
        _candle_identity_in_process("1"),
        _candle_identity_in_process("2"),
        _candle_identity_in_process("3"),
    }

    assert len(keys) == 1, f"ISSUE322_UNSTABLE_OOS_IDENTITY: {sorted(keys)}"


# ============================================================
# ContextVar isolation (sequential + concurrent)
# ============================================================


def _make_sql_observer():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (7,)
    registry = {
        "ME_SHORT_INTEGRITY_V1": {
            "scanner_name": "MOMENTUM_EXHAUSTION",
            "direction": "SHORT",
            "status": "RUNNING",
        }
    }
    return ScannerSignalIntegrityObserver(conn, registry), conn, cursor


def _observe(observer, candle, detected_at):
    return observer.observe(
        scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT",
        symbol="BTCUSDT",
        signal_time=detected_at,
        entry_timeframe="5m",
        signal_candle_open_time=candle,
        reference_price=100.0,
        invalidation_price=99.0,
        target_1=102.0,
        target_2=None,
        score=80.0,
        features={},
        parameters={},
        market_regime="RANGE",
    )


def _insert_source_ids(cursor):
    out = []
    for call in cursor.execute.call_args_list:
        if "INSERT INTO research.prospective_observation" in call.args[0]:
            out.append(call.args[1][1])
    return out


def test_contextvar_does_not_leak_across_sequential_calls():
    observer, _, cursor = _make_sql_observer()
    candle_a, candle_b = 1795680000000, 1795680300000
    start = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)

    with (
        patch.object(observer, "_is_db_active", return_value=True),
        patch.object(observer, "_is_direction_active", return_value=True),
    ):
        _observe(observer, candle_a, start)
        assert _CANDLE_LINEAGE.get() is None  # cleaned up after the call
        _observe(observer, candle_b, start)
        assert _CANDLE_LINEAGE.get() is None  # cleaned up after the call

    ids = _insert_source_ids(cursor)
    assert ids == [
        ScannerSignalIntegrityObserver._make_candle_source_key(
            "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", candle_a
        ),
        ScannerSignalIntegrityObserver._make_candle_source_key(
            "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", candle_b
        ),
    ]


def test_contextvar_does_not_leak_across_concurrent_calls():
    observer, _, cursor = _make_sql_observer()
    base = 1795680000000
    candles = [base + i * 300_000 for i in range(8)]
    expected = {
        candle: ScannerSignalIntegrityObserver._make_candle_source_key(
            "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", candle
        )
        for candle in candles
    }
    start = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)

    barrier = threading.Barrier(len(candles))

    def run(candle):
        barrier.wait()  # maximize overlap between observe() calls
        _observe(observer, candle, start)

    with (
        patch.object(observer, "_is_db_active", return_value=True),
        patch.object(observer, "_is_direction_active", return_value=True),
    ):
        threads = [
            threading.Thread(target=run, args=(candle,)) for candle in candles
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    produced = _insert_source_ids(cursor)
    # Every candle produced exactly its own identity — no cross-contamination
    # between concurrent calls (a shared / leaking lineage would collapse these).
    assert sorted(produced) == sorted(expected.values())
    assert _CANDLE_LINEAGE.get() is None


# ============================================================
# scanner_setup terminal protection (Task C) — PostgreSQL + JSONL
# ============================================================


def _upsert_sql() -> str:
    source = Path(ScannerRepository._save_pg.__code__.co_filename)
    text = source.read_text(encoding="utf-8")

    start = text.index("    def _save_pg(")
    end = text.index("    def _save_jsonl(", start)

    return text[start:end]


def test_upsert_preserves_original_run_id():
    """A repeat scan must not steal the original run provenance."""
    sql = _upsert_sql()

    assert "ON CONFLICT" in sql

    update_clause = sql.split("DO UPDATE SET", 1)[1]

    assert not re.search(
        r"\brun_id\s*=\s*EXCLUDED\.run_id\b",
        update_clause,
        re.IGNORECASE,
    ), "ISSUE322_HISTORICAL_RUN_ID_OVERWRITE"


def test_upsert_does_not_unconditionally_resurrect_status():
    """A duplicate detection cannot update terminal setup rows."""
    sql = _upsert_sql()
    assert "ON CONFLICT" in sql
    update_clause = sql.split("DO UPDATE SET", 1)[1]

    assert re.search(
        r"WHERE\s+dds\.scanner_setup\.status\s+NOT\s+IN\s*"
        r"\(\s*'EXECUTED'\s*,\s*'INVALIDATED'\s*,"
        r"\s*'EXPIRED'\s*\)",
        update_clause,
        re.IGNORECASE,
    ), "ISSUE322_MISSING_TERMINAL_UPSERT_GUARD"


# ============================================================
# Behavioral tests — isolated JSONL repository
# ============================================================


def _candidate_for_integrity(*, candle=1777294700000, state="READY_TO_TRADE"):
    return SetupCandidate(
        scanner_name="TREND_PULLBACK",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        signal_candle_open_time=candle,
        reference_price=100.0,
        state=state,
    )


@pytest.mark.parametrize(
    "terminal_status",
    ["EXECUTED", "INVALIDATED", "EXPIRED"],
)
def test_jsonl_terminal_setup_not_resurrected(tmp_path, terminal_status):
    path = tmp_path / "setups.jsonl"

    repo = ScannerRepository(backend="jsonl", jsonl_path=str(path))

    original = _candidate_for_integrity(state=terminal_status)

    repo.save_setup(original)

    before = read_records(path)

    assert len(before) == 1
    assert before[0]["status"] == terminal_status

    duplicate = replace(
        original,
        reference_price=101.0,
        state="READY_TO_TRADE",
    )

    repo.save_setup(duplicate)

    after = read_records(path)

    assert len(after) == 1
    assert after[0]["setup_id"] == before[0]["setup_id"]

    assert after[0] == before[0], (
        f"ISSUE322_JSONL_TERMINAL_RESURRECTION: {terminal_status}"
    )


def test_jsonl_new_candle_creates_new_setup(tmp_path):
    path = tmp_path / "setups.jsonl"

    repo = ScannerRepository(backend="jsonl", jsonl_path=str(path))

    first = _candidate_for_integrity(state="EXECUTED")

    repo.save_setup(first)

    next_candle = _candidate_for_integrity(
        candle=first.signal_candle_open_time + 300_000,
    )

    repo.save_setup(next_candle)

    records = read_records(path)

    assert len(records) == 2
    assert len({record["signal_candle_open_time"] for record in records}) == 2

    assert any(record["status"] == "EXECUTED" for record in records)
    assert any(record["status"] == "READY_TO_TRADE" for record in records)
