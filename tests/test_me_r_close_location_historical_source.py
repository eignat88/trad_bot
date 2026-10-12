from unittest.mock import MagicMock

import pytest

from app.shadow.me_r_close_location_historical_source import (
    CANDLE_MS,
    HistoricalSourceError,
    fetch_historical_5m,
)

T0 = 1790000100000
T0 = T0 // CANDLE_MS * CANDLE_MS


def row(t, high="102", low="98"):
    return [str(t), "100", high, low, "100", "1000", "0"]


def client_with(rows):
    client = MagicMock()
    client._public_get.return_value = {
        "retCode": 0,
        "result": {"list": rows},
    }
    return client


def test_complete_window():
    client = client_with([row(T0 + CANDLE_MS), row(T0)])

    result = fetch_historical_5m(
        client, "BTCUSDT",
        T0, T0 + CANDLE_MS,
        asof_ms=T0 + 3 * CANDLE_MS,
    )

    assert len(result.candles) == 2
    assert result.candles[0].timestamp == T0
    assert result.candles[1].timestamp == T0 + CANDLE_MS


def test_missing_candle_fails_closed():
    client = client_with([row(T0)])

    with pytest.raises(HistoricalSourceError, match="INCOMPLETE"):
        fetch_historical_5m(
            client, "BTCUSDT",
            T0, T0 + CANDLE_MS,
            asof_ms=T0 + 3 * CANDLE_MS,
        )


def test_unclosed_candle_rejected():
    client = client_with([])

    with pytest.raises(HistoricalSourceError, match="NOT_FULLY_CLOSED"):
        fetch_historical_5m(
            client, "BTCUSDT",
            T0, T0 + CANDLE_MS,
            asof_ms=T0 + CANDLE_MS,
        )

    client._public_get.assert_not_called()


def test_duplicate_candle_rejected():
    client = client_with([row(T0), row(T0)])

    with pytest.raises(HistoricalSourceError, match="DUPLICATE"):
        fetch_historical_5m(
            client, "BTCUSDT",
            T0, T0,
            asof_ms=T0 + 2 * CANDLE_MS,
        )


def test_invalid_ohlc_rejected():
    client = client_with([row(T0, high="99")])

    with pytest.raises(HistoricalSourceError, match="INVALID_OHLC"):
        fetch_historical_5m(
            client, "BTCUSDT",
            T0, T0,
            asof_ms=T0 + 2 * CANDLE_MS,
        )


def test_bounded_request():
    client = client_with([row(T0)])

    fetch_historical_5m(
        client, "BTCUSDT",
        T0, T0,
        asof_ms=T0 + 2 * CANDLE_MS,
    )

    kwargs = client._public_get.call_args.kwargs
    assert kwargs["category"] == "linear"
    assert kwargs["interval"] == "5"
    assert kwargs["start"] == T0
    assert kwargs["end"] == T0


def test_negative_volume_rejected():
    client = client_with([
        [str(T0), "100", "102", "98", "100", "-1", "0"]
    ])

    with pytest.raises(HistoricalSourceError, match="INVALID_OHLC"):
        fetch_historical_5m(
            client, "BTCUSDT", T0, T0,
            asof_ms=T0 + 2 * CANDLE_MS,
        )


def test_nan_volume_rejected():
    client = client_with([
        [str(T0), "100", "102", "98", "100", "nan", "0"]
    ])

    with pytest.raises(HistoricalSourceError, match="INVALID_OHLC"):
        fetch_historical_5m(
            client, "BTCUSDT", T0, T0,
            asof_ms=T0 + 2 * CANDLE_MS,
        )


def test_multiple_pages_complete():
    client = MagicMock()

    def fetch(endpoint, **kwargs):
        start = kwargs["start"]
        end = kwargs["end"]
        candles = [
            row(t)
            for t in range(start, end + CANDLE_MS, CANDLE_MS)
        ]
        candles.reverse()
        return {
            "retCode": 0,
            "result": {"list": candles},
        }

    client._public_get.side_effect = fetch

    result = fetch_historical_5m(
        client,
        "BTCUSDT",
        T0,
        T0 + 240 * CANDLE_MS,
        asof_ms=T0 + 242 * CANDLE_MS,
    )

    assert len(result.candles) == 241
    assert client._public_get.call_count == 2

    timestamps = [c.timestamp for c in result.candles]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == 241
