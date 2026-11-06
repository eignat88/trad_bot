"""ME_LONG_V2_INVERSE_SHORT_V1 — source-capture independence audits.

Requirement 1: raw MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 detector events must
reach the contour INDEPENDENTLY of the trading direction gate and the
expectancy filter — without changing either of them.

Also audits (requirements 7/8/10):
* the V2 direction-gate blocks in config.yaml are untouched;
* the production gate / expectancy code paths are untouched and do not
  reference the new contour;
* migration 085 is additive, prospective-only (no backfill possible) and
  never touches production paper tables; migration 084 is additive-only;
* the scanner_runner capture hook is fail-open (flag off → zero DB touch;
  store unavailable → error sink, never a scanner failure);
* the separate activation fixator (scripts/me_inverse_short_v1_activate.py)
  validates freeze_ts and refuses unsafe state transitions.

No PostgreSQL is required by this suite.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.paper.me_long_v2_inverse_short_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID,
    SOURCE_SCANNER,
    STRATEGY_ID,
    protocol_fingerprint,
)
from app.scanners.direction_gate import ScannerDirectionGatePolicy
from app.scanners.expectancy_filter import (
    ExpectancyFilter,
    ExpectancyRecord,
    filter_candidates,
)
from app.scanners.models import (
    IndicatorSnapshot,
    MarketContext,
    MarketLevels,
    SetupCandidate,
    SetupState,
)
from app.scanners.orchestrator import ScannerOrchestrator

PROJECT_ROOT = Path(__file__).parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
MIGRATION_084 = PROJECT_ROOT / "sql" / "migrations" / "084_paper_execution_policy_recovery_v1.sql"
MIGRATION_085 = PROJECT_ROOT / "sql" / "migrations" / "085_me_long_v2_inverse_short_v1.sql"
SCHEMA_SQL = PROJECT_ROOT / "app" / "db" / "schema.sql"
ACTIVATE_SCRIPT = PROJECT_ROOT / "scripts" / "me_inverse_short_v1_activate.py"
RUNNER_SCRIPT = PROJECT_ROOT / "me_inverse_short_runner.py"

SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _v2_long_candidate(symbol: str = "BTCUSDT") -> SetupCandidate:
    candle_ms = int(SIGNAL_AT.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name=SOURCE_SCANNER,
        scanner_version="1.0.0",
        symbol=symbol,
        direction="LONG",
        entry_timeframe="5m",
        detected_at=SIGNAL_AT,
        setup_started_at=SIGNAL_AT,
        signal_candle_open_time=candle_ms,
        reference_price=100.0,
        entry_zone_low=99.8,
        entry_zone_high=100.1,
        invalidation_price=97.5,
        target_1=103.0,
        score=45.0,
        reasons=("stub fixture",),
        market_regime="TREND_UP",
        state=SetupState.SETUP_READY,
        features={"exhaustion_magnitude": 0.01, "rsi_14": 70.0},
    )


def _ctx(symbol: str = "BTCUSDT") -> MarketContext:
    return MarketContext(
        symbol=symbol,
        candles_5m=(),
        candles_15m=(),
        candles_1h=(),
        candles_4h=(),
        indicators=IndicatorSnapshot(),
        market_regime="TREND_UP",
        levels=MarketLevels(),
        evaluated_at=SIGNAL_AT,
    )


def _gate_policy() -> ScannerDirectionGatePolicy:
    """The production static blocklist: V2 is blocked in BOTH directions."""
    return ScannerDirectionGatePolicy.static_fallback(
        scanner_names=[SOURCE_SCANNER],
        blocked_combinations=[
            (SOURCE_SCANNER, "LONG"),
            (SOURCE_SCANNER, "SHORT"),
        ],
        regime_whitelist={},
    )


def _expectancy_filter() -> ExpectancyFilter:
    """Historically negative expectancy: the filter WOULD reject the event."""
    return ExpectancyFilter(
        records={
            (SOURCE_SCANNER, "LONG"): ExpectancyRecord(
                scanner_name=SOURCE_SCANNER,
                direction="LONG",
                samples=120,
                avg_r_after_costs=-1.5,
                win_rate=0.20,
                profit_factor=0.40,
            ),
        },
    )


def _orchestrator(candidate: SetupCandidate) -> ScannerOrchestrator:
    orchestrator = ScannerOrchestrator(enabled_scanners=[SOURCE_SCANNER])

    class _StubScanner:
        def scan(self, ctx):  # noqa: ARG001 - fixture stub
            return [candidate]

    orchestrator.scanners = {SOURCE_SCANNER: _StubScanner()}
    return orchestrator


def _load_activate_module():
    spec = importlib.util.spec_from_file_location(
        "me_inverse_short_v1_activate_under_test", ACTIVATE_SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Requirement 1: capture BEFORE gate / expectancy, both untouched
# ---------------------------------------------------------------------------

class TestPreGateSourceCapture:
    def test_orchestrator_publishes_source_events_pre_gate(self):
        candidate = _v2_long_candidate()
        orchestrator = _orchestrator(candidate)
        valid, stats = orchestrator.scan_all_with_stats(
            _ctx(),
            expectancy_filter=_expectancy_filter(),
            min_avg_r=0.0,
            min_samples=10,
            gate_policy=_gate_policy(),
            regime_filter=True,
            scanner_regime_whitelist={},
            trading_mode="paper",
        )

        # 1. The event is published for the contour...
        captured = stats["_me_inverse_short_source_events"]
        assert len(captured) == 1
        assert captured[0].setup_id == candidate.setup_id
        assert captured[0].symbol == candidate.symbol
        assert captured[0].scanner_name == SOURCE_SCANNER
        assert captured[0].direction == "LONG"

        # 2. ...while BOTH trading filters still reject it:
        assert not any(
            c.scanner_name == SOURCE_SCANNER for c in valid
        ), "gate-blocked V2 candidate must never enter the tradeable list"
        assert stats[SOURCE_SCANNER]["candidates_found"] == 1
        assert stats[SOURCE_SCANNER]["setups_saved"] == 0

        # The gate decision itself is still BLOCKED:
        decision = _gate_policy().evaluate(SOURCE_SCANNER, "LONG", "TREND_UP")
        assert decision.allowed is False

    def test_expectancy_filter_still_rejects_the_event(self):
        candidate = _v2_long_candidate()
        expectancy = _expectancy_filter()
        kept, rejected = filter_candidates(
            [candidate],
            expectancy,
            min_avg_r=0.0,
            min_samples=10,
            trading_mode="paper",
        )
        assert kept == []
        assert rejected == 1
        # The filter's records are unchanged by the capture path.
        record = expectancy.records[(SOURCE_SCANNER, "LONG")]
        assert record.avg_r_after_costs == -1.5
        assert record.samples == 120
        assert len(expectancy.records) == 1

    def test_stats_key_is_metadata_and_runner_forwards_it(self):
        orchestrator = _orchestrator(_v2_long_candidate())
        _, stats = orchestrator.scan_all_with_stats(
            _ctx(),
            expectancy_filter=_expectancy_filter(),
            gate_policy=_gate_policy(),
            regime_filter=True,
            scanner_regime_whitelist={},
        )
        # "_" prefix ⇒ scanner_runner's stats loop skips it as a counter.
        assert "_me_inverse_short_source_events" in stats

        runner_src = (PROJECT_ROOT / "scanner_runner.py").read_text(encoding="utf-8")
        assert '"_me_inverse_short_source_events", []' in runner_src
        assert "_capture_me_inverse_short_source(repository, inv_candidate)" in runner_src
        orchestrator_src = (
            PROJECT_ROOT / "app" / "scanners" / "orchestrator.py"
        ).read_text(encoding="utf-8")
        assert 'stats["_me_inverse_short_source_events"]' in orchestrator_src
        assert orchestrator_src.index('stats["_me_inverse_short_source_events"]') < orchestrator_src.index(
            "gate_accepted: list[SetupCandidate] = []",
        ), "capture point must stay BEFORE the direction-gate evaluation"

    def test_capture_route_is_pre_filter_source(self):
        from app.paper.me_long_v2_inverse_short_v1.protocol import (
            CAPTURE_ROUTES,
            ROUTE_PRE_FILTER_SOURCE,
        )
        assert ROUTE_PRE_FILTER_SOURCE in CAPTURE_ROUTES
        assert set(CAPTURE_ROUTES) == {"PRE_FILTER_SOURCE"}


# ---------------------------------------------------------------------------
# Requirements 7/8: gates, expectancy and production config untouched
# ---------------------------------------------------------------------------

class TestProductionGatesUntouched:
    def test_config_yaml_still_blocks_both_v2_directions(self):
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        blocked = {tuple(pair) for pair in config["blocked_scanner_directions"]}
        assert (SOURCE_SCANNER, "LONG") in blocked
        assert (SOURCE_SCANNER, "SHORT") in blocked

    def test_config_yaml_v2_execution_policy_unchanged(self):
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        policy = config["execution_policies"][SOURCE_SCANNER]["LONG"]
        assert policy == {
            "policy": "FIXED_TP_SL_HORIZON_V1",
            "enabled": True,
            "hold_minutes": 240,
            "dca_enabled": False,
            "trailing_enabled": False,
            "breakeven_enabled": False,
            "tp_enabled": True,
            "expiry_enabled": False,
        }

    def test_gate_and_expectancy_modules_never_reference_the_contour(self):
        for relative in (
            "app/scanners/direction_gate.py",
            "app/scanners/expectancy_filter.py",
        ):
            source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            assert STRATEGY_ID not in source
            assert "ME_INVERSE_SHORT" not in source

    def test_contour_package_never_imports_gate_or_expectancy(self):
        package = PROJECT_ROOT / "app" / "paper" / "me_long_v2_inverse_short_v1"
        for path in sorted(package.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert "from app.scanners.direction_gate" not in source, path.name
            assert "from app.scanners.expectancy_filter" not in source, path.name
            assert "blocked_scanner_directions" not in source, path.name

    def test_paper_and_production_tables_never_written_by_contour(self):
        package = PROJECT_ROOT / "app" / "paper" / "me_long_v2_inverse_short_v1"
        for path in sorted(package.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert "INSERT INTO dds.paper_trade" not in source, path.name
            assert "INSERT INTO dds.scanner_setup" not in source, path.name
            assert "ensure_schema" not in source, path.name


# ---------------------------------------------------------------------------
# scanner_runner capture hook: fail-open (requirement 1 delivery)
# ---------------------------------------------------------------------------

class _BrokenRepository:
    """Repository without any PostgreSQL backend attributes."""

    def __init__(self, save_error_raises: bool = False) -> None:
        self.save_error_raises = save_error_raises
        self.errors: list[dict] = []

    def save_error(self, **kwargs) -> None:
        if self.save_error_raises:
            raise RuntimeError("error sink down")
        self.errors.append(kwargs)


@pytest.fixture
def scanner_runner_module():
    import scanner_runner

    previous = (
        scanner_runner._me_inverse_short_store,
        scanner_runner._me_inverse_short_store_retry_at,
    )
    scanner_runner._me_inverse_short_store = None
    scanner_runner._me_inverse_short_store_retry_at = 0.0
    yield scanner_runner
    (
        scanner_runner._me_inverse_short_store,
        scanner_runner._me_inverse_short_store_retry_at,
    ) = previous


class TestScannerRunnerFailOpen:
    def test_flag_off_touches_nothing(self, scanner_runner_module, monkeypatch):
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)
        repository = _BrokenRepository()
        scanner_runner_module._capture_me_inverse_short_source(
            repository, _v2_long_candidate(),
        )
        assert repository.errors == []
        assert scanner_runner_module._me_inverse_short_store is None

    def test_store_unavailable_sinks_error_and_never_raises(
        self, scanner_runner_module, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        repository = _BrokenRepository()
        # Must not raise even though there is no database at all.
        scanner_runner_module._capture_me_inverse_short_source(
            repository, _v2_long_candidate(),
        )
        assert len(repository.errors) == 1
        error = repository.errors[0]
        assert error["error_type"] == "ME_INVERSE_SHORT_SOURCE_CAPTURE_ERROR"
        assert error["scanner_name"] == SOURCE_SCANNER
        assert "status=EXCEPTION" in error["error_message"]

    def test_error_sink_failure_is_also_swallowed(
        self, scanner_runner_module, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        repository = _BrokenRepository(save_error_raises=True)
        scanner_runner_module._capture_me_inverse_short_source(
            repository, _v2_long_candidate(),
        )  # no exception = contract upheld

    def test_foreign_candidates_are_ignored_before_any_work(
        self, scanner_runner_module, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        repository = _BrokenRepository()
        foreign = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="ETHUSDT", direction="LONG",
        )
        short_source = SetupCandidate(
            scanner_name=SOURCE_SCANNER, symbol="ETHUSDT", direction="SHORT",
        )
        scanner_runner_module._capture_me_inverse_short_source(repository, foreign)
        scanner_runner_module._capture_me_inverse_short_source(repository, short_source)
        assert repository.errors == []
        assert scanner_runner_module._me_inverse_short_store is None


# ---------------------------------------------------------------------------
# Migration audits (requirements 5/7/8/10)
# ---------------------------------------------------------------------------

class TestMigrations:
    def test_migration_085_is_additive_and_untouches_production(self):
        sql = MIGRATION_085.read_text(encoding="utf-8")
        # No production state touched:
        for forbidden in (
            "UPDATE dds.paper_trade",
            "INSERT INTO dds.paper_trade",
            "DELETE FROM dds.paper_trade",
            "ALTER TABLE dds.paper_trade",
            "UPDATE dds.scanner_setup",
            "INSERT INTO dds.scanner_setup",
            "blocked_scanner_directions",
        ):
            assert forbidden not in sql, forbidden
        # Own tables only:
        assert "dds.me_long_v2_inverse_short_v1_activation" in sql
        assert "dds.me_long_v2_inverse_short_v1_event" in sql
        assert "dds.me_long_v2_inverse_short_v1_position" in sql

    def test_migration_085_forbids_historical_backfill_by_construction(self):
        sql = MIGRATION_085.read_text(encoding="utf-8")
        assert "CHECK (signal_time > activation_freeze_ts)" in sql
        # Activation is fail-closed when seeded (NOT_DEPLOYED):
        assert "READY_TO_START" in sql
        assert "protocol_fingerprint IS NULL AND freeze_ts IS NULL" in sql

    def test_migration_085_namespaces_positions_and_locks_deadline(self):
        sql = MIGRATION_085.read_text(encoding="utf-8")
        assert "position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_V1:%'" in sql
        assert "CHECK (deadline_at > entered_at)" in sql
        assert "fn_me_long_v2_inverse_short_v1_guard" in sql
        assert "entered_at IS DISTINCT FROM OLD.entered_at" in sql
        assert "deadline_at IS DISTINCT FROM OLD.deadline_at" in sql
        assert (
            "CREATE TRIGGER trg_me_long_v2_inverse_short_v1_guard\n"
            "BEFORE UPDATE ON dds.me_long_v2_inverse_short_v1_position"
        ) in sql
        # Closed rows are terminal:
        assert "closed position is terminal" in sql

    def test_migration_085_exit_reasons_match_the_frozen_protocol(self):
        from app.paper.me_long_v2_inverse_short_v1.protocol import EXIT_REASONS

        sql = MIGRATION_085.read_text(encoding="utf-8")
        for reason in EXIT_REASONS:
            assert f"'{reason}'" in sql, reason
        assert set(EXIT_REASONS) == {
            "STOP_LOSS", "STOP_LOSS_GAP", "TAKE_PROFIT_1", "TIMEOUT",
        }
        # ...and the same CHECK exists in the bootstrap schema:
        schema = SCHEMA_SQL.read_text(encoding="utf-8")
        for reason in EXIT_REASONS:
            assert f"'{reason}'" in schema

    def test_migration_084_is_additive_only(self):
        sql = MIGRATION_084.read_text(encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS execution_policy " in sql
        assert "ADD COLUMN IF NOT EXISTS execution_policy_version" in sql
        assert "ADD COLUMN IF NOT EXISTS planned_exit_at" in sql
        # No existing row is modified, backfilled or deleted:
        assert "UPDATE" not in sql
        assert "DELETE" not in sql
        assert "DROP" not in sql

    def test_schema_bootstrap_contains_recovery_columns_and_contour_tables(self):
        schema = SCHEMA_SQL.read_text(encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS execution_policy_version" in schema
        assert "ADD COLUMN IF NOT EXISTS planned_exit_at" in schema
        # schema.sql is split on ";" — it must contain NO function bodies.
        assert "$$" not in schema
        assert "CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v1_activation" in schema
        assert "CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v1_event" in schema
        assert "CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v1_position" in schema
        assert "trg_me_long_v2_inverse_short_v1_guard" not in schema  # migration only
        # Trigger lives in the migration, referenced from the bootstrap:
        assert MIGRATION_085.name in schema


# ---------------------------------------------------------------------------
# Activation fixator (requirement 10: separate fingerprint/freeze fixation)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def activate():
    return _load_activate_module()


class TestActivationFixator:

    def test_parse_freeze_ts_accepts_aware_iso(self, activate):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        parsed = activate.parse_freeze_ts("2026-05-31T00:00:00Z", now=now)
        assert parsed == datetime(2026, 5, 31, tzinfo=timezone.utc)
        parsed = activate.parse_freeze_ts("2026-05-31T00:00:00+00:00", now=now)
        assert parsed.tzinfo is not None

    def test_parse_freeze_ts_refuses_future(self, activate):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        with pytest.raises(activate.ActivationError, match="future"):
            activate.parse_freeze_ts("2026-06-02T00:00:00Z", now=now)

    def test_parse_freeze_ts_refuses_naive_and_garbage(self, activate):
        with pytest.raises(activate.ActivationError, match="timezone-aware"):
            activate.parse_freeze_ts("2026-05-31T00:00:00")
        with pytest.raises(activate.ActivationError, match="ISO-8601"):
            activate.parse_freeze_ts("not-a-timestamp")
        with pytest.raises(activate.ActivationError):
            activate.parse_freeze_ts("   ")
        with pytest.raises(activate.ActivationError):
            activate.parse_freeze_ts(" 2026-05-31T00:00:00Z ")

    def test_plan_activation_writes_fingerprint_and_freeze_atomically(self, activate):
        freeze = datetime(2026, 6, 1, tzinfo=timezone.utc)
        fingerprint = protocol_fingerprint()
        sql, params = activate.plan_activation(
            current_fingerprint=fingerprint,
            freeze_ts=freeze,
            existing_status="READY_TO_START",
            existing_fingerprint=None,
            existing_freeze=None,
            re_freeze=False,
            note="n",
        )
        assert sql == activate.UPDATE_SQL
        assert "updated_at = now()" in sql
        assert params == ("RUNNING", fingerprint, freeze.isoformat(), "n", EXPERIMENT_ID)

    def test_plan_activation_refuses_to_overwrite_running(self, activate):
        freeze = datetime(2026, 6, 1, tzinfo=timezone.utc)
        kwargs = dict(
            current_fingerprint=protocol_fingerprint(),
            freeze_ts=freeze,
            existing_status="RUNNING",
            existing_fingerprint="f" * 64,
            existing_freeze=datetime(2026, 1, 1, tzinfo=timezone.utc),
            note="",
        )
        with pytest.raises(activate.ActivationError, match="different"):
            activate.plan_activation(re_freeze=False, **kwargs)
        with pytest.raises(activate.ActivationError, match="idempotent"):
            activate.plan_activation(
                re_freeze=False, **dict(
                    kwargs,
                    existing_fingerprint=protocol_fingerprint(),
                    existing_freeze=freeze,
                ),
            )
        # Explicit re-freeze is allowed (new cohort, new boundary).
        _, params = activate.plan_activation(re_freeze=True, **kwargs)
        assert params[0] == "RUNNING"
        assert params[1] == protocol_fingerprint()

    def test_plan_activation_refuses_cancelled_row(self, activate):
        freeze = datetime(2026, 6, 1, tzinfo=timezone.utc)
        with pytest.raises(activate.ActivationError, match="CANCELLED"):
            activate.plan_activation(
                current_fingerprint=protocol_fingerprint(),
                freeze_ts=freeze,
                existing_status="CANCELLED",
                existing_fingerprint="f" * 64,
                existing_freeze=freeze,
                re_freeze=True,
                note="",
            )

    def test_plan_drain_keeps_fingerprint_and_freeze(self, activate):
        freeze = datetime(2026, 6, 1, tzinfo=timezone.utc)
        fingerprint = "a" * 64
        sql, params = activate.plan_drain(
            existing_status="RUNNING",
            existing_fingerprint=fingerprint,
            existing_freeze=freeze,
            note="drain",
        )
        assert params == ("DRAINING", fingerprint, freeze.isoformat(), "drain", EXPERIMENT_ID)
        with pytest.raises(activate.ActivationError):
            activate.plan_drain(
                existing_status="READY_TO_START",
                existing_fingerprint=None,
                existing_freeze=None,
                note="",
            )

    def test_render_status_reports_not_deployed_until_match(self, activate):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        fingerprint = protocol_fingerprint()

        pending = activate.render_status(
            current_fingerprint=fingerprint,
            stored_status="READY_TO_START",
            stored_fingerprint=None,
            stored_freeze=None,
            now=now,
            runtime_enabled=False,
        )
        assert pending["active"] is False
        assert pending["contour_status"] == "NOT_DEPLOYED"
        assert pending["fingerprint_matches"] is False

        active = activate.render_status(
            current_fingerprint=fingerprint,
            stored_status="RUNNING",
            stored_fingerprint=fingerprint,
            stored_freeze=datetime(2026, 1, 1, tzinfo=timezone.utc),
            now=now,
            runtime_enabled=True,
        )
        assert active["active"] is True
        assert active["contour_status"] == "ACTIVE"
        assert active["fingerprint_matches"] is True
        assert active["reason"] == "ACTIVE"

        stale = activate.render_status(
            current_fingerprint=fingerprint,
            stored_status="RUNNING",
            stored_fingerprint="0" * 64,
            stored_freeze=datetime(2026, 1, 1, tzinfo=timezone.utc),
            now=now,
            runtime_enabled=True,
        )
        assert stale["active"] is False
        assert stale["reason"] == "PROTOCOL_FINGERPRINT_MISMATCH"
        assert stale["contour_status"] == "NOT_DEPLOYED"

    def test_activation_script_only_touches_its_own_table(self, activate):
        assert "dds.paper_trade" not in activate.UPDATE_SQL
        assert "dds.scanner_setup" not in activate.UPDATE_SQL
        assert "dds.me_long_v2_inverse_short_v1_activation" in activate.UPDATE_SQL
        assert "SELECT status, protocol_fingerprint, freeze_ts" in activate.SELECT_SQL


# ---------------------------------------------------------------------------
# Standalone runner wiring
# ---------------------------------------------------------------------------

class TestRunnerWiring:
    def test_runner_is_fail_closed_and_dedicated(self):
        source = RUNNER_SCRIPT.read_text(encoding="utf-8")
        # Fail-closed startup (exit code 2) without the two-factor activation:
        assert "if not contour.activated:" in source
        assert "return 2" in source
        assert "ME_INVERSE_SHORT_V1_ENABLED" in source
        # Dedicated store only — never the production engine/schema bootstrap:
        assert "connect_pg_store" in source
        assert "ensure_schema" not in source
        assert "PaperTradingEngine" not in source
        # Dry-run mode exists and touches no database:
        assert "--dry-run" in source
        assert "run_dry_run(settings)" in source

    def test_runner_and_activation_script_compile(self):
        import py_compile

        py_compile.compile(str(RUNNER_SCRIPT), doraise=True)
        py_compile.compile(str(ACTIVATE_SCRIPT), doraise=True)
