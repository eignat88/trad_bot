from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models import Candle
from app.research.me_reverse_long_v3.historical_source import (
    fetch_v3_execution_candles,
)
from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SOURCE_VALIDATED,
    SOURCE_INCOMPLETE_COVERAGE,
)

UTC = timezone.utc
ENTRY = datetime(2026, 10, 12, 17, 5, tzinfo=UTC)
DEADLINE = ENTRY + timedelta(minutes=240)


def candles():
    return [
        Candle(
            timestamp=int(
                (ENTRY + timedelta(minutes=i * 5)).timestamp() * 1000
            ),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
        )
        for i in range(48)
    ]


def test_historical_fetch_exact_window():
    source = MagicMock()
    source.fetch_5m_candles.return_value = SimpleNamespace(
        status=SOURCE_VALIDATED,
        candles=candles(),
        reason_code="SOURCE_VALIDATED",
    )

    result, error = fetch_v3_execution_candles(
        source, "BTCUSDT", ENTRY, DEADLINE
    )

    assert error is None
    assert len(result) == 48

    source.fetch_5m_candles.assert_called_once_with(
        symbol="BTCUSDT",
        start_ms=int(ENTRY.timestamp() * 1000),
        end_ms=int(
            (DEADLINE - timedelta(minutes=5)).timestamp() * 1000
        ),
        evaluation_asof_ms=int(DEADLINE.timestamp() * 1000),
    )


def test_incomplete_source_never_returns_candles():
    source = MagicMock()
    source.fetch_5m_candles.return_value = SimpleNamespace(
        status=SOURCE_INCOMPLETE_COVERAGE,
        candles=candles()[:30],
        reason_code="SOURCE_INCOMPLETE_COVERAGE",
    )

    result, error = fetch_v3_execution_candles(
        source, "BTCUSDT", ENTRY, DEADLINE
    )

    assert result == []
    assert error == "SOURCE_INCOMPLETE_COVERAGE"


def test_immature_window_does_not_fetch():
    source = MagicMock()

    result, error = fetch_v3_execution_candles(
        source, "BTCUSDT", ENTRY, DEADLINE - timedelta(seconds=1)
    )

    assert result == []
    assert error == "IMMATURE_WINDOW"
    source.fetch_5m_candles.assert_not_called()


def test_naive_timestamp_rejected():
    source = MagicMock()

    result, error = fetch_v3_execution_candles(
        source, "BTCUSDT", ENTRY.replace(tzinfo=None), DEADLINE
    )

    assert result == []
    assert error == "NAIVE_TIMESTAMP"
    source.fetch_5m_candles.assert_not_called()


def test_real_srr_validator_accepts_complete_v3_window():
    from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
        HistoricalCandleSourceResult,
        validate_source_result,
    )

    data = candles()
    start_ms = int(ENTRY.timestamp() * 1000)
    last_open_ms = int(
        (DEADLINE - timedelta(minutes=5)).timestamp() * 1000
    )

    result = validate_source_result(
        HistoricalCandleSourceResult(
            status=SOURCE_VALIDATED,
            candles=tuple(data),
        ),
        symbol="BTCUSDT",
        requested_start_ms=start_ms,
        requested_end_ms=last_open_ms,
        required_close_ms=last_open_ms,
        evaluation_asof_ms=int(DEADLINE.timestamp() * 1000),
    )

    assert result.status == SOURCE_VALIDATED
    assert len(result.candles) == 48


def test_real_srr_validator_rejects_missing_last_candle():
    from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
        HistoricalCandleSourceResult,
        validate_source_result,
    )

    start_ms = int(ENTRY.timestamp() * 1000)
    last_open_ms = int(
        (DEADLINE - timedelta(minutes=5)).timestamp() * 1000
    )

    result = validate_source_result(
        HistoricalCandleSourceResult(
            status=SOURCE_VALIDATED,
            candles=tuple(candles()[:-1]),
        ),
        symbol="BTCUSDT",
        requested_start_ms=start_ms,
        requested_end_ms=last_open_ms,
        required_close_ms=last_open_ms,
        evaluation_asof_ms=int(DEADLINE.timestamp() * 1000),
    )

    assert result.status == SOURCE_INCOMPLETE_COVERAGE
    assert result.reason_code == "SOURCE_INTERVAL_INCOMPLETE"
