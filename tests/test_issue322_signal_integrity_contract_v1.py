"""Issue #322 — baseline regression contracts.

No real database, network, scanner or production state.
Expected RED on the unmodified base revision.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

from app.db.repository import ScannerRepository


def _source_key_in_process(seed: str) -> int:
    code = """
from app.research.prospective_observer import ProspectiveOOSObserver
print(ProspectiveOOSObserver._make_source_key(
    None,
    "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
    "BTCUSDT",
    "SHORT",
    "2026-11-26T09:00:00+00:00",
))
"""
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = seed
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return int(result.stdout.strip())


def _upsert_sql() -> str:
    source = Path(ScannerRepository._save_pg.__code__.co_filename)
    text = source.read_text(encoding="utf-8")

    start = text.index("    def _save_pg(")
    end = text.index("    def _save_jsonl(", start)

    return text[start:end]


def test_source_key_same_within_one_process():
    """Existing deterministic behaviour within one Python process."""
    from app.research.prospective_observer import ProspectiveOOSObserver

    args = (
        None,
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        "BTCUSDT",
        "SHORT",
        "2026-11-26T09:00:00+00:00",
    )

    assert (
        ProspectiveOOSObserver._make_source_key(*args)
        == ProspectiveOOSObserver._make_source_key(*args)
    )


def test_source_key_survives_restart():
    """Same original event must keep identity across hash seeds."""
    keys = {
        _source_key_in_process("1"),
        _source_key_in_process("2"),
        _source_key_in_process("3"),
    }

    assert len(keys) == 1, (
        f"ISSUE322_UNSTABLE_OOS_IDENTITY: {sorted(keys)}"
    )


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

from dataclasses import replace

import pytest

from app.db.repository import read_records
from app.scanners.models import SetupCandidate


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
def test_jsonl_terminal_setup_not_resurrected(
    tmp_path, terminal_status
):
    path = tmp_path / "setups.jsonl"

    repo = ScannerRepository(
        backend="jsonl",
        jsonl_path=str(path),
    )

    original = _candidate_for_integrity(
        state=terminal_status,
    )

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

    repo = ScannerRepository(
        backend="jsonl",
        jsonl_path=str(path),
    )

    first = _candidate_for_integrity(
        state="EXECUTED",
    )

    repo.save_setup(first)

    next_candle = _candidate_for_integrity(
        candle=first.signal_candle_open_time + 300_000,
    )

    repo.save_setup(next_candle)

    records = read_records(path)

    assert len(records) == 2
    assert len({
        record["signal_candle_open_time"]
        for record in records
    }) == 2

    assert any(
        record["status"] == "EXECUTED"
        for record in records
    )
    assert any(
        record["status"] == "READY_TO_TRADE"
        for record in records
    )
