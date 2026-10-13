from types import SimpleNamespace

import pytest

from app.research.me_reverse_long_v3.closed_context import _closed_only


STEP = 300_000
ASOF = STEP * 100


def candle(index):
    return SimpleNamespace(timestamp=index * STEP)


def test_v3_contiguous_fresh_candles_accepted():
    result = _closed_only(
        [candle(i) for i in range(90, 100)],
        STEP,
        ASOF,
    )
    assert len(result) == 10


def test_v3_gapped_candles_rejected():
    candles = [
        candle(i) for i in range(90, 100)
        if i != 95
    ]

    with pytest.raises(ValueError, match="GAPPED_SOURCE_CANDLES"):
        _closed_only(candles, STEP, ASOF)


def test_v3_stale_candles_rejected():
    candles = [candle(i) for i in range(89, 99)]

    with pytest.raises(ValueError, match="STALE_SOURCE_CANDLES"):
        _closed_only(candles, STEP, ASOF)
