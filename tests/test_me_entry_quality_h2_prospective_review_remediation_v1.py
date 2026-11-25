"""PR #313 review remediation — regression tests for the five findings.

P1-Decimal   — exact, type-safe source prices through the whole runtime;
P1-Watermark — capture-order cursor + overlap: late-committed events with
               earlier signal_time are NEVER lost (mandatory scenario: B
               captured first, A delivered later with earlier signal_time —
               both accepted, both evaluated exactly once);
P1-Budget    — actual HTTP request accounting (pagination/retry), restart-safe
               hourly window (fake transport only — never real Bybit);
P2-Exit      — ``--once`` returns non-zero on failure and always closes the
               store;
P2-Migration — covered on real PostgreSQL in
               ``test_me_entry_quality_h2_prospective_pg_integration_v1.py``.

All tests are offline: fake source connection + fake Bybit client only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models import Candle
from app.research.me_entry_quality_h2_prospective_v1 import runner as runner_mod
from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
    BybitClosedCandleProvider,
    CandleApiBudget,
)
from app.research.me_entry_quality_h2_prospective_v1.config import (
    H2ProspectiveConfig,
)
from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    STATE_CONFIRMED_DELAYED,
    STATE_INVALID_SOURCE,
    parse_source_price,
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
from app.research.me_entry_quality_h2_v1.result import H2Policy, VARIANT_TURN_1
from app.research.me_upstream_source_v1.contracts import lineage_hash

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MS = 300_000

CRAFTED = (
    [100.0 + i for i in range(17)]
    + [118.0, 114.0, 116.5, 113.0, 111.5, 115.0]
)
WARMUP = 106

TERMINAL_ANY = {STATE_CONFIRMED_DELAYED, "EXPIRED"}


def source_row(key, *, signal_ms, captured_ms, price=None):
    """A #305 row with EXPLICIT signal and capture instants."""
    signal = EPOCH + timedelta(milliseconds=signal_ms)
    freeze = EPOCH + timedelta(milliseconds=signal_ms - 3_600_000)
    captured = EPOCH + timedelta(milliseconds=captured_ms)
    lineage = {
        "source_identity": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|BTCUSDT|LONG|5m|1",
        "source_signal_time": signal.isoformat(),
        "source_signal_candle_open_time": signal_ms // 300_000 * 300_000,
        "source_scanner": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        "source_direction": "LONG",
        "source_features": {"rsi_14": 71.25},
        "source_pipeline_fingerprint": "fp-test-1",
    }
    return {
        "source_event_key": key,
        "source_identity": lineage["source_identity"],
        "symbol": "BTCUSDT",
        "signal_time": signal,
        "captured_at": captured,
        "activation_freeze_ts": freeze,
        "signal_candle_open_time": signal_ms // 300_000 * 300_000,
        "entry_timeframe": "5m",
        "source_fingerprint": "fp-test-1",
        "source_reference_price": (
            Decimal("123.4567890123456789") if price is None else price
        ),
        "features": {
            "rsi_14": 71.25,
            "rsi_delta_3": 4.5,
            "entry_confirmation_rsi_rising": True,
        },
        "lineage": lineage,
        "lineage_hash": lineage_hash(lineage),
    }


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
        after_us, _after_us2, after_key, limit = params
        picked = [
            r for r in self._rows
            if r["captured_at"] > after_us
            or (r["captured_at"] == after_us and r["source_event_key"] > after_key)
        ]
        picked.sort(key=lambda r: (r["captured_at"], r["source_event_key"]))
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


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms / 1000.0


TURN1_W3 = H2Policy(variant_id=VARIANT_TURN_1, observation_window=3)


def build_runtime_at(store, rows, *, asof_ms, config=None, force_redeliver=False):
    config = config or H2ProspectiveConfig(
        enabled=True,
        candle_budget_confirmed=True,
        candle_retry_backoff_s=0.0,
        candle_request_pause_s=0.0,
    )
    client = FakeBybitClient(candle_source_rows())
    provider = BybitClosedCandleProvider(
        budget=CandleApiBudget(
            sleep=lambda _s: None, retry_backoff_s=0.0, request_pause_s=0.0
        ),
        enabled=True,
        client_factory=lambda: client,
    )
    runtime = H2ProspectiveRuntime(
        config=config,
        store=store,
        candle_provider=provider,
        source_connection_factory=lambda: FakeSourceConnection(
            rows, force_redeliver
        ),
        policies=[TURN1_W3],
        clock=Clock(asof_ms),
    )
    return runtime, client


# ─── P1-Watermark: mandatory late-delivery scenarios ────────────────────────

class TestLateDeliveryNeverLost:
    def test_signal_order_inversion_both_accepted_both_evaluated_once(self):
        # MANDATORY scenario: B (signal 10:10) captured first; A (signal
        # 10:05 — EARLIER) captured/delivered later.  A signal_time watermark
        # would drop A forever; the capture-order cursor must not.
        store = InMemoryProspectiveStore()
        s_a = BASE_MS + 17 * MS + 60_000   # A: earlier signal
        s_b = BASE_MS + 18 * MS + 60_000   # B: later signal
        rows = [
            source_row("evt-a", signal_ms=s_a, captured_ms=BASE_MS + 20 * MS),
            source_row("evt-b", signal_ms=s_b, captured_ms=s_b + 5_000),
        ]
        runtime, _ = build_runtime_at(store, rows, asof_ms=BASE_MS + 22 * MS)
        report = runtime.run_cycle()
        assert report.counters["source_events_accepted"] == 2
        # both events reached a terminal outcome exactly once
        assert len(store.results) == 2
        assert all(
            r["status"] in TERMINAL_ANY | {STATE_INVALID_SOURCE}
            for r in store.results.values()
        )
        keys = {k[0] for k in store.results}
        assert keys == {"evt-a", "evt-b"}
        # a second cycle (overlap re-read) re-delivers but never re-evaluates
        runtime2, _ = build_runtime_at(store, rows, asof_ms=BASE_MS + 23 * MS)
        report2 = runtime2.run_cycle()
        assert report2.counters["idempotent_duplicates"] == 2
        assert report2.counters["source_events_accepted"] == 0
        assert len(store.results) == 2

    def test_late_commit_after_restart_with_older_capture_is_picked_up(self):
        # capture-to-commit disorder across a restart: A's capture instant is
        # OLDER than the cursor, yet its commit became visible only later.
        store = InMemoryProspectiveStore()
        s_b = BASE_MS + 18 * MS + 60_000
        b = source_row("evt-b", signal_ms=s_b, captured_ms=s_b + 5_000)
        r1, _ = build_runtime_at(store, [b], asof_ms=BASE_MS + 19 * MS)
        r1.run_cycle()
        assert store.get_cursor()[1] == "evt-b"

        # "restart": A becomes visible late, captured BEFORE B's capture
        a = source_row(
            "evt-a",
            signal_ms=BASE_MS + 17 * MS + 60_000,
            captured_ms=BASE_MS + 17 * MS + 10_000,
        )
        r2, _ = build_runtime_at(
            store, [b, a], asof_ms=BASE_MS + 22 * MS, force_redeliver=False
        )
        report2 = r2.run_cycle()
        # the overlap rewind re-reads B (idempotent) and finds A
        assert report2.counters["idempotent_duplicates"] == 1
        assert report2.counters["source_events_accepted"] == 1
        keys = {k[0] for k in store.results}
        assert keys == {"evt-a", "evt-b"}
        evals_after_2 = len(store.evaluations)

        # a third cycle evaluates nothing again (exactly-once)
        r3, _ = build_runtime_at(store, [b, a], asof_ms=BASE_MS + 23 * MS)
        r3.run_cycle()
        assert len(store.evaluations) == evals_after_2
        assert len(store.results) == 2

    def test_equal_captured_at_ties_are_read_exactly_once(self):
        # two rows sharing ONE captured_at: the (captured_at, key) cursor
        # must carry both across batch boundaries
        store = InMemoryProspectiveStore()
        c = BASE_MS + 20 * MS
        rows = [
            source_row("evt-a", signal_ms=BASE_MS + 17 * MS + 60_000, captured_ms=c),
            source_row("evt-b", signal_ms=BASE_MS + 18 * MS + 60_000, captured_ms=c),
        ]
        config = H2ProspectiveConfig(
            enabled=True,
            candle_budget_confirmed=True,
            candle_retry_backoff_s=0.0,
            candle_request_pause_s=0.0,
            source_batch_limit=1,  # force one row per batch
        )
        runtime, _ = build_runtime_at(
            store, rows, asof_ms=BASE_MS + 22 * MS, config=config
        )
        report = runtime.run_cycle()
        assert report.counters["source_events_accepted"] == 2
        assert {k[0] for k in store.results} == {"evt-a", "evt-b"}
        assert store.get_cursor() == (c * 1000, "evt-b")  # ms -> us

    def test_batch_boundary_drains_bounded_batches_without_loss(self):
        # more rows than one cycle's batch cap: the rest is picked up by the
        # NEXT cycle from the persisted cursor — nothing is skipped
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                f"evt-{i}",
                signal_ms=BASE_MS + (17 + i) * MS + 60_000,
                captured_ms=BASE_MS + (17 + i) * MS + 70_000,
            )
            for i in range(3)
        ]
        config = H2ProspectiveConfig(
            enabled=True,
            candle_budget_confirmed=True,
            candle_retry_backoff_s=0.0,
            candle_request_pause_s=0.0,
            source_batch_limit=1,
            source_max_batches_per_cycle=2,
        )
        runtime, _ = build_runtime_at(
            store, rows, asof_ms=BASE_MS + 22 * MS, config=config
        )
        report = runtime.run_cycle()
        assert report.counters["source_events_accepted"] == 2  # capped batches
        runtime2, _ = build_runtime_at(
            store, rows, asof_ms=BASE_MS + 23 * MS, config=config
        )
        report2 = runtime2.run_cycle()
        assert report2.counters["source_events_accepted"] == 1
        assert {k[0] for k in store.results} == {"evt-0", "evt-1", "evt-2"}

    def test_crash_between_ingest_and_checkpoint_replays_idempotently(self):
        # crash window: ingests committed, cursor checkpoint lost.  The
        # checkpoint-last design must replay without duplicating outcomes.
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                "evt-a",
                signal_ms=BASE_MS + 18 * MS + 60_000,
                captured_ms=BASE_MS + 18 * MS + 70_000,
            )
        ]
        real_set_cursor = store.set_cursor
        store.crash_checkpoint = True

        def crashing_set_cursor(us, key):
            if getattr(store, "crash_checkpoint", False):
                store.crash_checkpoint = False
                raise RuntimeError("simulated crash before checkpoint")
            return real_set_cursor(us, key)

        store.set_cursor = crashing_set_cursor
        runtime, _ = build_runtime_at(store, rows, asof_ms=BASE_MS + 22 * MS)
        with pytest.raises(RuntimeError):
            runtime.run_cycle()
        assert store.get_cursor() == (0, "")  # checkpoint never advanced
        assert len(store.ingest_rows) == 1  # ingest DID commit

        # "restart": the same rows are replayed
        runtime2, _ = build_runtime_at(store, rows, asof_ms=BASE_MS + 23 * MS)
        report2 = runtime2.run_cycle()
        assert report2.counters["idempotent_duplicates"] == 1
        assert report2.counters["source_events_accepted"] == 0
        assert len(store.results) == 1  # one outcome, not two
        assert store.get_cursor() != (0, "")  # checkpoint catches up


# ─── P1-Decimal: exact prices end to end ────────────────────────────────────

class TestDecimalExactness:
    def test_long_decimal_reaches_the_engine_verbatim(self):
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                "evt-1",
                signal_ms=BASE_MS + 18 * MS + 60_000,
                captured_ms=BASE_MS + 18 * MS + 70_000,
                price=Decimal("123.4567890123456789"),
            )
        ]
        runtime, _ = build_runtime_at(store, rows, asof_ms=BASE_MS + 22 * MS)
        report = runtime.run_cycle()
        assert report.counters.get("variants_invalid_source", 0) == 0
        stored = store.ingest_rows["evt-1"]["row"]
        assert stored["source_reference_price"] == Decimal("123.4567890123456789")
        assert parse_source_price(stored["source_reference_price"]) == Decimal(
            "123.4567890123456789"
        )

    def test_unparseable_restored_price_becomes_invalid_source(self):
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                "evt-1",
                signal_ms=BASE_MS + 18 * MS + 60_000,
                captured_ms=BASE_MS + 18 * MS + 70_000,
            )
        ]
        runtime, _ = build_runtime_at(
            store, rows, asof_ms=BASE_MS + 18 * MS + 61_000
        )
        runtime.run_cycle()  # creates the PENDING variant state

        def broken_get_source_row(key):
            raise ValueError("stored price is not numeric")

        store.get_source_row = broken_get_source_row
        runtime2, _ = build_runtime_at(store, rows, asof_ms=BASE_MS + 22 * MS)
        report2 = runtime2.run_cycle()
        assert report2.counters["variants_invalid_source"] == 1
        stored = list(store.results.values())[0]
        assert stored["status"] == STATE_INVALID_SOURCE

    def test_nan_inf_zero_negative_are_rejected_at_parse(self):
        for bad in (
            float("nan"), float("inf"), float("-inf"), 0, -1, -0.0001,
            True, "nan", "inf", "1e5", "-5", "12,34", " 12", "12 ", "",
        ):
            with pytest.raises(ValueError):
                parse_source_price(bad)

    def test_exact_string_form_is_plain_decimal(self):
        from app.research.me_entry_quality_h2_prospective_v1.contracts import (
            exact_price_str,
        )
        assert exact_price_str(Decimal("123.4567890123456789")) == (
            "123.4567890123456789"
        )
        assert exact_price_str(0.1) == "0.1"  # repr-based, no binary artifact


# ─── P2-Exit: honest exit codes, always-closed resources ────────────────────

class _FakeRuntime:
    def __init__(self, error=None):
        self._error = error

    def run_cycle(self):
        if self._error is not None:
            raise self._error
        from app.research.me_entry_quality_h2_prospective_v1.runtime import (
            CycleReport,
        )
        return CycleReport(status="RUNNING")


class _FakeStore:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class TestRunnerExitCodes:
    def test_successful_once_exits_zero_and_closes_store(self, monkeypatch):
        fake_store = _FakeStore()
        monkeypatch.setattr(
            runner_mod,
            "build_runtime",
            lambda _cfg: (_FakeRuntime(), fake_store),
        )
        monkeypatch.setenv("H2_PROSPECTIVE_ENABLED", "1")
        monkeypatch.setenv("H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES", "1")
        assert runner_mod.main(["--once"]) == runner_mod.EXIT_OK
        assert fake_store.closed == 1

    def test_failed_once_exits_nonzero_and_closes_store(self, monkeypatch):
        fake_store = _FakeStore()
        monkeypatch.setattr(
            runner_mod,
            "build_runtime",
            lambda _cfg: (_FakeRuntime(RuntimeError("boom")), fake_store),
        )
        monkeypatch.setenv("H2_PROSPECTIVE_ENABLED", "1")
        monkeypatch.setenv("H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES", "1")
        assert runner_mod.main(["--once"]) == runner_mod.EXIT_CYCLE_FAILED
        assert runner_mod.EXIT_CYCLE_FAILED != 0
        assert fake_store.closed == 1

    def test_disabled_once_exits_not_active(self, monkeypatch):
        def _explode(*_a, **_k):
            raise AssertionError("no I/O may happen while disabled")

        monkeypatch.setattr(runner_mod, "_make_store", _explode)
        monkeypatch.setattr(runner_mod, "_make_source_connection", _explode)
        monkeypatch.setenv("H2_PROSPECTIVE_ENABLED", "0")
        assert runner_mod.main(["--once"]) == runner_mod.EXIT_NOT_ACTIVE

    def test_config_error_exits_config_error(self, monkeypatch):
        monkeypatch.setenv("H2_PROSPECTIVE_ENABLED", "1")  # no pins, no opt-in
        monkeypatch.delenv("H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES", raising=False)
        monkeypatch.delenv("H2_PROSPECTIVE_POLICY_FINGERPRINT_PINS", raising=False)
        assert runner_mod.main(["--once"]) == runner_mod.EXIT_CONFIG_ERROR


class TestRunnerClientFactoryPins:
    def test_research_client_is_single_attempt_per_page(self):
        # review P1-budget: the "1 pagination page = 1 HTTP request" budget
        # invariant is pinned by the research client factory settings
        config = H2ProspectiveConfig(
            enabled=True, candle_budget_confirmed=True,
            allow_unfrozen_policies=True,
        )
        client = runner_mod._make_bybit_client_factory(config)()
        assert client.settings.bybit_max_attempts == 1
        assert getattr(client.settings, "bybit_rate_limit_max_retries", 0) == 0

    def test_runtime_budget_reserves_exact_page_cost(self):
        config = H2ProspectiveConfig(
            enabled=True, candle_budget_confirmed=True,
            allow_unfrozen_policies=True,
        )
        runtime, _store = runner_mod.build_runtime(
            config,
            store_factory=InMemoryProspectiveStore,
            source_connection_factory=lambda: None,
        )
        budget = runtime._provider.budget
        assert budget.requests_per_page == 1
