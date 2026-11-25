"""Issue #312 — H2 prospective runtime: runtime integration tests (FR-07).

End-to-end cycles against in-memory fakes (no database, no network):

* 13 — restart restores state: pending events resume, terminal outcomes are
       never recreated and redeliveries stay idempotent;
* 14 — a Bybit/API failure is fail-closed for the research runtime only:
       states stay ``PENDING_CONFIRMATION``, nothing is fabricated;
* 16 — with no approved execution model there is NO fictional Net E[R] or
       return metric anywhere: the entry price stays UNDETERMINED;
* 17 — disabled runtime performs no Bybit request, no source read and no
       write;
* 18 — production isolation: the package never touches scanner/trading
       modules and the migration only touches its own schema;
plus the FR-08 telemetry counters and the FR-04 state transitions through a
real (pure PR #311) pipeline evaluation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Candle
from app.research.me_entry_quality_h2_prospective_v1 import runner as runner_mod
from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
    BybitClosedCandleProvider,
    CandleApiBudget,
)
from app.research.me_entry_quality_h2_prospective_v1.config import (
    H2ProspectiveConfig,
    H2ProspectiveConfigError,
    load_config,
)
from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    RUNTIME_BLOCKED_CANDLE_SOURCE,
    RUNTIME_DISABLED,
    STATE_CONFIRMED_DELAYED,
    STATE_PENDING_CONFIRMATION,
    candidate_policies,
    immutable_result_hash,
)
from app.research.me_entry_quality_h2_prospective_v1.runtime import (
    H2ProspectiveRuntime,
)
from app.research.me_entry_quality_h2_prospective_v1.source_reader import (
    SOURCE_EVENT_COLUMNS,
    assert_select_only,
)
from app.research.me_entry_quality_h2_prospective_v1.store import (
    InMemoryProspectiveStore,
)
from app.research.me_entry_quality_h2_v1.result import (
    ENTRY_PRICE_UNDETERMINED,
    H2Policy,
    VARIANT_TURN_1,
)
from app.research.me_upstream_source_v1.contracts import lineage_hash

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MS = 300_000
SIGNAL_MS = BASE_MS + 18 * MS + 60_000

CRAFTED = (
    [100.0 + i for i in range(17)]
    + [118.0, 114.0, 116.5, 113.0, 111.5, 115.0]
)
WARMUP = 106


# ─── crafted #305 source rows ────────────────────────────────────────────────

def make_source_row(key="evt-1", signal_ms=SIGNAL_MS):
    signal = EPOCH + timedelta(milliseconds=signal_ms)
    lineage = {
        "source_identity": f"ident-{key}",
        "source_signal_time": signal.isoformat(),
        "source_signal_candle_open_time": signal_ms // MS * MS,
        "source_scanner": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        "source_direction": "LONG",
        "source_features": {"rsi_14": 71.25},
        "source_pipeline_fingerprint": "fp-test-1",
    }
    return {
        "source_event_key": key,
        "source_identity": f"ident-{key}",
        "symbol": "BTCUSDT",
        "signal_time": signal,
        "captured_at": signal + timedelta(seconds=20),
        "activation_freeze_ts": signal - timedelta(hours=1),
        "signal_candle_open_time": signal_ms // MS * MS,
        "entry_timeframe": "5m",
        "source_fingerprint": "fp-test-1",
        "source_reference_price": 1234.5,
        "features": {
            "rsi_14": 71.25,
            "rsi_delta_3": 4.5,
            "entry_confirmation_rsi_rising": True,
        },
        "lineage": lineage,
        "lineage_hash": lineage_hash(lineage),
    }


class FakeSourceCursor:
    def __init__(self, rows, force_redeliver=False):
        self._rows = list(rows)
        self._force_redeliver = force_redeliver
        self._result = []

    def execute(self, sql, params=()):
        assert_select_only(sql)
        if self._force_redeliver:
            self._result = [self._tuple(r) for r in self._rows]
            return
        wm_ts, _wm2, wm_key, limit = params
        picked = [
            r for r in self._rows
            if r["signal_time"] > wm_ts
            or (r["signal_time"] == wm_ts and r["source_event_key"] > wm_key)
        ]
        picked.sort(key=lambda r: (r["signal_time"], r["source_event_key"]))
        self._result = [self._tuple(r) for r in picked[: int(limit)]]

    @staticmethod
    def _tuple(row):
        return tuple(row.get(col) for col in SOURCE_EVENT_COLUMNS)

    def fetchall(self):
        return list(self._result)

    def close(self):
        return None


class FakeSourceConnection:
    def __init__(self, rows, force_redeliver=False):
        self._rows = rows
        self._force_redeliver = force_redeliver
        self.closed = False

    def cursor(self):
        return FakeSourceCursor(self._rows, self._force_redeliver)

    def close(self):
        self.closed = True


class FakeBybitClient:
    def __init__(self, rows, error=None):
        self._rows = rows
        self._error = error
        self.calls = 0

    def get_klines_range(self, symbol, interval, *, start_ms, end_ms,
                         page_size, max_pages):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return [
            Candle(int(r["open_ms"]), r["open"], r["high"], r["low"],
                   r["close"], r["volume"])
            for r in self._rows
        ]


# ─── crafted 5m candle series (same recipe as the PR #311 tests) ────────────

def candle_source_rows():
    warmup = [99.0 - 0.5 * (WARMUP - 1 - i) for i in range(WARMUP)]
    placed = [(-WARMUP + j, c) for j, c in enumerate(warmup)] + [
        (i, c) for i, c in enumerate(CRAFTED)
    ]
    rows = []
    for bucket, close in placed:
        prev = rows[-1]["close"] if rows else float(close)
        rows.append({
            "open_ms": BASE_MS + bucket * MS,
            "open": prev,
            "high": max(prev, float(close)) + 0.5,
            "low": min(prev, float(close)) - 0.5,
            "close": float(close),
            "volume": 1.0,
        })
    return rows


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms / 1000.0


def build_runtime(store, rows, candle_rows, *, asof_ms, policies=None,
                  force_redeliver=False, client_error=None, enabled=True,
                  budget_confirmed=True):
    config = H2ProspectiveConfig(
        enabled=enabled,
        candle_budget_confirmed=budget_confirmed,
        candle_retry_backoff_s=0.0,
        candle_request_pause_s=0.0,
    )
    client = FakeBybitClient(candle_rows, error=client_error)
    provider = BybitClosedCandleProvider(
        budget=CandleApiBudget(
            sleep=lambda _s: None, retry_backoff_s=0.0, request_pause_s=0.0
        ),
        enabled=budget_confirmed,
        client_factory=lambda: client,
    )
    source_rows = rows
    runtime = H2ProspectiveRuntime(
        config=config,
        store=store,
        candle_provider=provider,
        source_connection_factory=lambda: FakeSourceConnection(
            source_rows, force_redeliver
        ),
        policies=policies,
        clock=Clock(asof_ms),
    )
    return runtime, provider, client


TURN1_W3 = H2Policy(variant_id=VARIANT_TURN_1, observation_window=3)


class TestHappyPath:
    def test_event_flows_to_confirmed_delayed(self):
        store = InMemoryProspectiveStore()
        rows = [make_source_row()]
        candles = candle_source_rows()
        # cycle 1: after the signal, before the O_1 close -> PENDING
        runtime, provider, client = build_runtime(
            store, rows, candles, asof_ms=SIGNAL_MS + 1_000,
            policies=[TURN1_W3],
        )
        report1 = runtime.run_cycle()
        assert report1.counters["source_events_accepted"] == 1
        assert report1.counters["variants_pending"] == 1
        assert report1.status == "RUNNING"
        # cycle 2: the O_1 candle is closed -> TURN_1 confirms
        runtime2, _, _ = build_runtime(
            store, rows, candles, asof_ms=BASE_MS + 19 * MS,
            policies=[TURN1_W3],
        )
        report2 = runtime2.run_cycle()
        assert report2.counters["variants_confirmed_delayed"] == 1
        stored = store.results[("evt-1", VARIANT_TURN_1, TURN1_W3.fingerprint())]
        assert stored["status"] == STATE_CONFIRMED_DELAYED
        # FR-05 fields present and honest
        payload = stored["result_payload"]
        assert payload["entry_candidate_price"] is None
        assert payload["entry_price_basis"] == ENTRY_PRICE_UNDETERMINED
        assert payload["confirmation_time_ms"] == BASE_MS + 19 * MS
        assert payload["h2_rsi_at_confirmation"] is not None
        # telemetry journal + health snapshots
        assert len(store.evaluations) == 2
        assert store.health_rows[-1]["status"] == "RUNNING"

    def test_fetch_happens_once_per_event_per_cycle(self):
        store = InMemoryProspectiveStore()
        runtime, provider, client = build_runtime(
            store, [make_source_row()], candle_source_rows(),
            asof_ms=BASE_MS + 19 * MS,
            policies=[
                TURN1_W3,
                H2Policy(variant_id="H2_RSI_TURN_2", observation_window=6),
            ],
        )
        runtime.run_cycle()
        assert client.calls == 1  # two variants share one fetch


class TestRestartRecovery:
    def test_restart_restores_state_without_recreating_outcomes(self):
        # criterion 13
        store = InMemoryProspectiveStore()
        rows = [make_source_row()]
        candles = candle_source_rows()
        r1, _, _ = build_runtime(store, rows, candles,
                                 asof_ms=SIGNAL_MS + 1_000,
                                 policies=[TURN1_W3])
        r1.run_cycle()
        assert store.get_watermark()[1] == "evt-1"

        # "restart": a fresh runtime over the SAME persistent store; the
        # source redelivers the same event (watermark regression) and the
        # evaluation advances to the confirmation candle.
        r2, _, _ = build_runtime(store, rows, candles,
                                 asof_ms=BASE_MS + 19 * MS,
                                 policies=[TURN1_W3], force_redeliver=True)
        report = r2.run_cycle()
        assert report.counters["idempotent_duplicates"] == 1
        assert report.counters["source_events_accepted"] == 0
        assert report.counters["variants_confirmed_delayed"] == 1
        keys = list(store.results)
        assert keys == [("evt-1", VARIANT_TURN_1, TURN1_W3.fingerprint())]

        # a THIRD cycle after the terminal outcome changes nothing
        r3, _, _ = build_runtime(store, rows, candles,
                                 asof_ms=BASE_MS + 22 * MS,
                                 policies=[TURN1_W3], force_redeliver=True)
        report3 = r3.run_cycle()
        assert report3.counters["variants_confirmed_delayed"] == 0
        assert len(store.results) == 1
        assert len(store.evaluations) == 2  # no re-evaluation of terminal


class TestFailClosed:
    def test_api_failure_leaves_state_pending(self):
        # criterion 14: fail closed, no fabricated outcome
        store = InMemoryProspectiveStore()
        runtime, provider, client = build_runtime(
            store, [make_source_row()], candle_source_rows(),
            asof_ms=BASE_MS + 19 * MS, policies=[TURN1_W3],
            client_error=RuntimeError("HTTP 429"),
        )
        report = runtime.run_cycle()
        assert report.counters["candle_fetch_errors"] == 1
        assert store.results == {}
        state = store.get_variant_state(
            source_event_key="evt-1",
            variant_id=VARIANT_TURN_1,
            policy_fingerprint=TURN1_W3.fingerprint(),
        )
        assert state.state == STATE_PENDING_CONFIRMATION

    def test_unconfirmed_budget_blocks_candle_source(self):
        # FR-02: budget unconfirmed -> BLOCKED_CANDLE_SOURCE, no requests
        store = InMemoryProspectiveStore()
        runtime, provider, client = build_runtime(
            store, [make_source_row()], candle_source_rows(),
            asof_ms=BASE_MS + 19 * MS, policies=[TURN1_W3],
            budget_confirmed=False,
        )
        report = runtime.run_cycle()
        assert report.status == RUNTIME_BLOCKED_CANDLE_SOURCE
        assert client.calls == 0
        assert report.counters["source_events_accepted"] == 1  # ingest runs
        assert store.results == {}


class TestNoFictionalExecution:
    def test_no_net_er_or_return_metric_anywhere(self):
        # criterion 16
        store = InMemoryProspectiveStore()
        runtime, _, _ = build_runtime(
            store, [make_source_row()], candle_source_rows(),
            asof_ms=BASE_MS + 19 * MS, policies=[TURN1_W3],
        )
        runtime.run_cycle()
        stored = store.results[("evt-1", VARIANT_TURN_1, TURN1_W3.fingerprint())]
        payload = stored["result_payload"]
        banned = ("net", "return", "pnl", "profit", "mae", "mfe", "expectancy",
                  "drawdown", "roi", "yield")
        for key in payload:
            assert not any(b in key.lower() for b in banned), key
        assert payload["entry_candidate_price"] is None
        assert payload["entry_price_basis"] == ENTRY_PRICE_UNDETERMINED
        assert payload["entry_reference_label"] == (
            "CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE"
        )

    def test_migration_has_no_yield_metrics_and_no_execution_price(self):
        from pathlib import Path

        migration = (
            Path(__file__).resolve().parents[1]
            / "sql" / "migrations" / "095_me_entry_quality_h2_prospective_v1.sql"
        ).read_text(encoding="utf-8")
        assert "entry_candidate_price NUMERIC" in migration
        assert "CHECK (entry_candidate_price IS NULL)" in migration
        for banned in ("net_er", "net_e[r]", "expected_return", "pnl"):
            assert banned not in migration.lower()


class TestDisabledRuntime:
    def test_disabled_makes_no_connection_no_request_no_write(self):
        # criterion 17
        store = InMemoryProspectiveStore()
        writes_before = store.writes

        def _explode():
            raise AssertionError("source connection must not be opened")

        config = H2ProspectiveConfig(enabled=False)
        provider = BybitClosedCandleProvider(
            budget=CandleApiBudget(), enabled=True,
            client_factory=_explode,
        )
        runtime = H2ProspectiveRuntime(
            config=config,
            store=store,
            candle_provider=provider,
            source_connection_factory=_explode,
            policies=[TURN1_W3],
        )
        report = runtime.run_cycle()
        assert report.status == RUNTIME_DISABLED
        assert store.writes == writes_before
        assert provider.budget.api_requests == 0

    def test_config_defaults_are_disabled_and_strict(self):
        assert H2ProspectiveConfig().enabled is False
        assert H2ProspectiveConfig().candle_budget_confirmed is False
        with pytest.raises(H2ProspectiveConfigError):
            load_config({"H2_PROSPECTIVE_ENABLED": "maybe"})
        config = load_config({"H2_PROSPECTIVE_ENABLED": "1"})
        assert config.enabled is True

    def test_runner_exits_before_any_io_when_disabled(self, monkeypatch):
        def _explode(*_a, **_k):
            raise AssertionError("no I/O may happen while disabled")

        monkeypatch.setattr(runner_mod, "_make_store", _explode)
        monkeypatch.setattr(runner_mod, "_make_source_connection", _explode)
        monkeypatch.setenv("H2_PROSPECTIVE_ENABLED", "0")
        assert runner_mod.main(["--once"]) == runner_mod.EXIT_NOT_ACTIVE


class TestPolicyPinning:
    def test_unpinned_candidate_grid_runs(self):
        store = InMemoryProspectiveStore()
        runtime, _, _ = build_runtime(
            store, [], candle_source_rows(), asof_ms=SIGNAL_MS,
        )
        assert len(runtime.policies) == 6  # 3 variants x windows 3/6

    def test_mismatched_fingerprint_pin_fails_closed(self):
        with pytest.raises(ValueError, match="POLICY_FINGERPRINT_MISMATCH"):
            H2ProspectiveRuntime(
                config=H2ProspectiveConfig(
                    enabled=True,
                    policy_fingerprint_pins=("00" * 32,),
                ),
                store=InMemoryProspectiveStore(),
                candle_provider=BybitClosedCandleProvider(
                    budget=CandleApiBudget(), enabled=False
                ),
                policies=[TURN1_W3],
            )


class TestProductionIsolation:
    def test_package_never_imports_trading_modules(self):
        import ast
        from pathlib import Path

        package_dir = (
            Path(__file__).resolve().parents[1]
            / "app" / "research" / "me_entry_quality_h2_prospective_v1"
        )
        banned = (
            "scanner_runner", "app.execution", "app.paper", "app.risk",
            "app.strategy", "app.scanners.scanners", "app.storage",
        )
        for py in package_dir.glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    for bad in banned:
                        assert not name.startswith(bad), (py.name, name)

    def test_migration_only_touches_its_own_schema(self):
        from pathlib import Path

        migration = (
            Path(__file__).resolve().parents[1]
            / "sql" / "migrations" / "095_me_entry_quality_h2_prospective_v1.sql"
        ).read_text(encoding="utf-8")
        assert "CREATE SCHEMA IF NOT EXISTS research_me_entry_quality_h2_prospective" in migration
        for bad in ("dds.", "research_me_upstream_coverage.", "research_me_entry_quality_h1"):
            assert bad not in migration, bad

    def test_source_reader_sql_is_select_only(self):
        assert_select_only("SELECT 1")
        with pytest.raises(ValueError):
            assert_select_only("DROP TABLE x")
