import pytest

from app.research.me_r_long_early_mae_v3.repository import (
    MeEarlyMaeV3Repository,
)


class FakeCursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if sql.lstrip().startswith("UPDATE"):
            assert "%(" not in sql
            assert all(isinstance(p, tuple) for _, p in self.calls)

            allowed = params[4:]
            if self.state not in allowed:
                self.rowcount = 0
            else:
                self.rowcount = 1

    def fetchone(self):
        return (self.state,) if self.state is not None else None


class FakeConnection:
    def __init__(self, state):
        self.cursor_obj = FakeCursor(state)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def repo_for(state):
    conn = FakeConnection(state)
    return MeEarlyMaeV3Repository(conn), conn


@pytest.mark.parametrize("state", ["DETECTED", "ENTRY_PENDING", "ENTRY_FILLED"])
def test_incomplete_coverage_allowed(state):
    repo, conn = repo_for(state)

    repo.mark_source_incomplete(123, "MISSING_BASELINE_CANDLES")

    assert conn.commits == 1
    assert conn.rollbacks == 0

    sql, params = conn.cursor_obj.calls[-1]
    assert "coverage_status=COALESCE(%s, coverage_status)" in sql
    assert params[:4] == (
        None, "INCOMPLETE", "MISSING_BASELINE_CANDLES", 123
    )
    assert state in params[4:]


@pytest.mark.parametrize("state", ["FINALIZED", "ENTRY_NOT_FILLED", "INVALID"])
def test_terminal_source_cannot_be_changed(state):
    repo, conn = repo_for(state)

    with pytest.raises(ValueError, match="SOURCE_STATE_IMMUTABLE_OR_TERMINAL"):
        repo.mark_source_incomplete(123, "TEST")

    assert conn.commits == 0
    assert conn.rollbacks == 1


@pytest.mark.parametrize("operation", ["not_filled", "invalid"])
def test_entry_filled_rejects_status_transition(operation):
    repo, conn = repo_for("ENTRY_FILLED")

    with pytest.raises(ValueError, match="SOURCE_STATE_IMMUTABLE_OR_TERMINAL"):
        if operation == "not_filled":
            repo.mark_source_not_filled(123)
        else:
            repo.mark_source_invalid(123, "TEST")

    assert conn.commits == 0
    assert conn.rollbacks == 1


@pytest.mark.parametrize("state", ["DETECTED", "ENTRY_PENDING"])
def test_pre_entry_state_transitions(state):
    repo, conn = repo_for(state)

    repo.mark_source_not_filled(123, "EXPIRED")

    sql, params = conn.cursor_obj.calls[-1]
    assert params[:4] == ("ENTRY_NOT_FILLED", None, "EXPIRED", 123)
    assert params[4:] == ("DETECTED", "ENTRY_PENDING")
    assert conn.commits == 1


def test_missing_source_rejected():
    repo, conn = repo_for(None)

    with pytest.raises(ValueError, match="SOURCE_STATE_IMMUTABLE_OR_TERMINAL"):
        repo.mark_source_incomplete(123, "TEST")

    assert conn.rollbacks == 1
