"""Issue #259 — SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1.

Offline unit/integration tests for the SRR SHORT signal-time fill capture:
activation policy, event identity, idempotency/concurrency, API budget,
orderbook validation, SRR execution_R cost normalization, eligibility and
the scanner wrapper isolation contract.

Every test uses a mock Bybit client and fake connections — no network, no
PostgreSQL, and no real trading orders are possible anywhere in this module
(read-only public market endpoints only).
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import app.research.me_short_a_fill_feasibility_v1 as me_mod
import app.research.srr_short_fill_feasibility_v1 as mod
from app.exchange.bybit_client import BybitRateLimitError

ROOT = Path(__file__).parents[1]

# NOTE: timestamps are refreshed by the autouse fixture BEFORE every test so
# a long full-suite session (this module is collected minutes before it
# executes) can never turn a fresh quote into an accidental DELAYED one.
NOW = datetime.now(timezone.utc)
ACTIVATION = NOW - timedelta(days=2)
SIGNAL = NOW - timedelta(seconds=1)

_DEFAULT = object()

REF = Decimal("100")
INVALIDATION = Decimal("100.2")
VARIANT_STOP = Decimal("100.4")      # ref + 2.0 * structural_R
VARIANT_TARGET = Decimal("99.7")     # ref - 1.5 * structural_R

LINEAR_INSTRUMENT = {"result": {"list": [{
    "symbol": "BTCUSDT",
    "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001",
                      "minNotionalValue": "5"}}]}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    global NOW, ACTIVATION, SIGNAL
    NOW = datetime.now(timezone.utc)
    ACTIVATION = NOW - timedelta(days=2)
    SIGNAL = NOW - timedelta(seconds=1)
    monkeypatch.setenv("SRR_SHORT_FILL_CAPTURE_ENABLED", "1")
    monkeypatch.setenv("SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS", "60000")
    monkeypatch.delenv("SRR_SHORT_FILL_EXPECTED_ACTIVATION_TS", raising=False)
    monkeypatch.delenv("SRR_SHORT_FILL_MAX_PER_CYCLE", raising=False)
    monkeypatch.delenv("SRR_SHORT_FILL_MAX_PER_RUN", raising=False)
    mod.reset_stats()
    mod.reset_budget()
    mod.clear_static_identity_cache()
    me_mod._INSTRUMENT_CACHE.clear()
    yield
    mod.reset_stats()
    mod.reset_budget()
    mod.clear_static_identity_cache()
    me_mod._INSTRUMENT_CACHE.clear()


# ── fakes ────────────────────────────────────────────────────────────────


def obs_row(obs_id=1, experiment_id=mod.SOURCE_EXPERIMENT_ID,
            direction="SHORT", symbol="BTCUSDT", signal_time=_DEFAULT,
            reference_price=REF, invalidation_price=INVALIDATION,
            variant_stop=VARIANT_STOP, variant_target=VARIANT_TARGET):
    """One ``research.prospective_observation`` row as fetched by capture."""
    if signal_time is _DEFAULT:  # resolved per test, never at import time
        signal_time = SIGNAL
    return (obs_id, experiment_id, direction, symbol, signal_time,
            reference_price, invalidation_price, variant_stop,
            variant_target)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.rowcount = -1

    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params or ()))
        norm = " ".join(sql.split())
        up = norm.upper()
        if up.startswith("SELECT") and "SRR_SHORT_FILL_ACTIVATION_V1" in up:
            row = self.conn.activation_row
            self._rows = [row] if row else []
        elif "FROM RESEARCH.PROSPECTIVE_OBSERVATION" in up:
            row = self.conn.observations.get(params[0])
            self._rows = [row] if row else []
        elif up.startswith("SELECT 1 FROM RESEARCH.SRR_SHORT_FILL_FEASIBILITY_V1"):
            exists = self.conn.duplicate_exists or params[1] in \
                self.conn.duplicate_observation_ids
            self._rows = [(1,)] if exists else []
        elif up.startswith("INSERT INTO RESEARCH.SRR_SHORT_FILL_FEASIBILITY_V1"):
            self.rowcount = 0 if self.conn.claim_conflict else 1
        elif up.startswith("UPDATE RESEARCH.SRR_SHORT_FILL_FEASIBILITY_V1"):
            if self.conn.finalize_error is not None:
                raise self.conn.finalize_error
            self.conn.finalize_params = params
            self.rowcount = self.conn.finalize_rowcount
        else:  # pragma: no cover - unexpected statement is a test bug
            raise AssertionError(f"unexpected statement: {norm}")

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, activation_row=_DEFAULT,
                 observations=None, duplicate_exists=False,
                 duplicate_observation_ids=(), claim_conflict=False,
                 finalize_rowcount=1, finalize_error=None):
        if activation_row is _DEFAULT:  # resolved per test, not at import
            activation_row = ("RUNNING", ACTIVATION)
        self.activation_row = activation_row
        self.observations = observations if observations is not None else {
            1: obs_row(),
        }
        self.duplicate_exists = duplicate_exists
        self.duplicate_observation_ids = set(duplicate_observation_ids)
        self.claim_conflict = claim_conflict
        self.finalize_rowcount = finalize_rowcount
        self.finalize_error = finalize_error
        self.finalize_params = None
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    @property
    def inserts(self):
        return [s for s in self.statements
                if s[0].lstrip().upper().startswith("INSERT")]

    @property
    def updates(self):
        return [s for s in self.statements
                if s[0].lstrip().upper().startswith("UPDATE")]

    @property
    def selects(self):
        return [s for s in self.statements
                if s[0].lstrip().upper().startswith("SELECT")]


class FakeClient:
    """Orderbook + instruments-info replay; optionally raise on orderbook."""

    def __init__(self, book=None, instruments=None, book_error=None):
        self.book = book
        self.instruments = instruments
        self.book_error = book_error
        self.calls = []

    def _public_get(self, endpoint, **params):
        self.calls.append((endpoint, params))
        if endpoint.endswith("/orderbook"):
            if self.book_error is not None:
                raise self.book_error
            return self.book or {}
        return self.instruments if self.instruments is not None else \
            {"result": {"list": []}}

    @property
    def orderbook_calls(self):
        return [c for c in self.calls if c[0].endswith("/orderbook")]


def book(ts=100000, bids=None, asks=None):
    return {"result": {
        "ts": ts,
        "b": bids if bids is not None else [["99.9", "5"], ["99.8", "5"]],
        "a": asks if asks is not None else [["100.1", "5"]]}}


def live_book(**kwargs):
    """Book stamped with the real wall clock (inside the staleness bound)."""
    return book(ts=int(datetime.now(timezone.utc).timestamp() * 1000), **kwargs)


def capture_kwargs(**overrides):
    kwargs = dict(
        scanner_name="SUPPORT_RESISTANCE_REACTION",
        direction="SHORT",
        symbol="BTCUSDT",
        signal_time=SIGNAL,
        reference_price=REF,
        invalidation_price=INVALIDATION,
        observation_ids=[1],
    )
    kwargs.update(overrides)
    return kwargs


def good_client(**kwargs):
    kwargs.setdefault("book", live_book())
    kwargs.setdefault("instruments", LINEAR_INSTRUMENT)
    return FakeClient(**kwargs)


def finalize_row(conn):
    """Decode the finalizing UPDATE into a column -> value dict."""
    params = conn.finalize_params
    assert params is not None, "capture never finalized its claim"
    cols = mod._FINALIZE_COLUMNS
    row = dict(zip(cols, params[:len(cols)]))
    row["_event_key"] = params[-1]
    return row


def json_field(row, name):
    value = row[name]
    return value if isinstance(value, dict) else json.loads(value)


# ── gates: feature flag / identity / configuration ───────────────────────


def test_feature_flag_disabled_no_side_effects(monkeypatch):
    monkeypatch.delenv("SRR_SHORT_FILL_CAPTURE_ENABLED", raising=False)
    db, client = FakeConnection(), good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.statements == []
    assert client.calls == []
    assert mod.stats()["flag_denied"] == 1


def test_requires_connection_and_client():
    assert mod.capture(None, good_client(), **capture_kwargs()) is False
    assert mod.capture(FakeConnection(), None, **capture_kwargs()) is False
    assert mod.capture(FakeConnection(), FakeClient(),
                       **capture_kwargs(observation_ids=[])) is False


@pytest.mark.parametrize("overrides", [
    {"scanner_name": "BREAKOUT_RETEST"},
    {"direction": "LONG"},
    {"scanner_name": "MOMENTUM_EXHAUSTION", "direction": "SHORT"},
])
def test_wrong_scanner_or_direction_denied(overrides):
    db, client = FakeConnection(), good_client()
    assert mod.capture(db, client, **capture_kwargs(**overrides)) is False
    assert db.statements == []
    assert client.calls == []


def test_static_identity_must_be_verified(monkeypatch):
    monkeypatch.setattr(mod, "_static_identity_ok", lambda: False)
    db, client = FakeConnection(), good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.statements == []
    assert client.calls == []
    assert mod.stats()["identity_denied"] == 1


def test_static_identity_reads_frozen_registry():
    # Positive control: the real registry/adapter identity verifies OK and
    # the verification is cached afterwards.
    mod.clear_static_identity_cache()
    assert mod._static_identity_ok() is True
    assert mod._static_identity_ok() is True


def test_missing_quote_delay_config_fails_closed(monkeypatch):
    monkeypatch.delenv("SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS", raising=False)
    db, client = FakeConnection(), good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.statements == []
    assert client.calls == []
    assert mod.stats()["config_denied"] == 1


@pytest.mark.parametrize("value", ["0", "-5", "abc", ""])
def test_invalid_quote_delay_config_fails_closed(monkeypatch, value):
    monkeypatch.setenv("SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS", value)
    db, client = FakeConnection(), good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.statements == []
    assert mod.stats()["config_denied"] == 1


@pytest.mark.parametrize("value", ["abc", "-1"])
def test_invalid_api_budget_config_fails_closed(monkeypatch, value):
    monkeypatch.setenv("SRR_SHORT_FILL_MAX_PER_CYCLE", value)
    db, client = FakeConnection(), good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.statements == []
    assert mod.stats()["config_denied"] == 1


def test_expected_activation_ts_must_match_db(monkeypatch):
    monkeypatch.setenv("SRR_SHORT_FILL_EXPECTED_ACTIVATION_TS",
                       (ACTIVATION + timedelta(hours=1)).isoformat())
    db, client = FakeConnection(), good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.inserts == []
    assert client.calls == []
    assert mod.stats()["config_denied"] == 1

    # Exact match of the same instant in a different textual form is accepted.
    monkeypatch.setenv(
        "SRR_SHORT_FILL_EXPECTED_ACTIVATION_TS",
        ACTIVATION.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    db2, client2 = FakeConnection(), good_client()
    assert mod.capture(db2, client2, **capture_kwargs()) is True


def test_malformed_expected_activation_ts_fails_closed(monkeypatch):
    monkeypatch.setenv("SRR_SHORT_FILL_EXPECTED_ACTIVATION_TS", "not-a-date")
    db, client = FakeConnection(), good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.inserts == []
    assert mod.stats()["config_denied"] == 1


# ── activation policy ────────────────────────────────────────────────────


@pytest.mark.parametrize("activation_row", [
    None,
    ("DISABLED", None),
    ("RUNNING", None),
    ("COMPLETED", ACTIVATION),
    ("CANCELLED", ACTIVATION),
])
def test_activation_gates_fail_closed(activation_row):
    db = FakeConnection(activation_row=activation_row)
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.inserts == []
    assert client.calls == []  # no market data requested when gate is closed
    assert mod.stats()["activation_denied"] == 1


def test_activation_record_is_read_only_for_runtime():
    db = FakeConnection()
    mod.capture(db, good_client(), **capture_kwargs())
    # The runtime only ever SELECTs the activation row.
    activation_statements = [s for s in db.statements
                             if "srr_short_fill_activation_v1" in s[0].lower()]
    assert activation_statements
    assert all(s[0].lstrip().upper().startswith("SELECT")
               for s in activation_statements)


def test_strict_signal_time_boundary(monkeypatch):
    # Exactly at the boundary and before it: refused, no reservation.
    for index, signal in enumerate(
            (ACTIVATION, ACTIVATION - timedelta(microseconds=1),
             ACTIVATION - timedelta(days=1))):
        db = FakeConnection(observations={1: obs_row(signal_time=signal)})
        client = good_client()
        assert mod.capture(db, client,
                           **capture_kwargs(signal_time=signal)) is False
        assert db.inserts == []
        assert client.calls == []
        assert mod.stats()["pre_activation_denied"] == index + 1

    # Strictly after the boundary: captured (quote may still be DELAYED,
    # which only affects eligibility, never the reservation).
    after = ACTIVATION + timedelta(microseconds=1)
    db = FakeConnection(observations={1: obs_row(signal_time=after)})
    client = good_client()
    assert mod.capture(db, client,
                       **capture_kwargs(signal_time=after)) is True
    assert len(db.inserts) == 1


def test_activation_read_failure_rolls_back():
    db = FakeConnection()
    db.cursor = MagicMock(side_effect=RuntimeError("db down"))
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.rollbacks == 1
    assert client.calls == []
    assert mod.stats()["capture_errors"] == 1


# ── event identity resolution ────────────────────────────────────────────


def test_only_srr_short_observation_ids_are_captured():
    # observe() may return ids belonging to several experiments.
    observations = {
        1: obs_row(obs_id=1, experiment_id="SRR_OOS_SCANNER_V1_PROSPECTIVE"),
        2: obs_row(obs_id=2),
        3: obs_row(obs_id=3, direction="LONG"),
        4: obs_row(obs_id=4, symbol="ETHUSDT"),
    }
    db = FakeConnection(observations=observations)
    client = good_client()
    assert mod.capture(db, client,
                       **capture_kwargs(observation_ids=[1, 2, 3, 4])) is True
    assert len(db.inserts) == 1
    claim = db.inserts[0][1]
    assert claim[1] == 2  # exactly the SRR SHORT observation_id
    assert claim[5] == "BTCUSDT"
    assert len(client.orderbook_calls) == 1


def test_observation_without_row_is_denied():
    db = FakeConnection(observations={})
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.inserts == []
    assert client.calls == []


def test_candidate_signal_time_must_match_observation():
    db = FakeConnection()
    client = good_client()
    # The candidate's signal_time differs from the stored observation ->
    # the id cannot be bound to THIS event.
    assert mod.capture(db, client,
                       **capture_kwargs(signal_time=SIGNAL - timedelta(hours=1))
                       ) is False
    assert db.inserts == []
    assert client.calls == []


def test_candidate_reference_price_must_match_observation():
    db = FakeConnection()
    client = good_client()
    assert mod.capture(db, client,
                       **capture_kwargs(reference_price=Decimal("101"))) is False
    assert db.inserts == []
    assert client.calls == []


@pytest.mark.parametrize("bad_geometry", [
    {"variant_stop": Decimal("100.9")},   # not ref + 2.0 * structural_R
    {"variant_target": Decimal("99.9")},  # not ref - 1.5 * structural_R
    {"variant_stop": None},
    {"variant_target": None},
])
def test_frozen_geometry_mismatch_denied(bad_geometry):
    db = FakeConnection(observations={1: obs_row(**bad_geometry)})
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.inserts == []
    assert client.calls == []


def test_invalid_geometry_never_reaches_db():
    # SHORT invalidation below the entry: not a valid frozen geometry.
    bad_row = obs_row(reference_price=Decimal("100"),
                      invalidation_price=Decimal("99"))
    db = FakeConnection(observations={1: bad_row})
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs(
        reference_price=Decimal("100"),
        invalidation_price=Decimal("99"))) is False
    assert db.inserts == []
    assert client.calls == []


def test_claim_insert_identity_contract():
    db, client = FakeConnection(), good_client()
    assert mod.capture(db, client, **capture_kwargs()) is True
    sql, params = db.inserts[0]
    assert "ON CONFLICT DO NOTHING" in sql
    assert "observation_id" in sql
    assert params[1] == 1                          # source observation id
    assert params[2] == mod.EXPERIMENT_ID
    assert params[3] == mod.SOURCE_EXPERIMENT_ID
    assert params[4] == "SUPPORT_RESISTANCE_REACTION"
    assert params[6] == "SHORT"
    assert params[7] == SIGNAL
    assert params[8] == ACTIVATION                 # immutable boundary
    assert params[12] == REF
    assert params[14] == Decimal("0.2")            # structural_R
    assert params[15] == Decimal("0.4")            # execution_R = 2.0 * sR
    assert params[16] == VARIANT_STOP
    assert params[17] == VARIANT_TARGET
    assert isinstance(params[18], int)             # signal_to_capture_ms
    assert params[18] >= 0


# ── idempotency / concurrency / scanner restart ──────────────────────────


def test_duplicate_event_skips_before_any_api_call():
    db = FakeConnection(duplicate_exists=True)
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.inserts == []
    assert client.calls == []  # dedup happens before budget and market data
    assert mod.stats()["duplicate_skipped"] == 1


def test_concurrent_duplicate_claim_makes_no_second_api_call():
    # Another process won the atomic claim between our SELECT and INSERT.
    db = FakeConnection(claim_conflict=True)
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert len(db.inserts) == 1
    assert client.calls == []  # reservation lost -> no Bybit request
    assert mod.stats()["duplicate_skipped"] == 1


def test_scanner_restart_replay_is_idempotent():
    first = FakeConnection()
    assert mod.capture(first, good_client(), **capture_kwargs()) is True
    key = first.inserts[0][1][0]

    # "Restart": fresh counters/budget, same deterministic identity.
    mod.reset_budget()
    second = FakeConnection(duplicate_exists=True)
    assert mod.capture(second, good_client(), **capture_kwargs()) is False
    assert second.inserts == []
    assert second.commits == 0

    # Deterministic event_key for identical identity inputs.
    obs = {"observation_id": 1, "symbol": "BTCUSDT", "signal_time": SIGNAL,
           "reference_price": REF}
    assert mod._event_key(obs) == mod._event_key(dict(obs))


def test_event_key_is_unique_per_observation():
    signal_a, signal_b = SIGNAL, SIGNAL + timedelta(seconds=1)
    db = FakeConnection(observations={
        1: obs_row(signal_time=signal_a),
        2: obs_row(obs_id=2, signal_time=signal_b),
    })
    assert mod.capture(db, good_client(),
                       **capture_kwargs(observation_ids=[1],
                                        signal_time=signal_a)) is True
    assert mod.capture(db, good_client(),
                       **capture_kwargs(observation_ids=[2],
                                        signal_time=signal_b)) is True
    keys = [s[1][0] for s in db.inserts]
    assert len(keys) == len(set(keys)) == 2
    obs_ids = [s[1][1] for s in db.inserts]
    assert sorted(obs_ids) == [1, 2]


# ── API budget ───────────────────────────────────────────────────────────


def test_cycle_api_budget_is_enforced_and_reset(monkeypatch):
    monkeypatch.setenv("SRR_SHORT_FILL_MAX_PER_CYCLE", "1")
    signal_b = SIGNAL + timedelta(seconds=1)
    observations = {
        1: obs_row(signal_time=SIGNAL),
        2: obs_row(obs_id=2, signal_time=signal_b),
    }
    db = FakeConnection(observations=observations)
    client = good_client()

    assert mod.capture(db, client, **capture_kwargs(
        observation_ids=[1])) is True
    assert mod.capture(db, client,
                       **capture_kwargs(observation_ids=[2],
                                        signal_time=signal_b)) is False
    assert len(db.inserts) == 1
    assert len(client.orderbook_calls) == 1
    assert mod.stats()["budget_denied"] == 1

    # Per-cycle reset (run_scan_cycle) restores the cycle allowance only.
    mod.begin_capture_cycle()
    assert mod.capture(db, client, **capture_kwargs(
        observation_ids=[2], signal_time=signal_b)) is True
    assert len(db.inserts) == 2
    assert len(client.orderbook_calls) == 2


def test_run_api_budget_bounds_the_whole_process(monkeypatch):
    monkeypatch.setenv("SRR_SHORT_FILL_MAX_PER_RUN", "1")
    signal_b = SIGNAL + timedelta(seconds=1)
    observations = {
        1: obs_row(signal_time=SIGNAL),
        2: obs_row(obs_id=2, signal_time=signal_b),
    }
    db = FakeConnection(observations=observations)
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs(
        observation_ids=[1])) is True
    mod.begin_capture_cycle()  # cycle reset must NOT reset the run counter
    assert mod.capture(db, client, **capture_kwargs(
        observation_ids=[2], signal_time=signal_b)) is False
    assert len(db.inserts) == 1
    assert mod.stats()["budget_denied"] == 1


def test_api_budget_limits_are_capped(monkeypatch):
    # Configuration can never exceed the hard caps (bounded API usage).
    monkeypatch.setenv("SRR_SHORT_FILL_MAX_PER_CYCLE", "1000000")
    assert mod._read_limit(mod.MAX_PER_CYCLE_ENV, mod.DEFAULT_MAX_PER_CYCLE,
                           mod.HARD_MAX_PER_CYCLE) == mod.HARD_MAX_PER_CYCLE


def test_run_scan_cycle_resets_the_cycle_budget():
    source = (ROOT / "scanner_runner.py").read_text(encoding="utf-8")
    assert "begin_capture_cycle()" in source
    assert source.index("def run_scan_cycle") < source.index(
        "begin_capture_cycle()")


# ── PostgreSQL failures ──────────────────────────────────────────────────


def test_postgres_failure_fails_closed_without_api():
    db = FakeConnection()
    db.cursor = MagicMock(side_effect=RuntimeError("pg down"))
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert db.rollbacks == 1
    assert client.calls == []


def test_finalize_failure_returns_false_and_rolls_back():
    db = FakeConnection(finalize_error=RuntimeError("finalize down"))
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    assert len(db.inserts) == 1  # the claim itself was committed first
    assert db.rollbacks >= 1
    assert mod.stats()["capture_errors"] == 1


def test_finalize_never_touches_a_finalized_row():
    db = FakeConnection(finalize_rowcount=0)
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is False
    # The UPDATE carries the PENDING-only guard.
    sql = db.updates[0][0]
    assert "feasibility_status='PENDING'" in sql


# ── Bybit failures (rate limit / timeout / transport) ────────────────────


@pytest.mark.parametrize("error,reason", [
    (RuntimeError("boom"), "orderbook:RuntimeError"),
    (BybitRateLimitError("10006"), "orderbook:BybitRateLimitError"),
    (TimeoutError("read timeout"), "orderbook:TimeoutError"),
])
def test_bybit_failures_record_an_error_row(error, reason):
    db = FakeConnection()
    client = good_client(book_error=error)
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    assert row["feasibility_status"] == "ERROR"
    assert row["quote_quality_status"] == "NO_QUOTE"
    assert row["failure_reason"] == reason
    assert row["eligible_for_execution_stats"] is False
    assert row["estimated_fill"] is None
    assert json.loads(row["raw_snapshot"])["error"] == reason
    # Exactly one logical orderbook attempt — no local retry layer.
    assert len(client.orderbook_calls) == 1


def test_bybit_failure_does_not_leak_into_scanner():
    # Wrapper-level isolation is asserted separately; here: capture itself
    # never raises regardless of exchange failure.
    db = FakeConnection()
    assert mod.capture(db, good_client(book_error=RuntimeError("x")),
                       **capture_kwargs()) is True


# ── orderbook validation / quality statuses ──────────────────────────────


def test_empty_orderbook_invalid():
    db = FakeConnection()
    client = good_client(book=book(bids=[], asks=[]))
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    assert row["feasibility_status"] == "INVALID_BOOK"
    assert row["quote_quality_status"] == "INVALID_BOOK"
    assert row["eligible_for_execution_stats"] is False


def test_crossed_top_of_book_invalid():
    db = FakeConnection()
    client = good_client(book=live_book(bids=[["99.9", "5"]],
                                        asks=[["99.0", "5"]]))
    assert mod.capture(db, client, **capture_kwargs()) is True
    assert finalize_row(db)["feasibility_status"] == "INVALID_BOOK"


def test_stale_orderbook_excluded():
    stale_ts = int((datetime.now(timezone.utc) -
                    timedelta(seconds=60)).timestamp() * 1000)
    db = FakeConnection()
    client = good_client(book=book(ts=stale_ts))
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    assert row["feasibility_status"] == "STALE_BOOK"
    assert row["quote_quality_status"] == "STALE_BOOK"
    assert row["eligible_for_execution_stats"] is False


def test_future_orderbook_timestamp_excluded():
    future_ts = int((datetime.now(timezone.utc) +
                     timedelta(seconds=10)).timestamp() * 1000)
    db = FakeConnection()
    client = good_client(book=book(ts=future_ts))
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    assert row["quote_quality_status"] == "FUTURE_BOOK"
    assert row["feasibility_status"] == "FUTURE_BOOK"
    assert row["eligible_for_execution_stats"] is False


def test_signal_to_quote_delay_is_measured_independently(monkeypatch):
    # A FRESH book does not prove signal-time alignment: the signal was
    # generated 10 minutes before the capture started.
    monkeypatch.setenv("SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS", "60000")
    signal = datetime.now(timezone.utc) - timedelta(minutes=10)
    observations = {1: obs_row(signal_time=signal)}
    db = FakeConnection(observations=observations)
    client = good_client()  # book stamped "now" -> book_age_ms ~ 0
    assert mod.capture(db, client,
                       **capture_kwargs(signal_time=signal)) is True
    row = finalize_row(db)
    assert row["book_age_ms"] is not None and row["book_age_ms"] < 10000
    assert row["signal_to_capture_ms"] >= 600000
    assert row["quote_quality_status"] == "DELAYED"
    assert row["eligible_for_execution_stats"] is False


def test_future_signal_time_is_refused_before_budget_and_api():
    # signal_time ahead of the capture start = clock/identity anomaly.
    future_signal = datetime.now(timezone.utc) + timedelta(minutes=1)
    db = FakeConnection(observations={1: obs_row(signal_time=future_signal)})
    client = good_client()
    assert mod.capture(db, client,
                       **capture_kwargs(signal_time=future_signal)) is False
    assert db.inserts == []
    assert client.calls == []
    assert mod.stats()["timing_denied"] == 1
    assert mod.stats()["budget_denied"] == 0  # refusal precedes the budget


def test_timings_and_capture_timestamps_are_stored():
    db = FakeConnection()
    client = good_client()
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    # signal_time / capture_started_at are immutable claim data; the rest
    # of the timing evidence is finalized in one UPDATE.
    claim = db.inserts[0][1]
    assert claim[7] == SIGNAL
    assert row["captured_at"] >= SIGNAL
    assert row["capture_finished_at"] >= claim[10]  # capture_started_at
    assert row["capture_finished_at"] >= row["captured_at"]
    assert isinstance(row["api_request_ms"], int) and row["api_request_ms"] >= 0
    assert row["book_time_ms"] is not None
    assert row["book_age_ms"] is not None
    assert row["signal_to_capture_ms"] >= 0


# ── eligibility: depth / constraints ─────────────────────────────────────


def test_happy_path_is_fresh_ok_and_eligible():
    db = FakeConnection()
    client = good_client(book=live_book(bids=[["99.9", "5"]],
                                        asks=[["100.1", "5"]]))
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    assert row["feasibility_status"] == "OK"
    assert row["quote_quality_status"] == "FRESH"
    assert row["eligible_for_execution_stats"] is True
    assert row["bid"] == Decimal("99.9")
    assert row["ask"] == Decimal("100.1")
    assert row["spread_bps"] > 0
    assert row["reference_to_bid_bps"] > 0
    assert row["estimated_fill"] == Decimal("99.9")
    diagnostics = json_field(row, "source_diagnostics")
    assert diagnostics["constraints_known"] is True
    assert diagnostics["executable_net_r_computed"] is False
    assert diagnostics["backfill"] == "FORBIDDEN"
    # Frozen execution geometry lives on the immutable claim row.
    claim = db.inserts[0][1]
    assert claim[14] == Decimal("0.2")   # structural_R
    assert claim[15] == Decimal("0.4")   # execution_R = 2.0 * structural_R
    assert claim[16] == VARIANT_STOP
    assert claim[17] == VARIANT_TARGET


def test_insufficient_depth_is_stored_but_not_eligible():
    db = FakeConnection()
    client = good_client(book=live_book(bids=[["99.9", "0.01"]],
                                        asks=[["100.1", "0.01"]]))
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    assert row["feasibility_status"] == "INSUFFICIENT_DEPTH"
    assert row["estimated_fill"] is None
    assert row["eligible_for_execution_stats"] is False


def test_unknown_instrument_constraints_are_never_confirmed_executable():
    db = FakeConnection()
    client = good_client(book=live_book(),
                         instruments={"result": {"list": []}})
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    constraints = json_field(row, "exchange_constraints")
    assert constraints["known"] is False
    assert constraints["lookup_error"] == "instruments_info:MISSING_LOT_SIZE_FILTER"
    # Depth is fine, but the scenario cannot be CONFIRMED executable.
    assert row["feasibility_status"] == "OK"
    assert row["eligible_for_execution_stats"] is False


def test_instrument_lookup_failure_keeps_capture_alive():
    db = FakeConnection()
    client = good_client(book=live_book())

    def boom(endpoint, **params):
        client.calls.append((endpoint, params))
        if endpoint.endswith("/instruments-info"):
            raise RuntimeError("instruments down")
        return client.book

    client._public_get = boom
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    assert json_field(row, "exchange_constraints")["known"] is False
    assert row["eligible_for_execution_stats"] is False


def test_min_notional_blocks_base_scenario():
    db = FakeConnection()
    instruments = {"result": {"list": [{
        "lotSizeFilter": {"minOrderQty": "10", "qtyStep": "1",
                          "minNotionalValue": "500"}}]}}
    client = good_client(book=live_book(), instruments=instruments)
    assert mod.capture(db, client, **capture_kwargs()) is True
    row = finalize_row(db)
    # 100 USDT -> qty 1 < minOrderQty 10 -> below exchange minimum.
    assert row["feasibility_status"] == "BELOW_EXCHANGE_MIN"
    assert row["eligible_for_execution_stats"] is False


# ── pure calculation: scenarios, rounding, SRR cost model ────────────────


def test_three_notional_scenarios_are_evaluated():
    out = mod.calculate_snapshot(
        100, 0.2, book(ts=100100),
        constraints={"min_order_qty": Decimal("0.001"),
                     "qty_step": Decimal("0.001"),
                     "min_notional": Decimal("5")},
        now_ms=100200)
    assert [s["notional_usdt"] for s in out["scenario_results"]] == \
        list(mod.DEFAULT_NOTIONAL_SCENARIOS)
    assert out["execution_r"] == Decimal("0.4")
    assert out["variant_stop"] == Decimal("100.4")
    assert out["variant_target"] == Decimal("99.7")


def test_qty_step_rounding_down():
    out = mod.calculate_snapshot(
        100, 0.2, book(ts=100100),
        constraints={"min_order_qty": Decimal("1"),
                     "qty_step": Decimal("1"), "min_notional": None},
        notional_scenarios=[150], now_ms=100200)
    scenario = out["scenario_results"][0]
    assert scenario["requested_qty"] == Decimal("1.5")
    assert scenario["executable_qty"] == Decimal("1")


def test_no_partial_execution_without_depth():
    out = mod.calculate_snapshot(
        100, 0.2, book(ts=100100, bids=[["99.9", "0.5"]], asks=[["100.1", "5"]]),
        constraints={"min_order_qty": None, "qty_step": None,
                     "min_notional": None},
        notional_scenarios=[100], now_ms=100200)
    scenario = out["scenario_results"][0]
    assert scenario["executable_qty"] == Decimal("1")
    assert scenario["estimated_fill"] is None  # 0.5 depth < 1.0 needed
    assert scenario["status"] == "INSUFFICIENT_DEPTH"
    assert scenario["entry_slippage_bps"] is None


def test_execution_r_cost_normalization_is_exact():
    # ref=100, structural_R=0.2 -> execution_R=0.4
    # fill=99.9 -> entry dev 0.1%; spread (100.1-99.9)/100 = 0.2%;
    # 2*fee = 0.11%  => cost_pct = 0.41% => cost_r = 0.0041*100/0.4
    out = mod.calculate_snapshot(
        100, 0.2, book(ts=100100),
        constraints={"min_order_qty": None, "qty_step": None,
                     "min_notional": None},
        notional_scenarios=[100], now_ms=100200)
    scenario = out["scenario_results"][0]
    assert scenario["estimated_fill"] == Decimal("99.9")
    assert scenario["observed_roundtrip_cost_pct"] == Decimal("0.0041")
    assert scenario["observed_cost_r"] == Decimal("1.025")
    # Frozen NORMAL cost_R for the same entry: 0.0021 * 100 / 0.4 = 0.525
    assert scenario["frozen_normal_cost_r"] == Decimal("0.525")
    assert scenario["frozen_elevated_cost_r"] == Decimal("0.775")
    assert scenario["extra_cost_r_vs_frozen_normal"] == Decimal("0.5")
    assert scenario["cost_class"] == "ABOVE_ELEVATED"
    assert out["structural_r"] == Decimal("0.2")
    assert out["execution_r"] == Decimal("0.4")


def test_no_double_counting_of_costs():
    out = mod.calculate_snapshot(
        100, 0.2, book(ts=100100),
        constraints={"min_order_qty": None, "qty_step": None,
                     "min_notional": None},
        notional_scenarios=[100], now_ms=100200)
    scenario = out["scenario_results"][0]
    ref = Decimal("100")
    fill = scenario["estimated_fill"]
    bid, ask = Decimal("99.9"), Decimal("100.1")
    mid = (bid + ask) / 2
    expected = (ref - fill) / ref + (ask - bid) / mid + 2 * mod.FEE_PER_SIDE
    assert scenario["observed_roundtrip_cost_pct"] == expected
    # Exactly one entry term, one spread term, one fee term; and the module
    # never emits a recomputed Net R at all.
    assert "entry_deviation_pct" in scenario
    assert not any(key.startswith("net") for key in scenario)
    assert not any(key.startswith("net") for key in out)


def test_calculate_snapshot_rejects_invalid_geometry():
    with pytest.raises(ValueError):
        mod.calculate_snapshot(100, 0, book(ts=100100), now_ms=100200)
    with pytest.raises(ValueError):
        mod.calculate_snapshot(-1, 0.2, book(ts=100100), now_ms=100200)
    with pytest.raises(ValueError):
        mod.calculate_snapshot(100, 0.2, book(ts=100100),
                               notional_scenarios=[0], now_ms=100200)


# ── scanner wrapper: routing, isolation, ordering ────────────────────────


def _wrapper_kwargs(**overrides):
    kwargs = dict(
        scanner_name="SUPPORT_RESISTANCE_REACTION",
        direction="SHORT",
        symbol="BTCUSDT",
        signal_time=SIGNAL,
        reference_price=100.0,
        invalidation_price=100.2,
        target_1=99.0,
        target_2=None,
        score=1.0,
        features={},
        parameters={},
        market_regime=None,
    )
    kwargs.update(overrides)
    return kwargs


def test_wrapper_captures_srr_short_after_observe(monkeypatch):
    import scanner_runner
    order = []
    prospective_obs = MagicMock()
    prospective_obs.observe.side_effect = lambda **kw: order.append("observe") or [11, 22]
    prospective_obs._conn = FakeConnection()
    seen = {}

    def fake_capture(conn, client, **kwargs):
        order.append("capture")
        seen.update(kwargs)
        seen["conn"] = conn
        return True

    monkeypatch.setattr(mod, "capture", fake_capture)
    me_calls = []
    monkeypatch.setattr(me_mod, "capture", lambda *a, **k: me_calls.append(1))

    out = scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **_wrapper_kwargs())
    assert out == [11, 22]                     # observe() semantics unchanged
    assert order == ["observe", "capture"]     # never inside the transaction
    assert seen["observation_ids"] == [11, 22]
    assert seen["scanner_name"] == "SUPPORT_RESISTANCE_REACTION"
    assert seen["direction"] == "SHORT"
    assert seen["conn"] is prospective_obs._conn
    # The ME SHORT A capture must not fire for an SRR scanner even when
    # its own feature flag is on.
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **_wrapper_kwargs())
    assert me_calls == []


def test_wrapper_skips_capture_without_flag(monkeypatch):
    import scanner_runner
    monkeypatch.delenv("SRR_SHORT_FILL_CAPTURE_ENABLED", raising=False)
    prospective_obs = MagicMock()
    prospective_obs.observe.return_value = [1]
    calls = []
    monkeypatch.setattr(mod, "capture", lambda *a, **k: calls.append(1))
    scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **_wrapper_kwargs())
    assert calls == []


@pytest.mark.parametrize("overrides", [
    {"direction": "LONG"},
    {"scanner_name": "MOMENTUM_EXHAUSTION", "direction": "SHORT"},
    {"scanner_name": "BREAKOUT_RETEST", "direction": "SHORT"},
])
def test_wrapper_routes_only_srr_short(monkeypatch, overrides):
    import scanner_runner
    prospective_obs = MagicMock()
    prospective_obs.observe.return_value = [1]
    calls = []
    monkeypatch.setattr(mod, "capture", lambda *a, **k: calls.append(1))
    scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **_wrapper_kwargs(**overrides))
    assert calls == []


def test_wrapper_isolates_capture_failures(monkeypatch):
    import scanner_runner
    prospective_obs = MagicMock()
    prospective_obs.observe.return_value = [5]

    def boom(*args, **kwargs):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(mod, "capture", boom)
    out = scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **_wrapper_kwargs())
    assert out == [5]  # scanner-visible result unaffected


def test_wrapper_does_not_call_capture_without_observations(monkeypatch):
    import scanner_runner
    prospective_obs = MagicMock()
    prospective_obs.observe.return_value = []  # nothing new for SRR
    calls = []
    monkeypatch.setattr(mod, "capture", lambda *a, **k: calls.append(1))
    out = scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **_wrapper_kwargs())
    assert out == []
    assert calls == []


# ── production isolation: no trading objects touched ─────────────────────


def test_module_has_no_order_submission_surface():
    source = (ROOT / "app" / "research" /
              "srr_short_fill_feasibility_v1.py").read_text(encoding="utf-8")
    for forbidden in ("/v5/order/create", "set-leverage", "set-trading-stop",
                      "place_order", "submit_order", "create_order",
                      "paper_exchange", "live_exchange"):
        assert forbidden not in source
    # Only read-only public market endpoints are reachable from here.
    assert "/v5/market/orderbook" in source


def test_capture_never_raises(monkeypatch):
    # Even a fully broken environment must produce False, not an exception.
    db = FakeConnection()
    db.cursor = MagicMock(side_effect=MemoryError("simulated"))
    result = mod.capture(db, good_client(), **capture_kwargs())
    assert result is False
