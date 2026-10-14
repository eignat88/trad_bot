from datetime import datetime, timezone
from app.research.me_r_long_early_mae_v3.identity import source_identity

T = datetime(2026, 1, 1, tzinfo=timezone.utc)

def test_same_symbol_and_signal_candle_is_stable():
    a = source_identity("BTCUSDT", "ME_R_LONG_CLOSED_V1", T)
    b = source_identity("BTCUSDT", "ME_R_LONG_CLOSED_V1", T)
    assert a == b

def test_symbol_source_identity_or_candle_changes_identity():
    assert source_identity("BTCUSDT", "ME_R_LONG_CLOSED_V1", T) != source_identity("ETHUSDT", "ME_R_LONG_CLOSED_V1", T)
    assert source_identity("BTCUSDT", "ME_R_LONG_CLOSED_V1", T) != source_identity("BTCUSDT", "OTHER", T)
    assert source_identity("BTCUSDT", "ME_R_LONG_CLOSED_V1", T) != source_identity("BTCUSDT", "ME_R_LONG_CLOSED_V1", datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc))
