"""Tests for ``ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1``.

Covers the independent observe-only runtime that wires the Issue #208
collector (``capture_source_signal``, ``record_event_lineage``,
``capture_post_signal_candle``, ``finalize_outcome``) to real sources:

* the shared deterministic ``event_key`` with Phase 1,
* separate least-privilege DB roles,
* CLOSED 5m candles only (the forming candle can never be observed),
* the full 240m ``STOP_FIRST`` path,
* strictly forward-only behaviour after the new freeze,
* lifecycle, heartbeat/diagnostics and idempotency,
* and the isolation guarantees: no frozen OOS, no Phase 1 mutation, no
  scanner gates, no paper/live surface.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.research import me_short_a_forward_outcome_lineage_v1 as lineage
from app.research import me_short_a_forward_outcome_runtime_v1 as mod
from app.research.me_short_a_forward_outcome_runtime_v1 import (
    Candle,
    Candidate,
    ForwardOutcomeRuntime,
    InMemoryCandleSource,
    RuntimeConfig,
    RuntimeMetrics,
    replace_activation,
    resolve_config,
    runtime_protocol_hash,
    runtime_protocol_payload,
    utcnow,
    verify_runtime_identity,
)

ROOT = Path(__file__).resolve().parent.parent

# Fixture dates are deliberately in the past so that the collector's own
# "not in the future" candle check is stable regardless of the wall clock.
FREEZE = datetime(2025, 1, 15, 12, 0, 0, 0, tzinfo=timezone.utc)
SIGNAL = datetime(2025, 1, 15, 13, 5, 0, 0, tzinfo=timezone.utc)  # 5m-aligned
NOW = SIGNAL + timedelta(minutes=240)

SYMBOL = "BTCUSDT"
# Prices use the SAME normalization the frozen identity requires
# (Decimal(str(float))), so a numeric round-trip cannot change the hashed key.
REF = Decimal("100.0")
STOP = Decimal("100.2")
TARGET = Decimal("98.0")

OUTCOME_TABLE = "research.me_short_a_outcome_observation_v1"
SOURCE_TABLE = "research.me_short_a_source_signal_v1"
LINEAGE_TABLE = "research.me_short_a_event_lineage_v1"
CANDLE_TABLE = "research.me_short_a_post_signal_candle_v1"
HEARTBEAT_TABLE = "research.me_short_a_forward_outcome_runtime_heartbeat_v1"


@pytest.fixture(autouse=True)
def _capture_flag(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_LINEAGE_CAPTURE_ENABLED", "1")
    yield


# ── fakes ──────────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.rowcount = -1

    def execute(self, sql, params=None):
        params = params or ()
        self.conn.statements.append((sql, params))
        head = " ".join(sql.lstrip().upper().split())
        if head.startswith("SELECT"):
            self._rows = self.conn.route_select(head, params)
            self.rowcount = len(self._rows)
        else:
            self.conn.inserts.append((sql, params))
            self.rowcount = 0 if self.conn.conflict_skip else 1

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return list(rows)

    def close(self):
        pass


class FakeConnection:
    """DB-API-ish double that records writes and routes the read-only SELECTs."""

    def __init__(self, activation_row=("RUNNING", FREEZE), conflict_skip=False):
        self.activation_row = activation_row
        self.conflict_skip = conflict_skip
        self.statements = []
        self.inserts = []
        self.commits = 0
        self.rollbacks = 0
        self.phase1_rows = []
        self.pending_events = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    def route_select(self, head, params):
        if "ME_SHORT_A_LINEAGE_ACTIVATION_V1" in head and "STATUS,FREEZE_TS" in head:
            return [self.activation_row] if self.activation_row else []
        if "ME_SHORT_A_FILL_FEASIBILITY_V1" in head:
            key = params[0] if params else None
            return [r for r in self.phase1_rows if r[0] == key]
        if "ME_SHORT_A_EVENT_LINEAGE_V1" in head and "COUNT(*)" in head:
            return [(len(self._pending()),)]
        if "ME_SHORT_A_EVENT_LINEAGE_V1" in head:
            return self._pending()
        return []

    def _pending(self):
        """Mimic the append-only tables: an event is pending until its outcome
        row exists. Seeded ``pending_events`` survive a simulated restart."""
        settled = set()
        for sql, params in self.inserts_for(OUTCOME_TABLE):
            payload = dict(zip(_insert_columns(sql), params))
            settled.add(payload["event_key"])
        out = []
        for sql, params in self.inserts_for(LINEAGE_TABLE):
            payload = dict(zip(_insert_columns(sql), params))
            if payload["event_key"] in settled:
                continue
            out.append([
                payload["symbol"], payload["signal_time"],
                payload["reference_price"], payload["stop_price"],
                payload["target_price"], payload["event_key"],
            ])
        for row in self.pending_events:
            if row[5] not in settled:
                out.append(list(row))
        return out

    @property
    def inserted_tables(self):
        out = []
        for sql, _ in self.inserts:
            match = re.search(r"INSERT INTO ([a-z0-9_.]+)", sql, re.I)
            if match:
                out.append(match.group(1))
        return out

    def inserts_for(self, table):
        return [s for s in self.inserts if s[0].startswith(f"INSERT INTO {table}")]


class FakeRawCandle:
    def __init__(self, timestamp, open_, high, low, close, volume=1.0):
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class FakeKlineClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_klines(self, symbol, interval="5", limit=200):
        self.calls.append((symbol, interval, limit))
        return list(self.rows)


class RowCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        self._rows = list(self.rows)

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class RowConn:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []
        self.inserts = []

    def cursor(self):
        return RowCursor(self.rows)


# ── helpers ────────────────────────────────────────────────────────────


def clock_at(value: datetime):
    return lambda: value


def make_window(signal_time=SIGNAL, symbol=SYMBOL, *,
                high=Decimal("100.0"), retrieved_at=None):
    """The five CLOSED 5m candles whose highs form the frozen A
    ``reference_price``. The last of them is the trigger candle and closes
    exactly at ``signal_time``."""
    if retrieved_at is None:
        retrieved_at = signal_time
    out = []
    for idx in range(5):
        open_time = signal_time - timedelta(minutes=5 * (5 - idx))
        close_time = open_time + timedelta(minutes=5)
        out.append(Candle(
            symbol=symbol,
            open_time=open_time,
            close_time=close_time,
            open=high - 1,
            high=high,
            low=high - 1,
            close=high - 1,
            retrieved_at=retrieved_at,
            source="test",
        ))
    return out


def make_candidate(**kwargs):
    fields = dict(
        symbol=SYMBOL,
        signal_time=SIGNAL,
        reference_price=REF,
        invalidation_price=STOP,
        target_1=TARGET,
        trigger_5m_close=Decimal("99.4"),
        entry_zone_low=Decimal("99.2"),
        entry_zone_high=Decimal("99.4"),
        market_context="TREND_START",
        signal_time_quote="BTCUSDT@13:05",
        observed_at=NOW,
    )
    fields.update(kwargs)
    if "recent_high_window" not in kwargs:
        fields["recent_high_window"] = tuple(
            make_window(fields["signal_time"]))
    return Candidate(**fields)


def make_outcome_candles(signal_time=SIGNAL, *, high=None, low=None, close=None):
    out = []
    for idx, open_time in enumerate(lineage.expected_window_opens(signal_time)):
        h = high(idx) if callable(high) else (high if high is not None else Decimal("99"))
        lo = low(idx) if callable(low) else (low if low is not None else Decimal("98"))
        c = close(idx) if callable(close) else (close if close is not None else Decimal("98.5"))
        out.append(Candle(
            symbol=SYMBOL,
            open_time=open_time,
            close_time=open_time + timedelta(minutes=5),
            open=c,
            high=h,
            low=lo,
            close=c,
            retrieved_at=signal_time + timedelta(minutes=245),
            source="test",
        ))
    return out


def config(enabled=True, status="RUNNING", freeze=FREEZE):
    return RuntimeConfig(
        experiment=mod.EXPERIMENT,
        protocol_version=mod.PROTOCOL_VERSION,
        protocol_hash=runtime_protocol_hash(),
        enabled=enabled,
        freeze_ts=freeze,
        activation_status=status,
        heartbeat_interval_seconds=60,
        diagnostics_retention_rows=2000,
        idle_sleep_seconds=0.0,
        max_events_per_cycle=100,
        registry_path="registry.json",
    )


def build(conn=None, *, cfg=None, candles=None, candidates=(), now=NOW):
    conn = conn if conn is not None else FakeConnection()
    source = InMemoryCandleSource(candles if candles is not None
                                  else make_outcome_candles())
    runtime = ForwardOutcomeRuntime(
        conn,
        config=cfg if cfg is not None else config(),
        candle_source=source,
        candidate_source=lambda _since: list(candidates),
        clock=clock_at(now),
        runner_id="runtime-under-test",
    )
    return runtime, conn


def _insert_columns(sql):
    match = re.search(r"INSERT INTO [a-z0-9_.]+ \((.*?)\)", sql, re.I | re.S)
    assert match, sql[:120]
    return [c.strip() for c in match.group(1).split(",")]


def _payload(row):
    sql, params = row
    cols = _insert_columns(sql)
    assert sql.count("%s") == len(params), f"{sql[:80]}: bind mismatch"
    return dict(zip(cols, params))


# ── identity and registry ──────────────────────────────────────────────


def test_runtime_protocol_hash_matches_registry_json():
    registry = mod.load_registry()
    assert registry["runtime_protocol_hash"] == runtime_protocol_hash()
    assert registry["protocol_version"] == mod.PROTOCOL_VERSION
    assert registry["experiment"] == mod.EXPERIMENT


def test_runtime_protocol_hash_is_sha256_of_canonical_payload():
    import hashlib

    canonical = json.dumps(runtime_protocol_payload(), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True, default=str)
    assert runtime_protocol_hash() == hashlib.sha256(
        canonical.encode("utf-8")).hexdigest()


def test_registry_declares_observe_only_and_forbidden_actions():
    registry = mod.load_registry()
    assert registry["mode"] == "OBSERVE_ONLY_NO_ORDERS"
    assert registry["order_pathway"] is None
    assert registry["paper_surface"] is None
    assert registry["live_surface"] is None
    assert registry["scanner_gate_surface"] is None
    forbidden = " ".join(registry["forbidden_actions"]).lower()
    for verb in ("deploy", "backfill", "paper", "live", "direction gate"):
        assert verb in forbidden, verb


def test_registry_roles_are_separate_least_privilege_roles():
    registry = mod.load_registry()
    roles = {registry["collector_role"], registry["runtime_role"],
             registry["audit_role"], registry["runtime_audit_role"]}
    assert roles == {
        "trad_bot_me_short_a_collector",
        "trad_bot_me_short_a_runtime",
        "trad_bot_me_short_a_audit",
        "trad_bot_me_short_a_runtime_audit",
    }
    assert "trad_bot" not in roles
    assert "analytics_agent" not in roles


def test_registry_and_module_share_the_same_role_names():
    payload = runtime_protocol_payload()
    assert payload["runtime_role"] == mod.RUNTIME_ROLE
    assert payload["collector_role"] == mod.COLLECTOR_ROLE
    assert payload["audit_role"] == mod.AUDIT_ROLE
    assert payload["runtime_audit_role"] == mod.RUNTIME_AUDIT_ROLE


def test_runtime_identity_is_verified_field_by_field():
    ok, problems = verify_runtime_identity(
        protocol_version=mod.PROTOCOL_VERSION,
        protocol_hash=runtime_protocol_hash())
    assert ok and problems == []

    ok, problems = verify_runtime_identity(
        protocol_version="SOMETHING_ELSE",
        protocol_hash=runtime_protocol_hash())
    assert not ok
    assert any("protocol_version" in p for p in problems)

    ok, problems = verify_runtime_identity(
        protocol_version=mod.PROTOCOL_VERSION,
        protocol_hash="0" * 64)
    assert not ok
    assert any("protocol_hash" in p for p in problems)


def test_registry_shares_the_phase1_event_key_contract():
    registry = mod.load_registry()
    assert registry["event_key_shared_with_phase1"] is True
    assert registry["event_key_contract"] == (
        "sha256(symbol|SHORT|<utc isoformat signal_time>|"
        "<str(Decimal(str(reference_price)))>|"
        "<str(Decimal(str(invalidation_price)))>|"
        "<str(Decimal(str(target_1)))>)")
    assert registry["collector_protocol_version"] == lineage.PROTOCOL_VERSION


# ── fail-closed configuration and lifecycle ────────────────────────────


def test_resolve_config_is_disabled_without_the_feature_flag():
    cfg = resolve_config(environ={})
    assert cfg.enabled is False
    assert cfg.activated is False
    assert cfg.fail_closed_reason is not None

    cfg = resolve_config(environ={mod.FEATURE_FLAG: "1"})
    assert cfg.enabled is True
    # still not activated: the activation snapshot is refreshed from the DB
    assert cfg.activated is False
    assert "activation status" in cfg.fail_closed_reason


def test_activation_snapshot_and_replace_activation_are_consistent():
    cfg = config(status="READY_TO_START", freeze=None)
    assert cfg.activated is False
    refreshed = replace_activation(cfg, "RUNNING", FREEZE)
    assert refreshed.activated is True
    assert refreshed.freeze_ts == FREEZE
    assert refreshed.experiment == cfg.experiment
    assert refreshed.protocol_hash == cfg.protocol_hash


@pytest.mark.parametrize("status,freeze,activated", [
    ("RUNNING", FREEZE, True),
    ("RUNNING", None, False),
    ("READY_TO_START", FREEZE, False),
    ("COMPLETED", FREEZE, False),
    ("CANCELLED", FREEZE, False),
    (None, None, False),
])
def test_only_running_with_freeze_activates(status, freeze, activated):
    assert replace_activation(config(), status, freeze).activated is activated


def test_run_cycle_writes_nothing_but_a_heartbeat_when_not_activated():
    conn = FakeConnection(activation_row=("READY_TO_START", None))
    runtime, conn = build(
        conn,
        candidates=[make_candidate()],
        cfg=config(enabled=True, status="READY_TO_START", freeze=None))
    runtime.run_cycle()
    assert conn.inserted_tables == [HEARTBEAT_TABLE]
    assert runtime.metrics.skipped_not_activated == 1
    assert runtime.metrics.candidates_seen == 0
    assert runtime.lifecycle_phase == "BLOCKED_NOT_ACTIVATED"


def test_pre_freeze_candidate_is_skipped_and_never_written():
    pre_freeze = make_candidate(signal_time=FREEZE - timedelta(minutes=5))
    runtime, conn = build(candidates=[pre_freeze])
    runtime.run_cycle()
    assert runtime.metrics.skipped_pre_freeze == 1
    assert SOURCE_TABLE not in conn.inserted_tables
    assert LINEAGE_TABLE not in conn.inserted_tables
    assert OUTCOME_TABLE not in conn.inserted_tables


def test_candidate_at_the_freeze_instant_is_not_forward_only():
    at_freeze = make_candidate(signal_time=FREEZE)
    runtime, conn = build(candidates=[at_freeze])
    runtime.run_cycle()
    assert runtime.metrics.skipped_pre_freeze == 1
    assert conn.inserted_tables == [HEARTBEAT_TABLE]


def test_lifecycle_states_are_validated_and_progress():
    runtime, _ = build()
    assert runtime.state == mod.RUNNER_STATE_IDLE
    runtime.start()
    assert runtime.state == mod.RUNNER_STATE_RUNNING
    runtime.request_stop()
    assert runtime.state == mod.RUNNER_STATE_STOPPING
    runtime.stop("DONE")
    assert runtime.state == mod.RUNNER_STATE_STOPPED
    assert runtime.lifecycle_phase == "DONE"

    runtime2, _ = build()
    runtime2.fail("boom")
    assert runtime2.state == mod.RUNNER_STATE_FAILED
    assert runtime2.metrics.errors == 1
    assert runtime2.diagnostics().last_error == "boom"

    with pytest.raises(ValueError):
        runtime2._transition("NOT_A_STATE", "x")


def test_diagnostics_snapshot_is_serializable_and_complete():
    runtime, _ = build(candidates=[make_candidate()])
    runtime.run_cycle()
    payload = runtime.diagnostics().as_dict()
    json.dumps(payload)  # must be JSON-serializable
    assert payload["runner_id"] == "runtime-under-test"
    assert payload["protocol_version"] == mod.PROTOCOL_VERSION
    assert payload["activation_status"] == "RUNNING"
    assert payload["activated"] is True
    assert payload["metrics"]["cycles_completed"] == 1
    assert payload["config_fingerprint"] == runtime.config_fingerprint()
    assert isinstance(payload["freeze_ts"], str)
    assert payload["fail_closed_reason"] is None


def test_metrics_counters_are_monotonic_and_bounded_to_known_names():
    metrics = RuntimeMetrics()
    metrics.add("candidates_seen", 3)
    assert metrics.candidates_seen == 3
    with pytest.raises(KeyError):
        metrics.add("not_a_counter")
    assert set(metrics.as_dict()) == set(mod.COUNTER_NAMES)


# ── idempotency ────────────────────────────────────────────────────────


def test_same_candidate_twice_is_captured_once():
    candidate = make_candidate()
    runtime, conn = build(candidates=[candidate, candidate])
    runtime.run_cycle()
    assert runtime.metrics.candidates_seen == 2
    assert runtime.metrics.signals_captured == 1
    assert runtime.metrics.lineages_recorded == 1
    assert runtime.metrics.skipped_idempotent >= 1
    assert conn.inserted_tables.count(SOURCE_TABLE) == 1
    assert conn.inserted_tables.count(LINEAGE_TABLE) == 1


def test_replaying_the_same_cycle_adds_no_new_evidence():
    candidate = make_candidate()
    runtime, conn = build(candidates=[candidate])
    runtime.run_cycle()
    first = [s for s in conn.inserts if "runtime_heartbeat" not in s[0]]
    runtime._candidates = lambda _since: [candidate]
    runtime.run_cycle()
    again = [s for s in conn.inserts if "runtime_heartbeat" not in s[0]]
    assert again == first


def test_seen_set_is_bounded():
    runtime, _ = build()
    for i in range(mod.SEEN_IDS_LIMIT + 50):
        runtime._remember(f"id-{i}")
    assert len(runtime._seen_candidate_ids) == mod.SEEN_IDS_LIMIT
    assert runtime._already_seen(f"id-{mod.SEEN_IDS_LIMIT + 40}")
    assert not runtime._already_seen("id-0")


def test_every_collector_insert_is_an_idempotent_append_only_write():
    runtime, conn = build(candidates=[make_candidate()])
    runtime.run_cycle()
    for sql, params in conn.inserts:
        if HEARTBEAT_TABLE in sql:
            assert "ON CONFLICT DO NOTHING" in sql
        else:
            assert "ON CONFLICT(" in sql
        assert sql.count("%s") == len(params)


# ── closed 5m candles only ─────────────────────────────────────────────


def test_forming_candle_is_never_observable():
    from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
        to_runtime_candle,
    )

    open_time = NOW - timedelta(minutes=5)
    raw = FakeRawCandle(int(open_time.timestamp() * 1000), 1, 2, 0.5, 1.5)
    assert to_runtime_candle(
        raw, symbol=SYMBOL,
        retrieved_at=NOW - timedelta(minutes=4)) is None
    assert to_runtime_candle(
        raw, symbol=SYMBOL, retrieved_at=NOW + timedelta(minutes=1)) is not None


def test_candle_rejects_retrieval_before_close():
    open_time = NOW - timedelta(minutes=10)
    with pytest.raises(ValueError):
        Candle(
            symbol=SYMBOL, open_time=open_time,
            close_time=open_time + timedelta(minutes=5),
            open=Decimal("1"), high=Decimal("2"), low=Decimal("0.5"),
            close=Decimal("1.5"), retrieved_at=open_time + timedelta(minutes=1))


def test_bybit_source_returns_only_closed_candles_inside_the_window():
    from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
        BybitClosedCandleSource,
    )

    start = NOW - timedelta(minutes=30)
    rows = [
        FakeRawCandle(int((start + timedelta(minutes=5 * i)).timestamp() * 1000),
                      1, 2, 0.5, 1.5)
        for i in range(7)      # the last one is still forming
    ]
    source = BybitClosedCandleSource(FakeKlineClient(rows), clock=clock_at(NOW))
    got = source.closed_candles(SYMBOL, start=start, end=NOW)
    assert len(got) == 6
    assert all(c.close_time <= NOW for c in got)
    assert all(start <= c.open_time < NOW for c in got)


def test_in_memory_candle_source_respects_the_asof_clock():
    candles = make_outcome_candles()
    opens = lineage.expected_window_opens(SIGNAL)
    source = InMemoryCandleSource(candles)
    got = source.closed_candles(
        SYMBOL, start=opens[0], end=opens[-1] + timedelta(minutes=5))
    assert len(got) == len(opens) == 47
    assert all(c.close_time <= utcnow() for c in got)


def test_candidate_rejects_a_window_that_closes_after_the_signal():
    late = list(make_window())
    late[-1] = Candle(
        symbol=SYMBOL, open_time=SIGNAL - timedelta(minutes=5),
        close_time=SIGNAL + timedelta(minutes=5),
        open=Decimal("99"), high=Decimal("100"), low=Decimal("98"),
        close=Decimal("99"), retrieved_at=NOW)
    with pytest.raises(ValueError):
        make_candidate(recent_high_window=tuple(late))


def test_candidate_rejects_a_reference_that_is_not_the_window_max():
    with pytest.raises(ValueError):
        make_candidate(reference_price=Decimal("99.9"))


def test_candidate_rejects_long_geometry():
    with pytest.raises(ValueError):
        make_candidate(invalidation_price=Decimal("99.9"))
    with pytest.raises(ValueError):
        make_candidate(target_1=Decimal("101"))


def test_runtime_rejects_a_lookahead_candidate_before_any_write():
    late = list(make_window())
    late[-1] = Candle(
        symbol=SYMBOL, open_time=SIGNAL - timedelta(minutes=5),
        close_time=SIGNAL + timedelta(minutes=5),
        open=Decimal("99"), high=Decimal("100"), low=Decimal("98"),
        close=Decimal("99"), retrieved_at=NOW)
    candidate = make_candidate()
    object.__setattr__(candidate, "recent_high_window", tuple(late))
    runtime, conn = build(candidates=[candidate])
    runtime.run_cycle()
    assert runtime.metrics.skipped_lookahead == 1
    assert SOURCE_TABLE not in conn.inserted_tables


# ── full 240m STOP_FIRST path through the runtime ─────────────────────


def test_full_path_source_signal_lineage_candles_outcome():
    runtime, conn = build(candidates=[make_candidate()])
    runtime.run_cycle()

    assert conn.inserted_tables.count(SOURCE_TABLE) == 1
    assert conn.inserted_tables.count(LINEAGE_TABLE) == 1
    assert conn.inserted_tables.count(CANDLE_TABLE) == 47
    assert conn.inserted_tables.count(OUTCOME_TABLE) == 1
    assert conn.inserted_tables.count(HEARTBEAT_TABLE) == 1

    assert runtime.metrics.signals_captured == 1
    assert runtime.metrics.lineages_recorded == 1
    assert runtime.metrics.candles_captured == 47   # grid-aligned 240m window
    assert runtime.metrics.outcomes_finalized == 1

    outcome = _payload(conn.inserts_for(OUTCOME_TABLE)[0])
    assert outcome["event_key"] == lineage.event_key(
        SYMBOL, lineage.DIRECTION, SIGNAL, REF, STOP, TARGET)
    assert outcome["intrabar_policy"] == "STOP_FIRST"
    assert outcome["horizon_minutes"] == 240
    assert outcome["is_final"] is True
    assert outcome["exit_fill_semantics"] == "MODELED_UNKNOWN"
    assert outcome["partial_fill_status"] == "UNKNOWN_NOT_INDEPENDENTLY_CAPTURED"
    assert outcome["touch_basis"] == "CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL"


def test_stop_first_ambiguity_resolves_to_stop_through_the_runtime():
    candles = make_outcome_candles(high=Decimal("100.5"), low=Decimal("97.5"))
    runtime, conn = build(candidates=[make_candidate()], candles=candles)
    runtime.run_cycle()

    outcome = _payload(conn.inserts_for(OUTCOME_TABLE)[0])
    assert outcome["ambiguous_intrabar"] is True
    assert outcome["sl_before_tp"] is True
    assert outcome["tp_before_sl"] is False
    assert outcome["path_class"] == "STOP_HIT"
    assert Decimal(str(outcome["gross_r"])) == Decimal("-1")


def test_target_hit_path_is_finalized_through_the_runtime():
    candles = make_outcome_candles(high=Decimal("99"), low=Decimal("97.5"))
    runtime, conn = build(candidates=[make_candidate()], candles=candles)
    runtime.run_cycle()

    outcome = _payload(conn.inserts_for(OUTCOME_TABLE)[0])
    assert outcome["path_class"] == "TARGET_HIT"
    assert outcome["tp_before_sl"] is True
    assert Decimal(str(outcome["gross_r"])) > 0


def test_timeout_path_is_finalized_at_the_last_in_window_close():
    candles = make_outcome_candles(high=Decimal("99.5"), low=Decimal("98.2"))
    runtime, conn = build(candidates=[make_candidate()], candles=candles)
    runtime.run_cycle()

    outcome = _payload(conn.inserts_for(OUTCOME_TABLE)[0])
    assert outcome["path_class"] == "TIMEOUT_240M"
    assert outcome["sl_before_tp"] is False
    assert outcome["tp_before_sl"] is False


def test_outcome_is_not_finalized_before_the_horizon_is_settled():
    partial = make_outcome_candles()[:10]
    runtime, conn = build(
        candidates=[make_candidate()], candles=partial,
        now=SIGNAL + timedelta(minutes=60))
    runtime.run_cycle()

    assert OUTCOME_TABLE not in conn.inserted_tables
    assert runtime.metrics.outcomes_finalized == 0
    assert runtime.metrics.skipped_unmatured >= 1
    assert runtime.metrics.candles_captured == 10


def test_outcome_is_finalized_exactly_once_across_cycles():
    candidate = make_candidate()
    runtime, conn = build(candidates=[candidate])
    runtime.run_cycle()
    runtime._candidates = lambda _since: []
    runtime.run_cycle()

    assert len(conn.inserts_for(OUTCOME_TABLE)) == 1
    assert runtime.metrics.outcomes_finalized == 1


def test_incomplete_window_produces_source_incomplete_without_economics():
    # the horizon IS settled (the last candle closes at +240m) but only every
    # other candle of the 47 was observed -> coverage is incomplete
    full = make_outcome_candles()
    candles = [c for idx, c in enumerate(full) if idx % 2 == 0]
    assert candles[-1].close_time == full[-1].close_time
    runtime, conn = build(candidates=[make_candidate()], candles=candles)
    runtime.run_cycle()

    rows = conn.inserts_for(OUTCOME_TABLE)
    assert len(rows) == 1
    outcome = _payload(rows[0])
    assert outcome["outcome_status"] == "SOURCE_INCOMPLETE"
    assert outcome["is_final"] is False
    assert outcome["path_class"] is None
    assert outcome["exit_price"] is None
    assert outcome["gross_r"] is None
    assert outcome["net_r_normal"] is None
    assert outcome["return_at_240m"] is None
    assert outcome["candles_present"] == len(candles)
    assert outcome["candles_expected"] == 47


def test_restart_resumes_pending_events_from_the_append_only_tables():
    runtime, conn = build(candidates=[])
    key = lineage.event_key(SYMBOL, lineage.DIRECTION, SIGNAL, REF, STOP, TARGET)
    conn.pending_events = [[SYMBOL, SIGNAL, REF, STOP, TARGET, key]]
    runtime.run_cycle()
    assert runtime.metrics.candles_captured == 47
    assert runtime.metrics.outcomes_finalized == 1
    assert len(conn.inserts_for(OUTCOME_TABLE)) == 1


def test_phase1_linkage_is_asserted_only_when_a_snapshot_exists():
    candidate = make_candidate()
    key = lineage.event_key(SYMBOL, lineage.DIRECTION, SIGNAL, REF, STOP, TARGET)

    runtime, conn = build(candidates=[candidate])
    conn.phase1_rows = [[key, [42]]]
    runtime.run_cycle()
    lineage_row = _payload(conn.inserts_for(LINEAGE_TABLE)[0])
    assert lineage_row["phase1_linked"] is True
    assert lineage_row["phase1_event_key"] == key
    assert lineage_row["phase1_observation_id"] == 42
    assert lineage_row["source_observation_id"] is None   # never invented


def test_phase1_absence_leaves_the_lineage_unlinked_not_faked():
    runtime, conn = build(candidates=[make_candidate()])
    conn.phase1_rows = []
    runtime.run_cycle()
    lineage_row = _payload(conn.inserts_for(LINEAGE_TABLE)[0])
    assert lineage_row["phase1_linked"] is False
    assert lineage_row["phase1_event_key"] is None
    assert lineage_row["phase1_observation_id"] is None


# ── sources: read-only, honest, no imputation ──────────────────────────


def test_scanner_setup_source_is_read_only():
    from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
        DbScannerSetupCandidateSource,
    )

    conn = FakeConnection()
    source = DbScannerSetupCandidateSource(conn, InMemoryCandleSource([]))
    source.fetch(SIGNAL)
    assert conn.inserts == []
    for sql, _params in conn.statements:
        assert sql.lstrip().upper().startswith("SELECT")
        assert "dds.scanner_setup" in sql
        for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not re.search(rf"\b{verb}\b", sql), verb


def test_scanner_setup_source_maps_the_phase1_identity_fields():
    from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
        DbScannerSetupCandidateSource,
    )

    window = make_window()
    row_ref, row_stop, row_target = 100.0, 100.2, 98.0
    rows = [[
        "setup-1", SYMBOL, SIGNAL, row_ref, row_stop, row_target,
        99.2, 99.4,
        int(window[-1].open_time.timestamp() * 1000),
        {"trigger_5m_close": 99.4, "signal_time_quote": "q"},
        "TREND_START", "READY_TO_TRADE",
    ]]
    source = DbScannerSetupCandidateSource(
        RowConn(rows), InMemoryCandleSource(window))
    candidates = source.fetch(FREEZE)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.signal_time == SIGNAL          # Phase 1 uses detected_at
    assert candidate.reference_price == Decimal("100.0")
    assert candidate.invalidation_price == Decimal("100.2")
    assert candidate.target_1 == Decimal("98.0")
    assert candidate.trigger_5m_close == Decimal("99.4")
    assert len(candidate.recent_high_window) == 5
    # THE contract: the key computed from the persisted row is byte-identical
    # to the key Phase 1 computed from the scanner's own float geometry.
    expected = lineage.event_key(
        SYMBOL, lineage.DIRECTION, SIGNAL, row_ref, row_stop, row_target)
    assert mod.event_key_for(candidate) == expected


def test_price_normalization_reproduces_the_phase1_float_identity():
    from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
        canonical_price,
    )

    # a numeric column round-trip must not change the hashed string
    assert canonical_price(Decimal("100.00000000")) == Decimal("100.0")
    assert canonical_price(100.0) == Decimal("100.0")
    assert canonical_price("100.20000000") == Decimal("100.2")
    assert canonical_price(None) is None
    assert canonical_price("not-a-price") is None

    expected = lineage.event_key(
        SYMBOL, lineage.DIRECTION, SIGNAL, 100.0, 100.2, 98.0)
    for raw in (100.0, Decimal("100.00000000"), "100.0"):
        candidate = make_candidate(
            reference_price=canonical_price(raw),
            invalidation_price=Decimal("100.2"),
            target_1=Decimal("98.0"))
        assert mod.event_key_for(candidate) == expected


def test_reference_window_mismatch_is_reported_not_imputed():
    from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
        DbScannerSetupCandidateSource,
    )

    window = make_window(high=Decimal("99.5"))   # does NOT reproduce REF=100
    rows = [[
        "setup-2", SYMBOL, SIGNAL, float(REF), float(STOP), float(TARGET),
        99.2, 99.4, 0, {"trigger_5m_close": 99.4}, "TREND_START", "READY",
    ]]
    source = DbScannerSetupCandidateSource(
        RowConn(rows), InMemoryCandleSource(window))
    candidates = source.fetch(FREEZE)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.recent_high_window == ()
    assert "recent_high_window_candles" in candidate.missing_fields
    assert ("recent_high_window_reference_mismatch"
            in candidate.source_quality_flags)


def test_missing_trigger_close_is_recorded_explicitly():
    candidate = make_candidate(trigger_5m_close=None, recent_high_window=())
    runtime, conn = build(candidates=[candidate])
    runtime.run_cycle()
    rows = conn.inserts_for(SOURCE_TABLE)
    assert rows
    source_row = _payload(rows[0])
    missing = json.loads(source_row["missing_fields"])
    assert "trigger_5m_close" in missing
    assert "recent_high_window_candles" in missing
    assert source_row["trigger_5m_close"] is None


# ── heartbeat / diagnostics ────────────────────────────────────────────


def test_heartbeat_is_append_only_and_carries_counters():
    runtime, conn = build(candidates=[make_candidate()])
    runtime.run_cycle()
    rows = conn.inserts_for(HEARTBEAT_TABLE)
    assert len(rows) == 1
    payload = _payload(rows[0])
    assert "ON CONFLICT DO NOTHING" in rows[0][0]
    assert payload["runner_state"] == mod.RUNNER_STATE_RUNNING
    assert payload["runner_id"] == "runtime-under-test"
    assert payload["runtime_protocol_version"] == mod.PROTOCOL_VERSION
    assert payload["runtime_protocol_hash"] == runtime_protocol_hash()
    assert payload["cycles_completed"] == 1
    assert payload["signals_captured"] == 1
    assert payload["lineages_recorded"] == 1
    assert payload["candles_captured"] == 47
    assert payload["outcomes_finalized"] == 1
    assert payload["activation_status"] == "RUNNING"
    assert payload["config_fingerprint"] == runtime.config_fingerprint()
    assert payload["errors"] == 0
    assert payload["last_error"] is None


def test_heartbeat_never_holds_prices_or_outcomes():
    runtime, conn = build(candidates=[make_candidate()])
    runtime.run_cycle()
    sql, params = conn.inserts_for(HEARTBEAT_TABLE)[0]
    cols = _insert_columns(sql)
    for evidence in ("reference_price", "stop_price", "target_price",
                     "exit_price", "gross_r", "net_r_normal", "path_class",
                     "return_at_240m", "event_key", "symbol"):
        assert evidence not in cols, evidence
    for value in params:
        assert not isinstance(value, Decimal), value


def test_a_failing_event_does_not_kill_the_runner_but_is_counted():
    class ExplodingCandles:
        def closed_candles(self, *args, **kwargs):
            raise RuntimeError("candle source down")

    conn = FakeConnection()
    runtime = ForwardOutcomeRuntime(
        conn,
        config=config(),
        candle_source=ExplodingCandles(),
        candidate_source=lambda _since: [make_candidate(recent_high_window=())],
        clock=clock_at(NOW),
        runner_id="runtime-under-test",
    )
    conn.pending_events = [[
        SYMBOL, SIGNAL, REF, STOP, TARGET,
        lineage.event_key(SYMBOL, lineage.DIRECTION, SIGNAL, REF, STOP, TARGET)]]
    runtime.run_cycle()
    assert conn.inserts_for(HEARTBEAT_TABLE)
    assert runtime.metrics.errors >= 1
    assert runtime.diagnostics().last_error is not None
    # one bad event must never stop the observe-only loop
    assert runtime.state == mod.RUNNER_STATE_RUNNING


def test_a_failing_cycle_moves_the_runner_to_failed():
    conn = FakeConnection()

    def boom(_since):
        return ()

    class ExplodingConn(FakeConnection):
        def route_select(self, head, params):
            raise RuntimeError("db gone")

    conn = ExplodingConn()
    runtime = ForwardOutcomeRuntime(
        conn,
        config=config(),
        candle_source=InMemoryCandleSource([]),
        candidate_source=boom,
        clock=clock_at(NOW),
        runner_id="runtime-under-test",
    )
    runtime.run_cycle()
    assert runtime.state == mod.RUNNER_STATE_FAILED
    assert runtime.metrics.errors == 1


def test_heartbeat_count_matches_the_number_of_cycles():
    runtime, conn = build(candidates=[])
    for _ in range(3):
        runtime.run_cycle()
    assert len(conn.inserts_for(HEARTBEAT_TABLE)) == 3
    assert runtime.metrics.cycles_completed == 3


# ── isolation: nothing frozen is touched, no orders exist ──────────────


def test_runtime_never_writes_phase1_or_the_frozen_oos():
    runtime, conn = build(candidates=[make_candidate()])
    runtime.run_cycle()
    for sql, _params in conn.inserts:
        body = sql.lower()
        assert "me_short_a_fill_feasibility_v1" not in body
        assert "prospective_observation" not in body
        assert "prospective_outcome" not in body
        assert "dds." not in body
    for sql, _params in conn.statements:
        body = sql.lower()
        if "me_short_a_fill_feasibility_v1" in body:
            assert body.lstrip().startswith("select")
        if "dds.scanner_setup" in body:
            assert body.lstrip().startswith("select")


def test_runtime_module_has_no_order_or_paper_or_live_surface():
    for name in ("me_short_a_forward_outcome_runtime_v1.py",
                 "me_short_a_forward_outcome_runtime_sources_v1.py",
                 "me_short_a_forward_outcome_runtime_runner_v1.py"):
        source = (ROOT / "app" / "research" / name).read_text(encoding="utf-8")
        for forbidden in ("create_order", "place_order", "submit_order",
                          "paper_trade", "open_position", "close_position",
                          "live_safety", "set_leverage"):
            assert forbidden not in source.lower(), (name, forbidden)


def test_scanner_gates_are_not_imported_or_invoked():
    for name in ("me_short_a_forward_outcome_runtime_v1.py",
                 "me_short_a_forward_outcome_runtime_sources_v1.py",
                 "me_short_a_forward_outcome_runtime_runner_v1.py"):
        source = (ROOT / "app" / "research" / name).read_text(encoding="utf-8")
        for forbidden in ("app.scanners", "scanner_runner", "paper_runner",
                          "app.paper", "app.live", "direction_gate"):
            assert not re.search(rf"(?:import|from)\s+.*{re.escape(forbidden)}",
                                 source), (name, forbidden)
        # no direct call into scanner/paper/live entry points either
        for call in ("scan_all(", "scan(", "run_paper(", "run_scanner("):
            assert call not in source, (name, call)


def test_long_direction_is_structurally_excluded():
    runtime, conn = build()
    assert runtime.config.experiment == mod.EXPERIMENT
    assert lineage._gated(conn, SYMBOL, "LONG", SIGNAL) is None
    payload = runtime_protocol_payload()
    assert payload["direction"] == "SHORT"
    assert payload["symbol_scope"] == "MOMENTUM_EXHAUSTION_SHORT_CANDIDATES"
    assert payload["order_pathway_forbidden"] is True
