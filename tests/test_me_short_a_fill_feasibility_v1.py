"""Issue #202 Phase 1: quote math, scenarios, constraints, gates, idempotency.

All tests are offline: no exchange calls, no PostgreSQL. The fake connection
replays the exact SQL contract used by ``capture`` so fail-closed activation,
strict post-freeze eligibility and idempotency are asserted on real statements.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.research.me_short_a_fill_feasibility_v1 import (
    DEFAULT_NOTIONAL_SCENARIOS,
    ELEVATED_ROUND_TRIP,
    EXPERIMENT,
    NORMAL_ROUND_TRIP,
    _estimate_bid_fill,
    calculate_snapshot,
    capture,
    instrument_constraints,
)
import app.research.me_short_a_fill_feasibility_v1 as mod

FREEZE = datetime(2026, 10, 19, 8, 26, tzinfo=timezone.utc)
AFTER_FREEZE = FREEZE + timedelta(minutes=5)


@pytest.fixture(autouse=True)
def _clear_instrument_cache():
    mod._INSTRUMENT_CACHE.clear()
    yield
    mod._INSTRUMENT_CACHE.clear()


# ── fakes ─────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.rowcount = -1

    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params or ()))
        head = sql.lstrip().upper()
        if "SELECT STATUS,FREEZE_TS" in head:
            self._rows = [self.conn.activation_row] if self.conn.activation_row else []
        elif head.startswith("SELECT 1"):
            self._rows = [(1,)] if self.conn.duplicate_exists else []
        elif head.startswith("INSERT"):
            self.rowcount = 0 if self.conn.conflict_skip else 1

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, activation_row=None, duplicate_exists=False,
                 conflict_skip=False):
        self.activation_row = activation_row
        self.duplicate_exists = duplicate_exists
        self.conflict_skip = conflict_skip
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
        return [s for s in self.statements if s[0].lstrip().upper().startswith("INSERT")]


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
        return self.instruments or {"result": {"list": []}}


def book(ts=100000, bids=None, asks=None):
    return {"result": {
        "ts": ts,
        "b": bids if bids is not None else [["99.9", "2"], ["99.8", "3"]],
        "a": asks if asks is not None else [["100.1", "3"]]}}


def live_book(**kwargs):
    """Book with a timestamp inside the staleness bound of real wall-clock."""
    return book(ts=int(datetime.now(timezone.utc).timestamp() * 1000), **kwargs)


def default_constraints(**overrides):
    base = {"min_order_qty": None, "qty_step": None, "min_notional": None}
    base.update(overrides)
    return base


def base_kwargs(**overrides):
    kwargs = dict(scanner_name="MOMENTUM_EXHAUSTION", direction="SHORT",
                  symbol="BTCUSDT", signal_time=AFTER_FREEZE,
                  reference_price=Decimal("100"),
                  invalidation_price=Decimal("100.2"),
                  target_1=Decimal("99"))
    kwargs.update(overrides)
    return kwargs


# ── quote math ────────────────────────────────────────────────


def test_weighted_short_fill_and_spread():
    out = calculate_snapshot(
        100, 100.2, book(), constraints=default_constraints(),
        notional_scenarios=[300], now_ms=100100)
    assert out["status"] == "OK"
    assert Decimal("99.8") <= out["estimated_fill"] <= Decimal("99.9")
    assert out["spread_bps"] > 0
    assert out["reference_to_bid_bps"] > 0
    assert out["structural_r"] == Decimal("0.2")
    assert out["structural_r_pct"] == Decimal("0.2")
    assert out["book_age_ms"] == 100


def test_estimate_bid_fill_partial_book_returns_none():
    assert _estimate_bid_fill([["99.9", "0.001"]], 100) is None
    assert _estimate_bid_fill([["0", "5"]], 100) is None  # invalid level skipped


def test_multi_scenario_depth_sensitivity():
    # 100/500 USDT fit the book; 1000 USDT needs more depth than available.
    out = calculate_snapshot(
        100, 100.2,
        book(bids=[["99.9", "2"], ["99.8", "3"], ["99.7", "2"]]),
        constraints=default_constraints(), now_ms=100100)
    scenarios = {s["notional_usdt"]: s for s in out["scenario_results"]}
    assert [s["notional_usdt"] for s in out["scenario_results"]] == \
        list(DEFAULT_NOTIONAL_SCENARIOS)
    assert scenarios[Decimal("100")]["status"] == "OK"
    assert scenarios[Decimal("500")]["status"] == "OK"
    assert scenarios[Decimal("1000")]["status"] == "INSUFFICIENT_DEPTH"
    assert scenarios[Decimal("1000")]["estimated_fill"] is None
    # Top-level mirrors the base (first) scenario only.
    assert out["status"] == "OK"
    assert out["requested_notional_usdt"] == Decimal("100")
    # Larger scenarios must not be silently feasible when depth is missing.
    assert scenarios[Decimal("500")]["estimated_fill"] is not None


def test_insufficient_depth_and_stale_quote():
    out = calculate_snapshot(
        100, 100.2, book(), constraints=default_constraints(),
        notional_scenarios=[10000], now_ms=100100)
    assert out["status"] == "INSUFFICIENT_DEPTH"
    out = calculate_snapshot(100, 100.2, book(), now_ms=120000)
    assert out["status"] == "STALE_BOOK"


def test_future_book_skew_rejected():
    out = calculate_snapshot(
        100, 100.2, book(ts=103000), now_ms=100100)
    assert out["status"] == "STALE_BOOK"


def test_invalid_crossed_book_and_geometry():
    bad = book()
    bad["result"]["a"] = [["99.0", "1"]]
    assert calculate_snapshot(100, 100.2, bad, now_ms=100100)["status"] == "INVALID_BOOK"
    empty = book()
    empty["result"]["b"] = []
    assert calculate_snapshot(100, 100.2, empty, now_ms=100100)["status"] == "INVALID_BOOK"
    with pytest.raises(ValueError):
        calculate_snapshot(100, 99.9, book(), now_ms=100100)
    with pytest.raises(ValueError):
        calculate_snapshot(100, 100.2, book(), notional_scenarios=[0], now_ms=100100)


# ── spread / slippage / cost model ────────────────────────────


def test_cost_estimate_and_class_boundaries():
    out = calculate_snapshot(
        100, 100.2, book(), constraints=default_constraints(),
        notional_scenarios=[100], now_ms=100100)
    cost_pct = out["estimated_roundtrip_cost_pct"]
    assert cost_pct is not None
    # entry deviation + spread + 2*fee, all non-negative
    assert cost_pct > 0
    assert float(out["estimated_roundtrip_cost_r"]) == pytest.approx(
        float(cost_pct * Decimal("500")), rel=1e-9)
    assert out["cost_class"] in {"NORMAL", "ELEVATED", "ABOVE_ELEVATED"}

    # Frozen classification thresholds must drive cost_class exactly.
    wide = book(bids=[["99.0", "50"]], asks=[["101.0", "50"]])
    out_wide = calculate_snapshot(
        100, 100.2, wide, constraints=default_constraints(),
        notional_scenarios=[100], now_ms=100100)
    assert out_wide["cost_class"] == "ABOVE_ELEVATED"
    assert out_wide["estimated_roundtrip_cost_pct"] > ELEVATED_ROUND_TRIP

    tight = book(bids=[["99.999", "50"]], asks=[["100.001", "50"]])
    out_tight = calculate_snapshot(
        100, 100.2, tight, constraints=default_constraints(),
        notional_scenarios=[100], now_ms=100100)
    assert out_tight["cost_class"] == "NORMAL"
    assert out_tight["estimated_roundtrip_cost_pct"] <= NORMAL_ROUND_TRIP


# ── exchange minQty / minNotional ─────────────────────────────


def test_below_min_qty_and_min_notional_scenarios():
    constraints = default_constraints(
        min_order_qty=Decimal("5"), qty_step=Decimal("1"),
        min_notional=Decimal("600"))
    out = calculate_snapshot(
        100, 100.2, book(bids=[["99.9", "1000"]], asks=[["100.1", "1000"]]),
        constraints=constraints, now_ms=100100)
    scenarios = {s["notional_usdt"]: s for s in out["scenario_results"]}
    # 100 USDT -> 1 qty < minOrderQty 5 -> blocked
    assert scenarios[Decimal("100")]["status"] == "BELOW_EXCHANGE_MIN"
    assert scenarios[Decimal("100")]["below_min_qty"] is True
    assert "minOrderQty" in scenarios[Decimal("100")]["reason"]
    # 500 USDT -> 5 qty ok but notional ~500 < 600 -> blocked
    assert scenarios[Decimal("500")]["status"] == "BELOW_EXCHANGE_MIN"
    assert scenarios[Decimal("500")]["below_min_notional"] is True
    assert "minNotional" in scenarios[Decimal("500")]["reason"]
    # 1000 USDT -> 10 qty, notional ~1000 -> feasible
    assert scenarios[Decimal("1000")]["status"] == "OK"
    assert out["constraints"]["known"] is True


def test_qty_step_rounding_and_unknown_constraints_flag():
    constraints = default_constraints(
        min_order_qty=Decimal("0.5"), qty_step=Decimal("0.5"))
    out = calculate_snapshot(
        100, 100.2, book(bids=[["99.9", "100"]], asks=[["100.1", "100"]]),
        constraints=constraints, now_ms=100100)
    scenario = out["scenario_results"][0]
    assert scenario["requested_qty"] == Decimal("1")
    assert scenario["executable_qty"] == Decimal("1")
    assert scenario["constraints_known"] is True

    out_unknown = calculate_snapshot(
        100, 100.2, book(bids=[["99.9", "100"]], asks=[["100.1", "100"]]),
        constraints=None, constraints_error="instruments_info:Timeout",
        now_ms=100100)
    scenario = out_unknown["scenario_results"][0]
    assert scenario["constraints_known"] is False
    assert scenario["status"] == "OK"  # depth feasible; min checks unassessed
    assert out_unknown["constraints"]["lookup_error"] == "instruments_info:Timeout"


def test_instrument_constraints_parsing_and_cache():
    client = FakeClient(instruments={"result": {"list": [{
        "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001",
                          "minNotional": "5"}}]}})
    got, err = instrument_constraints(client, "BTCUSDT")
    assert err is None
    assert got == {"min_order_qty": Decimal("0.001"),
                   "qty_step": Decimal("0.001"),
                   "min_notional": Decimal("5")}
    # Second call served from cache: single endpoint hit.
    instrument_constraints(client, "BTCUSDT")
    assert len(client.calls) == 1

    failing = FakeClient(book_error=None)
    failing._public_get = MagicMock(side_effect=RuntimeError("down"))
    got, err = instrument_constraints(failing, "ETHUSDT")
    assert got is None
    assert err == "instruments_info:RuntimeError"


# ── capture gates / fail-closed activation ────────────────────


def test_no_capture_when_disabled_or_wrong_direction(monkeypatch):
    monkeypatch.delenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", raising=False)
    db, client = FakeConnection(), FakeClient()
    assert capture(db, client, **base_kwargs()) is False
    assert db.statements == []

    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    assert capture(db, client, **base_kwargs(direction="LONG")) is False
    assert capture(db, client, **base_kwargs(scanner_name="BREAKOUT_RETEST")) is False
    assert db.statements == []
    assert client.calls == []


def test_capture_requires_connection_and_client(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    assert capture(None, FakeClient(), **base_kwargs()) is False
    assert capture(FakeConnection(), None, **base_kwargs()) is False


@pytest.mark.parametrize("activation_row", [
    None,
    ("READY_TO_START", None),
    ("RUNNING", None),
    ("COMPLETED", FREEZE),
    ("CANCELLED", FREEZE),
])
def test_activation_gate_fails_closed(monkeypatch, activation_row):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    db = FakeConnection(activation_row=activation_row)
    client = FakeClient(book=book())
    assert capture(db, client, **base_kwargs()) is False
    assert db.inserts == []
    assert client.calls == []  # no market data requested when gate is closed


def test_strict_post_freeze_boundary(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    db = FakeConnection(activation_row=("RUNNING", FREEZE))
    client = FakeClient(book=book(ts=100000),
                        instruments={"result": {"list": []}})
    at_freeze = base_kwargs(signal_time=FREEZE)
    assert capture(db, client, **at_freeze) is False
    assert db.inserts == []

    before = base_kwargs(signal_time=FREEZE - timedelta(seconds=1))
    assert capture(db, client, **before) is False
    assert db.inserts == []

    after = base_kwargs()
    assert capture(db, client, **after) is True
    assert len(db.inserts) == 1
    assert db.commits == 1
    # Stored freeze must equal the activation boundary read from the DB.
    assert db.inserts[0][1][4] == FREEZE
    assert db.inserts[0][1][2] == AFTER_FREEZE


def test_invalid_geometry_never_reaches_db(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    db = FakeConnection(activation_row=("RUNNING", FREEZE))
    client = FakeClient(book=book())
    assert capture(db, client, **base_kwargs(target_1=Decimal("101"))) is False
    assert capture(db, client, **base_kwargs(invalidation_price=Decimal("99"))) is False
    assert capture(db, client, **base_kwargs(target_1=None)) is False
    assert db.statements == []


# ── idempotency ───────────────────────────────────────────────


def test_event_key_stable_and_duplicate_replay_skips(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    db = FakeConnection(activation_row=("RUNNING", FREEZE), duplicate_exists=True)
    client = FakeClient(book=book())
    assert capture(db, client, **base_kwargs()) is False
    assert db.inserts == []
    assert client.calls == []  # dedup happens before any market data request

    # ON CONFLICT DO NOTHING race: insert statement ran but wrote nothing.
    db2 = FakeConnection(activation_row=("RUNNING", FREEZE), conflict_skip=True)
    assert capture(db2, client, **base_kwargs()) is False
    assert len(db2.inserts) == 1
    assert db2.commits == 1

    # Same signal identity always hashes to the same event_key.
    db3 = FakeConnection(activation_row=("RUNNING", FREEZE))
    assert capture(db3, client, **base_kwargs()) is True
    key = db3.inserts[0][1][0]
    db4 = FakeConnection(activation_row=("RUNNING", FREEZE))
    assert capture(db4, client, **base_kwargs()) is True
    assert db4.inserts[0][1][0] == key


# ── write path / SQL contract ─────────────────────────────────


def test_insert_revalidates_activation_and_is_idempotent_sql(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    db = FakeConnection(activation_row=("RUNNING", FREEZE))
    client = FakeClient(book=book())
    assert capture(db, client, **base_kwargs(), observation_ids=[7, 8]) is True
    sql, params = db.inserts[0]
    # Anti-replay guard: activation re-checked atomically with the insert.
    assert "WHERE EXISTS (SELECT 1 FROM research.me_short_a_fill_activation_v1" in sql
    assert "status='RUNNING' AND freeze_ts=%s" in sql
    assert "ON CONFLICT(event_key) DO NOTHING" in sql
    # Signal ids / observation ids linkage required by the issue capture list.
    assert json.loads(params[-4]) == [7, 8]
    # Raw book snapshot preserved for replay.
    assert json.loads(params[-3])["result"]["ts"] == 100000
    assert params[-2] == EXPERIMENT
    assert params[-1] == FREEZE


def test_capture_persists_scenarios_costs_and_constraints(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    db = FakeConnection(activation_row=("RUNNING", FREEZE))
    client = FakeClient(
        book=live_book(bids=[["99.9", "100"]], asks=[["100.1", "100"]]),
        instruments={"result": {"list": [{
            "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001"}}]}})
    assert capture(db, client, **base_kwargs()) is True
    sql, params = db.inserts[0]
    columns = sql.split("(", 1)[1].split(")")[0].split(",")
    columns = [c.strip() for c in columns]
    row = dict(zip(columns, params[:len(columns)]))
    assert row["structural_r"] == Decimal("0.2")
    assert row["structural_r_pct"] == Decimal("0.2")
    assert row["min_order_qty"] == Decimal("0.001")
    assert row["qty_step"] == Decimal("0.001")
    assert row["estimated_roundtrip_cost_pct"] is not None
    assert row["cost_class"] in {"NORMAL", "ELEVATED", "ABOVE_ELEVATED"}
    payload = json.loads(row["scenario_results"])
    assert [s["notional_usdt"] for s in payload["scenarios"]] == \
        ["100", "500", "1000"]
    assert payload["scenarios"][0]["status"] == "OK"
    assert payload["exchange_constraints"]["known"] is True
    assert payload["cost_model"]["normal_round_trip"] == "0.0021"


def test_orderbook_error_still_records_error_row(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    db = FakeConnection(activation_row=("RUNNING", FREEZE))
    client = FakeClient(book_error=RuntimeError("boom"),
                        instruments={"result": {"list": []}})
    assert capture(db, client, **base_kwargs()) is True
    sql, params = db.inserts[0]
    columns = [c.strip() for c in sql.split("(", 1)[1].split(")")[0].split(",")]
    row = dict(zip(columns, params[:len(columns)]))
    assert row["feasibility_status"] == "ERROR"
    assert row["reason"] == "orderbook:RuntimeError"
    assert row["structural_r"] == Decimal("0.2")


def test_capture_failure_rolls_back_and_fails_closed(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    db = FakeConnection(activation_row=("RUNNING", FREEZE))
    db.cursor = MagicMock(side_effect=RuntimeError("db down"))
    assert capture(db, FakeClient(book=book()), **base_kwargs()) is False
    assert db.rollbacks == 1


# ── scanner integration wrapper ───────────────────────────────


def _wrapper_kwargs():
    return dict(scanner_name="MOMENTUM_EXHAUSTION", direction="SHORT",
                symbol="BTCUSDT", signal_time=AFTER_FREEZE,
                reference_price=100.0, invalidation_price=100.2,
                target_1=99.0, target_2=None, score=1.0, features={},
                parameters={}, market_regime=None)


def test_wrapper_preserves_observe_result_and_captures(monkeypatch):
    import scanner_runner
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    prospective_obs = MagicMock()
    prospective_obs.observe.return_value = [11, 22]
    prospective_obs._conn = FakeConnection()
    seen = {}

    def fake_capture(conn, client, **kwargs):
        seen.update(kwargs)
        seen["conn"] = conn
        return True

    monkeypatch.setattr(mod, "capture", fake_capture)
    out = scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **_wrapper_kwargs())
    assert out == [11, 22]
    prospective_obs.observe.assert_called_once()
    assert seen["symbol"] == "BTCUSDT"
    assert seen["observation_ids"] == [11, 22]
    assert seen["conn"] is prospective_obs._conn


def test_wrapper_skips_capture_for_non_me_short(monkeypatch):
    import scanner_runner
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    prospective_obs = MagicMock()
    prospective_obs.observe.return_value = [1]
    calls = []
    monkeypatch.setattr(mod, "capture", lambda *a, **k: calls.append(1))
    kwargs = _wrapper_kwargs()
    kwargs["direction"] = "LONG"
    scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **kwargs)
    assert calls == []

    monkeypatch.delenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", raising=False)
    scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **_wrapper_kwargs())
    assert calls == []


def test_wrapper_isolates_capture_failures(monkeypatch):
    import scanner_runner
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    prospective_obs = MagicMock()
    prospective_obs.observe.return_value = [5]

    def boom(*args, **kwargs):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(mod, "capture", boom)
    out = scanner_runner._observe_prospective_with_fill(
        object(), prospective_obs, **_wrapper_kwargs())
    assert out == [5]  # scanner-visible result unaffected
