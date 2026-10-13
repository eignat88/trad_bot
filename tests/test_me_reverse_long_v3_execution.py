from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_r_long_early_mae_v2.candles import ClosedCandle
from app.research.me_reverse_long_v3.execution import evaluate_v3_execution
from app.research.me_reverse_long_v3.protocol import V3Geometry

UTC = timezone.utc
START = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)
GEOMETRY = V3Geometry(100.0, 97.5, 103.0)


def candles_with_event(event=None):
    result = []
    for i in range(48):
        start = START + timedelta(minutes=5 * i)
        high, low, close = 101.0, 99.0, 100.0
        if i == 2:
            if event == "TP":
                high = 104.0
            elif event == "SL":
                low = 97.0
            elif event == "BOTH":
                high, low = 104.0, 97.0
        result.append(ClosedCandle(
            start, start + timedelta(minutes=5),
            100.0, high, low, close,
        ))
    return result


@pytest.mark.parametrize("event,expected", [
    ("TP", "TP_FIRST"),
    ("SL", "SL_FIRST"),
    ("BOTH", "AMBIGUOUS_STOP_FIRST"),
    (None, "TIMEOUT"),
])
def test_execution_reasons(event, expected):
    outcome, error = evaluate_v3_execution(
        candles_with_event(event), START, GEOMETRY
    )
    assert error is None
    assert outcome.exit_reason == expected
    assert outcome.net_r_normal > outcome.net_r_elevated


def test_missing_candle_not_finalized():
    candles = candles_with_event("TP")
    candles.pop(20)
    outcome, error = evaluate_v3_execution(
        candles, START, GEOMETRY
    )
    assert outcome is None
    assert error == "MISSING_CANDLE_GAP"


def test_no_partial_window_finalization():
    outcome, error = evaluate_v3_execution(
        candles_with_event()[:20], START, GEOMETRY
    )
    assert outcome is None
    assert error == "INCOMPLETE_WINDOW_END"


def test_invalid_geometry():
    outcome, error = evaluate_v3_execution(
        candles_with_event(), START, V3Geometry(100, 101, 103)
    )
    assert outcome is None
    assert error == "INVALID_GEOMETRY"


def test_gross_r():
    outcome, error = evaluate_v3_execution(
        candles_with_event("TP"), START, GEOMETRY
    )
    assert error is None
    assert outcome.gross_r == pytest.approx(1.2)


def test_stop_first_ambiguity():
    outcome, error = evaluate_v3_execution(
        candles_with_event("BOTH"), START, GEOMETRY
    )
    assert error is None
    assert outcome.ambiguous is True
    assert outcome.gross_r == pytest.approx(-1.0)
