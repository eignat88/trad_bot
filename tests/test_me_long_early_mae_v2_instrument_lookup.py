from unittest.mock import MagicMock

from app.research.me_r_long_early_mae_v2.repository import (
    MeEarlyMaeV2Repository,
)


def test_existing_instrument_lookup_is_read_only():
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = (123,)

    repository = MeEarlyMaeV2Repository(connection)

    assert repository.ensure_instrument("BTCUSDT") == 123

    query, params = cursor.execute.call_args.args
    assert "SELECT instrument_id FROM dds.instrument" in query
    assert params == ("BTCUSDT",)
    assert "INSERT" not in query.upper()

    connection.commit.assert_not_called()
    connection.rollback.assert_called_once()


def test_unknown_instrument_returns_none_without_insert():
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = None

    repository = MeEarlyMaeV2Repository(connection)

    assert repository.ensure_instrument("UNKNOWNUSDT") is None

    connection.commit.assert_not_called()
    connection.rollback.assert_called_once()


def test_instrument_lookup_error_rolls_back():
    connection = MagicMock()
    connection.cursor.return_value.execute.side_effect = RuntimeError("DB error")

    repository = MeEarlyMaeV2Repository(connection)

    try:
        repository.ensure_instrument("BTCUSDT")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected database error")

    connection.commit.assert_not_called()
    connection.rollback.assert_called_once()
