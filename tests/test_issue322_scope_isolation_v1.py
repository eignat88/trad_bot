"""Issue #322 — scope isolation and ME_SHORT lifecycle (Tasks B, E, F).

The new deterministic candle identity is applied ONLY to the
``MOMENTUM_EXHAUSTION`` / ``SHORT`` family (Task B).  This module pins the
boundary:

* every OTHER scanner family keeps the frozen legacy identity and receives NO
  added fail-closed — even when a candle lineage is absent or present;
* no SRR identity-cutover gate is introduced (that was an out-of-scope change to
  an unrelated family; removed per Tasks B / F);
* terminal ME_SHORT experiments (``CANCELLED`` / ``COMPLETED``) are never
  reactivated or captured (Task E), and the real registry stays terminal.

Offline only — isolated mock connection, no real DB / network / production state.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.research.prospective_observer import ProspectiveOOSObserver
from app.research.scanner_signal_integrity_observer import (
    ScannerSignalIntegrityObserver,
)

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)
CANDLE = 1795680000000


def _observer(registry):
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (7,)
    return ScannerSignalIntegrityObserver(conn, registry), conn, cursor


def _observe(observer, *, scanner_name, direction, candle=None, entry_tf="5m"):
    return observer.observe(
        scanner_name=scanner_name,
        direction=direction,
        symbol="BTCUSDT",
        signal_time=START,
        entry_timeframe=entry_tf if candle is not None else None,
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


# ---------------------------------------------------------------------------
# Task B: other families keep legacy identity and no added fail-closed
# ---------------------------------------------------------------------------


def _legacy_key(scanner_name, direction):
    # The frozen base identity, computed in-process (stable within one process).
    return ProspectiveOOSObserver._make_source_key(
        None, scanner_name, "BTCUSDT", direction, START
    )


def test_non_me_short_keeps_legacy_identity_without_candle():
    registry = {
        "BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1": {
            "scanner_name": "BREAKOUT_RETEST",
            "direction": "LONG",
            "status": "RUNNING",
        }
    }
    observer, _, cursor = _observer(registry)

    with (
        patch.object(observer, "_is_db_active", return_value=True),
        patch.object(observer, "_is_direction_active", return_value=True),
    ):
        _observe(
            observer,
            scanner_name="BREAKOUT_RETEST",
            direction="LONG",
            candle=None,
        )

    ids = _insert_source_ids(cursor)
    assert len(ids) == 1, "non-ME_SHORT must not fail-closed without a candle"
    assert ids[0] == _legacy_key("BREAKOUT_RETEST", "LONG")
    assert ids[0] != ScannerSignalIntegrityObserver._make_candle_source_key(
        "BREAKOUT_RETEST", "BTCUSDT", "LONG", "5m", CANDLE
    )


def test_non_me_short_ignores_candle_for_identity():
    """Passing a candle must NOT switch another family to candle identity."""
    registry = {
        # Generic id -> plain insert path; the SRR scanner family is what
        # matters here (it is NOT ME_SHORT, so it must keep legacy identity).
        "SRR_IDENTITY_SCOPE_V1": {
            "scanner_name": "SUPPORT_RESISTANCE_REACTION",
            "direction": "LONG",
            "status": "RUNNING",
        }
    }
    observer, _, cursor = _observer(registry)

    with (
        patch.object(observer, "_is_db_active", return_value=True),
        patch.object(observer, "_is_direction_active", return_value=True),
    ):
        _observe(
            observer,
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            direction="LONG",
            candle=CANDLE,
        )

    ids = _insert_source_ids(cursor)
    assert len(ids) == 1
    assert ids[0] == _legacy_key("SUPPORT_RESISTANCE_REACTION", "LONG")


def test_no_srr_identity_cutover_gate_is_introduced():
    """The out-of-scope SRR cutover fail-closed must not exist (Tasks B / F)."""
    source = (
        ROOT / "app/research/scanner_signal_integrity_observer.py"
    ).read_text(encoding="utf-8")
    frozen = (
        ROOT / "app/research/prospective_observer.py"
    ).read_text(encoding="utf-8")

    assert "SRR_OOS_IDENTITY_CUTOVER_TS" not in source
    assert "_srr_identity_cutover_allows" not in source
    assert "SRR_OOS_IDENTITY_CUTOVER_TS" not in frozen


# ---------------------------------------------------------------------------
# Task E: terminal ME_SHORT experiments are never captured / reactivated
# ---------------------------------------------------------------------------


def test_terminal_me_short_experiments_are_not_captured():
    for status in ("CANCELLED", "COMPLETED"):
        registry = {
            "ME_SHORT_GEOM_A_V1": {
                "scanner_name": "MOMENTUM_EXHAUSTION",
                "direction": "SHORT",
                "status": status,
            }
        }
        observer, _, cursor = _observer(registry)

        with (
            patch.object(observer, "_is_db_active", return_value=True),
            patch.object(observer, "_is_direction_active", return_value=True),
        ):
            _observe(
                observer,
                scanner_name="MOMENTUM_EXHAUSTION",
                direction="SHORT",
                candle=CANDLE,
            )

        assert _insert_source_ids(cursor) == [], (
            f"terminal ME_SHORT experiment reactivated (status={status})"
        )


def test_real_registry_me_short_entries_remain_terminal():
    """Guard: the fix must not flip the real ME_SHORT lifecycle to active."""
    registry = json.loads(
        (ROOT / "app/research/prospective_registry.json").read_text()
    )
    statuses = {
        exp["experiment_id"]: exp.get("status")
        for exp in registry["experiments"]
        if exp["experiment_id"].startswith("ME_SHORT_GEOM_")
    }

    assert statuses.get("ME_SHORT_GEOM_A_V1") == "CANCELLED"
    assert statuses.get("ME_SHORT_GEOM_B_V1") == "CANCELLED"
    assert statuses.get("ME_SHORT_GEOM_C_V1") == "CANCELLED"
    assert statuses.get("ME_SHORT_GEOM_A_EXECUTION_V1") == "COMPLETED"
