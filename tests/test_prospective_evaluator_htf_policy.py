from datetime import datetime, timezone
from types import SimpleNamespace

from app.research.prospective_evaluator import (
    ProspectiveOOSEvaluator,
    _check_tp_sl,
)


def test_htf_frozen_intrabar_policy_stop_first():
    obs = {
        "features": {
            "_frozen_intrabar_policy": "STOP_FIRST",
        }
    }

    assert ProspectiveOOSEvaluator._get_frozen_intrabar_policy(obs) == "STOP_FIRST"


def test_stop_first_marks_sl_before_tp_on_ambiguous_candle():
    signal_time = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
    signal_ts = int(signal_time.timestamp() * 1000)

    candles = [
        SimpleNamespace(
            timestamp=signal_ts + 5 * 60_000,
            high=111.0,
            low=89.0,
        )
    ]

    result = _check_tp_sl(
        candles=candles,
        entry_price=100.0,
        stop_price=90.0,
        target_price=110.0,
        max_minutes=240,
        signal_time=signal_time,
        is_short=False,
        intrabar_policy="STOP_FIRST",
    )

    assert result["ambiguous_intrabar"] is True
    assert result["sl_before_tp"] is True
    assert result["tp_before_sl"] is False
