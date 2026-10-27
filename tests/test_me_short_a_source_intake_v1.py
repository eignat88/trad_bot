"""ME SHORT A prospective source intake writer (Issue #208 corrective).

Contract tests for ``app/research/me_short_a_source_intake_v1.py``:

* the deterministic ``event_key`` is byte-identical to the Phase 1 / Issue
  #208 identity, at FULL microsecond signal-time precision;
* capture is strictly forward-only (pre-freeze events — including the
  confirmed incident event — are refused, never backfilled);
* every write is one atomic, idempotent ``INSERT ... ON CONFLICT DO NOTHING``
  with the activation ``WHERE EXISTS`` guard;
* capture fails OPEN on any error and must never affect scanner execution;
* the writer never writes outside ``research.me_short_a_source_intake_v1``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import app.research.me_short_a_source_intake_v1 as mod
from app.research.me_short_a_source_intake_v1 import (
    ACTIVATION_FLAG,
    CAPTURE_ROUTES,
    EXPERIMENT,
    INSERT_SQL,
    ROUTE_REGIME_FILTER_REJECTED,
    ROUTE_TRADEABLE_FLOW,
    activation_snapshot,
    capture,
    capture_with_status,
    compute_event_key,
)
from app.research.me_short_a_forward_outcome_lineage_v1 import (
    event_key as lineage_event_key,
)

# Intake activation boundary: the fixed mechanism starts strictly after the
# confirmed incident (TSLAUSDT 2026-10-27 10:43:57.672455+00).
INTAKE_FREEZE = datetime(2026, 10, 27, 12, 0, 0, 0, tzinfo=timezone.utc)
AFTER_FREEZE = datetime(2026, 10, 27, 12, 5, 0, 0, tzinfo=timezone.utc)
# The confirmed incident event itself (sub-second precision, real wall clock).
INCIDENT_TS = datetime(2026, 10, 27, 10, 43, 57, 672455, tzinfo=timezone.utc)

SYMBOL = "TSLAUSDT"
REF = Decimal("342.10")
STOP = Decimal("342.78")
TARGET = Decimal("338.0")


@pytest.fixture(autouse=True)
def _intake_flag(monkeypatch):
    monkeypatch.setenv(ACTIVATION_FLAG, "1")
    yield


# ── fakes ─────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.rowcount = -1

    def execute(self, sql, params=None):
        params = params or ()
        self.conn.statements.append((sql, params))
        head = " ".join(sql.lstrip().upper().split())
        if self.conn.fail_on is not None and self.conn.fail_on in head:
            raise RuntimeError(f"simulated db failure at {self.conn.fail_on}")
        if "SELECT STATUS,INTAKE_FREEZE_TS" in head:
            self._rows = [self.conn.activation_row] if self.conn.activation_row else []
        elif head.startswith("INSERT"):
            if self.conn.conflict_skip:
                self.rowcount = 0
            else:
                self.rowcount = 1
                self.conn.persisted.append((sql, params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, activation_row=("RUNNING", INTAKE_FREEZE),
                 conflict_skip=False, fail_on=None):
        self.activation_row = activation_row
        self.conflict_skip = conflict_skip
        self.fail_on = fail_on
        self.statements = []
        self.persisted = []
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


def base_kwargs(**overrides):
    kwargs = dict(
        scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT",
        symbol=SYMBOL,
        signal_time=AFTER_FREEZE,
        reference_price=REF,
        invalidation_price=STOP,
        target_1=TARGET,
        capture_route=ROUTE_REGIME_FILTER_REJECTED,
        blocked_stage="regime_filter",
        blocked_reason="SHORT in TREND_UP",
        market_regime="TREND_UP",
    )
    kwargs.update(overrides)
    return kwargs


# ── identity ──────────────────────────────────────────────────


def test_event_key_matches_the_frozen_identity_formula():
    """sha256(symbol|SHORT|<utc iso signal_time>|<ref>|<stop>|<target>) on
    the FROZEN price representation (the scanner's float repr:
    REF=Decimal('342.10') hashes as ``342.1``)."""
    key = compute_event_key(SYMBOL, AFTER_FREEZE, REF, STOP, TARGET)
    expected = hashlib.sha256(
        f"{SYMBOL}|SHORT|{AFTER_FREEZE.isoformat()}|342.1|342.78|338.0"
        .encode()
    ).hexdigest()
    assert key == expected


def test_event_key_is_byte_identical_to_phase1_and_lineage():
    """One event joins intake -> Phase 1 -> lineage -> outcome on ONE key.

    The Phase 1 writer (``me_short_a_fill_feasibility_v1.capture``) hashes
    ``f"{symbol}|SHORT|{event_time.isoformat()}|{ref}|{stop}|{target}"``
    over ``Decimal(str(<float>))`` prices — the scanner's in-memory float
    representation, verified against production rows; the lineage module
    hashes the same payload from canonical inputs. The intake writer must
    produce that exact key for EVERY spelling of the same price.
    """
    key = compute_event_key(SYMBOL, AFTER_FREEZE, REF, STOP, TARGET)
    # Phase 1's real code path (scanner float prices):
    ref, stop, target = Decimal(str(342.1)), Decimal(str(342.78)), Decimal(str(338.0))
    phase1_formula = hashlib.sha256(
        f"{SYMBOL}|SHORT|{AFTER_FREEZE.isoformat()}|{ref}|{stop}|{target}"
        .encode()
    ).hexdigest()
    assert key == phase1_formula
    # and the frozen lineage formula on canonical inputs:
    assert key == lineage_event_key(
        SYMBOL, "SHORT", AFTER_FREEZE,
        Decimal("342.1"), Decimal("342.78"), Decimal("338.0"))


def test_event_key_canonicalizes_price_representation():
    """Every spelling of ONE number shares ONE frozen identity key.

    ``Decimal("342.10")`` == ``Decimal("342.1")`` == ``"342.10"`` == ``342.1``
    for the identity, and ``Decimal("338.0")`` == ``Decimal("338")`` ==
    ``338.0``. This is what keeps the writer's key equal to the key the
    runtime recomputes from the stored ``numeric`` column after a PostgreSQL
    round-trip (regression: the round-trip used to desynchronise on
    ``Decimal("342.10")``)."""
    base = compute_event_key(SYMBOL, AFTER_FREEZE, Decimal("342.1"),
                             Decimal("342.78"), Decimal("338.0"))
    assert compute_event_key(SYMBOL, AFTER_FREEZE, Decimal("342.10"),
                             Decimal("342.780"), Decimal("338")) == base
    assert compute_event_key(SYMBOL, AFTER_FREEZE, "342.10", 342.78, 338.0) == base
    assert compute_event_key(SYMBOL, AFTER_FREEZE, 342.1, "342.78", "338.0") == base


def test_identity_columns_are_stored_in_the_frozen_representation():
    """The row's identity columns hold exactly what the key was built from
    (Phase 1 stores its identity inputs the same way)."""
    conn = FakeConnection()
    capture(conn, **base_kwargs(reference_price=Decimal("342.10"),
                                invalidation_price=Decimal("342.780"),
                                target_1=Decimal("338")))
    _, params = conn.inserts[0]
    spelled = [str(p) for p in params]
    assert "342.1" in spelled and "342.78" in spelled and "338.0" in spelled
    assert "342.10" not in spelled and "342.780" not in spelled and "338" not in spelled


def test_one_event_across_price_spellings_maps_to_one_event_key():
    """``ON CONFLICT (event_key)`` deduplicates one event only if the key is
    stable across spellings — otherwise one event would append two rows."""
    conn = FakeConnection()
    capture(conn, **base_kwargs(reference_price=Decimal("342.10"),
                                target_1=Decimal("338")))
    capture(conn, **base_kwargs(reference_price=Decimal("342.1"),
                                target_1=Decimal("338.0")))
    keys = [p[0] for _, p in conn.inserts]
    assert len(keys) == 2 and keys[0] == keys[1]


def test_event_key_survives_microsecond_precision():
    """Real detections are wall-clock with microseconds (the incident at
    10:43:57.672455). The identity must preserve them exactly — a lost
    microsecond would desynchronise the key from Phase 1."""
    key = compute_event_key(SYMBOL, INCIDENT_TS, REF, STOP, TARGET)
    expected = hashlib.sha256(
        f"{SYMBOL}|SHORT|{INCIDENT_TS.isoformat()}|342.1|342.78|338.0"
        .encode()
    ).hexdigest()
    assert key == expected
    assert INCIDENT_TS.microsecond == 672455
    # And the key differs from the whole-second truncation:
    truncated = INCIDENT_TS.replace(microsecond=0)
    assert key != compute_event_key(SYMBOL, truncated, REF, STOP, TARGET)


def test_event_key_is_deterministic_under_repeat():
    assert (compute_event_key(SYMBOL, AFTER_FREEZE, REF, STOP, TARGET)
            == compute_event_key(SYMBOL, AFTER_FREEZE, REF, STOP, TARGET))


# ── gates ─────────────────────────────────────────────────────


def test_capture_requires_the_feature_flag(monkeypatch):
    monkeypatch.delenv(ACTIVATION_FLAG, raising=False)
    conn = FakeConnection()
    assert capture(conn, **base_kwargs()) is False
    assert conn.inserts == []


def test_capture_rejects_other_scanners_and_directions():
    conn = FakeConnection()
    assert capture(conn, **base_kwargs(scanner_name="TREND_PULLBACK")) is False
    assert capture(conn, **base_kwargs(direction="LONG")) is False
    assert conn.inserts == []


def test_capture_rejects_unknown_route():
    conn = FakeConnection()
    assert capture(conn, **base_kwargs(capture_route="PAPER_ORDER")) is False
    assert conn.inserts == []


def test_capture_skips_when_activation_is_missing():
    conn = FakeConnection(activation_row=None)
    assert capture(conn, **base_kwargs()) is False
    assert conn.inserts == []


def test_capture_skips_when_not_running():
    conn = FakeConnection(activation_row=("READY_TO_START", None))
    assert capture(conn, **base_kwargs()) is False
    conn = FakeConnection(activation_row=("CANCELLED", INTAKE_FREEZE))
    assert capture(conn, **base_kwargs()) is False
    assert conn.inserts == []


def test_no_backfill_pre_freeze_event_including_the_incident():
    """TSLAUSDT 2026-10-27 10:43:57.672455+00 must NEVER enter the intake:
    it predates the activation boundary of the fixed mechanism."""
    conn = FakeConnection()
    assert capture(
        conn, **base_kwargs(signal_time=INCIDENT_TS)) is False
    assert capture(
        conn,
        **base_kwargs(signal_time=INTAKE_FREEZE),
    ) is False  # strict > boundary
    assert conn.inserts == []


def test_post_freeze_event_is_captured():
    conn = FakeConnection()
    assert capture(conn, **base_kwargs()) is True
    assert len(conn.inserts) == 1
    assert conn.commits == 1


def test_invalid_frozen_a_geometry_is_refused():
    conn = FakeConnection()
    assert capture(conn, **base_kwargs(invalidation_price=REF)) is False
    assert capture(conn, **base_kwargs(target_1=REF)) is False
    assert capture(conn, **base_kwargs(target_1=Decimal("343.0"))) is False
    assert capture(conn, **base_kwargs(reference_price=Decimal("0"))) is False
    assert conn.inserts == []


def test_unparseable_geometry_is_refused():
    conn = FakeConnection()
    assert capture(conn, **base_kwargs(reference_price="not-a-price")) is False
    assert conn.inserts == []


# ── atomicity / idempotency ───────────────────────────────────


def test_write_is_one_atomic_idempotent_insert():
    conn = FakeConnection()
    assert capture(conn, **base_kwargs()) is True
    assert len(conn.inserts) == 1
    sql, params = conn.inserts[0]
    # One atomic INSERT ... SELECT with the activation guard, replay-safe.
    assert sql.lstrip().upper().startswith("INSERT")
    assert "ON CONFLICT (event_key) DO NOTHING" in sql
    assert "WHERE EXISTS (" in sql
    assert "'RUNNING'" in sql  # guard re-checks activation atomically
    assert "UPDATE" not in sql.upper().replace("ON CONFLICT", "")
    # Bind-parameter contract: every %s has a matching parameter.
    assert sql.count("%s") == len(params)


def test_idempotent_replay_claims_no_second_capture():
    conn = FakeConnection(conflict_skip=True)
    assert capture(conn, **base_kwargs()) is False  # rowcount 0 -> no claim
    assert conn.commits == 1  # the write was still attempted atomically


def test_repeat_capture_attempts_use_the_same_event_key():
    conn = FakeConnection()
    capture(conn, **base_kwargs())
    capture(conn, **base_kwargs())
    keys = [p[0] for _, p in conn.inserts]
    assert len(keys) == 2 and keys[0] == keys[1] == compute_event_key(
        SYMBOL, AFTER_FREEZE, REF, STOP, TARGET)


def test_insert_targets_only_the_intake_table():
    conn = FakeConnection()
    capture(conn, **base_kwargs())
    sql, _ = conn.inserts[0]
    assert "INSERT INTO research.me_short_a_source_intake_v1" in sql
    for forbidden in ("fill_feasibility", "source_signal", "event_lineage",
                      "outcome_observation", "prospective_observation",
                      "scanner_setup", "scanner_setup ", "paper", "live"):
        assert forbidden not in sql.lower()


def test_where_exists_guard_carries_the_experiment_identity():
    conn = FakeConnection()
    capture(conn, **base_kwargs())
    sql, params = conn.inserts[0]
    assert params[-1] == EXPERIMENT
    assert "research.me_short_a_source_intake_activation_v1" in sql


def test_insert_sql_bind_contract():
    """33 columns: 32 bind params (captured_at is NOW()) + 1 guard param."""
    assert INSERT_SQL.count("%s") == 33
    assert "ON CONFLICT (event_key) DO NOTHING" in INSERT_SQL
    assert "WHERE EXISTS (" in INSERT_SQL


# ── payload honesty ───────────────────────────────────────────


def test_route_and_block_metadata_are_persisted():
    conn = FakeConnection()
    capture(conn, **base_kwargs(setup_id="setup-42"))
    _, params = conn.inserts[0]
    # captured_at is NOW() inside SQL, so assert via the raw bind parameters.
    assert ROUTE_REGIME_FILTER_REJECTED in params
    assert "regime_filter" in params
    assert "SHORT in TREND_UP" in params
    assert "TREND_UP" in params
    assert "setup-42" in params


def test_microsecond_signal_time_is_passed_through_unmodified():
    conn = FakeConnection()
    capture(conn, **base_kwargs(signal_time=INCIDENT_TS.replace(
        hour=12, minute=1)))
    _, params = conn.inserts[0]
    assert INCIDENT_TS.replace(hour=12, minute=1) in params


# ── epoch-millisecond timestamps (review P1) ──────────────────


def test_datetime_parses_epoch_milliseconds_to_utc():
    """Scanner candle fields are epoch MILLISECONDS (e.g.
    ``signal_candle_open_time = 1793102400000``). Reading them as ISO
    strings used to fail the whole capture instead of converting them."""
    expected = datetime(2026, 10, 27, 12, 0, 0, 0, tzinfo=timezone.utc)
    assert mod._datetime(1793102400000) == expected
    assert mod._datetime(1793102400000.0) == expected
    # epoch seconds (small magnitude) stay seconds:
    assert mod._datetime(1793102400) == expected
    # ISO strings and datetimes are unchanged:
    assert mod._datetime("2026-10-27T12:00:00Z") == expected
    assert mod._datetime(expected) == expected
    with pytest.raises(ValueError):
        mod._datetime(True)  # bool is not a timestamp


def test_signal_candle_open_time_epoch_ms_is_stored_as_utc_datetime():
    """An epoch-ms ``signal_candle_open_time`` and epoch-ms window candle
    times reach the INSERT as UTC datetimes, never as raw integers."""
    expected = datetime(2026, 10, 27, 12, 0, 0, 0, tzinfo=timezone.utc)
    window = [
        {"open_time": 1793102340000,   # 2026-10-27 11:59:00Z
         "close_time": 1793102400000,  # 2026-10-27 12:00:00Z
         "high": "342.10", "close": "341.0"},
    ]
    conn = FakeConnection()
    capture(conn, **base_kwargs(
        signal_time=AFTER_FREEZE,
        signal_candle_open_time=1793102400000,
        recent_high_window=window))
    _, params = conn.inserts[0]
    assert expected in params                       # signal_candle_open_time
    assert datetime(2026, 10, 27, 11, 59, 0, 0,
                    tzinfo=timezone.utc) in params  # window_start
    assert 1793102400000 not in params              # never a raw integer
    assert 1793102340000 not in params


def test_no_lookahead_window_candles_only():
    """Candles closing after the signal can never enter the window."""
    window = [
        {"open_time": AFTER_FREEZE - timedelta(minutes=5),
         "close_time": AFTER_FREEZE,
         "high": "342.10", "close": "341.0"},
        {"open_time": AFTER_FREEZE,
         "close_time": AFTER_FREEZE + timedelta(minutes=5),
         "high": "999.0", "close": "999.0"},  # lookahead -> must be dropped
    ]
    conn = FakeConnection()
    capture(conn, **base_kwargs(recent_high_window=window))
    _, params = conn.inserts[0]
    dumped = [p for p in params if isinstance(p, str) and p.startswith("[")]
    assert dumped, "window json missing"
    window_json = json.loads(dumped[0])
    assert len(window_json) == 1
    assert window_json[0]["high"] == "342.10"
    flags = [json.loads(p) for p in params
             if isinstance(p, str) and p.startswith("[") and p != dumped[0]]
    flat = [name for group in flags for name in group]
    assert "recent_high_window_lookahead_rejected" in flat
    assert "recent_high_window_closed_before_signal" in flat


def test_capture_never_returns_a_candidate():
    """Research-only: the writer exposes no path back to the trading flow."""
    conn = FakeConnection()
    assert capture(conn, **base_kwargs()) in (True, False)


# ── fail-open ─────────────────────────────────────────────────


def test_db_failure_on_write_fails_open_and_rolls_back():
    conn = FakeConnection(fail_on="INSERT INTO")
    assert capture(conn, **base_kwargs()) is False
    assert conn.rollbacks == 1  # no half-written research state


def test_db_failure_on_activation_read_fails_open():
    conn = FakeConnection(fail_on="SELECT STATUS,INTAKE_FREEZE_TS")
    assert capture(conn, **base_kwargs()) is False
    assert conn.inserts == []
    assert conn.rollbacks == 1  # the cached writer connection is un-aborted


def test_activation_read_failure_recovers_on_the_cached_connection():
    """A capture attempted BEFORE migration 080 is installed must not poison
    the CACHED writer connection: it fails open, rolls back, and the very
    next capture on the SAME connection succeeds — no scanner restart."""
    conn = FakeConnection(fail_on="SELECT STATUS,INTAKE_FREEZE_TS")
    assert capture_with_status(conn, **base_kwargs()) == "ERROR"
    assert conn.rollbacks == 1 and conn.inserts == []
    conn.fail_on = None                       # migration 080 now installed
    assert capture_with_status(conn, **base_kwargs()) == "CAPTURED"
    assert conn.commits == 1 and len(conn.persisted) == 1


def test_write_failure_recovers_on_the_cached_connection():
    conn = FakeConnection(fail_on="INSERT INTO")
    assert capture_with_status(conn, **base_kwargs()) == "ERROR"
    assert conn.rollbacks == 1
    conn.fail_on = None
    assert capture_with_status(conn, **base_kwargs()) == "CAPTURED"


def test_capture_fails_open_on_none_connection():
    assert capture(None, **base_kwargs()) is False


# ── activation snapshot / module surface ──────────────────────


def test_activation_snapshot_reads_status_and_freeze():
    conn = FakeConnection()
    assert activation_snapshot(conn) == ("RUNNING", INTAKE_FREEZE)
    assert activation_snapshot(FakeConnection(activation_row=None)) is None


def test_capture_routes_cover_every_trading_decision():
    """A trading filter decision can never prevent research capture."""
    assert CAPTURE_ROUTES == (
        "TRADEABLE_FLOW",
        "SHADOW_CONTROL",
        "OBSERVE_ONLY",
        "DIRECTION_GATE_BLOCKED",
        "EXPECTANCY_FILTER_REJECTED",
        "REGIME_FILTER_REJECTED",
    )


def test_module_has_no_order_paper_or_live_surface():
    source = mod.__file__
    text = open(source, encoding="utf-8").read().lower()
    for forbidden in ("place_order", "create_order", "submit_order",
                      "paper_runner", "live_runner", "position"):
        assert forbidden not in text
