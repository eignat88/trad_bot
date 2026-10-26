"""Issue #208 — ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1.

Offline tests (no exchange, no PostgreSQL): frozen identity parity with the
Phase 1 ``event_key``, fail-closed gates, no-lookahead, source coverage,
controlled 240m STOP_FIRST finalization, lineage quarantine and the
append-only / no-economics-on-incomplete-source invariants.

The fake connection replays the exact SQL contract used by the capture
entry points, so activation gating, the strict post-freeze boundary and
idempotency are asserted on the real statements.
"""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import app.research.me_short_a_forward_outcome_lineage_v1 as mod
from app.research.me_short_a_forward_outcome_lineage_v1 import (
    DIRECTION,
    EXIT_FILL_SEMANTICS,
    EXPERIMENT,
    LINEAGE_STATUSES,
    MAX_EXPECTED_CANDLES,
    MAX_HOLD_MINUTES,
    NORMAL_ROUND_TRIP,
    ELEVATED_ROUND_TRIP,
    OUTCOME_STATUS_MATCHED_FINALIZED,
    OUTCOME_STATUS_SOURCE_INCOMPLETE,
    OUTCOME_STATUS_UNMATURED,
    PARTIAL_FILL_STATUS,
    PATH_STOP_HIT,
    PATH_TARGET_HIT,
    PATH_TIMEOUT_240M,
    PROTOCOL_HASH,
    PROTOCOL_PAYLOAD,
    PROTOCOL_VERSION,
    TOUCH_BASIS,
    canonical_decimal,
    capture_post_signal_candle,
    capture_source_signal,
    compute_outcome,
    event_key,
    expected_candle_count,
    expected_window_opens,
    finalize_outcome,
    identity_payload,
    record_event_lineage,
    verify_identity,
    window_bounds,
)

FREEZE = datetime(2026, 10, 26, 12, 53, 57, 708435, tzinfo=timezone.utc)
SIGNAL = datetime(2026, 10, 26, 13, 5, tzinfo=timezone.utc)          # grid-aligned
OFF_GRID_SIGNAL = datetime(2026, 10, 26, 13, 7, 30, tzinfo=timezone.utc)
AFTER_FREEZE = SIGNAL

EXPECTED = expected_candle_count(SIGNAL)          # 47 for a grid-aligned signal
EXPECTED_OFF_GRID = expected_candle_count(OFF_GRID_SIGNAL)  # 48

SYMBOL = "BTCUSDT"
REF = Decimal("100")
STOP = Decimal("100.2")     # frozen A: invalidation = recent_high * 1.002
TARGET = Decimal("98")      # frozen A: target_1 < reference
STRUCTURAL_R = STOP - REF   # 0.2


# ── fakes ──────────────────────────────────────────────────────────────


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
        elif head.startswith("INSERT"):
            self.rowcount = 0 if self.conn.conflict_skip else 1
        else:  # pragma: no cover - defensive
            self._rows = []

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, activation_row=("RUNNING", FREEZE), conflict_skip=False):
        self.activation_row = activation_row
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


@pytest.fixture(autouse=True)
def _capture_flag(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_LINEAGE_CAPTURE_ENABLED", "1")
    yield


# ── helpers ────────────────────────────────────────────────────────────


def make_candles(signal_time, *, count=None, high=None, low=None,
                 close=None, skip=(), open_flags=()):
    """Closed 5m candles covering the frozen 240m window.

    ``high``/``low``/``close`` may be callables taking the 0-based slot index.
    ``skip`` lists slot indexes that are absent; ``open_flags`` lists slots
    whose candle is reported as still open.
    """
    opens = expected_window_opens(signal_time)
    if count is not None:
        opens = opens[:count]
    out = []
    for idx, open_time in enumerate(opens):
        if idx in skip:
            continue
        h = high(idx) if callable(high) else (high if high is not None else 99)
        lo = low(idx) if callable(low) else (low if low is not None else 98)
        c = close(idx) if callable(close) else (close if close is not None else 98.5)
        out.append({
            "open_time": open_time.isoformat(),
            "high": h, "low": lo, "open": c, "close": c,
            "is_closed": idx not in open_flags,
            "source": "test",
        })
    return out


def make_recent_high_window(signal_time, *, count=5, high=None):
    """The ``count`` closed 5m candles whose highs formed ``reference_price``.

    The last of them IS the trigger candle and closes exactly at
    ``signal_time`` — the frozen scanner uses ``candles_5m[-5:]`` including the
    trigger candle, so the window must never close after the signal time.
    """
    end = signal_time
    out = []
    for idx in range(count):
        open_time = end - timedelta(minutes=5 * (count - idx))
        close_time = open_time + timedelta(minutes=5)
        h = high(idx) if callable(high) else (high if high is not None else 100)
        out.append({
            "open_time": open_time.isoformat(),
            "close_time": close_time.isoformat(),
            "high": h, "low": h - 1, "open": h - 1, "close": h - 1,
            "source": "test",
        })
    return out


def outcome(candles, *, asof_time=None, signal_time=SIGNAL, **kwargs):
    return compute_outcome(
        candles,
        symbol=SYMBOL,
        signal_time=signal_time,
        reference_price=REF,
        stop_price=STOP,
        target_price=TARGET,
        asof_time=asof_time or (signal_time + timedelta(minutes=MAX_HOLD_MINUTES)),
        **kwargs)


# ── deterministic event identity ───────────────────────────────────────


def test_event_key_matches_phase1_formula_byte_for_byte():
    """The identity MUST stay byte-identical to migration 077 / PR #207."""
    expected = hashlib.sha256(
        f"{SYMBOL}|SHORT|{SIGNAL.isoformat()}|{REF}|{STOP}|{TARGET}".encode()
    ).hexdigest()
    assert event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET) == expected


def test_event_key_matches_phase1_capture_module_formula():
    """Cross-module parity: both collectors must derive the same key."""
    from app.research.me_short_a_fill_feasibility_v1 import capture as phase1_capture
    from unittest.mock import MagicMock

    conn = FakeConnection()
    client = MagicMock()
    client._public_get = MagicMock(side_effect=RuntimeError("no network"))
    os.environ["ME_SHORT_A_FILL_CAPTURE_ENABLED"] = "1"
    try:
        phase1_capture(
            conn, client, scanner_name="MOMENTUM_EXHAUSTION", direction="SHORT",
            symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
            invalidation_price=STOP, target_1=TARGET)
    finally:
        os.environ.pop("ME_SHORT_A_FILL_CAPTURE_ENABLED", None)
    phase1_params = conn.inserts[0][1]
    phase1_key = phase1_params[0]
    assert phase1_key == event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)


def test_event_key_is_deterministic_and_component_sensitive():
    base = event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)
    assert event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET) == base
    assert event_key("ETHUSDT", "SHORT", SIGNAL, REF, STOP, TARGET) != base
    assert event_key(SYMBOL, "SHORT", SIGNAL + timedelta(seconds=1), REF, STOP, TARGET) != base
    assert event_key(SYMBOL, "SHORT", SIGNAL, Decimal("100.01"), STOP, TARGET) != base
    assert event_key(SYMBOL, "SHORT", SIGNAL, REF, Decimal("100.3"), TARGET) != base
    assert event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, Decimal("97")) != base


def test_identity_payload_is_stable_across_datetime_representations():
    naive = SIGNAL.replace(tzinfo=None)
    assert identity_payload(SYMBOL, "SHORT", naive, REF, STOP, TARGET) == \
        identity_payload(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)


def test_identity_rejects_non_short_direction():
    with pytest.raises(ValueError):
        identity_payload(SYMBOL, "LONG", SIGNAL, REF, STOP, TARGET)


def test_protocol_hash_matches_registry_json():
    import json as _json
    from pathlib import Path

    registry = _json.loads(
        (Path(__file__).resolve().parent.parent / "app" / "research" /
         "me_short_a_forward_outcome_lineage_registry_v1.json").read_text("utf-8"))
    assert registry["protocol_hash"] == PROTOCOL_HASH
    assert registry["protocol_version"] == PROTOCOL_VERSION
    assert registry["direction"] == "SHORT"
    assert registry["scanner_name"] == "MOMENTUM_EXHAUSTION"
    assert registry["frozen_geometry"]["intrabar_policy"] == "STOP_FIRST"
    assert registry["frozen_geometry"]["max_hold_minutes"] == 240
    assert registry["backfill"].startswith("FORBIDDEN")
    assert registry["analysis_eligible_status"] == "MATCHED_FINALIZED"
    assert registry["lineage_output_statuses"] == list(LINEAGE_STATUSES)


def test_protocol_hash_is_sha256_of_canonical_payload():
    assert PROTOCOL_HASH == hashlib.sha256(PROTOCOL_PAYLOAD.encode()).hexdigest()


# ── identity verification / quarantine ─────────────────────────────────


def test_verify_identity_links_when_everything_matches():
    key = event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)
    result = verify_identity(
        event_key_stored=key, symbol=SYMBOL, direction="SHORT",
        signal_time=SIGNAL, reference_price=REF, stop_price=STOP, target_price=TARGET)
    assert result["event_key_integrity"] is True
    assert result["link_status"] == "LINKED"
    assert result["quarantine_reason"] is None
    assert result["linkage_checks"]["event_key_integrity"] is True


@pytest.mark.parametrize("field,value", [
    ("direction", "LONG"),
    ("protocol_version", "ME_SHORT_A_SOMETHING_ELSE_V9"),
    ("protocol_hash", "0" * 64),
])
def test_verify_identity_quarantines_protocol_mismatch(field, value):
    key = event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)
    kwargs = dict(event_key_stored=key, symbol=SYMBOL, direction="SHORT",
                  signal_time=SIGNAL, reference_price=REF, stop_price=STOP,
                  target_price=TARGET)
    if field == "direction":
        kwargs["direction"] = value
    else:
        kwargs[field] = value
    result = verify_identity(**kwargs)
    assert result["link_status"] == "QUARANTINED"
    assert field.split("_")[0] in result["quarantine_reason"]


def test_verify_identity_quarantines_key_mismatch():
    result = verify_identity(
        event_key_stored="deadbeef", symbol=SYMBOL, direction="SHORT",
        signal_time=SIGNAL, reference_price=REF, stop_price=STOP, target_price=TARGET)
    assert result["event_key_integrity"] is False
    assert result["link_status"] == "QUARANTINED"
    assert "event_key_integrity" in result["quarantine_reason"]


def test_verify_identity_quarantines_broken_geometry():
    key = event_key(SYMBOL, "SHORT", SIGNAL, REF, Decimal("99"), TARGET)
    result = verify_identity(
        event_key_stored=key, symbol=SYMBOL, direction="SHORT",
        signal_time=SIGNAL, reference_price=REF, stop_price=Decimal("99"),
        target_price=TARGET)
    assert result["link_status"] == "QUARANTINED"
    assert "frozen_geometry" in result["quarantine_reason"]


def test_verify_identity_flags_each_component_separately():
    key = event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)
    result = verify_identity(
        event_key_stored=key, symbol=SYMBOL, direction="SHORT",
        signal_time=SIGNAL, reference_price=REF, stop_price=STOP, target_price=TARGET)
    for name in ("direction_is_short", "scanner_protocol_version_matches",
                 "protocol_hash_matches", "frozen_geometry_valid",
                 "event_key_recomputes", "event_key_integrity"):
        assert result["linkage_checks"][name] is True, name


# ── window geometry / no lookahead ─────────────────────────────────────


def test_window_matches_frozen_evaluator_inequalities():
    """The horizon window is ``signal_time < open < signal_time + 240m``."""
    first, last, horizon = window_bounds(SIGNAL)
    assert horizon == SIGNAL + timedelta(minutes=MAX_HOLD_MINUTES)
    assert first > SIGNAL and first == SIGNAL + timedelta(minutes=5)
    assert last < horizon and last == SIGNAL + timedelta(minutes=235)
    opens = expected_window_opens(SIGNAL)
    assert all(SIGNAL < ts < horizon for ts in opens)
    assert all((b - a).total_seconds() == 300 for a, b in zip(opens, opens[1:]))
    # grid-aligned signal: the +0m candle is the trigger candle and is excluded
    assert len(opens) == 47 == EXPECTED


def test_window_count_is_derived_never_assumed():
    assert expected_candle_count(SIGNAL) == 47
    assert expected_candle_count(OFF_GRID_SIGNAL) == 48
    assert MAX_EXPECTED_CANDLES == 48
    opens = expected_window_opens(OFF_GRID_SIGNAL)
    horizon = OFF_GRID_SIGNAL + timedelta(minutes=MAX_HOLD_MINUTES)
    assert all(OFF_GRID_SIGNAL < ts < horizon for ts in opens)
    assert len(opens) == EXPECTED_OFF_GRID


def test_canonical_decimal_rejects_unparseable():
    with pytest.raises(ValueError):
        canonical_decimal("not-a-number")
    with pytest.raises(ValueError):
        canonical_decimal(None)


# ── outcome math: frozen 240m STOP_FIRST ───────────────────────────────


def test_stop_hit_is_exactly_minus_one_r():
    candles = make_candles(SIGNAL, high=lambda i: 100.5 if i == 3 else 99,
                           low=lambda i: 98.5, close=lambda i: 98.5)
    res = outcome(candles)
    assert res["outcome_status"] == OUTCOME_STATUS_MATCHED_FINALIZED
    assert res["path_class"] == PATH_STOP_HIT
    assert res["sl_before_tp"] is True
    assert res["tp_before_sl"] is False
    assert res["ambiguous_intrabar"] is False
    assert res["exit_price"] == STOP
    assert res["gross_r"] == Decimal("-1")
    assert res["time_to_sl_minutes"] == 20.0
    assert res["time_to_tp_minutes"] is None
    assert res["is_final"] is True


def test_target_hit_r_is_reward_over_structural_r():
    candles = make_candles(SIGNAL, high=lambda i: 99, low=lambda i: 97 if i == 7 else 98.5)
    res = outcome(candles)
    assert res["path_class"] == PATH_TARGET_HIT
    assert res["tp_before_sl"] is True
    assert res["sl_before_tp"] is False
    assert res["exit_price"] == TARGET
    expected_r = (REF - TARGET) / STRUCTURAL_R
    assert res["gross_r"] == expected_r == Decimal("10")
    assert res["time_to_tp_minutes"] == 40.0


def test_first_touch_decides_not_the_worst_candle():
    """A later stop touch must not overwrite an earlier target hit."""
    candles = make_candles(
        SIGNAL,
        high=lambda i: 101 if i == 20 else 99,
        low=lambda i: 97 if i == 2 else 98.5)
    res = outcome(candles)
    assert res["path_class"] == PATH_TARGET_HIT
    assert res["tp_before_sl"] is True
    assert res["sl_before_tp"] is False
    assert res["time_to_tp_minutes"] == 15.0
    assert res["time_to_sl_minutes"] is None


def test_ambiguous_intrabar_resolves_to_stop_first_at_minus_one_r():
    candles = make_candles(SIGNAL, high=lambda i: 101 if i == 5 else 99,
                           low=lambda i: 97 if i == 5 else 98.5)
    res = outcome(candles)
    assert res["ambiguous_intrabar"] is True
    assert res["path_class"] == PATH_STOP_HIT
    assert res["sl_before_tp"] is True
    assert res["tp_before_sl"] is False
    assert res["gross_r"] == Decimal("-1")
    assert res["exit_basis"] == "INTRABAR_STOP_FIRST"
    assert res["time_to_sl_minutes"] == res["time_to_tp_minutes"] == 30.0


def test_timeout_exits_at_last_in_window_close():
    candles = make_candles(SIGNAL, high=lambda i: 99, low=lambda i: 98.4,
                           close=lambda i: 98.5)
    res = outcome(candles)
    assert res["path_class"] == PATH_TIMEOUT_240M
    assert res["sl_before_tp"] is False
    assert res["tp_before_sl"] is False
    assert res["ambiguous_intrabar"] is False
    assert res["exit_price"] == Decimal("98.5")
    assert res["exit_basis"] == "LAST_IN_WINDOW_5M_CLOSE"
    assert res["gross_r"] == (REF - Decimal("98.5")) / STRUCTURAL_R
    # frozen direction-agnostic convention (favourable SHORT move = negative)
    assert res["return_at_240m"] == (Decimal("98.5") - REF) / REF * 100


def test_net_r_subtracts_frozen_costs_and_never_rebases_risk():
    candles = make_candles(SIGNAL, high=lambda i: 99, low=lambda i: 98.4,
                           close=lambda i: 98.5)
    res = outcome(candles)
    assert res["net_r_normal"] == res["gross_r"] - NORMAL_ROUND_TRIP * REF / STRUCTURAL_R
    assert res["net_r_elevated"] == res["gross_r"] - ELEVATED_ROUND_TRIP * REF / STRUCTURAL_R
    assert res["structural_r"] == STRUCTURAL_R


def test_mae_mfe_follow_frozen_short_definitions():
    candles = make_candles(SIGNAL, high=lambda i: 100.5 if i == 1 else 99.2,
                           low=lambda i: 96.5 if i == 9 else 98.4)
    res = outcome(candles)
    assert res["mae_240m"] == (Decimal("100.5") - REF) / REF * 100
    assert res["mfe_240m"] == (REF - Decimal("96.5")) / REF * 100


def test_limit_at_reference_touch_is_candle_touch_not_fill():
    touched = make_candles(SIGNAL, high=lambda i: 100.0 if i == 4 else 99)
    untouched = make_candles(SIGNAL, high=lambda i: 99.9)
    assert outcome(touched)["limit_at_reference_touch"] is True
    assert outcome(untouched)["limit_at_reference_touch"] is False
    for res in (outcome(touched), outcome(untouched)):
        assert res["touch_basis"] == TOUCH_BASIS
        assert res["touch_basis"] != "ORDER_BOOK_FILL"


def test_exit_fill_and_partial_fill_are_always_unknown():
    candles = make_candles(SIGNAL, high=lambda i: 100.5 if i == 1 else 99)
    res = outcome(candles)
    assert res["exit_fill_semantics"] == EXIT_FILL_SEMANTICS == "MODELED_UNKNOWN"
    assert res["partial_fill_status"] == PARTIAL_FILL_STATUS == \
        "UNKNOWN_NOT_INDEPENDENTLY_CAPTURED"


# ── source coverage / maturity / incomplete source ─────────────────────


def test_unmatured_horizon_reports_outcome_unmatured_without_economics():
    candles = make_candles(SIGNAL, high=lambda i: 99, low=lambda i: 98.5)
    res = outcome(candles, asof_time=SIGNAL + timedelta(minutes=60))
    assert res["outcome_status"] == OUTCOME_STATUS_UNMATURED
    assert res["is_final"] is False
    assert res["path_class"] is None
    assert res["gross_r"] is None
    assert res["exit_price"] is None
    assert res["net_r_normal"] is None
    assert res["return_at_240m"] is None


def test_missing_candles_yield_source_incomplete_with_zero_economics():
    candles = make_candles(SIGNAL, high=lambda i: 100.5 if i == 2 else 99,
                           skip={0, 1, 2, 3, 4})
    res = outcome(candles)
    assert res["outcome_status"] == OUTCOME_STATUS_SOURCE_INCOMPLETE
    assert res["coverage_complete"] is False
    assert res["candles_present"] == EXPECTED - 5
    assert len(res["candles_missing"]) == 5
    assert "post_signal_candle_highs_240m:incomplete_window" in res["missing_fields"]
    for field in ("path_class", "exit_price", "gross_r", "net_r_normal",
                  "net_r_elevated", "return_at_240m", "time_to_sl_minutes",
                  "time_to_tp_minutes"):
        assert res[field] is None, field
    assert res["is_final"] is False
    # observables that are still honest stay available
    assert res["candles_expected"] == EXPECTED
    assert res["mae_240m"] is not None


def test_open_candle_blocks_coverage_and_finalization():
    candles = make_candles(SIGNAL, high=lambda i: 100.5 if i == 2 else 99,
                           open_flags={10})
    res = outcome(candles)
    assert res["outcome_status"] == OUTCOME_STATUS_SOURCE_INCOMPLETE
    assert res["coverage_complete"] is False
    assert "OPEN_CANDLE_IN_WINDOW" in res["source_quality_flags"]
    assert res["is_final"] is False
    assert res["gross_r"] is None


def test_candle_closing_after_asof_is_never_used():
    """A candle closing beyond the ASOF is lookahead: dropped and unfinalizable."""
    candles = make_candles(SIGNAL, high=lambda i: 100.5 if i == 2 else 99)
    late = dict(candles[3])
    late["close_time"] = (SIGNAL + timedelta(minutes=250)).isoformat()
    res = outcome(candles[:3] + [late] + candles[4:])
    assert "CANDLE_CLOSE_AFTER_ASOF" in res["source_quality_flags"]
    assert res["outcome_status"] == OUTCOME_STATUS_SOURCE_INCOMPLETE
    assert res["is_final"] is False
    assert res["gross_r"] is None
    assert res["path_class"] is None
    assert res["candles_present"] == EXPECTED - 1
    # the recorded ASOF invariant is exactly the DB CHECK
    assert res["max_candle_close_time"] <= res["asof_time"]


def test_asof_ok_enforces_the_db_check_formula():
    candles = make_candles(SIGNAL, high=lambda i: 99)
    res = outcome(candles)
    assert res["asof_ok"] is True
    assert res["max_candle_close_time"] is not None
    assert res["max_candle_close_time"] <= res["asof_time"]
    assert res["asof_ok"] == (
        res["max_candle_close_time"] is not None
        and res["max_candle_close_time"] <= res["asof_time"])


def test_coverage_complete_equals_candles_present_check():
    """Mirrors the DB CHECK (coverage_complete = (candles_present = expected))."""
    for skip in ((), (0,), (0, 1, 2)):
        res = outcome(make_candles(SIGNAL, high=lambda i: 99, skip=set(skip)))
        assert res["coverage_complete"] == (res["candles_present"] == EXPECTED)


def test_candles_outside_window_are_flagged_and_ignored():
    candles = make_candles(SIGNAL, high=lambda i: 99)
    outside = dict(candles[0])
    outside["open_time"] = (SIGNAL + timedelta(minutes=300)).isoformat()
    outside["high"] = 100.9
    res = outcome(candles + [outside])
    assert "CANDLES_OUTSIDE_WINDOW_IGNORED" in res["source_quality_flags"]
    assert res["candles_present"] == EXPECTED
    assert res["coverage_complete"] is True


def test_source_quality_flags_and_missing_fields_are_preserved():
    candles = make_candles(SIGNAL, high=lambda i: 99)
    res = outcome(candles, source_quality_flags=["BOOK_UNAVAILABLE"],
                  missing_fields=["signal_time_quote"])
    assert "BOOK_UNAVAILABLE" in res["source_quality_flags"]
    assert "signal_time_quote" in res["missing_fields"]


def test_invalid_geometry_is_rejected_outright():
    with pytest.raises(ValueError):
        compute_outcome([], symbol=SYMBOL, signal_time=SIGNAL,
                        reference_price=REF, stop_price=Decimal("99"),
                        target_price=TARGET,
                        asof_time=SIGNAL + timedelta(minutes=240))
    with pytest.raises(ValueError):
        compute_outcome([], symbol=SYMBOL, signal_time=SIGNAL,
                        reference_price=REF, stop_price=STOP,
                        target_price=Decimal("101"),
                        asof_time=SIGNAL + timedelta(minutes=240))


# ── fail-closed gates ──────────────────────────────────────────────────


def test_capture_disabled_by_default_feature_flag(monkeypatch):
    monkeypatch.delenv("ME_SHORT_A_LINEAGE_CAPTURE_ENABLED", raising=False)
    conn = FakeConnection()
    assert capture_source_signal(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET) is False
    assert conn.inserts == []


@pytest.mark.parametrize("activation_row", [
    None,
    ("READY_TO_START", None),
    ("COMPLETED", FREEZE),
    ("CANCELLED", FREEZE),
    ("RUNNING", None),
])
def test_capture_requires_running_activation_with_freeze(activation_row):
    conn = FakeConnection(activation_row=activation_row)
    assert capture_source_signal(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET) is False
    assert conn.inserts == []


def test_contamination_guard_rejects_pre_freeze_signal():
    conn = FakeConnection()
    pre_freeze = FREEZE - timedelta(seconds=1)
    assert capture_source_signal(
        conn, symbol=SYMBOL, signal_time=pre_freeze, reference_price=REF,
        stop_price=STOP, target_price=TARGET) is False
    assert finalize_outcome(
        conn, symbol=SYMBOL, signal_time=pre_freeze, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        asof_time=pre_freeze + timedelta(minutes=240), candles=[]) is False
    assert conn.inserts == []


def test_long_gate_is_structurally_excluded():
    """LONG can never enter: no entry point accepts a direction at all."""
    import inspect

    for fn in (capture_source_signal, capture_post_signal_candle,
               record_event_lineage, finalize_outcome):
        params = inspect.signature(fn).parameters
        assert "direction" not in params, fn.__name__
        assert "direction" not in params, "no caller-supplied direction is accepted"
    # the module-level gate rejects anything that is not SHORT
    conn = FakeConnection()
    assert mod._gated(conn, SYMBOL, "LONG", SIGNAL) is None
    assert mod._gated(conn, SYMBOL, "SHORT", SIGNAL) == FREEZE


def test_module_never_exposes_order_or_paper_surface():
    source = mod.__file__
    body = open(source, encoding="utf-8").read().lower()
    for forbidden in ("create_order", "place_order", "submit_order",
                      "paper_trade", "live_trade", "set_leverage"):
        assert forbidden not in body


# ── append-only writers: idempotency and SQL contract ──────────────────


def test_capture_source_signal_is_idempotent():
    conn = FakeConnection(conflict_skip=True)
    assert capture_source_signal(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        trigger_5m_close=Decimal("99.5"),
        entry_zone_low=Decimal("99.31"), entry_zone_high=Decimal("99.6"),
        recent_high_window=make_recent_high_window(SIGNAL)) is False
    assert len(conn.inserts) == 1
    assert "ON CONFLICT(EVENT_KEY) DO NOTHING" in conn.inserts[0][0].upper()
    assert conn.commits == 1


def test_capture_source_signal_inserts_with_same_event_key():
    conn = FakeConnection()
    assert capture_source_signal(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        trigger_5m_close=Decimal("99.5"),
        entry_zone_low=Decimal("99.31"), entry_zone_high=Decimal("99.6"),
        market_context={"rsi": 71.2}) is True
    params = conn.inserts[0][1]
    assert params[0] == event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)
    assert params[3] == SYMBOL and params[4] == "SHORT"
    assert params[8] == FREEZE  # freeze boundary pinned on the row


def test_missing_source_fields_are_recorded_explicitly_not_imputed():
    conn = FakeConnection()
    capture_source_signal(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET)
    params = conn.inserts[0][1]
    missing = json.loads(params[24])
    assert "trigger_5m_close" in missing
    assert "entry_zone_low" in missing
    assert "entry_zone_high" in missing
    assert "recent_high_window_candles" in missing


def test_source_signal_lookahead_window_is_rejected():
    """A reference window closing after signal_time can never be stored."""
    conn = FakeConnection()
    future_window = [
        {"open_time": (SIGNAL + timedelta(minutes=5)).isoformat(), "high": 101,
         "low": 100, "open": 100.5, "close": 100.5},
        {"open_time": (SIGNAL + timedelta(minutes=10)).isoformat(), "high": 101,
         "low": 100, "open": 100.5, "close": 100.5},
    ]
    assert capture_source_signal(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        recent_high_window=future_window) is False
    assert conn.inserts == []


def test_post_signal_candle_rejects_open_and_future_candles():
    conn = FakeConnection()
    open_candle = {"open_time": (SIGNAL + timedelta(minutes=5)).isoformat(),
                   "high": 99, "low": 98, "open": 98.5, "close": 98.5,
                   "is_closed": False}
    assert capture_post_signal_candle(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET, candle=open_candle) is False

    past_candle = {"open_time": (SIGNAL - timedelta(minutes=5)).isoformat(),
                   "high": 99, "low": 98, "open": 98.5, "close": 98.5}
    assert capture_post_signal_candle(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET, candle=past_candle) is False
    assert conn.inserts == []


def test_post_signal_candle_records_touch_flags_on_candle_basis():
    conn = FakeConnection()
    candle = {"open_time": (SIGNAL + timedelta(minutes=5)).isoformat(),
              "high": 100.3, "low": 97.9, "open": 99, "close": 98}
    assert capture_post_signal_candle(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET, candle=candle) is True
    params = conn.inserts[0][1]
    assert params[0] == event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)
    assert params[13] is True   # high_touches_stop
    assert params[14] is True   # low_touches_target
    assert params[15] is True   # high_touches_reference
    assert params[16] == TOUCH_BASIS


def test_record_event_lineage_persists_recomputed_key_and_checks():
    conn = FakeConnection()
    assert record_event_lineage(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET, source_observation_id=12345,
        phase1_event_key=event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET),
        phase1_observation_id=777) is True
    params = conn.inserts[0][1]
    key = event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)
    assert params[0] == key
    assert params[11] == key          # event_key_recomputed
    assert params[12] is True         # event_key_integrity
    assert params[13] == "LINKED"
    assert params[18] == key          # phase1_event_key
    assert params[19] is True         # phase1_linked
    assert params[20] == 777


def test_record_event_lineage_quarantines_mismatched_key():
    conn = FakeConnection()
    assert record_event_lineage(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        event_key_stored="0" * 64) is True
    params = conn.inserts[0][1]
    assert params[13] == "QUARANTINED"
    assert params[12] is False
    assert "event_key_integrity" in params[14]
    assert params[18] is None and params[19] is False


def test_record_event_lineage_never_invents_phase1_linkage():
    conn = FakeConnection()
    record_event_lineage(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET, phase1_event_key="deadbeef")
    params = conn.inserts[0][1]
    assert params[13] == "QUARANTINED"
    assert params[18] is None
    assert params[19] is False


def test_record_event_lineage_pending_when_source_signal_missing():
    conn = FakeConnection()
    record_event_lineage(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET, source_signal_present=False)
    params = conn.inserts[0][1]
    assert params[13] == "PENDING"
    assert params[16] is False


def test_finalize_outcome_writes_only_settled_rows():
    conn = FakeConnection()
    unmatured = make_candles(SIGNAL, high=lambda i: 99)
    assert finalize_outcome(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        asof_time=SIGNAL + timedelta(minutes=60), candles=unmatured) is False
    assert conn.inserts == []


def test_finalize_outcome_persists_finalized_path_and_identity():
    conn = FakeConnection()
    candles = make_candles(SIGNAL, high=lambda i: 100.5 if i == 3 else 99,
                           low=lambda i: 98.5)
    assert finalize_outcome(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        asof_time=SIGNAL + timedelta(minutes=240), candles=candles,
        source_observation_id=4242) is True
    sql, params = conn.inserts[0]
    # column order is pinned by the SQL contract below; index -> column
    columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    assert columns[0] == "event_key" and columns[-1] == "source_observation_id"
    assert len(columns) == 46
    assert len(params) == 48          # 46 columns + 2 activation guard binds
    assert params[columns.index("event_key")] == \
        event_key(SYMBOL, "SHORT", SIGNAL, REF, STOP, TARGET)
    assert params[columns.index("outcome_status")] == "MATCHED_FINALIZED"
    assert params[columns.index("path_class")] == PATH_STOP_HIT
    assert params[columns.index("gross_r")] == Decimal("-1")
    assert params[columns.index("exit_price")] == STOP
    assert params[columns.index("is_final")] is True
    assert params[columns.index("finalized_at")] == SIGNAL + timedelta(minutes=240)
    assert params[columns.index("finalization_candle_close_time")] == \
        SIGNAL + timedelta(minutes=240)
    assert params[columns.index("horizon_end_time")] == SIGNAL + timedelta(minutes=240)
    assert params[columns.index("asof_time")] == SIGNAL + timedelta(minutes=240)
    assert params[columns.index("max_candle_close_time")] == \
        SIGNAL + timedelta(minutes=240)
    assert params[columns.index("candles_expected")] == EXPECTED
    assert params[columns.index("candles_present")] == EXPECTED
    assert params[columns.index("coverage_complete")] is True
    assert params[columns.index("asof_ok")] is True
    assert params[columns.index("touch_basis")] == TOUCH_BASIS
    assert params[columns.index("exit_fill_semantics")] == EXIT_FILL_SEMANTICS
    assert params[columns.index("partial_fill_status")] == PARTIAL_FILL_STATUS
    assert params[columns.index("source_observation_id")] == 4242


def test_finalize_outcome_source_incomplete_carries_no_economics():
    conn = FakeConnection()
    candles = make_candles(SIGNAL, high=lambda i: 100.5 if i == 3 else 99,
                           skip={0, 1, 2, 3, 4})
    assert finalize_outcome(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        asof_time=SIGNAL + timedelta(minutes=240), candles=candles) is True
    sql, params = conn.inserts[0]
    columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    assert params[columns.index("outcome_status")] == OUTCOME_STATUS_SOURCE_INCOMPLETE
    for column in ("path_class", "exit_price", "gross_r", "net_r_normal",
                   "net_r_elevated", "return_at_240m"):
        assert params[columns.index(column)] is None, column
    assert params[columns.index("is_final")] is False
    assert params[columns.index("finalized_at")] is None
    assert params[columns.index("finalization_candle_close_time")] is None
    # what was observable is still preserved for the audit
    assert params[columns.index("candles_present")] == EXPECTED - 5
    assert params[columns.index("coverage_complete")] is False


def test_finalize_outcome_is_idempotent_on_replay():
    conn = FakeConnection(conflict_skip=True)
    candles = make_candles(SIGNAL, high=lambda i: 99, low=lambda i: 98.5)
    assert finalize_outcome(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        asof_time=SIGNAL + timedelta(minutes=240), candles=candles) is False
    assert len(conn.inserts) == 1
    assert "ON CONFLICT(EVENT_KEY) DO NOTHING" in conn.inserts[0][0].upper()


def test_every_insert_is_guarded_by_the_activation_boundary():
    conn = FakeConnection()
    capture_source_signal(conn, symbol=SYMBOL, signal_time=SIGNAL,
                          reference_price=REF, stop_price=STOP,
                          target_price=TARGET)
    capture_post_signal_candle(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET,
        candle={"open_time": (SIGNAL + timedelta(minutes=5)).isoformat(),
                "high": 99, "low": 98, "open": 98.5, "close": 98.5})
    record_event_lineage(conn, symbol=SYMBOL, signal_time=SIGNAL,
                         reference_price=REF, stop_price=STOP,
                         target_price=TARGET)
    finalize_outcome(conn, symbol=SYMBOL, signal_time=SIGNAL,
                     reference_price=REF, stop_price=STOP, target_price=TARGET,
                     asof_time=SIGNAL + timedelta(minutes=240),
                     candles=make_candles(SIGNAL, high=lambda i: 99))
    assert len(conn.inserts) == 4
    for sql, params in conn.inserts:
        upper = sql.upper()
        assert "WHERE EXISTS (SELECT 1 FROM RESEARCH.ME_SHORT_A_LINEAGE_ACTIVATION_V1" in upper
        assert "STATUS='RUNNING'" in upper
        assert "FREEZE_TS=%S" in upper
        assert "ON CONFLICT(" in upper
        # the bind count must match the parameter tuple exactly, so a real
        # driver would never see a malformed statement
        assert sql.count("%s") == len(params), sql[:80]
        assert params[-2] == EXPERIMENT
        assert params[-1] == FREEZE


def test_no_update_or_delete_statements_are_ever_issued():
    conn = FakeConnection()
    capture_source_signal(conn, symbol=SYMBOL, signal_time=SIGNAL,
                          reference_price=REF, stop_price=STOP,
                          target_price=TARGET)
    record_event_lineage(conn, symbol=SYMBOL, signal_time=SIGNAL,
                         reference_price=REF, stop_price=STOP,
                         target_price=TARGET)
    for sql, _ in conn.statements:
        head = sql.lstrip().upper()
        assert not head.startswith("UPDATE")
        assert not head.startswith("DELETE")
        assert not head.startswith("TRUNCATE")


def test_failure_rolls_back_and_returns_false():
    class ExplodingCursor(FakeCursor):
        def execute(self, sql, params=None):
            if sql.lstrip().upper().startswith("INSERT"):
                raise RuntimeError("boom")
            super().execute(sql, params)

    class ExplodingConnection(FakeConnection):
        def cursor(self):
            return ExplodingCursor(self)

    conn = ExplodingConnection()
    assert capture_source_signal(
        conn, symbol=SYMBOL, signal_time=SIGNAL, reference_price=REF,
        stop_price=STOP, target_price=TARGET) is False
    assert conn.rollbacks == 1
