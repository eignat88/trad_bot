"""Issue #305 — wiring, placement and isolation audits.

Static + runtime proof that the upstream source capture
(ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1):

* receives the detector-level events IMMEDIATELY after ``scanner.scan(ctx)`` —
  BEFORE scoring, dedup, risk geometry, direction gate, regime filter and
  expectancy filter (static source audit + runtime publication);
* is fail-open towards the scanner (criterion 7) and OFF by default
  (criterion 1: no DB connection/writes while disabled);
* changes NO trading decision and NO existing capture hook (criteria 13/15);
* ships an additive, prospective-only migration (criterion 8).

No PostgreSQL is required by this suite.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.research.me_upstream_source_v1 import capture as capture_module
from app.research.me_upstream_source_v1.protocol import (
    ACTIVATION_FLAG,
    EXPERIMENT_ID,
    SOURCE_SCANNER,
    protocol_fingerprint,
)
from app.research.me_upstream_source_v1.store import InMemoryUpstreamSourceStore
from app.scanners.models import (
    IndicatorSnapshot,
    MarketContext,
    MarketLevels,
    SetupCandidate,
    SetupState,
)
from app.scanners.orchestrator import ScannerOrchestrator

PROJECT_ROOT = Path(__file__).parents[1]
ORCHESTRATOR_SRC = (PROJECT_ROOT / "app" / "scanners" / "orchestrator.py").read_text(
    encoding="utf-8"
)
SCANNER_RUNNER_SRC = (PROJECT_ROOT / "scanner_runner.py").read_text(encoding="utf-8")
MIGRATION_094 = (
    PROJECT_ROOT / "sql" / "migrations" / "094_me_upstream_source_capture_v1.sql"
)

SIGNAL_AT = datetime(2026, 11, 25, 12, 0, 30, tzinfo=timezone.utc)
FREEZE = datetime(2026, 11, 24, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _upstream_candidate(**overrides) -> SetupCandidate:
    values = dict(
        scanner_name=SOURCE_SCANNER,
        scanner_version="1.0.0",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        detected_at=SIGNAL_AT,
        setup_started_at=SIGNAL_AT,
        signal_candle_open_time=int(SIGNAL_AT.timestamp() // 300) * 300_000,
        reference_price=100.0,
        entry_zone_low=99.8,
        entry_zone_high=100.1,
        invalidation_price=97.5,
        target_1=103.0,
        score=0.0,
        reasons=(),
        market_regime="TREND_UP",
        state=SetupState.DETECTED,
        features={
            "rsi_14": 55.0,
            "rsi_delta_3": 1.25,
            "entry_confirmation_rsi_rising": True,
        },
    )
    values.update(overrides)
    return SetupCandidate(**values)


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


def _orchestrator(candidates: list[SetupCandidate]) -> ScannerOrchestrator:
    orchestrator = ScannerOrchestrator(enabled_scanners=[SOURCE_SCANNER])

    class _StubScanner:
        def scan(self, ctx):  # noqa: ARG001 - fixture stub
            return list(candidates)

    orchestrator.scanners = {SOURCE_SCANNER: _StubScanner()}
    return orchestrator


def _activation() -> dict:
    return {
        "experiment_id": EXPERIMENT_ID,
        "status": "RUNNING",
        "protocol_version": "1.0.0",
        "protocol_fingerprint": protocol_fingerprint(),
        "freeze_ts": FREEZE,
    }


@pytest.fixture()
def scanner_runner_module(monkeypatch):
    """Load scanner_runner once, with its project root on sys.path."""
    monkeypatch.syspath_prepend(str(PROJECT_ROOT))
    if "scanner_runner" in sys.modules:
        return sys.modules["scanner_runner"]
    spec = importlib.util.spec_from_file_location(
        "scanner_runner", PROJECT_ROOT / "scanner_runner.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scanner_runner"] = module
    spec.loader.exec_module(module)
    return module


class _FakeRepository:
    """Minimal ScannerRepository double: records error-sink calls."""

    def __init__(self):
        self.errors: list[dict] = []
        self._host = "127.0.0.1"
        self._port = 5432
        self._database = "trad_bot"
        self._user = "postgres"
        self._password = ""
        self._unix_sock = None

    def save_error(self, **kwargs):
        self.errors.append(kwargs)


# ---------------------------------------------------------------------------
# Static placement: capture BEFORE scoring / dedup / geometry / gates
# ---------------------------------------------------------------------------

class TestCapturePlacementBeforeScoring:
    def test_publication_is_before_scoring(self):
        publication = ORCHESTRATOR_SRC.index(
            'stats["_me_upstream_source_events"] = upstream_source_events'
        )
        scoring = ORCHESTRATOR_SRC.index("scored = [")
        assert publication < scoring, (
            "upstream publication must happen BEFORE scoring"
        )

    def test_publication_is_before_dedup_geometry_gates_expectancy(self):
        publication = ORCHESTRATOR_SRC.index(
            'stats["_me_upstream_source_events"] = upstream_source_events'
        )
        assert publication < ORCHESTRATOR_SRC.index("self.dedup.filter_new(")
        assert publication < ORCHESTRATOR_SRC.index("validate_risk_geometry(")
        assert publication < ORCHESTRATOR_SRC.index("gate_accepted: list[SetupCandidate] = []")
        assert publication < ORCHESTRATOR_SRC.index("filter_candidates(")
        # The regime filter body (the whitelist application itself).
        assert publication < ORCHESTRATOR_SRC.index(
            "whitelist = scanner_regime_whitelist or {}"
        )

    def test_publication_happens_directly_after_scanner_scan(self):
        scan = ORCHESTRATOR_SRC.index("candidates = scanner.scan(ctx)")
        collect = ORCHESTRATOR_SRC.index("upstream_source_events.append(")
        publication = ORCHESTRATOR_SRC.index(
            'stats["_me_upstream_source_events"] = upstream_source_events'
        )
        assert scan < collect < publication

    def test_publication_filters_the_me_v2_long_detector_only(self):
        block_start = ORCHESTRATOR_SRC.index("upstream_source_events.append(")
        block = ORCHESTRATOR_SRC[block_start - 600:block_start]
        assert f'c.scanner_name == "{SOURCE_SCANNER}"' in block
        assert 'c.direction == "LONG"' in block

    def test_signal_candle_identity_is_resolved_like_the_v3_path(self):
        # The published snapshot carries the ctx-resolved signal candle open
        # time, so its normalized identity equals the V3 ledger's
        # lineage->>'source_identity' (cross-ledger match key).
        assert "self._attach_signal_candle(c, ctx)" in ORCHESTRATOR_SRC

    def test_publication_block_is_fail_open(self):
        block_start = ORCHESTRATOR_SRC.index("upstream_source_events.append(")
        block = ORCHESTRATOR_SRC[block_start:block_start + 500]
        assert "except Exception:" in block


# ---------------------------------------------------------------------------
# Runtime publication through the orchestrator
# ---------------------------------------------------------------------------

class TestRuntimePublication:
    def test_me_long_candidate_is_published(self):
        candidate = _upstream_candidate()
        orchestrator = _orchestrator([candidate])
        _, stats = orchestrator.scan_all_with_stats(_ctx())
        events = stats["_me_upstream_source_events"]
        assert len(events) == 1
        assert events[0].setup_id == candidate.setup_id
        assert events[0].scanner_name == SOURCE_SCANNER
        assert events[0].direction == "LONG"

    def test_other_scanner_and_direction_are_not_published(self):
        other = _upstream_candidate(scanner_name="BREAKOUT_RETEST")
        short = _upstream_candidate(direction="SHORT")
        orchestrator = _orchestrator([other, short])
        _, stats = orchestrator.scan_all_with_stats(_ctx())
        assert stats["_me_upstream_source_events"] == []

    def test_published_identity_matches_v3_lineage_identity(self):
        from app.models import Candle
        from app.research.me_upstream_source_v1.identity import (
            normalized_identity,
        )

        candle = Candle(
            timestamp=1_700_000_000_000,
            open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0,
        )
        ctx = MarketContext(
            symbol="BTCUSDT",
            candles_5m=(candle,),
            candles_15m=(),
            candles_1h=(),
            candles_4h=(),
            indicators=IndicatorSnapshot(),
            market_regime="TREND_UP",
            levels=MarketLevels(),
            evaluated_at=SIGNAL_AT,
        )
        candidate = _upstream_candidate(signal_candle_open_time=0)
        orchestrator = _orchestrator([candidate])
        _, stats = orchestrator.scan_all_with_stats(ctx)
        published = stats["_me_upstream_source_events"][0]
        # The V3 path resolves the candle from ctx the same way
        # (_attach_signal_candle) — identities must agree exactly.
        assert published.signal_candle_open_time == candle.timestamp
        assert normalized_identity(published) == (
            f"{SOURCE_SCANNER}|BTCUSDT|LONG|5m|{candle.timestamp}"
        )


# ---------------------------------------------------------------------------
# scanner_runner hook: fail-open, disabled-by-default, isolated
# ---------------------------------------------------------------------------

class TestRunnerHook:
    def test_flag_off_touches_nothing(
        self, scanner_runner_module, monkeypatch,
    ):
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)
        repo = _FakeRepository()
        candidate = _upstream_candidate()

        def _explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("store must not be touched while disabled")

        monkeypatch.setattr(
            scanner_runner_module, "_get_me_upstream_source_store", _explode,
        )
        scanner_runner_module._capture_me_upstream_source_v1(repo, candidate)
        assert repo.errors == []
        assert scanner_runner_module._me_upstream_source_stats["disabled"] >= 1

    def test_foreign_candidates_are_ignored_before_any_work(
        self, scanner_runner_module, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        repo = _FakeRepository()
        before = dict(scanner_runner_module._me_upstream_source_stats)
        scanner_runner_module._capture_me_upstream_source_v1(
            repo, _upstream_candidate(scanner_name="BREAKOUT_RETEST"),
        )
        scanner_runner_module._capture_me_upstream_source_v1(
            repo, _upstream_candidate(direction="SHORT"),
        )
        scanner_runner_module._capture_me_upstream_source_v1(repo, None)
        assert dict(scanner_runner_module._me_upstream_source_stats) == before

    def test_candidate_lands_in_upstream_ledger(
        self, scanner_runner_module, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        repo = _FakeRepository()
        store = InMemoryUpstreamSourceStore(_activation())
        monkeypatch.setattr(
            scanner_runner_module, "_get_me_upstream_source_store",
            lambda repository: store,
        )
        candidate = _upstream_candidate()
        scanner_runner_module._capture_me_upstream_source_v1(repo, candidate)
        assert len(store.events) == 1
        assert repo.errors == []

    def test_store_unavailable_sinks_error_and_never_raises(
        self, scanner_runner_module, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        repo = _FakeRepository()
        monkeypatch.setattr(
            scanner_runner_module, "_get_me_upstream_source_store",
            lambda repository: None,
        )
        scanner_runner_module._capture_me_upstream_source_v1(
            repo, _upstream_candidate(),
        )
        assert len(repo.errors) == 1
        assert repo.errors[0]["error_type"] == "ME_UPSTREAM_SOURCE_CAPTURE_ERROR"

    def test_capture_exception_is_swallowed_and_sunk(
        self, scanner_runner_module, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        repo = _FakeRepository()
        monkeypatch.setattr(
            scanner_runner_module, "_get_me_upstream_source_store",
            lambda repository: object(),
        )

        def _boom(*args, **kwargs):
            raise RuntimeError("research DB down")

        monkeypatch.setattr(capture_module, "capture_with_status", _boom)
        scanner_runner_module._capture_me_upstream_source_v1(
            repo, _upstream_candidate(),
        )
        assert len(repo.errors) == 1
        assert "EXCEPTION" in repo.errors[0]["error_message"]

    def test_error_sink_failure_is_also_swallowed(
        self, scanner_runner_module, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")

        class _Repo(_FakeRepository):
            def save_error(self, **kwargs):
                raise RuntimeError("production DB down")

        monkeypatch.setattr(
            scanner_runner_module, "_get_me_upstream_source_store",
            lambda repository: None,
        )
        scanner_runner_module._capture_me_upstream_source_v1(
            _Repo(), _upstream_candidate(),
        )  # must not raise


# ---------------------------------------------------------------------------
# Regression: existing capture hooks and trading runtime untouched
# ---------------------------------------------------------------------------

class TestNoRegression:
    EXISTING_HOOKS = (
        "_capture_me_inverse_short_source(repository, inv_candidate)",
        "_capture_me_inverse_short_v2_source(repository, inv_candidate)",
        "_capture_me_inverse_short_oos2_source(",
        "_capture_me_inverse_short_oos2_v2_source(",
        "_capture_me_inverse_short_v3_source(",
    )

    def test_all_existing_capture_hooks_still_wired(self):
        for hook in self.EXISTING_HOOKS:
            assert hook in SCANNER_RUNNER_SRC, f"existing hook lost: {hook}"

    def test_v1_v3_source_stats_key_still_published_after_scoring(self):
        # The legacy post-scoring publication used by V1/V2/OOS-2/V3 is
        # unchanged: it must remain located AFTER the scoring block.
        assert (
            ORCHESTRATOR_SRC.index('stats["_me_inverse_short_source_events"]')
            > ORCHESTRATOR_SRC.index("scored = [")
        )

    def test_upstream_publication_is_a_separate_additive_key(self):
        assert (
            'stats["_me_upstream_source_events"]' in ORCHESTRATOR_SRC
        )
        assert (
            ORCHESTRATOR_SRC.count("upstream_source_events")
            >= 2  # declaration + publication + append
        )

    def test_trading_modules_never_reference_the_upstream_capture(self):
        for module_path in (
            "app/scanners/scoring.py",
            "app/scanners/direction_gate.py",
            "app/scanners/expectancy_filter.py",
            "app/scanners/risk_geometry.py",
            "app/scanners/deduplication.py",
        ):
            text = (PROJECT_ROOT / module_path).read_text(encoding="utf-8")
            assert "me_upstream_source" not in text
            assert "ME_UPSTREAM_SOURCE" not in text

    def test_capture_package_never_touches_trading_modules(self):
        package = PROJECT_ROOT / "app" / "research" / "me_upstream_source_v1"
        for py in package.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            assert "app.paper" not in text, py.name
            assert "app.db" not in text, py.name
            assert "save_setup" not in text, py.name
            assert "paper_trade" not in text, py.name
            # No exchange/Bybit code paths (imports or client use).
            assert "app.exchange" not in text, py.name
            assert "bybit_client" not in text, py.name

    def test_capture_hook_never_writes_trading_tables(self):
        hook_start = SCANNER_RUNNER_SRC.index(
            "def _capture_me_upstream_source_v1("
        )
        hook_end = SCANNER_RUNNER_SRC.index(
            "# --- Prospective OOS Observer ---", hook_start,
        )
        hook = SCANNER_RUNNER_SRC[hook_start:hook_end]
        for forbidden in (
            "save_setup", "paper_trade", "scanner_setup", "gate_accepted",
            "expectancy", "score_candidate",
        ):
            assert forbidden not in hook, forbidden


# ---------------------------------------------------------------------------
# Migration 094: additive, prospective-only, append-only
# ---------------------------------------------------------------------------

class TestMigration094:
    def test_migration_creates_only_its_own_schema(self):
        sql = MIGRATION_094.read_text(encoding="utf-8")
        assert "CREATE SCHEMA IF NOT EXISTS research_me_upstream_coverage" in sql
        # Never touches production / V3 / OOS-2 tables.
        for forbidden in (
            "ALTER TABLE dds", "DROP TABLE", "TRUNCATE",
            "me_long_v2_inverse_short_v3_event", "paper_trade", "scanner_setup",
        ):
            assert forbidden not in sql, forbidden

    def test_migration_is_prospective_only(self):
        sql = MIGRATION_094.read_text(encoding="utf-8")
        assert "signal_time > activation_freeze_ts" in sql
        assert "IMMUTABLE_FREEZE_TS" in sql

    def test_source_event_is_append_only(self):
        sql = MIGRATION_094.read_text(encoding="utf-8")
        assert "BEFORE UPDATE OR DELETE" in sql
        assert "SOURCE_EVENT_IMMUTABLE" in sql

    def test_state_event_is_append_only(self):
        sql = MIGRATION_094.read_text(encoding="utf-8")
        assert "STATE_EVENT_IMMUTABLE" in sql

    def test_activation_requires_freeze_for_running(self):
        sql = MIGRATION_094.read_text(encoding="utf-8")
        assert "CHECK (status <> 'RUNNING' OR freeze_ts IS NOT NULL)" in sql


# ---------------------------------------------------------------------------
# Deployment artifacts never enable the capture
# ---------------------------------------------------------------------------

class TestDeploySafety:
    def test_capture_flag_is_off_by_default(self):
        from app.research.me_upstream_source_v1.activation import (
            is_runtime_enabled,
        )
        assert is_runtime_enabled({}) is False
        assert is_runtime_enabled({ACTIVATION_FLAG: "0"}) is False

    def test_env_example_keeps_the_flag_disabled(self):
        env_example = PROJECT_ROOT / "deploy" / "env" / "me-upstream-source-capture.env.example"
        if not env_example.exists():
            pytest.skip("env example not shipped in this branch")
        text = env_example.read_text(encoding="utf-8")
        assert f"{ACTIVATION_FLAG}=0" in text
