from datetime import datetime, timezone

from tools.research.me_rl_v1_entry_geometry_counterfactual_audit_v1 import (
    Candle, Observation, evaluate_model_a, evaluate_model_b, validate_candles,
)

OPEN0 = 1790100000000


def candle(offset, high, low, close=None):
    value = close if close is not None else (high + low) / 2
    return Candle(
        timestamp=OPEN0 + offset * 300000,
        open=value,
        high=high,
        low=low,
        close=value,
    )


def obs(**kw):
    base = dict(
        observation_id=1, experiment_id="ME_RL_V1_GENERIC_V1",
        scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", symbol="BTCUSDT",
        direction="LONG", setup_id="setup", signal_time=datetime.fromtimestamp((OPEN0 + 300000) / 1000.0, timezone.utc),
        signal_candle_open_time=OPEN0, reference_price=102.0,
        entry_zone_low=101.0, entry_zone_high=103.0,
        invalidation_price=99.5, target_1=103.0, target_2=None, features={},
        market_regime="TREND_UP",
    )
    base.update(kw)
    return Observation(**base)


def test_signal_candle_and_complete_future_window():
    candles = [candle(0, 101, 100, 100.5), candle(1, 104, 99, 102)]
    a = evaluate_model_a(obs(), candles)
    b = evaluate_model_b(obs(), candles)
    assert a.signal_close == 100.5
    assert a.coverage_complete is False
    assert a.status == "INCOMPLETE"
    assert a.reason_code == "MISSING_CANDLES_47"
    assert b.signal_close == 100.5
    assert b.entry_price == 100.5


def test_model_a_invalid_recorded_geometry_excluded():
    outcome = evaluate_model_a(
        obs(invalidation_price=105.0, target_1=101.0),
        [candle(0, 101, 100, 100.5)],
    )
    assert outcome.status == "INVALID"
    assert outcome.reason_code == "INVALID_RECORDED_LONG_GEOMETRY"
    assert outcome.gross_r is None


def test_model_b_requires_verified_signal_candle():
    outcome = evaluate_model_b(obs(), [candle(1, 104, 99)])
    assert outcome.status == "INVALID"
    assert outcome.reason_code == "ENTRY_SOURCE_UNVERIFIED"
    assert outcome.gross_r is None


def test_no_lookahead_first_eligible_candle():
    candles = [candle(0, 200, 100.6, 150), candle(1, 200, 147, 150)]
    candles.extend(candle(i, 100, 99, 100) for i in range(2, 49))
    outcome = evaluate_model_b(obs(), candles)
    assert outcome.signal_close == 150.0
    assert outcome.coverage_complete is True
    assert outcome.exit_type == "TP_FIRST"
    assert outcome.status == "FINALIZED"
    assert outcome.gross_r > 0


def test_stop_first_intrabar_collision_policy():
    candles = [candle(0, 101, 100, 100.5), candle(1, 110, 90, 105)]
    candles.extend(candle(i, 100, 99, 100) for i in range(2, 49))
    outcome = evaluate_model_b(obs(), candles)
    assert outcome.coverage_complete is True
    assert outcome.status == "FINALIZED"
    assert outcome.exit_type == "INTRABAR_AMBIGUOUS"
    assert outcome.gross_r == -1.0


def test_validate_candles_rejects_nonfinite_and_misaligned():
    assert validate_candles([Candle(1, open=1, high=0.5, low=0.5, close=0.5)]) == ["INVALID_5M_ALIGNMENT"]
    assert validate_candles([Candle(1790100000000, open=1, high=0.8, low=0.5, close=0.5)]) == ["INVALID_HIGH_OHLC"]


def test_tp_first_even_if_sl_hit_later():
    candles = [candle(0, 101, 100, 100.5)]
    candles.append(candle(1, 104, 100, 102))
    candles.append(candle(2, 101, 98, 100))
    candles.extend(candle(i, 101, 100, 100.5) for i in range(3, 49))

    outcome = evaluate_model_b(obs(), candles)

    assert outcome.coverage_complete is True
    assert outcome.status == "FINALIZED"
    assert outcome.exit_type == "TP_FIRST"
    assert outcome.gross_r > 0


def test_sl_first_even_if_tp_hit_later():
    candles = [candle(0, 101, 100, 100.5)]
    candles.append(candle(1, 101, 97, 100))
    candles.append(candle(2, 105, 100, 102))
    candles.extend(candle(i, 101, 100, 100.5) for i in range(3, 49))

    outcome = evaluate_model_b(obs(), candles)

    assert outcome.coverage_complete is True
    assert outcome.status == "FINALIZED"
    assert outcome.exit_type == "SL_FIRST"
    assert outcome.gross_r == -1.0


def test_missing_middle_candle_blocks_finalization():
    candles = [candle(0, 101, 100, 100.5)]
    candles.extend(
        candle(i, 101, 100, 100.5)
        for i in range(1, 49)
        if i != 24
    )

    outcome = evaluate_model_b(obs(), candles)

    assert outcome.coverage_complete is False
    assert outcome.status == "INCOMPLETE"
    assert outcome.gross_r is None
    assert outcome.fully_net_r is None


def test_complete_no_hit_path_times_out():
    candles = [candle(0, 101, 100, 100.5)]
    candles.extend(
        candle(i, 101, 100, 100.5)
        for i in range(1, 49)
    )

    outcome = evaluate_model_b(obs(), candles)

    assert outcome.coverage_complete is True
    assert outcome.status == "FINALIZED"
    assert outcome.exit_type == "TIMEOUT"


def test_duplicate_future_candle_blocks_finalization():
    candles = [candle(0, 101, 100, 100.5)]
    candles.extend(candle(i, 101, 100, 100.5) for i in range(1, 49))
    candles.append(candle(12, 110, 90, 105))

    outcome = evaluate_model_b(obs(), candles)

    assert outcome.status == "INCOMPLETE"
    assert outcome.coverage_complete is False
    assert outcome.reason_code == "SOURCE_INVALID_DUPLICATE_CANDLE"
    assert outcome.gross_r is None


def test_invalid_future_ohlc_blocks_finalization():
    candles = [candle(0, 101, 100, 100.5)]
    candles.extend(candle(i, 101, 100, 100.5) for i in range(1, 49))
    candles[10] = candle(10, 99, 100, 100)

    outcome = evaluate_model_b(obs(), candles)

    assert outcome.status == "INCOMPLETE"
    assert outcome.coverage_complete is False
    assert outcome.reason_code.startswith("SOURCE_INVALID_")
    assert outcome.gross_r is None
