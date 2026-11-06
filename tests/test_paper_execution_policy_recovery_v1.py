"""Requirement 6: existing Paper restart recovery reads the execution policy.

Before this fix ``PaperTradingEngine._load_open_trades`` restored every
trading fact EXCEPT ``execution_policy`` / ``execution_policy_version`` /
``planned_exit_at``, so a fixed-horizon paper trade silently fell back to
DEFAULT behaviour after a restart (its original planned exit was lost).

This suite pins the full round-trip:

* engine recovery restores all three fields (and honours the restored
  ``planned_exit_at`` for the FIXED_HORIZON time exit — even when
  ``entered_at + hold_minutes`` would still be in the future);
* recovery is READ-ONLY (no writes are issued while loading);
* legacy rows without the columns recover with honest defaults;
* ``ScannerRepository.get_open_paper_trades`` / ``save_paper_trade`` read and
  persist the columns when they exist and fall back to the exact pre-084 SQL
  shape when a capability probe cannot prove they exist (runners never call
  ``ensure_schema``);
* migration 084 / schema.sql are additive-only (old paper trades untouched).

No PostgreSQL is required by this suite — fake connections only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.db.repository import ScannerRepository
from app.paper.engine import PaperTradeRecord, PaperTradingEngine, _parse_planned_exit

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)

# Column sets probed on dds.paper_trade.
ALL_POLICY_COLUMNS = ["execution_policy", "execution_policy_version", "planned_exit_at"]
NO_POLICY_COLUMNS: list[str] = []
ONLY_038_COLUMNS = ["execution_policy", "planned_exit_at"]  # migration 038, no 084


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _settings(**overrides) -> Settings:
    defaults = dict(
        initial_balance=10_000.0,
        risk_per_trade=0.01,
        max_symbol_exposure=1.0,
        max_open_positions=10,
        taker_fee=0.0,
        slippage_percent=0.0,
        execution_policies={
            "MOMENTUM_EXHAUSTION": {
                "SHORT": {
                    "policy": "FIXED_HORIZON_V1",
                    "enabled": True,
                    "hold_minutes": 240,
                    "dca_enabled": False,
                    "trailing_enabled": False,
                    "breakeven_enabled": False,
                    "tp_enabled": False,
                    "expiry_enabled": False,
                },
            },
        },
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    from app.config.settings import _load_execution_policies

    _load_execution_policies(settings, defaults)
    return settings


def _open_row(**overrides) -> dict:
    """A dds.paper_trade OPEN row as produced AFTER migration 038+084."""
    row = {
        "trade_id": 7,
        "setup_id": "setup-7",
        "symbol": "BTCUSDT",
        "scanner_name": "MOMENTUM_EXHAUSTION",
        "direction": "SHORT",
        "score": 38.0,
        "entry_price": 101.0,
        "entry_fee": 0.05555,
        "stop_price": 110.0,
        "target_1": 85.0,
        "target_2": None,
        "entry_timeframe": "5m",
        "position_size": 5.0,
        "risk_usdt": 45.0,
        "balance_before": 10_000.0,
        "market_regime": "RANGE",
        "funding_paid": 0.0,
        "funding_periods": 0,
        "entered_at": T0,
        "entry_market_price": 101.0,
        "slippage": 0.0,
        "mfe": 0.0,
        "mae": 0.0,
        "execution_policy": "FIXED_HORIZON_V1",
        "execution_policy_version": "FIXED_HORIZON_V1@2026-01",
        # The ORIGINAL plan (persisted at entry — shorter than hold_minutes
        # here on purpose, so the test can tell "restored" from "recomputed").
        "planned_exit_at": T0 + timedelta(minutes=2),
        "dca_enabled": False,
    }
    row.update(overrides)
    return row


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _RecoveryRepo:
    """Fake repository for PaperTradingEngine startup (read-only audit)."""

    def __init__(self, open_rows=None, account_snapshot=None):
        self.open_rows = list(open_rows or [])
        self.account_snapshot = account_snapshot
        self.calls: list[str] = []
        self.closed: list[dict] = []
        self.saved: list[PaperTradeRecord] = []
        self.risk_state = {"daily_loss_usdt": 0.0, "consecutive_losses": 0}
        self.safety_gate_state = {
            "is_blocked": False, "reason": None, "blocked_since": None,
        }

    def get_open_paper_trades(self):
        self.calls.append("get_open_paper_trades")
        return list(self.open_rows)

    def get_paper_risk_state(self):
        self.calls.append("get_paper_risk_state")
        return self.risk_state

    def get_paper_safety_gate_state(self):
        self.calls.append("get_paper_safety_gate_state")
        return self.safety_gate_state

    def set_paper_safety_gate_mode(self, mode):
        self.calls.append(f"set_paper_safety_gate_mode:{mode}")

    def insert_paper_safety_event(self, event):
        self.calls.append("insert_paper_safety_event")

    def get_latest_paper_account_snapshot(self):
        self.calls.append("get_latest_paper_account_snapshot")
        return self.account_snapshot

    def save_paper_trade(self, trade):
        self.calls.append("save_paper_trade")
        self.saved.append(trade)
        return 1

    def close_paper_trade(self, **kwargs):
        self.calls.append("close_paper_trade")
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        self.calls.append("update_paper_trade_funding")


# ---------------------------------------------------------------------------
# Engine-side recovery (the requirement itself)
# ---------------------------------------------------------------------------

class TestEngineRecovery:
    def test_recovery_restores_policy_version_and_planned_exit(self, caplog):
        repo = _RecoveryRepo(open_rows=[_open_row()])
        with caplog.at_level("INFO", logger="app.paper.engine"):
            engine = PaperTradingEngine(_settings(), repo, clock=_Clock(T0))

        trade = engine.open_trades["BTCUSDT"]
        assert trade.execution_policy == "FIXED_HORIZON_V1"
        assert trade.execution_policy_version == "FIXED_HORIZON_V1@2026-01"
        assert trade.planned_exit_at == T0 + timedelta(minutes=2)
        # Auditable recovery log for operators.
        assert any(
            "paper recovery: restored execution_policy=FIXED_HORIZON_V1" in r.getMessage()
            for r in caplog.records
        )

    def test_restored_planned_exit_drives_fixed_horizon_exit(self):
        """The persisted plan wins: entered_at+240m is still in the FUTURE."""
        planned = T0 + timedelta(minutes=2)  # persisted original plan
        row = _open_row(planned_exit_at=planned)
        # entered_at + hold_minutes(240) = T0+240min — must NOT be the driver.
        now = T0 + timedelta(minutes=5)
        repo = _RecoveryRepo(open_rows=[row])
        engine = PaperTradingEngine(_settings(), repo, clock=_Clock(now))

        trade = engine.open_trades["BTCUSDT"]
        assert trade.planned_exit_at == planned
        assert now < trade.entered_at + timedelta(minutes=240)

        # Price far from stop/target → only the restored time plan can close.
        closed = engine.check_exits({"BTCUSDT": 100.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "FIXED_HORIZON"
        assert closed[0].trade_id == 7

    def test_without_planned_exit_no_time_exit_before_horizon(self):
        """Control: DEFAULT rows keep their legacy behaviour (no early exit)."""
        row = _open_row(
            execution_policy="DEFAULT",
            execution_policy_version="",
            planned_exit_at=None,
        )
        repo = _RecoveryRepo(open_rows=[row])
        engine = PaperTradingEngine(_settings(), repo, clock=_Clock(T0 + timedelta(minutes=5)))
        trade = engine.open_trades["BTCUSDT"]
        assert trade.execution_policy == "DEFAULT"
        assert trade.execution_policy_version == ""
        assert trade.planned_exit_at is None

        closed = engine.check_exits({"BTCUSDT": 100.0})
        assert closed == []
        assert repo.closed == []

    def test_legacy_row_without_policy_columns_recovers_with_defaults(self):
        """A pre-038 row (no policy keys at all) must still recover."""
        row = _open_row()
        for key in ("execution_policy", "execution_policy_version", "planned_exit_at"):
            row.pop(key)
        repo = _RecoveryRepo(open_rows=[row])
        engine = PaperTradingEngine(_settings(), repo, clock=_Clock(T0))

        trade = engine.open_trades["BTCUSDT"]
        assert trade.execution_policy == "DEFAULT"
        assert trade.execution_policy_version == ""
        assert trade.planned_exit_at is None
        # Recovery never crashes and never re-persists anything.
        assert trade.trade_id == 7
        assert "save_paper_trade" not in repo.calls
        assert "close_paper_trade" not in repo.calls
        assert "update_paper_trade_funding" not in repo.calls

    def test_recovery_is_read_only(self):
        repo = _RecoveryRepo(open_rows=[_open_row()])
        PaperTradingEngine(_settings(), repo, clock=_Clock(T0))
        mutating = [
            c for c in repo.calls
            if c.split(":")[0] in {
                "save_paper_trade", "close_paper_trade", "update_paper_trade_funding",
                "insert_paper_safety_event",
            }
        ]
        assert mutating == []

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("", None),
            (datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
             datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)),
            (datetime(2026, 1, 1, 12, 0), datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)),
            ("2026-01-01T12:00:00+00:00", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)),
            ("2026-01-01T12:00:00Z", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)),
            ("2026-01-01T12:00:00", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)),
            ("not-a-timestamp", None),
            (12345, None),
        ],
    )
    def test_parse_planned_exit_variants(self, raw, expected):
        assert _parse_planned_exit(raw) == expected


# ---------------------------------------------------------------------------
# Repository-side capability-guarded read/write
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self._connection = connection
        self.executions: list[tuple[str, tuple]] = []
        self.description = None
        self._rows: list[tuple] = []
        self._one: tuple | None = None

    def execute(self, sql, params=()) -> None:
        text = " ".join(str(sql).split())
        self.executions.append((text, tuple(params)))

        if "information_schema.columns" in text:
            if self._connection.probe_fails:
                raise RuntimeError("information_schema unavailable")
            self.description = None
            self._rows = [(name,) for name in self._connection.policy_columns]
            self._one = None
            return

        if text.startswith("SELECT trade_id FROM dds.paper_trade WHERE setup_id"):
            self.description = None
            self._rows = []
            self._one = None  # no pre-existing trade for this setup
            return

        if "FROM dds.paper_trade" in text and "WHERE status = 'OPEN'" in text:
            between = text.split("SELECT", 1)[1].split("FROM dds.paper_trade", 1)[0]
            columns = [c.strip() for c in between.strip().split(",")]
            self.description = [(c,) for c in columns]
            row = self._connection.open_row or {}
            self._rows = [tuple(row.get(c) for c in columns)]
            self._one = None
            return

        if "INSERT INTO dds.paper_trade" in text:
            self._connection.inserts.append((text, tuple(params)))
            self.description = [("trade_id",)]
            self._one = (77,)
            self._rows = []
            return

        # UPDATE dds.scanner_setup / anything else
        self.description = None
        self._rows = []
        self._one = None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self) -> tuple | None:
        return self._one

    def close(self) -> None:
        pass


class _FakeConnection:
    def __init__(self, *, policy_columns, probe_fails=False, open_row=None):
        self.policy_columns = list(policy_columns)
        self.probe_fails = probe_fails
        self.open_row = open_row
        self.inserts: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0
        self._cursor = _FakeCursor(self)

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _repository(policy_columns, *, probe_fails=False, open_row=None):
    repository = ScannerRepository.__new__(ScannerRepository)
    repository._use_pg = True
    repository._conn = _FakeConnection(
        policy_columns=policy_columns,
        probe_fails=probe_fails,
        open_row=open_row,
    )
    return repository, repository._conn


def _trade_for_insert(**overrides) -> PaperTradeRecord:
    values = dict(
        trade_id=None,
        setup_id="setup-9",
        symbol="BTCUSDT",
        scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT",
        score=38.0,
        entry_price=101.0,
        entry_fee=0.05555,
        stop_price=110.0,
        target_1=85.0,
        target_2=None,
        position_size=5.0,
        risk_usdt=45.0,
        balance_before=10_000.0,
        market_regime="RANGE",
        entered_at=T0,
        entry_market_price=101.0,
        entry_slippage_cost=0.0,
        execution_policy="FIXED_HORIZON_V1",
        execution_policy_version="FIXED_HORIZON_V1@2026-01",
        planned_exit_at=T0 + timedelta(minutes=240),
    )
    values.update(overrides)
    return PaperTradeRecord(**values)


class TestRepositoryRoundTrip:
    def test_select_includes_all_three_policy_columns(self):
        repository, connection = _repository(
            ALL_POLICY_COLUMNS, open_row=_open_row(),
        )
        rows = repository.get_open_paper_trades()

        assert len(rows) == 1
        row = rows[0]
        assert row["execution_policy"] == "FIXED_HORIZON_V1"
        assert row["execution_policy_version"] == "FIXED_HORIZON_V1@2026-01"
        assert row["planned_exit_at"] == T0 + timedelta(minutes=2)
        # Legacy fields still restored exactly as before.
        assert row["trade_id"] == 7
        assert row["symbol"] == "BTCUSDT"
        assert row["entry_price"] == 101.0

        select = next(sql for sql, _ in connection._cursor.executions
                      if "WHERE status = 'OPEN'" in sql)
        for column in ALL_POLICY_COLUMNS:
            assert column in select

    def test_select_falls_back_when_probe_fails(self):
        repository, connection = _repository(
            NO_POLICY_COLUMNS, probe_fails=True, open_row=_open_row(),
        )
        rows = repository.get_open_paper_trades()

        assert len(rows) == 1
        row = rows[0]
        # Pre-084 shape: legacy columns only, honest defaults for the rest.
        assert row["execution_policy"] == "DEFAULT"
        assert row["execution_policy_version"] == ""
        assert row["planned_exit_at"] is None
        assert row["trade_id"] == 7
        assert connection.rollbacks >= 1  # failed probe was rolled back

        select = next(sql for sql, _ in connection._cursor.executions
                      if "WHERE status = 'OPEN'" in sql)
        assert "execution_policy" not in select
        assert "planned_exit_at" not in select

    def test_select_without_084_still_reads_038_columns(self):
        repository, connection = _repository(
            ONLY_038_COLUMNS, open_row=_open_row(),
        )
        rows = repository.get_open_paper_trades()
        select = next(sql for sql, _ in connection._cursor.executions
                      if "WHERE status = 'OPEN'" in sql)
        assert "execution_policy" in select
        assert "planned_exit_at" in select
        assert "execution_policy_version" not in select
        assert rows[0]["execution_policy"] == "FIXED_HORIZON_V1"
        assert rows[0]["execution_policy_version"] == ""  # column absent
        assert rows[0]["planned_exit_at"] == T0 + timedelta(minutes=2)

    def test_probe_is_queried_once_per_process(self):
        repository, connection = _repository(ALL_POLICY_COLUMNS, open_row=_open_row())
        repository.get_open_paper_trades()
        repository.get_open_paper_trades()
        repository.get_open_paper_trades()
        probes = [
            sql for sql, _ in connection._cursor.executions
            if "information_schema.columns" in sql
        ]
        assert len(probes) == 1
        # The probe is a parameterised, read-only catalog query.
        assert "table_schema = 'dds'" in probes[0]
        assert "table_name = 'paper_trade'" in probes[0]
        probe_params = next(
            params for sql, params in connection._cursor.executions
            if "information_schema.columns" in sql
        )
        assert set(probe_params) == set(ALL_POLICY_COLUMNS)

    def test_save_persists_policy_version_when_column_exists(self):
        repository, connection = _repository(ALL_POLICY_COLUMNS)
        trade = _trade_for_insert()

        trade_id = repository.save_paper_trade(trade)

        assert trade_id == 77
        # 1 commit for the cached capability probe + 1 for the INSERT itself.
        assert connection.commits == 2
        insert_sql, params = connection.inserts[0]
        assert "execution_policy_version" in insert_sql
        assert "execution_policy" in insert_sql
        assert "planned_exit_at" in insert_sql
        assert "OPEN" not in insert_sql  # status is a bound parameter now
        assert "OPEN" in params
        assert params[-3:] == (
            "FIXED_HORIZON_V1",
            trade.planned_exit_at,
            "FIXED_HORIZON_V1@2026-01",
        )

    @pytest.mark.parametrize("policy_columns", [NO_POLICY_COLUMNS, ONLY_038_COLUMNS])
    def test_save_keeps_legacy_insert_when_version_unproven(self, policy_columns):
        repository, connection = _repository(policy_columns)
        trade = _trade_for_insert()

        trade_id = repository.save_paper_trade(trade)

        assert trade_id == 77
        assert connection.commits == 2  # probe commit + INSERT commit
        insert_sql, _ = connection.inserts[0]
        assert "execution_policy_version" not in insert_sql
        # The pre-084 columns still round-trip.
        assert "execution_policy" in insert_sql
        assert "planned_exit_at" in insert_sql

    def test_jsonl_backend_reads_nothing_and_writes_nothing(self):
        repository = ScannerRepository.__new__(ScannerRepository)
        repository._use_pg = False
        repository._conn = _NoPgConnection()  # any access would explode

        assert repository.get_open_paper_trades() == []
        assert repository.save_paper_trade(_trade_for_insert()) is None
        assert repository.paper_trade_policy_columns() == []


class _NoPgConnection:
    def __getattr__(self, name):  # pragma: no cover - guard sentinel
        raise AssertionError(f"JSONL backend must never touch a connection: {name}")


# ---------------------------------------------------------------------------
# Migration / schema audits for the recovery fix
# ---------------------------------------------------------------------------

class TestRecoverySchemaAdditive:
    def test_migration_084_additive_and_defaulted(self):
        from pathlib import Path

        sql = (
            Path(__file__).parents[1] / "sql" / "migrations"
            / "084_paper_execution_policy_recovery_v1.sql"
        ).read_text(encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS execution_policy TEXT NOT NULL DEFAULT 'DEFAULT'" in sql
        assert "ADD COLUMN IF NOT EXISTS execution_policy_version TEXT NOT NULL DEFAULT ''" in sql
        assert "ADD COLUMN IF NOT EXISTS planned_exit_at TIMESTAMPTZ" in sql
        # No historical row may be rewritten: old paper trades stay untouched.
        assert "UPDATE" not in sql
        assert "DELETE" not in sql

    def test_schema_bootstrap_matches_migration_084(self):
        from pathlib import Path

        schema = (
            Path(__file__).parents[1] / "app" / "db" / "schema.sql"
        ).read_text(encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS execution_policy " in schema
        assert "ADD COLUMN IF NOT EXISTS execution_policy_version" in schema
        assert "ADD COLUMN IF NOT EXISTS planned_exit_at" in schema
        # The capability-guarded repository never requires a bootstrap call:
        from pathlib import Path as _Path

        repository_src = (
            _Path(__file__).parents[1] / "app" / "db" / "repository.py"
        ).read_text(encoding="utf-8")
        assert "paper_trade_policy_columns" in repository_src
        assert "information_schema.columns" in repository_src
