import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/research/srr_short_r_expansion_timeout_source_reconciliation_v1.py"
BASE = ROOT / "audit_db/srr_short_r_expansion_candle_path_integrity_audit_v1_20261008"


@pytest.fixture(scope="module")
def audit_data():
    spec = importlib.util.spec_from_file_location("srr_timeout_reconciliation_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    rows = {
        row["observation_id"]: row
        for row in map(
            module.normalize,
            module.load_csv(BASE / "srr_short_r_expansion_snapshot_clean_utf8.csv"),
        )
    }
    candles = module.load_candles(
        BASE / "offline_replay_01/srr_short_r_expansion_candle_path_integrity_audit_v1_candles.json"
    )
    return module, rows, candles


@pytest.mark.parametrize(
    "observation_id,expected",
    [
        (15489, 0.31883820856057266),
        (15630, 0.2857858205255316),
    ],
)
def test_stored_economics_independent_of_frozen(audit_data, observation_id, expected):
    module, rows, _ = audit_data
    actual = module.economics_from_flags(rows[observation_id])
    assert actual["net_r_normal"] == pytest.approx(expected)


def test_cutoff_tp_is_unverifiable(audit_data):
    module, rows, candles = audit_data
    row = rows[26938]
    result = module.timeline(row, candles[row["symbol"]])

    assert result["frozen_semantics_replay"]["path_class"] == "CUTOFF_BOUNDARY_UNCERTAIN"
    assert result["verdict"] == "BOUNDARY_UNCERTAIN"
    assert result["reason_code"] == "CUTOFF_TP_SL_TIMING_UNKNOWN"


@pytest.mark.parametrize(
    "observation_id,expected_close",
    [(22228, 0.07356), (18944, 90.17)],
)
def test_timeout_uses_confirmed_closed_candle(audit_data, observation_id, expected_close):
    module, rows, candles = audit_data
    row = rows[observation_id]
    result = module.timeline(row, candles[row["symbol"]])

    deployed = result["deployed_semantics_replay"]
    frozen = result["frozen_semantics_replay"]

    assert deployed["timeout_close_ms"] > result["coverage"]["cutoff_time_ms"]
    assert frozen["timeout_close_ms"] <= result["coverage"]["cutoff_time_ms"]
    assert frozen["timeout_close"] == pytest.approx(expected_close)
    assert result["verdict"] == "UNRESOLVED"
    assert result["reason_code"] == "POST_CUTOFF_CLOSE_SELECTED_SOURCE_NOT_PROVEN"


def test_corrected_paired_cohort(audit_data):
    module, rows, candles = audit_data

    computed = []
    for row in rows.values():
        replay = module.frozen_replay(row, candles[row["symbol"]])
        if replay.path_class in {"TP_FIRST", "SL_FIRST", "TIMEOUT", "AMBIGUOUS"}:
            computed.append(row["observation_id"])

    assert len(rows) == 50
    assert len(computed) == 46
    assert 26938 not in computed
    assert not {15928, 20207, 28709}.intersection(computed)
