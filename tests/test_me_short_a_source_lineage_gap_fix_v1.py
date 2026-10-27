"""Issue #208 corrective — ME SHORT A source lineage gap fix V1.

End-to-end regressions for the confirmed incident class (TSLAUSDT
2026-10-27 10:43:57.672455+00, ``MOMENTUM_EXHAUSTION`` ``SHORT`` in
``TREND_UP``): a candidate dropped by the TRADING regime filter must still
land in the research source intake, and the runtime's
``source signal -> Phase 1 -> lineage -> outcome`` chain must close on ONE
deterministic event_key.

Pinned here:

* the incident reproduction at the orchestrator level (regime filter still
  blocks the SHORT; ``setups_saved`` no longer reports phantom saves; the
  dropped candidate is available for research capture);
* the scanner-side research capture of every route — including
  ``REGIME_FILTER_REJECTED`` — with strict no-backfill behaviour for the
  incident event itself;
* byte-identical event identity across
  ``intake -> Phase 1 -> lineage -> runtime`` at FULL microsecond precision;
* the runtime's composite candidate source (``dds.scanner_setup`` UNION the
  research intake) with lossless keyset pagination and cross-transport
  deduplication;
* the full chain through the frozen collector to ``MATCHED_FINALIZED``
  evidence, plus honest ``MISSING_LINEAGE`` / ``OUTCOME_UNMATURED`` /
  ``SOURCE_INCOMPLETE`` reporting and zero trading-flow side effects.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.research import me_short_a_forward_outcome_lineage_v1 as lineage
from app.research import me_short_a_forward_outcome_runtime_v1 as mod
from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
    CompositeCandidateSource,
    DbScannerSetupCandidateSource,
    DbSourceIntakeCandidateSource,
)
from app.research.me_short_a_source_intake_v1 import (
    ACTIVATION_FLAG,
    compute_event_key,
)
from app.scanners.models import MarketContext, SetupCandidate, SetupState
from app.scanners.orchestrator import ScannerOrchestrator

import scanner_runner
import test_me_short_a_forward_outcome_runtime_v1 as base
import test_me_short_a_source_intake_v1 as writer_base

ROOT = Path(__file__).resolve().parent.parent

# The confirmed incident: real wall-clock detection WITH microseconds.
INCIDENT_TS = datetime(2026, 10, 27, 10, 43, 57, 672455, tzinfo=timezone.utc)
# The activation boundary of the FIXED mechanism: strictly after the
# incident, set at explicit activation. The incident can never be captured.
INTAKE_FREEZE_TS = datetime(2026, 10, 27, 12, 0, 0, 0, tzinfo=timezone.utc)
# A NEW prospective event after activation (the forward-only cohort).
NEW_EVENT_TS = datetime(2026, 10, 27, 12, 5, 0, 672455, tzinfo=timezone.utc)

INCIDENT_SYMBOL = "TSLAUSDT"
# Prices are canonicalized exactly like the writers do
# (Decimal(str(float))) — keep the literal strings equal on both sides.
INCIDENT_REF = Decimal("342.1")
INCIDENT_STOP = Decimal("342.78")
INCIDENT_TARGET = Decimal("338.0")

# ── runtime fixtures (dates deliberately in the past: base module's rule) ─
FREEZE = base.FREEZE                     # #208 activation boundary
INTAKE_FREEZE = datetime(2025, 1, 15, 12, 30, 0, 0, tzinfo=timezone.utc)
SIG_US = datetime(2025, 1, 15, 13, 5, 0, 672455, tzinfo=timezone.utc)
# Off-grid (microsecond) signals have one extra window candle; settle only
# once that candle — closing 5m after the horizon boundary — is closed.
NOW_US = SIG_US + timedelta(minutes=250)

SYMBOL = base.SYMBOL
REF = base.REF
STOP = base.STOP
TARGET = base.TARGET

SOURCE_TABLE = base.SOURCE_TABLE
LINEAGE_TABLE = base.LINEAGE_TABLE
CANDLE_TABLE = base.CANDLE_TABLE
OUTCOME_TABLE = base.OUTCOME_TABLE
HEARTBEAT_TABLE = base.HEARTBEAT_TABLE


@pytest.fixture(autouse=True)
def _capture_flags(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_LINEAGE_CAPTURE_ENABLED", "1")
    monkeypatch.setenv(ACTIVATION_FLAG, "1")
    yield


# ── incident candidate (the confirmed gap class) ────────────────────────


def make_me_short_candidate(detected_at=NEW_EVENT_TS, **overrides):
    fields = dict(
        symbol=INCIDENT_SYMBOL,
        scanner_name="MOMENTUM_EXHAUSTION",
        scanner_version="1.0.0",
        direction="SHORT",
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        setup_started_at=None,
        detected_at=detected_at,
        reference_price=float(INCIDENT_REF),
        entry_zone_low=341.9,
        entry_zone_high=342.2,
        invalidation_price=float(INCIDENT_STOP),
        target_1=float(INCIDENT_TARGET),
        target_2=None,
        score=50.0,
        market_regime="TREND_UP",
        state=SetupState.SETUP_READY,
        reasons=("ME_SHORT_A_TEST",),
        features={"trigger_5m_close": 342.0},
        setup_id=uuid4(),
        signal_candle_open_time=None,
    )
    fields.update(overrides)
    return SetupCandidate(**fields)


def mock_ctx(regime="TREND_UP", symbol=INCIDENT_SYMBOL):
    ctx = MagicMock(spec=MarketContext)
    ctx.symbol = symbol
    ctx.market_regime = regime
    ctx.candles_5m = ()
    ctx.candles_15m = ()
    ctx.candles_1h = ()
    ctx.candles_4h = ()
    return ctx


# ── A. incident reproduction at the orchestrator ────────────────────────


def test_regime_filter_still_blocks_the_short_but_reports_it_for_research():
    """The trading regime filter behaviour is UNCHANGED (SHORT in TREND_UP is
    excluded from the tradeable flow) — but the dropped candidate is now
    observable for the research source capture and ``setups_saved`` no
    longer reports a phantom save."""
    candidate = make_me_short_candidate()
    sid = candidate.setup_id
    orchestrator = ScannerOrchestrator(enabled_scanners=["MOMENTUM_EXHAUSTION"])
    mock_scanner = MagicMock()
    mock_scanner.scan.return_value = [candidate]
    orchestrator.scanners["MOMENTUM_EXHAUSTION"] = mock_scanner

    candidates, stats = orchestrator.scan_all_with_stats(
        mock_ctx(), regime_filter=True)

    stat = stats["MOMENTUM_EXHAUSTION"]
    assert stat["candidates_found"] == 1
    # Truthful funnel: nothing was returned for persistence.
    assert stat["setups_saved"] == 0
    # The trading flow never sees the candidate again.
    assert [c.setup_id for c in candidates] == []
    # But the research route has the ORIGINAL event with its route metadata.
    rejected = stats["_regime_rejected_candidates"]
    assert [c.setup_id for c in rejected] == [sid]
    assert rejected[0].direction == "SHORT"
    assert rejected[0].market_regime == "TREND_UP"
    assert rejected[0].detected_at == NEW_EVENT_TS


def test_regime_filter_semantics_are_unchanged_for_non_conflicts():
    """LONG in TREND_UP and SHORT in TREND_DOWN still pass — the filter only
    drops the direction/regime conflicts."""
    for direction, regime, dropped in (
        ("LONG", "TREND_UP", False),
        ("SHORT", "TREND_DOWN", False),
        ("LONG", "TREND_DOWN", True),
        ("SHORT", "TREND_UP", True),
    ):
        candidate = make_me_short_candidate(
            direction=direction, market_regime=regime,
            invalidation_price=342.78 if direction == "SHORT" else 341.0,
            target_1=338.0 if direction == "SHORT" else 346.0)
        orchestrator = ScannerOrchestrator(enabled_scanners=["MOMENTUM_EXHAUSTION"])
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = [candidate]
        orchestrator.scanners["MOMENTUM_EXHAUSTION"] = mock_scanner
        candidates, stats = orchestrator.scan_all_with_stats(
            mock_ctx(regime=regime), regime_filter=True)
        assert bool(stats["_regime_rejected_candidates"]) is dropped, (
            direction, regime)


def test_momentum_exhaustion_is_the_shadow_control_incident_class():
    """Precondition of the confirmed incident: MOMENTUM_EXHAUSTION is a
    shadow/control scanner, so its candidates rejoin ``valid`` after the
    direction gate and are only dropped by the regime filter."""
    assert "MOMENTUM_EXHAUSTION" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS


# ── B. scanner-side research capture (every route, fail-open) ───────────


class FakeRepo:
    def __init__(self):
        self.errors = []

    def save_error(self, symbol, scanner_name, error_type, error_message,
                   run_id=None, retry_count=0):
        self.errors.append((symbol, scanner_name, error_type, error_message))


def capture_route(conn, candidate, route, **kwargs):
    repo = FakeRepo()
    original = scanner_runner._get_me_short_a_source_intake_conn
    scanner_runner._get_me_short_a_source_intake_conn = lambda _repo: conn
    try:
        scanner_runner._capture_me_short_a_source(
            repo, candidate, capture_route=route, **kwargs)
    finally:
        scanner_runner._get_me_short_a_source_intake_conn = original
    return repo


@pytest.mark.parametrize("route", [
    "TRADEABLE_FLOW",
    "SHADOW_CONTROL",
    "OBSERVE_ONLY",
    "DIRECTION_GATE_BLOCKED",
    "EXPECTANCY_FILTER_REJECTED",
    "REGIME_FILTER_REJECTED",
])
def test_every_research_route_captures_the_original_event(route):
    """A trading filter decision (or block) can never prevent research
    capture of the original ME SHORT event."""
    conn = writer_base.FakeConnection(
        activation_row=("RUNNING", INTAKE_FREEZE_TS))
    candidate = make_me_short_candidate()
    repo = capture_route(
        conn, candidate, route,
        blocked_stage="regime_filter" if route.endswith("REJECTED") else None,
        blocked_reason="SHORT in TREND_UP")
    assert repo.errors == []
    assert len(conn.inserts) == 1
    _, params = conn.inserts[0]
    assert route in params
    # The event identity is the deterministic frozen one:
    assert params[0] == compute_event_key(
        INCIDENT_SYMBOL, NEW_EVENT_TS,
        Decimal(str(INCIDENT_REF)), Decimal(str(INCIDENT_STOP)),
        Decimal(str(INCIDENT_TARGET)))


def test_incident_event_is_never_backfilled():
    """TSLAUSDT 2026-10-27 10:43:57.672455+00 predates the activation
    boundary of the fixed mechanism and can never enter the new cohort."""
    conn = writer_base.FakeConnection(
        activation_row=("RUNNING", INTAKE_FREEZE_TS))
    candidate = make_me_short_candidate(detected_at=INCIDENT_TS)
    repo = capture_route(conn, candidate, "REGIME_FILTER_REJECTED")
    assert conn.inserts == []
    assert repo.errors == []   # refusal is not an error


def test_capture_is_independent_of_the_prospective_observer():
    """The hook never touches the prospective observer / Phase 1 paths and
    works even when research routing is absent (prospective_obs is None)."""
    conn = writer_base.FakeConnection(
        activation_row=("RUNNING", INTAKE_FREEZE_TS))
    candidate = make_me_short_candidate()
    repo = capture_route(conn, candidate, "REGIME_FILTER_REJECTED")
    assert len(conn.inserts) == 1
    assert repo.errors == []
    for sql, _ in conn.statements:
        assert "prospective" not in sql.lower()
        assert "fill_feasibility" not in sql.lower()


def test_source_capture_errors_have_separate_diagnostics():
    """Persistence errors are sunk to dds.scanner_error and never affect the
    scanner: the hook swallows everything."""
    conn = writer_base.FakeConnection(
        activation_row=("RUNNING", INTAKE_FREEZE_TS), fail_on="INSERT INTO")
    candidate = make_me_short_candidate()
    repo = capture_route(conn, candidate, "REGIME_FILTER_REJECTED")
    assert len(repo.errors) == 1
    assert repo.errors[0][2] == "ME_SHORT_A_SOURCE_INTAKE_ERROR"
    assert "REGIME_FILTER_REJECTED" in repo.errors[0][3]


def test_missing_connection_is_reported_and_never_raises():
    candidate = make_me_short_candidate()
    repo = FakeRepo()
    original = scanner_runner._get_me_short_a_source_intake_conn
    scanner_runner._get_me_short_a_source_intake_conn = lambda _repo: None
    try:
        scanner_runner._capture_me_short_a_source(
            repo, candidate, capture_route="REGIME_FILTER_REJECTED")
    finally:
        scanner_runner._get_me_short_a_source_intake_conn = original
    assert len(repo.errors) == 1


# ── C. one identity across the whole chain ──────────────────────────────


def test_event_key_chain_is_byte_identical_at_microsecond_precision():
    key = compute_event_key(
        INCIDENT_SYMBOL, NEW_EVENT_TS,
        INCIDENT_REF, INCIDENT_STOP, INCIDENT_TARGET)
    # Phase 1's own formula (me_short_a_fill_feasibility_v1):
    ref, stop, target = (Decimal(str(INCIDENT_REF)), Decimal(str(INCIDENT_STOP)),
                         Decimal(str(INCIDENT_TARGET)))
    import hashlib
    phase1_formula = hashlib.sha256(
        f"{INCIDENT_SYMBOL}|SHORT|{NEW_EVENT_TS.isoformat()}|{ref}|{stop}|{target}"
        .encode()).hexdigest()
    assert key == phase1_formula
    # The lineage / Issue #208 identity:
    assert key == lineage.event_key(
        INCIDENT_SYMBOL, lineage.DIRECTION, NEW_EVENT_TS,
        INCIDENT_REF, INCIDENT_STOP, INCIDENT_TARGET)


# ── test doubles for the runtime transports ─────────────────────────────


class GapConn(base.FakeConnection):
    """Faithful read double: ``dds.scanner_setup`` + the research intake.

    Implements exactly what the production SELECTs say: the
    scanner/direction predicate, the strict ``> lower`` boundary, the
    composite keyset cursor and a total order with ``LIMIT``.
    """

    def __init__(self, *, intake_rows=(), dds_rows=(),
                 intake_activation=("RUNNING", INTAKE_FREEZE), **kwargs):
        super().__init__(**kwargs)
        self.intake_rows = list(intake_rows)
        self.dds_rows = list(dds_rows)
        self.intake_activation = intake_activation

    def route_select(self, head, params):
        # Model a least-privilege role that lacks a SELECT (same contract as
        # the base double): surface the read error to the source under test.
        if self.fail_select and self.fail_select.upper() in head:
            raise PermissionError(f"permission denied for {self.fail_select}")
        if ("ME_SHORT_A_SOURCE_INTAKE_ACTIVATION_V1" in head
                and "STATUS,INTAKE_FREEZE_TS" in head):
            return [self.intake_activation] if self.intake_activation else []
        if "ME_SHORT_A_SOURCE_INTAKE_V1" in head and "ORDER BY" in head:
            return self._intake_page(params)
        if "DDS.SCANNER_SETUP" in head:
            return self._dds_page(params)
        return super().route_select(head, params)

    def _intake_page(self, params):
        scanner, direction, lower = params[0], params[1], params[2]
        limit = int(params[-1])
        cursor = None if len(params) == 4 else (params[3], params[5])
        out = []
        for row in sorted(self.intake_rows,
                          key=lambda r: (r["signal_time"], str(r["event_key"]))):
            if row["scanner_name"] != scanner or row["direction"] != direction:
                continue
            if row["signal_time"] <= lower:
                continue
            if cursor is not None:
                t, k = cursor
                if not (row["signal_time"] > t
                        or (row["signal_time"] == t
                            and str(row["event_key"]) > str(k))):
                    continue
            out.append(self._intake_row(row))
            if len(out) >= limit:
                break
        return out

    def _dds_page(self, params):
        scanner, direction, lower = params[0], params[1], params[2]
        limit = int(params[-1])
        cursor = None if len(params) == 4 else (params[3], params[5])
        out = []
        for row in sorted(self.dds_rows,
                          key=lambda r: (r["detected_at"], str(r["setup_id"]))):
            if row["scanner_name"] != scanner or row["direction"] != direction:
                continue
            if row["detected_at"] <= lower:
                continue
            if cursor is not None:
                det, sid = cursor
                if not (row["detected_at"] > det
                        or (row["detected_at"] == det
                            and str(row["setup_id"]) > str(sid))):
                    continue
            out.append([
                row["setup_id"], row["symbol"], row["detected_at"],
                row["reference_price"], row["invalidation_price"],
                row["target_1"], row["entry_zone_low"], row["entry_zone_high"],
                row["signal_candle_open_time"], row["features"],
                row["market_regime"], row["status"],
            ])
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _intake_row(row):
        return [
            row["event_key"], row["symbol"], row["signal_time"],
            row["reference_price"], row["stop_price"], row["target_price"],
            row.get("target_2"), row.get("trigger_5m_close"),
            row.get("entry_zone_low"), row.get("entry_zone_high"),
            row.get("signal_candle_open_time"), row.get("recent_high_window", []),
            row.get("market_context", {}), row.get("signal_time_quote", {}),
            row["capture_route"], row.get("blocked_stage"),
            row.get("blocked_reason"), row.get("market_regime"),
            row.get("setup_id"), row.get("missing_fields", []),
            row.get("source_quality_flags", []),
            row.get("no_lookahead_flags", []),
        ]


def make_intake_row(signal_time=SIG_US, *, symbol=SYMBOL,
                    ref=REF, stop=STOP, target=TARGET, **overrides):
    row = dict(
        event_key=compute_event_key(symbol, signal_time, ref, stop, target),
        symbol=symbol,
        signal_time=signal_time,
        reference_price=float(ref),
        stop_price=float(stop),
        target_price=float(target),
        target_2=None,
        trigger_5m_close=Decimal("99.4"),
        entry_zone_low=Decimal("99.2"),
        entry_zone_high=Decimal("99.4"),
        signal_candle_open_time=None,
        recent_high_window=[],
        market_context={},
        signal_time_quote={},
        capture_route="REGIME_FILTER_REJECTED",
        blocked_stage="regime_filter",
        blocked_reason="SHORT in TREND_UP",
        market_regime="TREND_UP",
        setup_id="setup-1",
        missing_fields=[],
        source_quality_flags=[],
        no_lookahead_flags=[],
        scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT",
    )
    row.update(overrides)
    return row


def make_dds_row(detected_at=SIG_US, *, setup_id="setup-1", **overrides):
    row = dict(
        setup_id=setup_id,
        symbol=SYMBOL,
        detected_at=detected_at,
        reference_price=float(REF),
        invalidation_price=float(STOP),
        target_1=float(TARGET),
        entry_zone_low=99.2,
        entry_zone_high=99.4,
        signal_candle_open_time=None,
        features={"trigger_5m_close": 99.4},
        market_regime="TREND_START",
        status="READY_TO_TRADE",
        scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT",
    )
    row.update(overrides)
    return row


def window_for(signal_time=SIG_US):
    return base.make_window(signal_time=signal_time, symbol=SYMBOL)


def candle_source_for(signal_time=SIG_US):
    return base.InMemoryCandleSource(
        list(base.make_window(signal_time=signal_time, symbol=SYMBOL))
        + list(base.make_outcome_candles(signal_time=signal_time)))


# ── D. the research intake candidate source ─────────────────────────────


def test_intake_source_is_read_only_on_the_two_intake_tables():
    conn = GapConn(intake_rows=[make_intake_row()])
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    source.fetch_page(None, freeze_ts=FREEZE)
    assert conn.inserts == []
    for sql, _params in conn.statements:
        assert sql.lstrip().upper().startswith("SELECT")
        assert "research.me_short_a_source_intake" in sql
        for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not re.search(rf"\b{verb}\b", sql), verb
    assert source.read_tables == (
        "research.me_short_a_source_intake_v1",
        "research.me_short_a_source_intake_activation_v1",
    )


@pytest.mark.parametrize("activation", [None, ("READY_TO_START", None),
                                        ("CANCELLED", INTAKE_FREEZE)])
def test_intake_source_is_fail_closed_until_activated(activation):
    conn = GapConn(intake_rows=[make_intake_row()], intake_activation=activation)
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    page = source.fetch_page(None, freeze_ts=FREEZE)
    assert page.rows_read == 0
    assert page.candidates == ()


def test_intake_source_enforces_the_activation_floor():
    """Strict forward-only: rows at or before ``intake_freeze_ts`` — the
    incident class included — are structurally unreadable."""
    at_floor = make_intake_row(signal_time=INTAKE_FREEZE)
    before = make_intake_row(signal_time=INTAKE_FREEZE - timedelta(minutes=1))
    after = make_intake_row(signal_time=INTAKE_FREEZE + timedelta(minutes=1))
    conn = GapConn(intake_rows=[before, at_floor, after])
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    page = source.fetch_page(None, freeze_ts=FREEZE)
    assert [c.signal_time for c in page.candidates] == [
        INTAKE_FREEZE + timedelta(minutes=1)]


def test_intake_source_applies_the_higher_of_both_freezes():
    conn = GapConn(intake_rows=[make_intake_row()])
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    # A legacy (lower) #208 freeze must never widen the cohort:
    page = source.fetch_page(None, freeze_ts=FREEZE)
    assert page.rows_read == 1
    _, params = conn.statements[-1]  # the page SELECT (after the activation)
    assert params[2] == INTAKE_FREEZE  # effective lower bound


def test_intake_source_preserves_microsecond_signal_time():
    conn = GapConn(intake_rows=[make_intake_row(signal_time=SIG_US)])
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    page = source.fetch_page(None, freeze_ts=FREEZE)
    assert len(page.candidates) == 1
    candidate = page.candidates[0]
    assert candidate.signal_time == SIG_US
    assert candidate.signal_time.microsecond == 672455
    # and the identity built from the row is the frozen one:
    assert mod.event_key_for(candidate) == compute_event_key(
        SYMBOL, SIG_US, REF, STOP, TARGET)


def test_intake_source_carries_the_route_metadata_for_observability():
    conn = GapConn(intake_rows=[make_intake_row()])
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    page = source.fetch_page(None, freeze_ts=FREEZE)
    candidate = page.candidates[0]
    assert candidate.market_context["capture_route"] == "REGIME_FILTER_REJECTED"
    assert candidate.market_context["blocked_stage"] == "regime_filter"
    assert candidate.market_context["market_regime"] == "TREND_UP"


def test_intake_source_fails_soft_and_lossless_on_read_errors():
    conn = GapConn(intake_rows=[make_intake_row()])
    conn.fail_select = "ME_SHORT_A_SOURCE_INTAKE_V1"
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    page = source.fetch_page(None, freeze_ts=FREEZE)
    assert page.rows_read == 0 and page.candidates == ()
    assert source.error_count == 1
    assert source.last_error
    # Lossless: the cursor never moved, so the same rows are re-read once the
    # failure clears — nothing is dropped, and the dds stream never stalls.
    conn.fail_select = None
    page = source.fetch_page(None, freeze_ts=FREEZE)
    assert page.rows_read == 1


def test_intake_source_keyset_paginates_without_drops():
    rows = [
        make_intake_row(signal_time=SIG_US, setup_id=f"setup-{i}",
                        symbol=f"SYM{i}USDT",
                        event_key=f"key-{i:02d}")
        for i in range(5)
    ]
    conn = GapConn(intake_rows=rows)
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    seen = []
    cursor = None
    for _ in range(10):
        page = source.fetch_page(cursor, freeze_ts=FREEZE, page_size=2)
        seen.extend(page.candidates)
        if page.rows_read <= 0:
            break
        cursor = page.next_cursor
    assert len(seen) == 5
    assert len({mod.event_key_for(c) for c in seen}) == 5


def test_intake_source_skips_invalid_rows_without_blocking_the_stream():
    good = make_intake_row(signal_time=SIG_US, symbol="GOODUSDT",
                           event_key="key-aaa")
    bad = make_intake_row(signal_time=SIG_US, symbol="BADUSDT",
                          event_key="key-zzz",
                          reference_price=None)   # unrepresentable row
    conn = GapConn(intake_rows=[bad, good])
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    page = source.fetch_page(None, freeze_ts=FREEZE, page_size=2)
    assert page.rows_read == 2
    assert [c.symbol for c in page.candidates] == ["GOODUSDT"]
    assert page.rejected == ("key-zzz",)
    # The cursor tracks the LAST RAW ROW even when it is the broken one: the
    # rejected row can never block the stream behind it.
    assert page.next_cursor == (SIG_US, "key-zzz")


def test_intake_source_reconstructs_the_closed_window_without_lookahead():
    row = make_intake_row()
    conn = GapConn(intake_rows=[row])
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    page = source.fetch_page(None, freeze_ts=FREEZE)
    candidate = page.candidates[0]
    assert len(candidate.recent_high_window) == 5
    assert all(c.close_time <= SIG_US for c in candidate.recent_high_window)
    assert max(c.high for c in candidate.recent_high_window) == REF


def test_lower_bound_is_the_intake_activation_floor():
    conn = GapConn()
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    assert source.lower_bound() == INTAKE_FREEZE
    conn.intake_activation = ("READY_TO_START", None)
    assert source.lower_bound() is None


# ── E. the composite candidate source ───────────────────────────────────


def test_composite_merges_both_transports_losslessly():
    dds = [make_dds_row(setup_id=f"dds-{i}",
                        detected_at=SIG_US + timedelta(seconds=i))
           for i in range(3)]
    intake = [make_intake_row(symbol=f"SYM{i}USDT", event_key=f"int-{i}")
              for i in range(3)]
    conn = GapConn(dds_rows=dds, intake_rows=intake)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    seen = []
    cursor = None
    for _ in range(10):
        page = sources.fetch_page(cursor, freeze_ts=FREEZE, page_size=2)
        seen.extend(page.candidates)
        if page.rows_read <= 0:
            break
        cursor = page.next_cursor
    assert len(seen) == 6          # nothing dropped across transports


def test_composite_collapses_the_same_event_across_transports():
    """One event can arrive through several transports; it is captured once."""
    dds = [make_dds_row(setup_id="setup-1")]
    intake = [make_intake_row()]
    conn = GapConn(dds_rows=dds, intake_rows=intake)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    page = sources.fetch_page(None, freeze_ts=FREEZE)
    assert page.rows_read == 2                     # both rows were read
    assert len(page.candidates) == 1               # one deterministic event
    assert mod.event_key_for(page.candidates[0]) == compute_event_key(
        SYMBOL, SIG_US, REF, STOP, TARGET)


def test_composite_re_read_after_stall_returns_the_same_rows():
    """The runtime only stores a page cursor after handling the page in
    full. A stalled page must be re-readable in full — never lost."""
    intake = [make_intake_row(symbol=f"SYM{i}USDT", event_key=f"int-{i}")
              for i in range(3)]
    conn = GapConn(intake_rows=intake)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    first = sources.fetch_page(None, freeze_ts=FREEZE, page_size=2)
    again = sources.fetch_page(None, freeze_ts=FREEZE, page_size=2)
    assert ([mod.event_key_for(c) for c in first.candidates]
            == [mod.event_key_for(c) for c in again.candidates])
    # And advancing past the page loses nothing:
    after = sources.fetch_page(first.next_cursor, freeze_ts=FREEZE, page_size=2)
    assert [c.symbol for c in after.candidates] == ["SYM2USDT"]


def test_composite_keeps_sub_cursors_when_a_source_reads_nothing():
    intake = [make_intake_row()]
    conn = GapConn(intake_rows=intake)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    page = sources.fetch_page(None, freeze_ts=FREEZE)
    cursor = page.next_cursor
    # Next fetch: the drained intake source must NOT lose its position.
    empty = sources.fetch_page(cursor, freeze_ts=FREEZE)
    assert empty.rows_read == 0
    assert empty.next_cursor == cursor


def test_composite_keeps_each_sources_own_freeze_bound():
    """The composite NEVER raises a common boundary (review P1).

    The ``dds.scanner_setup`` transport keeps the ORIGINAL Issue #208
    ``freeze_ts`` — its frozen cohort is never shrunk — while the research
    intake applies its own activation floor ``max(freeze_ts,
    intake_freeze_ts)`` internally.
    """
    conn = GapConn(dds_rows=[make_dds_row()], intake_rows=[make_intake_row()])
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    sources.fetch_page(None, freeze_ts=FREEZE)
    dds_params = [p for sql, p in conn.statements
                  if "DDS.SCANNER_SETUP" in sql.upper()][-1]
    intake_params = [p for sql, p in conn.statements
                     if ("ME_SHORT_A_SOURCE_INTAKE_V1" in sql.upper()
                         and "ORDER BY" in sql.upper())][-1]
    assert dds_params[2] == FREEZE          # original #208 freeze preserved
    assert intake_params[2] == INTAKE_FREEZE  # own floor = max(FREEZE, INTAKE_FREEZE)


def test_original_dds_cohort_range_is_preserved_behind_the_intake_floor():
    """Rows of the ORIGINAL acceptable range (between the #208 freeze and
    the later intake activation floor) stay readable — the old cohort is
    byte-for-byte what it was before the intake existed."""
    mid = INTAKE_FREEZE - timedelta(minutes=5)   # FREEZE < mid < INTAKE_FREEZE
    conn = GapConn(
        dds_rows=[make_dds_row(detected_at=mid, setup_id="dds-old-range")],
        intake_rows=[make_intake_row(symbol="LATER1USDT")],
    )
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    page = sources.fetch_page(None, freeze_ts=FREEZE)
    assert [c.symbol for c in page.candidates] == [SYMBOL, "LATER1USDT"]


def test_intake_backfill_before_the_activation_floor_is_impossible():
    """An intake row AFTER the original #208 freeze but BEFORE
    ``intake_freeze_ts`` can never be read back (no backfill), while a dds
    row at the very same instant — part of the original cohort — is still
    observed."""
    mid = INTAKE_FREEZE - timedelta(minutes=5)
    conn = GapConn(
        dds_rows=[make_dds_row(detected_at=mid, setup_id="dds-mid")],
        intake_rows=[make_intake_row(signal_time=mid,
                                     symbol="BACKFILL1USDT")],
    )
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    page = sources.fetch_page(None, freeze_ts=FREEZE)
    assert [c.symbol for c in page.candidates] == [SYMBOL]  # dds only
    assert "BACKFILL1USDT" not in [c.symbol for c in page.candidates]


def test_intake_source_binds_the_max_of_both_freezes():
    """The intake source binds ``max(original freeze_ts, intake_freeze_ts)``
    inside its own query — never only ``intake_freeze_ts`` and never less
    than the original freeze."""
    earlier_activation = INTAKE_FREEZE - timedelta(hours=4)  # < FREEZE
    conn = GapConn(
        intake_rows=[make_intake_row()],
        intake_activation=("RUNNING", earlier_activation),
    )
    source = DbSourceIntakeCandidateSource(conn, candle_source_for())
    page = source.fetch_page(None, freeze_ts=FREEZE)
    assert page.rows_read == 1                      # SIG_US > FREEZE anyway
    params = [p for sql, p in conn.statements
              if ("ME_SHORT_A_SOURCE_INTAKE_V1" in sql.upper()
                  and "ORDER BY" in sql.upper())][-1]
    assert params[2] == FREEZE                      # max(FREEZE, earlier) = FREEZE


def test_composite_cursor_shape_is_opaque_and_stable():
    conn = GapConn(dds_rows=[make_dds_row()],
                   intake_rows=[make_intake_row()])
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    page = sources.fetch_page(None, freeze_ts=FREEZE)
    cursor = page.next_cursor
    assert len(tuple(cursor)) == 2
    with pytest.raises(ValueError):
        sources.fetch_page((cursor[0],), freeze_ts=FREEZE)


def _drain(sources, *, freeze_ts=FREEZE, page_size, max_fetches=1000):
    seen = []
    cursor = None
    for _ in range(max_fetches):
        page = sources.fetch_page(cursor, freeze_ts=freeze_ts,
                                  page_size=page_size)
        seen.extend(page.candidates)
        if page.rows_read <= 0:
            break
        cursor = page.next_cursor
    return seen


def test_new_intake_rows_are_never_starved_by_a_constant_dds_stream():
    """Review P1: a constant full ``dds.scanner_setup`` stream must never
    starve the research intake — a new intake row is observed in the FIRST
    page, not after the whole dds backlog is drained."""
    dds = [make_dds_row(setup_id=f"dds-{i}",
                        detected_at=SIG_US + timedelta(seconds=i))
           for i in range(200)]
    intake = [make_intake_row(symbol="FRESH1USDT")]
    conn = GapConn(dds_rows=dds, intake_rows=intake)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    page = sources.fetch_page(None, freeze_ts=FREEZE, page_size=10)
    assert any(c.symbol == "FRESH1USDT" for c in page.candidates)  # page 1!
    seen = _drain(sources, page_size=10)
    assert len(seen) == 201
    assert len({mod.event_key_for(c) for c in seen}) == 201


def test_composite_drains_a_large_multi_transport_stream_losslessly():
    """Large streams across both transports lose nothing and duplicate
    nothing, no matter how small the page is."""
    dds = [make_dds_row(setup_id=f"dds-{i}",
                        detected_at=SIG_US + timedelta(seconds=i))
           for i in range(120)]
    intake = [make_intake_row(symbol=f"SYM{i:03d}USDT",
                              signal_time=SIG_US + timedelta(milliseconds=i))
              for i in range(120)]
    conn = GapConn(dds_rows=dds, intake_rows=intake)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    seen = _drain(sources, page_size=7)
    keys = [mod.event_key_for(c) for c in seen]
    assert len(seen) == 240                      # 120 + 120, nothing dropped
    assert len(set(keys)) == 240                 # nothing duplicated


def test_identical_timestamps_are_each_returned_exactly_once():
    """Rows sharing the very same timestamp are each returned exactly once —
    the per-source tie-breakers keep the keyset order total across page
    boundaries."""
    same = SIG_US
    dds = [make_dds_row(setup_id=f"dds-{sym}", detected_at=same, symbol=sym)
           for sym in ("ASYM", "BSYM", "CSYM")]
    intake = [make_intake_row(signal_time=same, symbol=f"D{i}SYM")
              for i in range(2)]
    conn = GapConn(dds_rows=dds, intake_rows=intake)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    seen = _drain(sources, page_size=2)          # tie-breaks across pages
    keys = [mod.event_key_for(c) for c in seen]
    assert sorted(c.symbol for c in seen) == ["ASYM", "BSYM", "CSYM",
                                              "D0SYM", "D1SYM"]
    assert len(set(keys)) == len(keys) == 5      # exactly once each


def test_same_timestamp_cross_transport_twin_collapses_to_one_candidate():
    """The very same event arriving through two transports at the same
    timestamp is collapsed to one candidate (one fetch, deterministic)."""
    same = SIG_US
    dds = [make_dds_row(setup_id="dds-ASYM", detected_at=same, symbol="ASYM")]
    intake = [make_intake_row(signal_time=same, symbol="ASYM")]
    conn = GapConn(dds_rows=dds, intake_rows=intake)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    page = sources.fetch_page(None, freeze_ts=FREEZE, page_size=10)
    assert page.rows_read == 2                   # both rows were read
    assert [c.symbol for c in page.candidates] == ["ASYM"]  # one event


def test_dds_read_error_retries_the_same_page_without_loss():
    """A surfaced read error stalls the composite page; retrying from the
    same cursor returns the same rows — nothing is lost."""
    dds = [make_dds_row(setup_id=f"dds-{i}",
                        detected_at=SIG_US + timedelta(seconds=i))
           for i in range(3)]
    conn = GapConn(dds_rows=dds)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    conn.fail_select = "DDS.SCANNER_SETUP"
    with pytest.raises(mod.DatabaseAccessError):
        sources.fetch_page(None, freeze_ts=FREEZE, page_size=2)
    conn.fail_select = None
    first = sources.fetch_page(None, freeze_ts=FREEZE, page_size=2)  # same cursor
    assert len(first.candidates) == 2
    seen = _drain(sources, page_size=2)          # full replay after the retry
    keys = {mod.event_key_for(c) for c in seen}
    assert len(seen) == 3 and len(keys) == 3     # lossless, no duplicates
    assert {mod.event_key_for(c) for c in first.candidates} <= keys


def test_intake_read_error_fails_soft_and_the_dds_stream_continues():
    """An intake read error never stalls the dds transport, and the intake
    rows behind it are re-read losslessly on the next fetch."""
    dds = [make_dds_row(setup_id="dds-1")]
    intake = [make_intake_row(symbol="FRESH1USDT")]
    conn = GapConn(dds_rows=dds, intake_rows=intake)
    sources = CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))
    conn.fail_select = "ME_SHORT_A_SOURCE_INTAKE_V1"
    page = sources.fetch_page(None, freeze_ts=FREEZE, page_size=10)
    assert [c.symbol for c in page.candidates] == [SYMBOL]      # dds unaffected
    conn.fail_select = None
    after = sources.fetch_page(page.next_cursor, freeze_ts=FREEZE, page_size=10)
    assert [c.symbol for c in after.candidates] == ["FRESH1USDT"]  # lossless


# ── F. the full chain through the frozen runtime ────────────────────────


def build_runtime(conn, *, candidates_source, now=NOW_US, candles=None):
    return mod.ForwardOutcomeRuntime(
        conn,
        config=base.config(),
        candle_source=candles or candle_source_for(),
        candidate_source=candidates_source,
        clock=base.clock_at(now),
        runner_id="gap-fix-under-test",
    )


def intake_composite(conn):
    return CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, candle_source_for()),
        DbSourceIntakeCandidateSource(conn, candle_source_for()),
    ))


def test_full_chain_intake_to_outcome_on_one_event_key():
    """THE acceptance chain: a regime-filter-rejected ME SHORT event is read
    from the research intake and closes source -> Phase 1 -> lineage ->
    outcome on one deterministic event_key."""
    key = compute_event_key(SYMBOL, SIG_US, REF, STOP, TARGET)
    conn = GapConn(intake_rows=[make_intake_row()])
    conn.phase1_rows = [[key, [42]]]     # a Phase 1 snapshot exists
    runtime = build_runtime(conn, candidates_source=intake_composite(conn))
    runtime.run_cycle()

    assert conn.inserted_tables.count(SOURCE_TABLE) == 1
    assert conn.inserted_tables.count(LINEAGE_TABLE) == 1
    assert conn.inserted_tables.count(OUTCOME_TABLE) == 1
    # Closed 5m candles only, on the frozen grid: 48 for an off-grid
    # (microsecond) signal — never a forming candle.
    expected_candles = len(lineage.expected_window_opens(SIG_US))
    assert conn.inserted_tables.count(CANDLE_TABLE) == expected_candles

    source_row = base._payload(conn.inserts_for(SOURCE_TABLE)[0])
    assert source_row["event_key"] == key
    assert source_row["signal_time"] == SIG_US      # µs precision preserved

    lineage_row = base._payload(conn.inserts_for(LINEAGE_TABLE)[0])
    assert lineage_row["event_key"] == key
    assert lineage_row["phase1_linked"] is True     # linked by IDENTICAL key
    assert lineage_row["phase1_event_key"] == key

    outcome = base._payload(conn.inserts_for(OUTCOME_TABLE)[0])
    assert outcome["event_key"] == key
    assert outcome["intrabar_policy"] == "STOP_FIRST"
    assert outcome["horizon_minutes"] == 240
    assert outcome["is_final"] is True


def test_stop_first_and_horizon_are_unchanged_for_intake_sourced_events():
    key = compute_event_key(SYMBOL, SIG_US, REF, STOP, TARGET)
    candles = base.make_outcome_candles(
        signal_time=SIG_US, high=Decimal("100.5"), low=Decimal("97.5"))
    conn = GapConn(intake_rows=[make_intake_row()])
    runtime = build_runtime(
        conn, candidates_source=intake_composite(conn),
        candles=base.InMemoryCandleSource(candles))
    runtime.run_cycle()
    outcome = base._payload(conn.inserts_for(OUTCOME_TABLE)[0])
    assert outcome["event_key"] == key
    assert outcome["ambiguous_intrabar"] is True
    assert outcome["sl_before_tp"] is True          # STOP_FIRST preserved
    assert outcome["path_class"] == "STOP_HIT"
    assert Decimal(str(outcome["gross_r"])) == Decimal("-1")


def test_outcome_before_the_horizon_stays_unmatured():
    """Nothing is final before the 240-minute horizon is settled on closed
    candles; the frozen status view reports the event as OUTCOME_UNMATURED."""
    conn = GapConn(intake_rows=[make_intake_row()])
    runtime = build_runtime(
        conn, candidates_source=intake_composite(conn),
        now=SIG_US + timedelta(minutes=60))
    runtime.run_cycle()
    assert conn.inserts_for(OUTCOME_TABLE) == []   # never final prematurely
    assert runtime.metrics.skipped_unmatured >= 1


def test_incomplete_data_stays_source_incomplete_without_economics():
    full = base.make_outcome_candles(signal_time=SIG_US)
    # every other candle, but the LAST one stays so the horizon is settled:
    # coverage is incomplete, so no economics may be produced
    candles = [c for idx, c in enumerate(full) if idx % 2 == 0] + [full[-1]]
    assert len(candles) < len(full)
    conn = GapConn(intake_rows=[make_intake_row()])
    runtime = build_runtime(
        conn, candidates_source=intake_composite(conn),
        candles=base.InMemoryCandleSource(candles))
    runtime.run_cycle()
    outcome = base._payload(conn.inserts_for(OUTCOME_TABLE)[0])
    assert outcome["outcome_status"] == "SOURCE_INCOMPLETE"
    assert outcome["is_final"] is False
    assert outcome["gross_r"] is None
    assert outcome["exit_price"] is None


def test_missing_phase1_is_reported_not_faked():
    """No Phase 1 snapshot -> the lineage is honest (``phase1_linked=False``,
    which the frozen status view reports as MISSING_LINEAGE)."""
    conn = GapConn(intake_rows=[make_intake_row()])
    conn.phase1_rows = []     # no Phase 1 snapshot exists
    runtime = build_runtime(conn, candidates_source=intake_composite(conn))
    runtime.run_cycle()
    lineage_row = base._payload(conn.inserts_for(LINEAGE_TABLE)[0])
    assert lineage_row["phase1_linked"] is False
    assert lineage_row["phase1_event_key"] is None
    assert lineage_row["phase1_observation_id"] is None


def test_replay_restarts_and_re_polling_never_duplicate_or_lose():
    key = compute_event_key(SYMBOL, SIG_US, REF, STOP, TARGET)
    conn = GapConn(intake_rows=[make_intake_row()])
    runtime = build_runtime(conn, candidates_source=intake_composite(conn))
    runtime.run_cycle()
    # A restart re-polls the SAME intake rows with a fresh seen-set:
    runtime2 = build_runtime(conn, candidates_source=intake_composite(conn))
    runtime2.run_cycle()
    # Exactly-once evidence: the replayed writes are ON CONFLICT DO NOTHING.
    for table in (SOURCE_TABLE, LINEAGE_TABLE, OUTCOME_TABLE):
        assert len(conn.persisted_for(table)) == 1, table
    assert base._payload(conn.persisted_for(OUTCOME_TABLE)[0])["event_key"] == key


def test_microsecond_identity_survives_the_whole_chain():
    """The incident-class timestamp precision is preserved end to end; the
    old whole-seconds validation would have rejected this event."""
    key = compute_event_key(SYMBOL, SIG_US, REF, STOP, TARGET)
    conn = GapConn(intake_rows=[make_intake_row()])
    runtime = build_runtime(conn, candidates_source=intake_composite(conn))
    runtime.run_cycle()
    for table in (SOURCE_TABLE, LINEAGE_TABLE, OUTCOME_TABLE):
        rows = conn.persisted_for(table)
        assert rows, table
        assert base._payload(rows[0])["event_key"] == key
    assert runtime.metrics.signals_captured == 1


def test_the_chain_never_touches_trading_or_frozen_cohort_tables():
    conn = GapConn(intake_rows=[make_intake_row()])
    runtime = build_runtime(conn, candidates_source=intake_composite(conn))
    runtime.run_cycle()
    allowed = {SOURCE_TABLE, LINEAGE_TABLE, CANDLE_TABLE, OUTCOME_TABLE,
               HEARTBEAT_TABLE}
    for table in conn.inserted_tables:
        assert table in allowed, table
    for sql, _params in conn.inserts:
        assert "prospective_observation" not in sql.lower()
        assert "prospective_outcome" not in sql.lower()
        assert "dds." not in sql.lower()
        assert "paper" not in sql.lower()
        assert "live" not in sql.lower()


def test_comeback_of_the_same_event_through_dds_is_not_double_counted():
    """An event that eventually reaches dds.scanner_setup AND is already in
    the intake is one event in the chain."""
    conn = GapConn(dds_rows=[make_dds_row(setup_id="setup-1")],
                   intake_rows=[make_intake_row()])
    runtime = build_runtime(conn, candidates_source=intake_composite(conn))
    runtime.run_cycle()
    assert conn.inserted_tables.count(SOURCE_TABLE) == 1
    assert conn.inserted_tables.count(OUTCOME_TABLE) == 1


# ── G. frozen status-view contract (text level) ────────────────────────


def test_status_view_reports_missing_source_or_lineage_as_missing_lineage():
    sql = (ROOT / "sql" / "migrations"
           / "078_me_short_a_forward_only_outcome_lineage_v1.sql").read_text(
        encoding="utf-8")
    block = re.search(r"CASE\n(.*?)ELSE 'MATCHED_FINALIZED'", sql, re.S)
    assert block is not None
    case = block.group(1)
    first_when = case.index("WHEN")
    assert "MISSING_LINEAGE" in case[first_when:case.index("OUTCOME_UNMATURED")]
    assert "s.event_key IS NULL" in case[:case.index("OUTCOME_UNMATURED")]
    assert "l.event_key IS NULL" in case[:case.index("OUTCOME_UNMATURED")]
    # Order is the frozen precedence: missing chain first, never a fake
    # MATCHED_FINALIZED for an unlinked event.
    assert case.index("MISSING_LINEAGE") < case.index("OUTCOME_UNMATURED")
    assert case.index("OUTCOME_UNMATURED") < case.index("SOURCE_INCOMPLETE")
    assert "o.is_final = FALSE" in case
