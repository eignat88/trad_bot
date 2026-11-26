"""PR #313 P1-overlap review remediation — reconciliation regression matrix.

Review ``RE_REVIEW_CHANGES_REQUIRED`` (Source Overlap Starvation): the
overlap reconciliation must have its OWN durable cursor, its own per-cycle
batch budget, deterministic pass bounds, crash-safe checkpoints, and an
HONEST completeness boundary (FR-05 diagnostics — no absolute no-loss claim
beyond the documented overlap window).

The 12 mandatory scenarios (review §5) run fully offline against synthetic
#305 rows and a fake connection — never a real Bybit request.  The
PostgreSQL side (real durable overlap cursor, migration 096 apply/re-apply)
lives in ``test_me_entry_quality_h2_prospective_pg_integration_v1.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import Candle
from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
    BybitClosedCandleProvider,
    CandleApiBudget,
)
from app.research.me_entry_quality_h2_prospective_v1.config import (
    H2ProspectiveConfig,
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

# Signal buckets 17/18 sit inside the crafted RSI-weakening candle pattern —
# exactly like the PR-#311-proven scenarios: they CONFIRM terminally.
SIGNAL_18_MS = BASE_MS + 18 * MS + 60_000
SIGNAL_17_MS = BASE_MS + 17 * MS + 60_000  # earlier signal (inversion cases)
# capture grid origin: right at the bucket-18 signal (capture >= freeze)
CAP0_MS = SIGNAL_18_MS
ASOF_MS = BASE_MS + 22 * MS  # past every O_1 close: terminal outcomes fire

CRAFTED = (
    [100.0 + i for i in range(17)]
    + [118.0, 114.0, 116.5, 113.0, 111.5, 115.0]
)
WARMUP = 106

TURN1_W3 = H2Policy(variant_id=VARIANT_TURN_1, observation_window=3)


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


class FakeSourceCursor:
    """SELECT-only fake honoring the (captured_at, key) cursor and the pass
    ceiling, plus the backlog COUNT query."""

    def __init__(self, rows):
        self._rows = rows
        self._result = []

    def execute(self, sql, params=()):
        assert_select_only(sql)
        if sql.lstrip().upper().startswith("SELECT COUNT("):
            after_us, _after_us2, after_key, ceiling_us = params
            self._result = [
                (len(self._pick(after_us, after_key, ceiling_us)),)
            ]
            return
        after_us, _after_us2, after_key, ceiling_us, limit = params
        picked = self._pick(after_us, after_key, ceiling_us)
        self._result = [self._tuple(r) for r in picked[: int(limit)]]

    def _pick(self, after_us, after_key, ceiling_us):
        picked = [
            r for r in self._rows
            if (
                r["captured_at"] > after_us
                or (r["captured_at"] == after_us
                    and r["source_event_key"] > after_key)
            )
            and r["captured_at"] <= ceiling_us
        ]
        picked.sort(key=lambda r: (r["captured_at"], r["source_event_key"]))
        return picked

    @staticmethod
    def _tuple(row):
        return tuple(row.get(col) for col in SOURCE_EVENT_COLUMNS)

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        return None


class FakeSourceConnection:
    """Holds the MUTABLE row list by reference — tests append rows between
    cycles to simulate late commits becoming visible."""

    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def cursor(self):
        return FakeSourceCursor(self._rows)

    def close(self):
        self.closed = True


class FakeBybitClient:
    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    def get_klines_range(self, symbol, interval, *, start_ms, end_ms,
                         page_size, max_pages):
        self.calls += 1
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


def make_config(**overrides):
    base = dict(
        enabled=True,
        candle_budget_confirmed=True,
        candle_retry_backoff_s=0.0,
        candle_request_pause_s=0.0,
        # these tests exercise INGEST/reconciliation, not the API budget:
        # evaluation of many events must not trip the research request caps
        max_events_per_cycle=10_000,
    )
    base.update(overrides)
    return H2ProspectiveConfig(**base)


def build_runtime(store, rows, *, asof_ms, config=None):
    config = config or make_config()
    client = FakeBybitClient(candle_source_rows())
    provider = BybitClosedCandleProvider(
        budget=CandleApiBudget(
            sleep=lambda _s: None,
            retry_backoff_s=0.0,
            request_pause_s=0.0,
            max_requests_per_cycle=100_000,
            max_requests_per_hour=1_000_000,
        ),
        enabled=True,
        client_factory=lambda: client,
    )
    runtime = H2ProspectiveRuntime(
        config=config,
        store=store,
        candle_provider=provider,
        source_connection_factory=lambda: FakeSourceConnection(rows),
        policies=[TURN1_W3],
        clock=Clock(asof_ms),
    )
    return runtime


def _us(ms_value: int) -> int:
    return int(ms_value) * 1000


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 1 + KEY MANDATORY: >1000 overlap rows, 2 batches/cycle, late
# event near the END of the window, several cycles AND a restart — the late
# event is discovered EXACTLY ONCE and never re-outcomed.
# ═══════════════════════════════════════════════════════════════════════════
class TestOverlapStarvation:
    def test_thousand_row_overlap_drains_progressively_with_restart(self):
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                f"evt-{i:04d}",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + (i - 1) * 1000,
            )
            for i in range(1, 1001)
        ]
        config = make_config(
            source_batch_limit=100,
            source_forward_batches_per_cycle=12,
            source_overlap_batches_per_cycle=2,  # review: 2-3 batches/cycle
            source_overlap_s=7200,
        )
        runtime = build_runtime(
            store, rows, asof_ms=ASOF_MS + 10_000, config=config
        )
        report1 = runtime.run_cycle()
        assert report1.counters["source_events_accepted"] == 1000
        assert report1.counters["forward_batches_scanned"] == 10

        # the late commit becomes visible AFTER the forward cursor passed its
        # capture instant: captured near the END of the overlap window,
        # signal_time EARLIER than the rest (signal-order inversion too)
        rows.append(
            source_row(
                "evt-late",
                signal_ms=SIGNAL_17_MS,
                captured_ms=CAP0_MS + 998_500,
            )
        )

        reports = [report1]
        prev_cursor = -1
        for cycle in range(2, 4):  # bounded budgets drain the pass slowly
            rt = build_runtime(
                store, rows, asof_ms=ASOF_MS + cycle * 10_000, config=config
            )
            rep = rt.run_cycle()
            reports.append(rep)
            # FR-02: the reconciliation budget is honoured every cycle
            assert rep.counters["overlap_batches_scanned"] <= 2
            state = store.get_overlap_state()
            assert state["pass_active"] is True  # still mid-pass
            assert state["cursor_captured_at_us"] >= prev_cursor
            prev_cursor = state["cursor_captured_at_us"]

        # restart MID-pass: a fresh runtime resumes from durable state
        rt_restart = build_runtime(
            store, rows, asof_ms=ASOF_MS + 50_000, config=config
        )
        rep_restart = rt_restart.run_cycle()
        assert rep_restart.counters["overlap_restart_recovery"] is True
        reports.append(rep_restart)
        for cycle in range(2, 8):
            rep = build_runtime(
                store, rows, asof_ms=ASOF_MS + 50_000 + cycle * 10_000,
                config=config,
            ).run_cycle()
            reports.append(rep)
            assert rep.counters["overlap_batches_scanned"] <= 2
            if rep.counters["overlap_pass_completed"]:
                break
        assert any(r.counters["overlap_pass_completed"] for r in reports)

        # the late event near the window end was discovered EXACTLY ONCE
        assert sum(r.counters["late_events_discovered"] for r in reports) == 1
        assert store.ingest_event_count() == 1001
        late_results = [k for k in store.results if k[0] == "evt-late"]
        assert len(late_results) == 1
        assert store.get_overlap_state()["pass_active"] is False

        # a further cycle re-reads the window idempotently — no second
        # discovery, no second outcome
        rep_after = build_runtime(
            store, rows, asof_ms=ASOF_MS + 200_000, config=config
        ).run_cycle()
        assert rep_after.counters["late_events_discovered"] == 0
        assert len([k for k in store.results if k[0] == "evt-late"]) == 1

    def test_overlap_progress_is_checkpointed_every_cycle(self):
        # review FR-03: progress is durable after every processed portion —
        # the sweep cursor never re-reads already-swept pages on later cycles
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                f"evt-{i:02d}",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + (i - 1) * 1000,
            )
            for i in range(1, 6)
        ]
        config = make_config(
            source_batch_limit=1,
            source_forward_batches_per_cycle=10,
            source_overlap_batches_per_cycle=1,
            source_overlap_s=7200,
        )
        runtime = build_runtime(
            store, rows, asof_ms=ASOF_MS + 10_000, config=config
        )
        runtime.run_cycle()
        first = store.get_overlap_state()
        assert first["pass_active"] is True
        assert first["cursor_captured_at_us"] > 0

        runtime2 = build_runtime(
            store, rows, asof_ms=ASOF_MS + 20_000, config=config
        )
        rep2 = runtime2.run_cycle()
        second = store.get_overlap_state()
        # continued from the durable cursor: exactly one NEW batch swept
        assert second["cursor_captured_at_us"] > first["cursor_captured_at_us"]
        assert rep2.counters["overlap_batches_scanned"] == 1
        assert rep2.counters["idempotent_duplicates"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios 3-4: forward and overlap backlogs cannot starve each other.
# ═══════════════════════════════════════════════════════════════════════════
class TestIndependentBudgets:
    def test_forward_backlog_cannot_starve_reconciliation(self):
        # scenario 3: the forward phase permanently fills its budget — the
        # overlap phase still runs with its OWN reserved budget
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                f"evt-{i:02d}",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + (i - 1) * 1000,
            )
            for i in range(1, 21)  # 20 rows > forward budget * limit
        ]
        config = make_config(
            source_batch_limit=2,
            source_forward_batches_per_cycle=2,   # saturated by forward
            source_overlap_batches_per_cycle=1,
            source_overlap_s=7200,
        )
        runtime = build_runtime(
            store, rows, asof_ms=ASOF_MS + 10_000, config=config
        )
        rep = runtime.run_cycle()
        assert rep.counters["forward_batches_scanned"] == 2  # budget full
        assert rep.counters["overlap_batches_scanned"] == 1  # still ran

    def test_reconciliation_backlog_cannot_block_forward_progress(self):
        # scenario 4: a huge reconciliation backlog (200 rows in the pass)
        # never prevents NEW forward rows from being ingested
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                f"old-{i:04d}",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + (i - 1) * 1000,
            )
            for i in range(1, 201)  # 200 rows => 20 overlap batches needed
        ]
        config = make_config(
            source_batch_limit=10,
            source_forward_batches_per_cycle=4,
            source_overlap_batches_per_cycle=1,  # backlog drains slowly
            source_overlap_s=7200,
        )
        runtime = build_runtime(
            store, rows, asof_ms=ASOF_MS + 10_000, config=config
        )
        rep1 = runtime.run_cycle()
        assert rep1.counters["source_events_accepted"] == 40  # forward partial
        assert store.get_overlap_state()["pass_active"] is True  # big backlog

        # a brand-new forward row must be ingested despite the backlog
        rows.append(
            source_row(
                "evt-new",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + 300_000,
            )
        )
        found = False
        total_overlap_batches = 0
        for cycle in range(2, 8):
            rt2 = build_runtime(
                store, rows, asof_ms=ASOF_MS + cycle * 10_000, config=config
            )
            rep2 = rt2.run_cycle()
            # reconciliation keeps working within its own budget every cycle
            # (0 batches only at a pass boundary, where the pass completes)
            assert rep2.counters["overlap_batches_scanned"] <= 1
            total_overlap_batches += rep2.counters["overlap_batches_scanned"]
            # ... and forward keeps progressing regardless
            assert rep2.counters["source_events_accepted"] > 0
            if any(k[0] == "evt-new" for k in store.results):
                found = True
                break
        assert found
        assert total_overlap_batches >= 4  # the backlog genuinely drained


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios 5-6: restart / crash safety of the durable pass cursor.
# ═══════════════════════════════════════════════════════════════════════════
class TestCrashSafety:
    def test_restart_mid_pass_resumes_without_resweeping(self):
        # scenario 5: a restart in the middle of a pass resumes at the
        # durable position (reported once as overlap_restart_recovery) and
        # does NOT re-sweep the already covered pages
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                f"evt-{i:02d}",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + (i - 1) * 1000,
            )
            for i in range(1, 31)  # 30 rows, 3 overlap batches needed
        ]
        config = make_config(
            source_batch_limit=10,
            source_forward_batches_per_cycle=4,
            source_overlap_batches_per_cycle=1,
            source_overlap_s=7200,
        )
        rt1 = build_runtime(store, rows, asof_ms=ASOF_MS + 10_000,
                            config=config)
        rt1.run_cycle()
        mid = store.get_overlap_state()
        assert mid["pass_active"] is True
        assert mid["cursor_captured_at_us"] > 0

        # "restart": a new runtime on the same durable store
        rt2 = build_runtime(store, rows, asof_ms=ASOF_MS + 20_000,
                            config=config)
        rep = rt2.run_cycle()
        assert rep.counters["overlap_restart_recovery"] is True
        # only the NEXT page is swept (10 rows), never the covered 10 again
        assert rep.counters["overlap_batches_scanned"] == 1
        assert rep.counters["idempotent_duplicates"] == 10
        after = store.get_overlap_state()
        assert after["cursor_captured_at_us"] > mid["cursor_captured_at_us"]

    def test_crash_between_overlap_ingest_and_checkpoint_replays(self):
        # scenario 6 / FR-04 point 3: crash AFTER ingest + states but BEFORE
        # the overlap checkpoint — the replay is idempotent, one outcome
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                "evt-a",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + 1000,
            )
        ]
        real_save = store.save_overlap_state
        store.crash_overlap = {"armed": True}

        def crashing_save(state):
            if store.crash_overlap["armed"]:
                store.crash_overlap["armed"] = False
                raise RuntimeError("simulated crash before overlap checkpoint")
            return real_save(state)

        store.save_overlap_state = crashing_save
        config = make_config(source_overlap_batches_per_cycle=1)
        rt1 = build_runtime(store, rows, asof_ms=ASOF_MS + 10_000,
                            config=config)
        try:
            rt1.run_cycle()
            crashed = False
        except RuntimeError:
            crashed = True
        assert crashed  # the checkpoint failure must propagate (no silent loss)
        assert len(store.ingest_rows) == 1  # ingest + states DID commit

        # "restart": replay from the durable (unmoved) state — idempotent
        rt2 = build_runtime(store, rows, asof_ms=ASOF_MS + 20_000,
                            config=config)
        rep2 = rt2.run_cycle()
        assert rep2.counters["source_events_accepted"] == 0
        assert len(store.results) == 1  # exactly one outcome


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios 7-9: capture ties, signal-order inversion, redelivery.
# ═══════════════════════════════════════════════════════════════════════════
class TestCaptureOrderEdgeCases:
    def test_equal_captured_at_ties_survive_pass_boundaries(self):
        # scenario 7: many rows sharing ONE captured_at straddling batch
        # boundaries inside the pass — nothing is dropped at any boundary
        store = InMemoryProspectiveStore()
        c_ms = CAP0_MS + 1000
        rows = [
            source_row(f"evt-{i}", signal_ms=SIGNAL_18_MS, captured_ms=c_ms)
            for i in range(5)
        ]
        config = make_config(
            source_batch_limit=1,
            source_forward_batches_per_cycle=10,
            source_overlap_batches_per_cycle=2,
            source_overlap_s=7200,
        )
        runtime = build_runtime(store, rows, asof_ms=ASOF_MS + 10_000,
                                config=config)
        runtime.run_cycle()
        assert {k[0] for k in store.results} == {f"evt-{i}" for i in range(5)}
        assert store.get_cursor() == (_us(c_ms), "evt-4")

    def test_late_delivery_with_earlier_signal_time_is_accepted_by_pass(self):
        # scenario 8: A has an EARLIER signal_time but is delivered late —
        # the overlap pass (not the forward scan) discovers and accepts it
        store = InMemoryProspectiveStore()
        b = source_row(
            "evt-b",
            signal_ms=SIGNAL_18_MS,
            captured_ms=CAP0_MS + 1000,
        )
        rows = [b]
        config = make_config(source_overlap_s=7200)
        rt1 = build_runtime(store, rows, asof_ms=ASOF_MS + 10_000,
                            config=config)
        rt1.run_cycle()
        assert store.get_cursor()[1] == "evt-b"

        a = source_row(
            "evt-a",
            signal_ms=SIGNAL_17_MS,      # EARLIER signal
            captured_ms=CAP0_MS + 500,   # captured before B
        )
        rows.append(a)  # late commit becomes visible
        rt2 = build_runtime(store, rows, asof_ms=ASOF_MS + 20_000,
                            config=config)
        rep2 = rt2.run_cycle()
        assert rep2.counters["source_events_accepted"] == 1
        assert rep2.counters["late_events_discovered"] == 1
        assert {k[0] for k in store.results} == {"evt-a", "evt-b"}

    def test_redelivery_never_recreates_outcome(self):
        # scenario 9: duplicate delivery across cycles/runs never creates a
        # second outcome and never re-enters terminal states
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                "evt-a",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + 1000,
            )
        ]
        config = make_config(source_overlap_s=7200)
        for i in range(3):
            rt = build_runtime(store, rows, asof_ms=ASOF_MS + (i + 1) * 10_000,
                               config=config)
            rt.run_cycle()
        assert store.ingest_event_count() == 1
        assert len(store.results) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 10: the coverage boundary is EXPLICIT (FR-05) — events beyond the
# guaranteed overlap are not silently claimed as covered.
# ═══════════════════════════════════════════════════════════════════════════
class TestCoverageBoundary:
    def test_event_outside_guaranteed_overlap_is_explicitly_recorded(self):
        store = InMemoryProspectiveStore()
        b = source_row(
            "evt-b",
            signal_ms=SIGNAL_18_MS,
            captured_ms=CAP0_MS + 1000,
        )
        rows = [b]
        config = make_config(source_overlap_s=0)  # zero documented window
        rt1 = build_runtime(store, rows, asof_ms=ASOF_MS + 10_000,
                            config=config)
        rt1.run_cycle()
        b_pos_us = store.get_cursor()[0]

        # a straggler captured BELOW the guaranteed floor appears
        a = source_row(
            "evt-a",
            signal_ms=SIGNAL_17_MS,
            captured_ms=CAP0_MS + 500,
        )
        rows.append(a)
        rt2 = build_runtime(store, rows, asof_ms=ASOF_MS + 20_000,
                            config=config)
        rep2 = rt2.run_cycle()
        # it is NOT silently claimed covered: the published guarantee floor
        # lies strictly ABOVE its capture instant, and the coverage contract
        # is part of every report
        overlap = rep2.details["overlap"]
        assert overlap["guarantee_floor_us"] >= b_pos_us
        assert overlap["guarantee_floor_us"] > _us(CAP0_MS + 500)
        assert overlap["coverage_guarantee"] in (
            "DOCUMENTED_OVERLAP_WINDOW",
            "BROKEN_WINDOW_CONTIGUOUS_SWEEP",
        )
        # and the event itself was correctly NOT reconciled (out of scope)
        assert not any(k[0] == "evt-a" for k in store.results)

    def test_fall_behind_beyond_window_reports_degraded_coverage(self):
        # FR-03/FR-05: when the sweep falls behind by MORE than the window,
        # the pass stays contiguous (no unchecked range is ever skipped) and
        # the cycle reports OVERLAP_COVERAGE_DEGRADED — never a false OK
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                "evt-1",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS,
            )
        ]
        config = make_config(
            source_batch_limit=10,
            source_forward_batches_per_cycle=4,
            source_overlap_batches_per_cycle=1,
            source_overlap_s=1,  # tiny window: easy to fall behind
        )
        rt1 = build_runtime(store, rows, asof_ms=ASOF_MS + 10_000,
                            config=config)
        rt1.run_cycle()  # pass 1 completes over [floor1, c1]
        prev_ceiling = store.get_overlap_state()["pass_ceiling_us"]

        # forward jumps FAR ahead (400s >> 1s window) before the next pass
        for i in range(2, 6):
            rows.append(
                source_row(
                    f"evt-{i}",
                    signal_ms=SIGNAL_18_MS,
                    captured_ms=CAP0_MS + i * 100_000,
                )
            )
        rt2 = build_runtime(store, rows, asof_ms=ASOF_MS + 20_000,
                            config=config)
        rep2 = rt2.run_cycle()
        assert rep2.status == "OVERLAP_COVERAGE_DEGRADED"
        assert rep2.counters["overlap_coverage_degraded"] is True
        assert rep2.details["overlap"]["coverage_guarantee"] == (
            "BROKEN_WINDOW_CONTIGUOUS_SWEEP"
        )
        # FR-03 no-gap rule: the new pass floor never EXCLUDES the unchecked
        # range left by the previous pass
        assert rep2.details["overlap"]["pass_floor_us"] <= prev_ceiling


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 11 + 12: empty source, and long runs with moving window bounds
# never jump over unchecked ranges.
# ═══════════════════════════════════════════════════════════════════════════
class TestPassBoundaries:
    def test_zero_source_events_report_source_empty_waiting(self):
        store = InMemoryProspectiveStore()
        config = make_config(source_overlap_s=7200)
        rt = build_runtime(store, [], asof_ms=ASOF_MS + 10_000,
                           config=config)
        rep = rt.run_cycle()
        assert rep.status == "SOURCE_EMPTY_WAITING"

    def test_pass_bounds_across_cycles_never_skip_unchecked_ranges(self):
        # scenario 12: many passes with changing window bounds — every pass
        # floor stays <= the previous pass ceiling (no silent gaps), and
        # every pass ceiling is pinned at pass start
        store = InMemoryProspectiveStore()
        rows = []
        config = make_config(
            source_batch_limit=10,
            source_forward_batches_per_cycle=2,
            source_overlap_batches_per_cycle=1,
            source_overlap_s=30,
        )
        floors, ceilings = [], []
        for cycle in range(1, 9):
            # each cycle: 3 new rows (forward backlog keeps moving)
            for j in range(3):
                idx = (cycle - 1) * 3 + j + 1
                rows.append(
                    source_row(
                        f"evt-{idx:02d}",
                        signal_ms=SIGNAL_18_MS,
                        captured_ms=CAP0_MS + idx * 1000,
                    )
                )
            rt = build_runtime(
                store, rows, asof_ms=ASOF_MS + cycle * 10_000, config=config
            )
            rep = rt.run_cycle()
            overlap = rep.details["overlap"]
            floors.append(overlap["pass_floor_us"])
            ceilings.append(overlap["pass_ceiling_us"])

        # contiguity across PASS STARTS: whenever a pass boundary was
        # recorded, the new floor never exceeds the previous ceiling
        for i in range(1, len(floors)):
            assert floors[i] <= max(ceilings[:i])
        # and every pass ceiling >= its own floor (a real, pinned range)
        assert all(c >= f for f, c in zip(floors, ceilings))

    def test_pass_ceiling_is_pinned_at_pass_start(self):
        # FR-03: forward advancement during a long pass must NOT extend the
        # active pass's range — the next pass picks up the new territory
        store = InMemoryProspectiveStore()
        rows = [
            source_row(
                f"evt-{i}",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + (i - 1) * 1000,
            )
            for i in range(1, 4)
        ]
        config = make_config(
            source_batch_limit=1,
            source_forward_batches_per_cycle=10,
            source_overlap_batches_per_cycle=1,
            source_overlap_s=7200,
        )
        rt1 = build_runtime(store, rows, asof_ms=ASOF_MS + 10_000,
                            config=config)
        rt1.run_cycle()
        state = store.get_overlap_state()
        pinned_ceiling = state["pass_ceiling_us"]
        assert state["pass_active"] is True

        # new territory appears while the pass is still running
        rows.append(
            source_row(
                "evt-new",
                signal_ms=SIGNAL_18_MS,
                captured_ms=CAP0_MS + 1_000_000,
            )
        )
        rt2 = build_runtime(store, rows, asof_ms=ASOF_MS + 20_000,
                            config=config)
        rt2.run_cycle()
        state2 = store.get_overlap_state()
        if state2["pass_active"]:
            # the ACTIVE pass still targets its pinned ceiling
            assert state2["pass_ceiling_us"] == pinned_ceiling
        else:
            # the pass finished; the NEXT pass picked up new bounds
            assert state2["pass_ceiling_us"] >= pinned_ceiling
