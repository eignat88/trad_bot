"""Regression fixture from read-only Bybit QUSDT / 27 Oct 2026."""
import csv
from pathlib import Path
from tools.research.me_short_a_execution_alignment_counterfactual_v1 import audit, evaluate

FIX = Path(__file__).parent / "fixtures"

def data(filename):
    with (FIX / filename).open(newline="") as file:
        return list(csv.DictReader(file))

def test_qusdt_2_tp_1_sl_with_fixed_absolute_stop_and_target():
    result = audit(data("me_short_a_issue206_qusdt_signals.csv"),
                   data("me_short_a_issue206_qusdt_candles.csv"))
    actual = [x["executable"] for x in result]
    assert [x["status"] for x in actual] == ["FINAL"] * 3
    assert [x["path"] for x in actual] == ["TP", "TP", "SL"]
    assert [x["exit_candle"][11:16] for x in actual] == ["06:30", "06:30", "03:25"]
    assert round(sum(float(x["net_r"]) for x in actual), 2) == 3.26
    # Control must stay untouched (separate frozen price normalization).
    assert all(x["frozen"]["status"] == "FINAL" for x in result)

def test_missing_history_cannot_be_counted_as_profit():
    s = data("me_short_a_issue206_qusdt_signals.csv")[0]
    assert evaluate(s, [], entry_key="estimated_fill")["status"] == "TIMEOUT_CLOSE_UNAVAILABLE"

def test_missing_gap_cannot_be_skipped():
    s = data("me_short_a_issue206_qusdt_signals.csv")[0]
    candles = data("me_short_a_issue206_qusdt_candles.csv")
    candles = [c for c in candles if "T03:20:" not in c["open_time"]]
    assert evaluate(s, candles, entry_key="estimated_fill")["status"] == "MISSING_CANDLE"

def test_stop_first_ambiguous():
    s = data("me_short_a_issue206_qusdt_signals.csv")[0]
    c = dict(data("me_short_a_issue206_qusdt_candles.csv")[1])
    c.update(high="0.24", low="0.15")
    assert evaluate(s, [c], entry_key="estimated_fill")["path"] == "AMBIGUOUS_STOP_FIRST"
