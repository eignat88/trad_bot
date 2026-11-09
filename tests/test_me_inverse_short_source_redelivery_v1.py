"""PR #267 — Retry failed prospective source captures (Codex review P2).

Defect: the production in-memory dedup marks the candidate as seen on the
FIRST scan; if the research capture then failed with a PostgreSQL error,
the contour event row was never persisted — and every later scan dropped
the candidate as a duplicate, losing the source signal forever.

Fix (``app/scanners/orchestrator.py``): ``_me_inverse_short_source_events``
is published from the PRE-dedup candidates, so an unconfirmed capture is
re-delivered by every subsequent scan of the same candidate until it
lands.  Safety comes from the capture layer itself:

* deterministic ``event_key`` (same candidate → same key),
* one idempotent ``INSERT … ON CONFLICT DO NOTHING`` per delivery —
  an already-captured candidate replays as a silent ``DUPLICATE``
  (one row, never two),
* prospective-only: only candidates the CURRENT scan emits are ever
  published (no historical backfill), and the freeze/activation checks
  still run before every write,
* production dedup, scoring, gates and trading behaviour are untouched —
  they are all driven by the post-dedup ``unique`` list.

Fault-injection scenario: first scan → capture failure (PostgreSQL) →
second scan same candidate → capture success → third scan → no duplicate.

Forbidden (honoured here): no merge, no deploy, no production migrations,
no Paper activation, no VPS restarts.
"""
from __future__ import annotations

import pg8000.dbapi as pg_dbapi

from app.paper.me_long_v2_inverse_short_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_v1.capture import (
    CAPTURED,
    CAPTURE_DUPLICATE,
    CAPTURE_ERROR,
    capture_with_status,
)
from app.paper.me_long_v2_inverse_short_v1.identity import event_key
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_SCANNER,
)
from app.paper.me_long_v2_inverse_short_v1.store import EVENT_TABLE
from app.scanners.models import SetupCandidate, SetupState
from tests.test_me_long_v2_inverse_short_pg_reconnect_v1 import (
    _contour,
    _FakeServer,
)
from tests.test_me_long_v2_inverse_short_source_capture import (
    _ctx,
    _gate_policy,
    _orchestrator,
    _v2_long_candidate,
)

_EVENT_INSERT = f"INSERT INTO {EVENT_TABLE}"
_GATE = _gate_policy()
_SOURCE_EVENTS = "_me_inverse_short_source_events"


def _scan(orchestrator):
    """One full production scan with the frozen gate/expectancy config."""
    return orchestrator.scan_all_with_stats(
        _ctx(),
        min_avg_r=0.0,
        min_samples=10,
        gate_policy=_GATE,
        regime_filter=True,
        scanner_regime_whitelist={},
        trading_mode="paper",
    )


def _deliver(store, clock, candidate) -> str:
    """Exactly what scanner_runner does for one published source event."""
    return capture_with_status(
        store, candidate,
        capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"},
        clock=clock,
    )


class TestUnconfirmedSourceEventRedelivery:
    def test_capture_failure_is_redelivered_until_confirmed_without_duplicates(
        self,
    ):
        server = _FakeServer()
        store, _contour_engine, clock = _contour(server)
        candidate = _v2_long_candidate()
        orchestrator = _orchestrator(candidate)
        key = event_key(candidate)

        # ── Scan 1: published (first delivery), capture FAILS (PG error) ──
        _, stats1 = _scan(orchestrator)
        published1 = stats1[_SOURCE_EVENTS]
        assert len(published1) == 1
        assert published1[0].setup_id == candidate.setup_id
        server.fail_execute_with = pg_dbapi.IntegrityError(
            "source_event_insert_rejected",
        )
        server.fail_execute_match = _EVENT_INSERT
        server.fail_execute_times = 1
        status1 = _deliver(store, clock, candidate)
        assert status1 == CAPTURE_ERROR
        assert store.list_events() == []          # NOT confirmed anywhere

        # ── Scan 2: the SAME candidate must be published AGAIN (the fix) ──
        # Pre-fix the dedup filtered it out and the signal was lost forever.
        _, stats2 = _scan(orchestrator)
        published2 = stats2[_SOURCE_EVENTS]
        assert len(published2) == 1, (
            "unconfirmed source event must be re-delivered on the next scan"
        )
        assert published2[0].setup_id == candidate.setup_id
        # Production dedup / gates unchanged: the candidate is still known
        # to dedup, the scanner still ran, the gate still blocks V2 LONG
        # and zero setups were saved.
        assert orchestrator.dedup.contains_key(candidate) is True
        assert stats2[SOURCE_SCANNER]["candidates_found"] == 1
        assert stats2[SOURCE_SCANNER]["setups_saved"] == 0
        assert _GATE.evaluate(SOURCE_SCANNER, "LONG", "TREND_UP").allowed is False

        # Capture now SUCCEEDS — exactly one row, deterministic key:
        status2 = _deliver(store, clock, candidate)
        assert status2 == CAPTURED
        events = store.list_events()
        assert len(events) == 1
        assert events[0].event_key == key

        # ── Scan 3: delivered again — DUPLICATE, never a second row ──
        _, stats3 = _scan(orchestrator)
        assert len(stats3[_SOURCE_EVENTS]) == 1
        status3 = _deliver(store, clock, candidate)
        assert status3 == CAPTURE_DUPLICATE
        assert store.list_events() == events       # identical, ONE row

        # Repeated delivery cycles stay stable (no drift, no duplicates):
        for _ in range(3):
            _, stats_n = _scan(orchestrator)
            assert len(stats_n[_SOURCE_EVENTS]) == 1
            assert _deliver(store, clock, candidate) == CAPTURE_DUPLICATE
            assert store.list_events() == events

        # Redelivery never moved the event out of its captured state:
        assert events[0].status == "CAPTURED"

    def test_non_source_candidates_are_never_published(self):
        """Only ORIGINAL V2 LONG source candidates reach the delivery list."""
        base = _v2_long_candidate()
        me_short = SetupCandidate(
            scanner_name=base.scanner_name,
            scanner_version=base.scanner_version,
            symbol=base.symbol,
            direction="SHORT",          # the contour only takes V2 LONG
            entry_timeframe=base.entry_timeframe,
            detected_at=base.detected_at,
            setup_started_at=base.setup_started_at,
            signal_candle_open_time=base.signal_candle_open_time,
            reference_price=base.reference_price,
            entry_zone_low=base.entry_zone_low,
            entry_zone_high=base.entry_zone_high,
            invalidation_price=base.invalidation_price,
            target_1=base.target_1,
            score=base.score,
            market_regime=base.market_regime,
            state=SetupState.SETUP_READY,
            features=dict(base.features or {}),
        )
        other_scanner = SetupCandidate(
            scanner_name="BREAKOUT_RETEST",
            scanner_version=base.scanner_version,
            symbol=base.symbol,
            direction="LONG",
            entry_timeframe=base.entry_timeframe,
            detected_at=base.detected_at,
            setup_started_at=base.setup_started_at,
            signal_candle_open_time=base.signal_candle_open_time,
            reference_price=base.reference_price,
            entry_zone_low=base.entry_zone_low,
            entry_zone_high=base.entry_zone_high,
            invalidation_price=base.invalidation_price,
            target_1=base.target_1,
            score=base.score,
            market_regime=base.market_regime,
            state=SetupState.SETUP_READY,
            features=dict(base.features or {}),
        )
        for candidate in (me_short, other_scanner):
            orchestrator = _orchestrator(candidate)
            _, stats = _scan(orchestrator)
            assert stats[_SOURCE_EVENTS] == []
            # …while the candidate itself flows through the normal
            # production pipeline (scan ran, nothing crashed):
            scanner_stats = stats.get(candidate.scanner_name)
            if scanner_stats is not None:
                assert scanner_stats["errors_count"] == 0
