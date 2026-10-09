from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.scanners.models import Candle
from app.shadow.me_r_close_location_historical_source import (
    CANDLE_MS,
    HistoricalSourceError,
    HistoricalWindow,
)
from app.shadow.me_r_long_close_location_oos_evaluator import (
    MERLongCLoOosEvaluator,
)

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def make_signal(
    decision=T0,
    signal_id=1,
    price=100.0,
):
    return {
        "signal_id": signal_id,
        "symbol": "BTCUSDT",
        "signal_price": price,
        "decision_time": decision,
        "signal_version": "1.2.0",
        **{
            f"evaluated_{h}m_at": None
            for h in (15, 30, 60, 120, 240)
        },
    }


def make_window(client, symbol, start_ms, end_ms, **kwargs):
    candles = tuple(
        Candle(
            timestamp=ts,
            open=100.0,
            high=103.0,
            low=99.0,
            close=101.0,
            volume=1000.0,
        )
        for ts in range(start_ms, end_ms + CANDLE_MS, CANDLE_MS)
    )

    return HistoricalWindow(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        candles=candles,
    )


def evaluator():
    repo = MagicMock()
    repo.save_outcome_partial.return_value = True
    return MERLongCLoOosEvaluator(repo, MagicMock()), repo


def test_no_future_candle_used():
    obj, repo = evaluator()
    decision = T0 + timedelta(minutes=2)
    signal = make_signal(decision=decision)

    with patch(
        "app.shadow.me_r_long_close_location_oos_evaluator._utc_now"
    ) as clock, patch(
        "app.shadow.me_r_long_close_location_oos_evaluator.fetch_historical_5m",
        side_effect=make_window,
    ) as fetch:
        clock.return_value = T0 + timedelta(minutes=20)

        assert obj._evaluate_signal(signal) is True

    assert fetch.called
    args = fetch.call_args.args
    first_open_ms = args[2]

    # Decision at 12:02 -> first eligible full candle opens 12:05.
    expected = int(
        (T0 + timedelta(minutes=5)).timestamp() * 1000
    )
    assert first_open_ms == expected


def test_240m_finalization():
    obj, repo = evaluator()
    signal = make_signal()

    with patch(
        "app.shadow.me_r_long_close_location_oos_evaluator._utc_now"
    ) as clock, patch(
        "app.shadow.me_r_long_close_location_oos_evaluator.fetch_historical_5m",
        side_effect=make_window,
    ):
        clock.return_value = T0 + timedelta(minutes=245)

        assert obj._evaluate_signal(signal) is True

    saved = repo.save_outcome_partial.call_args.kwargs

    assert saved["is_final"] is True
    assert saved["mfe_240m"] == pytest.approx(3.0)
    assert saved["mae_240m"] == pytest.approx(1.0)
    assert saved["mfe_240m_r"] == pytest.approx(1.2)
    assert saved["mae_240m_r"] == pytest.approx(0.4)


def test_no_mature_horizon_no_write():
    obj, repo = evaluator()
    signal = make_signal()

    with patch(
        "app.shadow.me_r_long_close_location_oos_evaluator._utc_now"
    ) as clock:
        clock.return_value = T0 + timedelta(minutes=10)
        assert obj._evaluate_signal(signal) is False

    repo.save_outcome_partial.assert_not_called()


def test_existing_horizon_not_recomputed():
    obj, repo = evaluator()
    signal = make_signal()
    signal["evaluated_15m_at"] = T0 + timedelta(minutes=20)

    with patch(
        "app.shadow.me_r_long_close_location_oos_evaluator._utc_now"
    ) as clock, patch(
        "app.shadow.me_r_long_close_location_oos_evaluator.fetch_historical_5m",
        side_effect=make_window,
    ):
        clock.return_value = T0 + timedelta(minutes=35)
        assert obj._evaluate_signal(signal) is True

    saved = repo.save_outcome_partial.call_args.kwargs

    assert "mfe_15m" not in saved
    assert "mfe_30m" in saved
    assert saved["is_final"] is False


def test_incomplete_source_no_write():
    obj, repo = evaluator()

    with patch(
        "app.shadow.me_r_long_close_location_oos_evaluator._utc_now"
    ) as clock, patch(
        "app.shadow.me_r_long_close_location_oos_evaluator.fetch_historical_5m",
        side_effect=HistoricalSourceError("INCOMPLETE_CANDLE_COVERAGE"),
    ):
        clock.return_value = T0 + timedelta(minutes=20)

        with pytest.raises(HistoricalSourceError):
            obj._evaluate_signal(make_signal())

    repo.save_outcome_partial.assert_not_called()


def test_legacy_signal_is_skipped():
    obj, repo = evaluator()
    signal = make_signal()
    signal["signal_version"] = "1.0.0"

    assert obj._evaluate_signal(signal) is False
    repo.save_outcome_partial.assert_not_called()


def test_updated_counter_requires_successful_save():
    obj, repo = evaluator()
    repo.get_eligible_signals.return_value = [
        make_signal(signal_id=1),
        make_signal(signal_id=2),
    ]

    with patch.object(
        obj,
        "_evaluate_signal",
        side_effect=[True, False],
    ):
        stats = obj.evaluate_pending()

    assert stats == {
        "checked": 2,
        "updated": 1,
        "errors": 0,
    }
