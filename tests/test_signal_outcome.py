import pytest

from app.models import Candle
from app.scanners.models import SetupCandidate
from app.scanners.outcome import SignalOutcome, evaluate_setup_outcome
from app.scanners.outcome_cli import (
    bybit_interval_for_timeframe,
    fetch_outcome_candles,
    process_pending_outcomes,
)


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


def test_bybit_interval_for_timeframe_mapping():
    assert bybit_interval_for_timeframe("5m") == "5"
    assert bybit_interval_for_timeframe("15m") == "15"
    assert bybit_interval_for_timeframe("1h") == "60"
    assert bybit_interval_for_timeframe("4h") == "240"
    with pytest.raises(ValueError, match="unsupported entry timeframe"):
        bybit_interval_for_timeframe("1d")


def test_fetch_outcome_candles_filters_after_signal_time():
    class Client:
        def get_klines(self, symbol, interval, limit):
            assert symbol == "BTCUSDT"
            assert interval == "5"
            assert limit == 200
            return [
                candle(700, high=200, low=1),
                candle(1_000, high=200, low=1),
                candle(1_300, high=101, low=100),
                candle(1_600, high=103, low=101),
            ]

    assert [c.timestamp for c in fetch_outcome_candles(Client(), candidate(), max_bars=2)] == [1_300, 1_600]


def test_process_pending_outcomes_saves_evaluated_rows():
    setup = candidate()

    class Repository:
        def __init__(self):
            self.saved: list[SignalOutcome] = []

        def get_setups_without_outcomes(self, *, limit, min_age_minutes):
            assert limit == 5
            assert min_age_minutes == 10
            return [setup]

        def save_signal_outcome(self, outcome):
            self.saved.append(outcome)

    class Client:
        def get_klines(self, symbol, interval, limit):
            return [
                candle(1_300, high=101.5, low=100.5, close=101),
                candle(1_600, high=103.5, low=101, close=103),
            ]

    repository = Repository()
    evaluated, failed = process_pending_outcomes(
        repository, Client(), limit=5, min_age_minutes=10, max_bars=2,
    )

    assert evaluated == 1
    assert failed == 0
    assert len(repository.saved) == 1
    assert repository.saved[0].first_event == "TP1"


def test_process_pending_outcomes_dry_run_does_not_save():
    class Repository:
        def get_setups_without_outcomes(self, *, limit, min_age_minutes):
            return [candidate()]

        def save_signal_outcome(self, outcome):
            raise AssertionError("dry-run must not save")

    class Client:
        def get_klines(self, symbol, interval, limit):
            return [candle(1_300, high=103.5, low=100.5, close=103)]

    assert process_pending_outcomes(Repository(), Client(), dry_run=True) == (1, 0)
