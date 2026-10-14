from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.outcomes import evaluate_baseline
from app.research.me_r_long_early_mae_v3.protocol import VARIANTS, execution_geometry
from app.research.me_r_long_early_mae_v3.variants import evaluate_variant

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
E, S, G = execution_geometry(100)

def candle(i, low=100, high=101, close=100):
    o = T + timedelta(minutes=5 * i)
    return ClosedCandle(o, o + timedelta(minutes=5), 100, high, low, close)

def test_exactly_seven_and_one_primary():
    assert len(VARIANTS) == 7
    assert [v.variant_id for v in VARIANTS if v.is_primary] == ["MAE_15m_050"]

def test_mae_trigger_uses_horizon_close_not_touch():
    candles = [candle(0, low=98), candle(1, low=100)]
    base = evaluate_baseline(candles, T, T + timedelta(minutes=240), E, S, G)
    result = evaluate_variant(VARIANTS[0], candles, T, E, S, G, base)
    assert result.trigger_result == "TRIGGERED"
    assert result.variant_exit_at == T + timedelta(minutes=10)
    assert result.variant_exit_price == 100
    assert result.execution_price_semantics == "RESEARCH_PRICE_PROXY_NOT_VERIFIED_EXECUTABLE"

def test_baseline_sl_tp_precedence_over_mae():
    candles = [candle(0, low=97), candle(1, low=98), candle(2, low=99)]
    base = evaluate_baseline(candles, T, T + timedelta(minutes=240), E, S, G)
    result = evaluate_variant(VARIANTS[0], candles, T, E, S, G, base)
    assert result.trigger_result == "BASELINE_SL_TP_PRECEDENCE"
    assert result.variant_exit_reason == "STOP_LOSS"

def test_normal_and_elevated_costs_are_both_present():
    candles = [candle(0, low=98), candle(1, low=100)]
    base = evaluate_baseline(candles, T, T + timedelta(minutes=240), E, S, G)
    result = evaluate_variant(VARIANTS[0], candles, T, E, S, G, base)
    assert result.net_r_elevated < result.net_r_normal
