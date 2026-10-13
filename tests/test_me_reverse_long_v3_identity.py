from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_reverse_long_v3.identity import v3_source_key

UTC = timezone.utc
OPEN = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)


def test_same_candle_same_identity():
    first = v3_source_key("BTCUSDT", "LONG", "1.0.0", OPEN)
    second = v3_source_key("BTCUSDT", "LONG", "1.0.0", OPEN)

    assert first == second
    assert first < 0


def test_other_candle_other_identity():
    assert v3_source_key("BTCUSDT", "LONG", "1.0.0", OPEN) != (
        v3_source_key(
            "BTCUSDT", "LONG", "1.0.0",
            OPEN + timedelta(minutes=5),
        )
    )


def test_other_symbol_other_identity():
    assert v3_source_key("BTCUSDT", "LONG", "1.0.0", OPEN) != (
        v3_source_key("ETHUSDT", "LONG", "1.0.0", OPEN)
    )


def test_other_version_other_identity():
    assert v3_source_key("BTCUSDT", "LONG", "1.0.0", OPEN) != (
        v3_source_key("BTCUSDT", "LONG", "1.0.1", OPEN)
    )


def test_unaligned_trigger_rejected():
    with pytest.raises(ValueError, match="UNALIGNED"):
        v3_source_key(
            "BTCUSDT", "LONG", "1.0.0",
            OPEN + timedelta(seconds=1),
        )
