from app.research.me_r_long_early_mae_v3.identity import source_identity

def test_same_symbol_and_candle_is_stable():
    a = source_identity("BTCUSDT", 123)
    b = source_identity("BTCUSDT", 123)
    assert a == b

def test_symbol_or_candle_changes_identity():
    assert source_identity("BTCUSDT", 123) != source_identity("ETHUSDT", 123)
    assert source_identity("BTCUSDT", 123) != source_identity("BTCUSDT", 124)
