from unittest.mock import MagicMock

from app.research.me_reverse_long_v3.activation import (
    load_v3_db_activation,
)


def test_v3_activation_sql_error_rolls_back():
    conn = MagicMock()
    cursor = conn.cursor.return_value

    cursor.execute.side_effect = RuntimeError("SQL failure")

    result = load_v3_db_activation(conn)

    assert result == (None, None)
    conn.rollback.assert_called_once()
    cursor.close.assert_called_once()


def test_v3_activation_success_does_not_rollback():
    conn = MagicMock()
    cursor = conn.cursor.return_value

    cursor.fetchone.return_value = ("RUNNING", None)

    result = load_v3_db_activation(conn)

    assert result == ("RUNNING", None)
    conn.rollback.assert_not_called()
    cursor.close.assert_called_once()
