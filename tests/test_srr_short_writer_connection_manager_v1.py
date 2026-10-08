import pytest

from app.research.srr_short_writer_connection_manager import (
    open_srr_outcome_writer,
)


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def kwargs(factory, reader=None):
    return dict(
        host="127.0.0.1",
        port=5432,
        database="trad_bot_srr_persistence_it_20261008",
        user="postgres",
        reader_conn=reader,
        connect_factory=factory,
    )


def test_writer_uses_independent_connection():
    reader = FakeConnection()
    writer_conn = FakeConnection()

    with open_srr_outcome_writer(
        **kwargs(lambda **_: writer_conn, reader)
    ) as writer:
        assert writer._conn is writer_conn
        assert writer._conn is not reader

    assert writer_conn.closed
    assert not reader.closed


def test_writer_exception_rolls_back_and_closes():
    conn = FakeConnection()

    with pytest.raises(ValueError, match="test failure"):
        with open_srr_outcome_writer(
            **kwargs(lambda **_: conn)
        ):
            raise ValueError("test failure")

    assert conn.rollbacks == 1
    assert conn.closed


def test_shared_connection_rejected_without_touching_reader():
    reader = FakeConnection()

    with pytest.raises(RuntimeError, match="independent"):
        with open_srr_outcome_writer(
            **kwargs(lambda **_: reader, reader)
        ):
            pass

    assert reader.rollbacks == 0
    assert not reader.closed


def test_connection_failure_propagates():
    def failing_factory(**_):
        raise ConnectionError("unavailable")

    with pytest.raises(ConnectionError, match="unavailable"):
        with open_srr_outcome_writer(
            **kwargs(failing_factory)
        ):
            pass


def test_normal_exit_rolls_back_and_closes():
    conn = FakeConnection()

    with open_srr_outcome_writer(
        **kwargs(lambda **_: conn)
    ):
        pass

    assert conn.rollbacks == 1
    assert conn.closed


def test_close_failure_does_not_hide_original_error():
    class BrokenCloseConnection(FakeConnection):
        def close(self):
            self.closed = True
            raise OSError("close failed")

    conn = BrokenCloseConnection()

    with pytest.raises(ValueError, match="original failure"):
        with open_srr_outcome_writer(
            **kwargs(lambda **_: conn)
        ):
            raise ValueError("original failure")

    assert conn.rollbacks == 1
    assert conn.closed


def test_close_failure_without_original_error_propagates():
    class BrokenCloseConnection(FakeConnection):
        def close(self):
            self.closed = True
            raise OSError("close failed")

    conn = BrokenCloseConnection()

    with pytest.raises(OSError, match="close failed"):
        with open_srr_outcome_writer(
            **kwargs(lambda **_: conn)
        ):
            pass

    assert conn.rollbacks == 1
    assert conn.closed
