import pytest

from app.models import Candle
from app.scanners.models import SetupCandidate
from app.scanners.outcome import evaluate_setup_outcome


def candidate(**overrides):
    base = SetupCandidate(
        scanner_name="TREND_PULLBACK",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        signal_candle_open_time=1_000,
        reference_price=101,
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=99,
        target_1=103,
        target_2=105,
    )
    return SetupCandidate(**{**base.__dict__, **overrides})


def candle(ts, open_=101, high=101, low=101, close=101):
    return Candle(ts, open_, high, low, close, 10)


def test_long_outcome_hits_tp1_after_entry():
    setup = candidate()
    outcome = evaluate_setup_outcome(setup, [
        candle(1_000, high=110, low=90),  # signal candle ignored
        candle(1_300, high=101.5, low=100.5, close=101),
        candle(1_600, high=103.2, low=100.8, close=103),
    ])

    assert outcome.entry_touched is True
    assert outcome.first_event == "TP1"
    assert outcome.result_r == pytest.approx(1.0)
    assert outcome.mfe_r == pytest.approx(1.1)
    assert outcome.mae_r == pytest.approx(-0.25)
    assert outcome.bars_to_entry == 1
    assert outcome.bars_to_exit == 2
    assert outcome.entry_price == 101
    assert outcome.exit_price == 103


def test_short_outcome_hits_sl_conservatively_when_stop_and_target_same_candle():
    setup = candidate(
        direction="SHORT",
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=102,
        target_1=98,
        target_2=96,
        reference_price=100,
    )
    outcome = evaluate_setup_outcome(setup, [
        candle(1_300, high=102.5, low=97.5, close=99),
    ])

    assert outcome.entry_touched is True
    assert outcome.first_event == "SL"
    assert outcome.result_r == pytest.approx(-1.0)
    assert outcome.mfe_r == pytest.approx(1.25)
    assert outcome.mae_r == pytest.approx(-1.25)


def test_outcome_no_entry_before_horizon():
    setup = candidate()
    outcome = evaluate_setup_outcome(setup, [
        candle(1_300, high=99.5, low=98, close=99),
        candle(1_600, high=99.8, low=98.5, close=99),
    ])

    assert outcome.entry_touched is False
    assert outcome.first_event == "NO_ENTRY"
    assert outcome.result_r == 0
    assert outcome.bars_to_entry is None
    assert outcome.bars_to_exit is None
    assert outcome.entry_price is None
    assert outcome.exit_price is None


def test_outcome_expires_with_unrealized_r_and_fee_adjustment():
    setup = candidate()
    outcome = evaluate_setup_outcome(setup, [
        candle(1_300, high=101.2, low=100.8, close=101.0),
        candle(1_600, high=102.0, low=100.5, close=102.0),
    ], max_bars=2, fee_slippage_r=0.1)

    assert outcome.first_event == "EXPIRED"
    assert outcome.result_r == pytest.approx(0.5)
    assert outcome.fee_slippage_adjusted_result_r == pytest.approx(0.4)
    assert outcome.bars_to_entry == 1
    assert outcome.bars_to_exit == 2


def test_outcome_rejects_invalid_risk_geometry():
    setup = candidate(invalidation_price=100.5)

    with pytest.raises(ValueError, match="invalid setup risk geometry"):
        evaluate_setup_outcome(setup, [candle(1_300, high=104, low=100)])
