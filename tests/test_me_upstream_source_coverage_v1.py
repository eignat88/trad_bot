"""Issue #305 — upstream source coverage reconciler tests.

Covers the read-only ``upstream source_event ↔ dds.me_long_v2_inverse_short_v3_event``
reconciliation semantics (acceptance criteria 9-12):

* COVERAGE_PASS only with sufficient evidence (criterion 9);
* upstream-only events -> COVERAGE_LAG (within grace) or COVERAGE_GAP
  (confirmed after grace) (criterion 10);
* unknown activity period / no frozen activation / capture errors ->
  SOURCE_COVERAGE_UNKNOWN (criterion 11);
* incomplete lineage / rejected events are excluded from the valid cohort
  and surface as INTEGRITY_CONFLICT (criterion 12).

No PostgreSQL is required: the SQL entry point is exercised through a
scripted fake DB-API connection; the matching core and the verdict logic
are pure functions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.research.me_upstream_source_v1.contracts import build_source_event
from app.research.me_upstream_source_v1.coverage import (
    COVERAGE_GAP,
    COVERAGE_LAG,
    COVERAGE_PASS,
    INTEGRITY_CONFLICT,
    REASON_CAPTURE_BLOCKED,
    REASON_CAPTURE_ERRORS,
    REASON_CONFIRMED_GAP,
    REASON_LINEAGE_MISMATCH,
    REASON_MISSING_FREEZE_TS,
    REASON_NO_MATCHED_EVENTS,
    REASON_NO_UPSTREAM_ACTIVITY,
    REASON_NO_V3_ACTIVITY,
    REASON_NOT_ACTIVATED,
    REASON_REJECTED_EVENTS,
    REASON_SUFFICIENT_EVIDENCE,
    REASON_V3_DUPLICATE_IDENTITY,
    REASON_WITHIN_GRACE,
    SOURCE_COVERAGE_UNKNOWN,
    evaluate_coverage,
    match_ledgers,
    reconcile_coverage,
    save_coverage_audit,
)
from app.research.me_upstream_source_v1.identity import (
    normalized_identity_values,
)
from app.research.me_upstream_source_v1.protocol import (
    EXPERIMENT_ID,
    protocol_fingerprint,
)
from app.scanners.models import SetupCandidate

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
FREEZE = datetime(2026, 11, 24, 0, 0, 0, tzinfo=timezone.utc)

# In production both ledgers capture the SAME SetupCandidate instance, so
# the fixture must share one stable setup_id across both sides.
FIXED_SETUP_ID = UUID("00000000-0000-4000-8000-000000000305")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candidate(
    *,
    symbol: str = "BTCUSDT",
    candle_ms: int = 1_700_000_000_000,
    reference_price: float = 100.0,
    score: float = 0.0,
    detected_at: datetime | None = None,
) -> SetupCandidate:
    return SetupCandidate(
        setup_id=FIXED_SETUP_ID,
        scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        scanner_version="1.0.0",
        symbol=symbol,
        direction="LONG",
        entry_timeframe="5m",
        detected_at=detected_at or (NOW - timedelta(seconds=30)),
        signal_candle_open_time=candle_ms,
        reference_price=reference_price,
        entry_zone_low=99.8,
        entry_zone_high=100.1,
        invalidation_price=97.5,
        target_1=103.0,
        score=score,
        features={
            "rsi_14": 55.0,
            "rsi_delta_3": 1.25,
            "entry_confirmation_rsi_rising": True,
            "volume_ratio": 0.5,
        },
    )


def _upstream_row(
    *,
    symbol: str = "BTCUSDT",
    candle_ms: int = 1_700_000_000_000,
    captured_at: datetime | None = None,
    detected_at: datetime | None = None,
    reference_price: float = 100.0,
    lineage=None,
) -> dict:
    event = build_source_event(
        _candidate(
            symbol=symbol,
            candle_ms=candle_ms,
            reference_price=reference_price,
            detected_at=detected_at,
        ),
        activation_freeze_ts=FREEZE,
        captured_at=captured_at or NOW,
    )
    return {
        "source_event_key": event.source_event_key,
        "source_identity": event.source_identity,
        "symbol": event.symbol,
        "signal_time": event.signal_time,
        "captured_at": event.captured_at,
        "created_at": event.captured_at,
        "lineage": lineage if lineage is not None else event.lineage,
    }


def _v3_row(
    *,
    symbol: str = "BTCUSDT",
    candle_ms: int = 1_700_000_000_000,
    created_at: datetime | None = None,
    detected_at: datetime | None = None,
    reference_price: float = 100.0,
    lineage_overrides: dict | None = None,
) -> dict:
    """A V3 ledger row as the reconciler sees it (column values)."""
    event = build_source_event(
        _candidate(
            symbol=symbol,
            candle_ms=candle_ms,
            reference_price=reference_price,
            detected_at=detected_at,
        ),
        activation_freeze_ts=FREEZE,
        captured_at=created_at or NOW,
    )
    lineage = dict(event.lineage)
    lineage.update(lineage_overrides or {})
    return {
        "event_key": "v3-" + event.source_event_key[:16],
        "symbol": symbol,
        "signal_time": event.signal_time,
        "captured_at": created_at or NOW,
        "created_at": created_at or NOW,
        "source_scanner": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        "source_direction": "LONG",
        "entry_timeframe": "5m",
        "signal_candle_open_time": candle_ms,
        "lineage": lineage,
    }


# ---------------------------------------------------------------------------
# Pure matching core
# ---------------------------------------------------------------------------

class TestMatchLedgers:
    def test_matched_pair_counts_as_matched(self):
        up = _upstream_row()
        v3 = _v3_row()
        counts = match_ledgers(
            [up], [v3], window_start=FREEZE, as_of=NOW, grace_seconds=900,
        )
        assert counts["matched"] == 1
        assert counts["upstream_only"] == 0
        assert counts["v3_only"] == 0
        assert counts["lineage_mismatches"] == 0

    def test_upstream_only_fresh_is_lag(self):
        counts = match_ledgers(
            [_upstream_row(captured_at=NOW - timedelta(seconds=10))],
            [],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["upstream_only_lag"] == 1
        assert counts["upstream_only_gap"] == 0

    def test_upstream_only_stale_is_gap(self):
        counts = match_ledgers(
            [_upstream_row(captured_at=NOW - timedelta(hours=3))],
            [],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["upstream_only_gap"] == 1
        assert counts["upstream_only_lag"] == 0

    def test_v3_only_stale_is_gap_and_fresh_is_lag(self):
        counts = match_ledgers(
            [],
            [
                _v3_row(created_at=NOW - timedelta(hours=3)),
                _v3_row(symbol="ETHUSDT", created_at=NOW - timedelta(seconds=5)),
            ],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["v3_only_gap"] == 1
        assert counts["v3_only_lag"] == 1

    def test_grace_boundary_exactly_at_cutoff_is_gap(self):
        counts = match_ledgers(
            [_upstream_row(captured_at=NOW - timedelta(seconds=900))],
            [],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["upstream_only_gap"] == 1

    def test_different_symbol_never_matches(self):
        counts = match_ledgers(
            [_upstream_row(symbol="BTCUSDT")],
            [_v3_row(symbol="ETHUSDT")],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["matched"] == 0
        assert counts["upstream_only"] == 1
        assert counts["v3_only"] == 1

    def test_snapshot_drift_on_volatile_fact_is_not_a_mismatch(self):
        # Two first-captures of one identity can observe different
        # re-detections while the candle forms: price/geometry/feature drift
        # is EXPECTED variance — diagnostic snapshot drift, never an
        # integrity mismatch (Codex review P1).
        counts = match_ledgers(
            [_upstream_row(reference_price=100.0)],
            [_v3_row(reference_price=101.0)],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["matched"] == 1
        assert counts["lineage_mismatches"] == 0
        assert counts["snapshot_drifts"] == 1

    def test_lineage_mismatch_on_immutable_source_field(self):
        # Drift on the IMMUTABLE SOURCE fields (scanner provenance of the
        # same identity) is the real integrity mismatch.
        counts = match_ledgers(
            [_upstream_row()],
            [_v3_row(lineage_overrides={
                "source_scanner_version": "9.9.9",
            })],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["matched"] == 1
        assert counts["lineage_mismatches"] == 1

    def test_no_lineage_mismatch_for_scoring_side_differences(self):
        # source_score / source_state / source_market_regime / source_reasons
        # / capture_* are rewritten by the later pipeline or are capture-side
        # metadata — they are NOT comparable lineage facts.
        overrides = {
            "source_score": 999.0,
            "source_state": "SETUP_READY",
            "source_market_regime": "RANGING",
            "source_reasons": ["totally different"],
            "capture_route": "PRE_FILTER_SOURCE",
            "capture_independent_of": ["direction_gate"],
            "capture_protocol_version": "9.9.9",
            "capture_protocol_fingerprint": "deadbeef",
        }
        counts = match_ledgers(
            [_upstream_row()],
            [_v3_row(lineage_overrides=overrides)],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["lineage_mismatches"] == 0
        assert counts["snapshot_drifts"] == 0

    def test_no_lineage_mismatch_for_redelivery_variance(self):
        # A redelivery of the same signal (fresh setup_id / detected_at) is
        # expected variance: it must never surface as a lineage mismatch.
        overrides = {
            "source_setup_id": "some-other-uuid",
            "source_signal_time": "2026-11-25T12:05:00+00:00",
            "source_state": "SETUP_READY",
        }
        counts = match_ledgers(
            [_upstream_row()],
            [_v3_row(lineage_overrides=overrides)],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["matched"] == 1
        assert counts["lineage_mismatches"] == 0
        assert counts["snapshot_drifts"] == 0

    def test_v3_duplicate_identity_is_counted(self):
        counts = match_ledgers(
            [],
            [_v3_row(), _v3_row(created_at=NOW - timedelta(seconds=1))],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["v3_duplicate_identities"] == 1

    def test_rows_outside_confirmed_activity_window_are_excluded(self):
        # The audit only uses the intersection of confirmed activity
        # periods, judged on the ORIGINAL EVENT time (signal_time): a V3
        # event from before the upstream capture's confirmed activity is
        # reported as outside-window, never as a coverage gap.
        counts = match_ledgers(
            [_upstream_row(detected_at=NOW - timedelta(seconds=30))],
            [_v3_row(detected_at=NOW - timedelta(days=2))],
            window_start=NOW - timedelta(hours=1),
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["outside_window_v3"] == 1
        assert counts["v3_only"] == 0
        assert counts["upstream_only"] == 1  # its in-window twin is unmatched

    def test_ingestion_skew_between_ledgers_never_excludes_rows(self):
        # Regression (VPS 20/22): the same event ingested at different wall
        # clock moments (upstream captured late, V3 captured promptly) must
        # stay matchable — ingestion time never shrinks the scope.
        counts = match_ledgers(
            [_upstream_row(captured_at=NOW)],  # late ingestion
            [_v3_row(created_at=NOW - timedelta(minutes=5))],  # early
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["matched"] == 1
        assert counts["outside_window_upstream"] == 0
        assert counts["outside_window_v3"] == 0
        assert counts["lineage_mismatches"] == 0

    def test_delayed_upstream_recovery_still_matches(self):
        # The upstream capture missed its first delivery and only landed the
        # event on a later re-delivery (10 min after detection) while V3 got
        # it in the original cycle: the pair must MATCH, not split into
        # false lag/gap verdicts.
        up = _upstream_row(
            captured_at=NOW - timedelta(seconds=30) + timedelta(minutes=10),
        )
        counts = match_ledgers(
            [up],
            [_v3_row(created_at=NOW - timedelta(seconds=30))],
            window_start=FREEZE,
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["matched"] == 1
        assert counts["upstream_only"] == 0
        assert counts["v3_only"] == 0

    def test_ingestion_skew_alone_is_never_outside_window(self):
        # Old ingestion times with in-window EVENT times are in scope: the
        # window follows the signal, not the delivery.
        counts = match_ledgers(
            [_upstream_row(captured_at=NOW - timedelta(days=2),
                           detected_at=NOW - timedelta(seconds=30))],
            [_v3_row(created_at=NOW - timedelta(days=2),
                     detected_at=NOW - timedelta(seconds=30))],
            window_start=NOW - timedelta(hours=1),
            as_of=NOW,
            grace_seconds=900,
        )
        assert counts["outside_window_upstream"] == 0
        assert counts["outside_window_v3"] == 0
        assert counts["matched"] == 1

    def test_delivery_latency_stats(self):
        up = _upstream_row(captured_at=NOW - timedelta(seconds=42))
        up["signal_time"] = NOW - timedelta(seconds=102)
        counts = match_ledgers(
            [up], [], window_start=FREEZE, as_of=NOW, grace_seconds=900,
        )
        assert counts["max_delivery_latency_seconds"] == 60.0
        assert counts["avg_delivery_latency_seconds"] == 60.0


# ---------------------------------------------------------------------------
# Deterministic verdict logic
# ---------------------------------------------------------------------------

class TestEvaluateCoverage:
    def _counts(self, **overrides):
        counts = {
            "upstream_activity_known": True,
            "v3_activity_known": True,
            "window_non_empty": True,
            "matched": 2,
            "upstream_only_lag": 0,
            "upstream_only_gap": 0,
            "v3_only_lag": 0,
            "v3_only_gap": 0,
            "lineage_mismatches": 0,
            "v3_duplicate_identities": 0,
            "rejected_events": 0,
            "capture_errors": 0,
        }
        counts.update(overrides)
        return counts

    def test_pass_only_with_sufficient_evidence(self):
        status, reason = evaluate_coverage(
            activation_present=True,
            freeze_present=True,
            capture_state="HEALTHY",
            counts=self._counts(),
        )
        assert status == COVERAGE_PASS
        assert reason == REASON_SUFFICIENT_EVIDENCE

    def test_pass_forbidden_without_matched_events(self):
        status, reason = evaluate_coverage(
            activation_present=True,
            freeze_present=True,
            capture_state=None,
            counts=self._counts(matched=0),
        )
        assert status == SOURCE_COVERAGE_UNKNOWN
        assert reason == REASON_NO_MATCHED_EVENTS

    def test_lag_for_fresh_unmatched(self):
        status, reason = evaluate_coverage(
            activation_present=True,
            freeze_present=True,
            capture_state=None,
            counts=self._counts(upstream_only_lag=1),
        )
        assert status == COVERAGE_LAG
        assert reason == REASON_WITHIN_GRACE

    def test_gap_for_confirmed_misses(self):
        status, reason = evaluate_coverage(
            activation_present=True,
            freeze_present=True,
            capture_state=None,
            counts=self._counts(v3_only_gap=1),
        )
        assert status == COVERAGE_GAP
        assert reason == REASON_CONFIRMED_GAP

    def test_gap_precedence_over_capture_errors(self):
        status, _ = evaluate_coverage(
            activation_present=True,
            freeze_present=True,
            capture_state=None,
            counts=self._counts(upstream_only_gap=1, capture_errors=5),
        )
        assert status == COVERAGE_GAP

    def test_capture_errors_make_coverage_unknown(self):
        status, reason = evaluate_coverage(
            activation_present=True,
            freeze_present=True,
            capture_state=None,
            counts=self._counts(capture_errors=1),
        )
        assert status == SOURCE_COVERAGE_UNKNOWN
        assert reason == REASON_CAPTURE_ERRORS

    def test_unknown_without_activation_or_freeze(self):
        assert evaluate_coverage(
            activation_present=False, freeze_present=False,
            capture_state=None, counts=self._counts(),
        ) == (SOURCE_COVERAGE_UNKNOWN, REASON_NOT_ACTIVATED)
        assert evaluate_coverage(
            activation_present=True, freeze_present=False,
            capture_state=None, counts=self._counts(),
        ) == (SOURCE_COVERAGE_UNKNOWN, REASON_MISSING_FREEZE_TS)

    def test_unknown_without_activity_periods(self):
        assert evaluate_coverage(
            activation_present=True, freeze_present=True, capture_state=None,
            counts=self._counts(upstream_activity_known=False),
        ) == (SOURCE_COVERAGE_UNKNOWN, REASON_NO_UPSTREAM_ACTIVITY)
        assert evaluate_coverage(
            activation_present=True, freeze_present=True, capture_state=None,
            counts=self._counts(v3_activity_known=False),
        ) == (SOURCE_COVERAGE_UNKNOWN, REASON_NO_V3_ACTIVITY)

    def test_conflict_on_blocked_state_lineage_duplicates_rejections(self):
        assert evaluate_coverage(
            activation_present=True, freeze_present=True,
            capture_state="BLOCKED", counts=self._counts(),
        ) == (INTEGRITY_CONFLICT, REASON_CAPTURE_BLOCKED)
        assert evaluate_coverage(
            activation_present=True, freeze_present=True, capture_state=None,
            counts=self._counts(lineage_mismatches=1),
        ) == (INTEGRITY_CONFLICT, REASON_LINEAGE_MISMATCH)
        assert evaluate_coverage(
            activation_present=True, freeze_present=True, capture_state=None,
            counts=self._counts(v3_duplicate_identities=1),
        ) == (INTEGRITY_CONFLICT, REASON_V3_DUPLICATE_IDENTITY)
        assert evaluate_coverage(
            activation_present=True, freeze_present=True, capture_state=None,
            counts=self._counts(rejected_events=1),
        ) == (INTEGRITY_CONFLICT, REASON_REJECTED_EVENTS)

    def test_conflict_precedence_over_gap(self):
        status, _ = evaluate_coverage(
            activation_present=True, freeze_present=True, capture_state=None,
            counts=self._counts(upstream_only_gap=3, lineage_mismatches=1),
        )
        assert status == INTEGRITY_CONFLICT


# ---------------------------------------------------------------------------
# SQL entry point over a scripted fake DB-API connection
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, script, log):
        self._script = script
        self._log = log
        self._rows: list[tuple] = []
        self.rowcount = -1

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        self._log.append((text, tuple(params)))
        for marker, rows in self._script:
            if marker in text:
                self._rows = list(rows(params) if callable(rows) else rows)
                return
        self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, script):
        self.log: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0
        self._cursor = _FakeCursor(script, self.log)

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _up_tuple(row: dict) -> tuple:
    """Row shape returned by the upstream SELECT (tuple cursor)."""
    return (
        row["source_event_key"], row["source_identity"], row["symbol"],
        row["signal_time"], row["captured_at"], row["created_at"],
        row["lineage"],
    )


def _v3_tuple(row: dict) -> tuple:
    """Row shape returned by the V3 SELECT (tuple cursor)."""
    return (
        row["event_key"], row["symbol"], row["signal_time"],
        row["captured_at"], row["created_at"], row["source_scanner"],
        row["source_direction"], row["entry_timeframe"],
        row["signal_candle_open_time"], row["lineage"],
    )


def _script(
    *,
    activation=(("RUNNING", FREEZE),),
    state=None,
    upstream=(),
    v3=(),
    health_min=None,
    health_sums=(0, 0),
    rejected=(),
):
    def _rows(value):
        return () if value is None else value

    return [
        ("FROM research_me_upstream_coverage.activation", _rows(activation)),
        ("FROM research_me_upstream_coverage.capture_state_event", _rows(state)),
        (
            "FROM research_me_upstream_coverage.source_event",
            tuple(_up_tuple(r) for r in _rows(upstream)),
        ),
        (
            "FROM dds.me_long_v2_inverse_short_v3_event",
            tuple(_v3_tuple(r) for r in _rows(v3)),
        ),
        ("MIN(recorded_at)", ((health_min,),)),
        ("SUM(duplicates)", (health_sums,)),
        ("FROM research_me_upstream_coverage.rejected_event", _rows(rejected)),
    ]


def _reconcile(script, **kwargs):
    conn = _FakeConnection(script)
    return reconcile_coverage(conn, **kwargs), conn


class TestReconcileCoverageSql:
    def test_not_activated_is_unknown(self):
        result, _ = _reconcile(_script(activation=None), as_of=NOW)
        assert result["status"] == SOURCE_COVERAGE_UNKNOWN
        assert result["reason"] == REASON_NOT_ACTIVATED

    def test_missing_freeze_is_unknown(self):
        result, _ = _reconcile(_script(activation=(("RUNNING", None),)), as_of=NOW)
        assert result["status"] == SOURCE_COVERAGE_UNKNOWN
        assert result["reason"] == REASON_MISSING_FREEZE_TS

    def test_matched_events_pass(self):
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
            ),
            as_of=NOW,
        )
        assert result["matched"] == 1
        assert result["status"] == COVERAGE_PASS
        assert result["reason"] == REASON_SUFFICIENT_EVIDENCE
        # The documented limitation is attached to every verdict.
        assert "detector" in result["scope_note"]

    def test_upstream_only_event_diagnostics(self):
        # Upstream-only needs CONFIRMED V3 activity in the window to be a
        # lag/gap — a fully silent V3 side is SOURCE_COVERAGE_UNKNOWN
        # (unknown activity period), covered by its own test below.
        fresh, _ = _reconcile(
            _script(
                upstream=(
                    _upstream_row(captured_at=NOW - timedelta(seconds=30)),
                    _upstream_row(
                        symbol="ETHUSDT", captured_at=NOW - timedelta(hours=1),
                    ),
                ),
                v3=(
                    _v3_row(symbol="ETHUSDT", created_at=NOW - timedelta(hours=1)),
                ),
            ),
            as_of=NOW,
        )
        assert fresh["upstream_only_lag"] == 1
        assert fresh["matched"] == 1
        assert fresh["status"] == COVERAGE_LAG

        stale, _ = _reconcile(
            _script(
                upstream=(
                    _upstream_row(captured_at=NOW - timedelta(hours=2)),
                    _upstream_row(
                        symbol="ETHUSDT", captured_at=NOW - timedelta(hours=2),
                    ),
                ),
                v3=(
                    _v3_row(symbol="ETHUSDT", created_at=NOW - timedelta(hours=2)),
                ),
            ),
            as_of=NOW,
        )
        assert stale["upstream_only_gap"] == 1
        assert stale["status"] == COVERAGE_GAP

    def test_v3_only_event_diagnostics(self):
        result, _ = _reconcile(
            _script(
                v3=(_v3_row(created_at=NOW - timedelta(hours=2)),),
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=2)),),
                health_min=NOW - timedelta(hours=2),
            ),
            as_of=NOW,
        )
        # The upstream row matches nothing (different identity? no — same
        # identity means matched): use a distinct symbol for the V3-only row.
        assert result["matched"] == 1

        result, _ = _reconcile(
            _script(
                v3=(_v3_row(symbol="ETHUSDT", created_at=NOW - timedelta(hours=2)),),
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=2)),),
                health_min=NOW - timedelta(hours=2),
            ),
            as_of=NOW,
        )
        assert result["v3_only_gap"] == 1
        assert result["status"] == COVERAGE_GAP

    def test_no_upstream_activity_is_unknown(self):
        result, _ = _reconcile(
            _script(
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
                health_min=None,
            ),
            as_of=NOW,
        )
        assert result["status"] == SOURCE_COVERAGE_UNKNOWN
        assert result["reason"] == REASON_NO_UPSTREAM_ACTIVITY

    def test_no_v3_activity_is_unknown(self):
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
            ),
            as_of=NOW,
        )
        assert result["status"] == SOURCE_COVERAGE_UNKNOWN
        assert result["reason"] == REASON_NO_V3_ACTIVITY

    def test_capture_errors_are_observable_and_forbid_pass(self):
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
                health_sums=(0, 2),
            ),
            as_of=NOW,
        )
        assert result["capture_errors"] == 2
        assert result["status"] == SOURCE_COVERAGE_UNKNOWN
        assert result["reason"] == REASON_CAPTURE_ERRORS

    def test_duplicate_deliveries_are_reported_not_a_conflict(self):
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
                health_sums=(7, 0),
            ),
            as_of=NOW,
        )
        assert result["duplicate_deliveries"] == 7
        assert result["status"] == COVERAGE_PASS

    def test_rejected_events_conflict_but_pre_freeze_rejections_do_not(self):
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
                rejected=(("INCOMPLETE_FEATURES",),),
            ),
            as_of=NOW,
        )
        assert result["rejected_events"] == 1
        assert result["status"] == INTEGRITY_CONFLICT
        assert result["reason"] == REASON_REJECTED_EVENTS

        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
                rejected=(("PRE_FREEZE",), ("PRE_FREEZE",)),
            ),
            as_of=NOW,
        )
        assert result["rejected_events"] == 0
        assert result["status"] == COVERAGE_PASS

    def test_immutable_source_mismatch_is_conflict(self):
        # Same identity, DIFFERENT immutable source provenance -> the real
        # integrity mismatch -> INTEGRITY_CONFLICT (highest precedence).
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1),
                            lineage_overrides={
                                "source_scanner_version": "9.9.9",
                            }),),
            ),
            as_of=NOW,
        )
        assert result["lineage_mismatches"] == 1
        assert result["status"] == INTEGRITY_CONFLICT

    def test_volatile_snapshot_drift_is_not_a_conflict(self):
        # Price/geometry/feature drift between two first-captures of one
        # identity is expected forming-candle variance: diagnostic
        # ``snapshot_drifts`` only — never an INTEGRITY_CONFLICT.
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(reference_price=100.0,
                                        captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(reference_price=101.0,
                            created_at=NOW - timedelta(hours=1)),),
            ),
            as_of=NOW,
        )
        assert result["lineage_mismatches"] == 0
        assert result["snapshot_drifts"] == 1
        assert result["status"] != INTEGRITY_CONFLICT
        assert result["status"] == COVERAGE_PASS

    def test_blocked_capture_state_is_conflict(self):
        result, _ = _reconcile(
            _script(
                state=(("BLOCKED", "EVENT_KEY_LINEAGE_CONFLICT", {}, NOW),),
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
            ),
            as_of=NOW,
        )
        assert result["status"] == INTEGRITY_CONFLICT
        assert result["reason"] == REASON_CAPTURE_BLOCKED

    def test_reconciliation_is_read_only_and_persists_nothing_by_default(self):
        _, conn = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
            ),
            as_of=NOW,
        )
        assert conn.commits == 0
        assert all(
            text.lstrip().upper().startswith(("SELECT", "WITH"))
            for text, _ in conn.log
        ), conn.log

    def test_save_coverage_audit_persists_explicitly(self):
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
            ),
            as_of=NOW,
        )
        conn = _FakeConnection(_script())
        save_coverage_audit(conn, result)
        assert conn.commits == 1
        assert any(
            "INSERT INTO research_me_upstream_coverage.coverage_audit" in text
            for text, _ in conn.log
        )

    # --- regression: activity-window basis (VPS 20/22) -------------------

    def test_ingestion_skew_between_ledgers_still_matches(self):
        # EXACT VPS failure shape: upstream ingested at wall-clock now, the
        # V3 ledger ingested the SAME event 5 minutes earlier.  Activity
        # starts follow the EVENT time, so both rows are in scope and the
        # pair matches (was: matched=0 because window_start jumped to the
        # later first-ingestion and excluded the whole V3 side).
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW),),
                v3=(_v3_row(created_at=NOW - timedelta(minutes=5)),),
            ),
            as_of=NOW,
        )
        assert result["matched"] == 1
        assert result["outside_window_v3"] == 0
        assert result["status"] == COVERAGE_PASS

    def test_activity_start_follows_event_time_not_ingestion(self):
        result, _ = _reconcile(
            _script(
                upstream=(
                    _upstream_row(captured_at=NOW,
                                  detected_at=NOW - timedelta(minutes=4)),),
                v3=(_v3_row(created_at=NOW - timedelta(minutes=4),
                            detected_at=NOW - timedelta(minutes=4)),),
            ),
            as_of=NOW,
        )
        # Both activity starts are the shared event time, not the skewed
        # ingestion times NOW and NOW-4min.
        assert result["upstream_activity_start"] == NOW - timedelta(minutes=4)
        assert result["v3_activity_start"] == NOW - timedelta(minutes=4)
        assert result["window_start"] == NOW - timedelta(minutes=4)
        assert result["matched"] == 1

    def test_activity_mismatch_v3_event_before_window_is_not_a_gap(self):
        # Mismatched activity periods: the V3 side saw an event BEFORE the
        # upstream capture's first confirmed event.  That absence is not a
        # confirmed miss (upstream activity was not confirmed then): it must
        # be outside-window evidence, never a false COVERAGE_GAP — and the
        # in-window unmatched event must still become a gap after grace
        # (no over-exclusion / no weakened gap detection).
        old = NOW - timedelta(hours=3)
        result, _ = _reconcile(
            _script(
                upstream=(
                    _upstream_row(symbol="BTCUSDT",
                                  detected_at=NOW - timedelta(minutes=20),
                                  captured_at=NOW - timedelta(minutes=20)),),
                v3=(
                    _v3_row(symbol="ETHUSDT",
                            detected_at=old, created_at=old),
                    _v3_row(symbol="SOLUSDT",
                            detected_at=NOW - timedelta(minutes=18),
                            created_at=NOW - timedelta(minutes=18)),
                ),
            ),
            as_of=NOW,
        )
        assert result["outside_window_v3"] == 1
        assert result["v3_only_gap"] == 1  # the IN-window one is still a gap
        assert result["upstream_only_gap"] == 1  # in-window, past grace
        assert result["status"] == COVERAGE_GAP

    def test_out_of_window_rows_never_produce_pass_evidence(self):
        # Rows outside the audited window cannot manufacture a PASS: with
        # nothing in scope the verdict stays UNKNOWN (matched >= 1 is still
        # required) — the false-PASS protection is not weakened.
        old = NOW - timedelta(days=2)
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(detected_at=old, captured_at=old),),
                v3=(_v3_row(detected_at=old, created_at=old),),
            ),
            as_of=NOW,
        )
        assert result["matched"] == 0
        assert result["status"] != COVERAGE_PASS
        assert result["status"] == SOURCE_COVERAGE_UNKNOWN

    def test_delayed_v3_delivery_within_grace_is_lag_not_gap(self):
        # Delivery delay (the "delays accounted" rule): an event ingested
        # into V3 just now is still within grace -> COVERAGE_LAG, and only a
        # delivery older than grace confirms the gap.
        fresh = NOW - timedelta(seconds=30)
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(symbol="BTCUSDT",
                                        detected_at=fresh,
                                        captured_at=fresh),),
                v3=(_v3_row(symbol="ETHUSDT",
                            detected_at=fresh, created_at=fresh),),
            ),
            as_of=NOW,
        )
        assert result["upstream_only_lag"] == 1
        assert result["status"] == COVERAGE_LAG

    def test_invalid_grace_rejected(self):
        conn = _FakeConnection(_script())
        with pytest.raises(ValueError):
            reconcile_coverage(conn, as_of=NOW, grace_seconds=-1)


# ---------------------------------------------------------------------------
# Deployment-gap diagnostics (Issue #305 SOURCE_ONLY routing defect):
# the unverifiable interval is recorded explicitly and NEVER converted into
# verdict evidence — the freeze_ts and the COVERAGE_PASS formula stay intact.
# ---------------------------------------------------------------------------

class TestDeploymentGapDiagnostics:
    GAP = (FREEZE, NOW - timedelta(hours=1))

    def test_unverified_intervals_recorded_without_verdict_change(self):
        script = _script(
            upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
            v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
        )
        base, _ = _reconcile(script, as_of=NOW)
        with_gap, _ = _reconcile(
            script, as_of=NOW, unverified_intervals=[self.GAP],
        )
        # Verdicts are IDENTICAL — the gap is diagnostics, not evidence.
        assert (with_gap["status"], with_gap["reason"]) == (
            base["status"], base["reason"],
        )
        assert base["status"] == COVERAGE_PASS
        assert "unverified_intervals" not in base
        assert with_gap["unverified_intervals"] == [
            (FREEZE.isoformat(), (NOW - timedelta(hours=1)).isoformat()),
        ]

    def test_gap_absence_never_produces_pass_evidence(self):
        # An empty interval (no events at all — exactly the outage shape)
        # stays UNKNOWN even when the gap is declared: no SOURCE_VALIDATED-
        # style verdict can arise from "no errors".
        result, _ = _reconcile(
            _script(upstream=(), v3=()),
            as_of=NOW,
            unverified_intervals=[self.GAP],
        )
        assert result["matched"] == 0
        assert result["status"] == SOURCE_COVERAGE_UNKNOWN

    def test_unverified_intervals_are_persisted_in_audit_diagnostics(self):
        result, _ = _reconcile(
            _script(
                upstream=(_upstream_row(captured_at=NOW - timedelta(hours=1)),),
                v3=(_v3_row(created_at=NOW - timedelta(hours=1)),),
            ),
            as_of=NOW,
            unverified_intervals=[self.GAP],
        )
        conn = _FakeConnection(_script())
        save_coverage_audit(conn, result)
        diagnostics = conn.log[-1][1][-1]
        assert "unverified_intervals" in diagnostics
        assert FREEZE.isoformat() in diagnostics

    @pytest.mark.parametrize("bad", [
        [(NOW, NOW)],                          # empty interval
        [(NOW, NOW - timedelta(hours=1))],     # reversed
        [("a", "b")],                          # not datetimes
        [(NOW,)],                              # wrong arity
        [(NOW, NOW, NOW)],                     # wrong arity
        [1, 2],                                # not interval items
    ])
    def test_malformed_intervals_rejected(self, bad):
        conn = _FakeConnection(_script())
        with pytest.raises(ValueError):
            reconcile_coverage(
                conn, as_of=NOW, unverified_intervals=bad,
            )

    def test_no_source_validated_verdict_exists(self):
        # "No errors" must never equal validated coverage: the vocabulary
        # has no SOURCE_VALIDATED verdict and PASS requires matched >= 1.
        import app.research.me_upstream_source_v1.coverage as mod
        import app.research.me_upstream_source_v1.contracts as contracts_mod

        values = {
            v for v in vars(mod).values()
            if isinstance(v, str)
        } | {
            v for v in vars(contracts_mod).values()
            if isinstance(v, str)
        }
        assert "SOURCE_VALIDATED" not in values


# ---------------------------------------------------------------------------
# Cross-ledger identity compatibility (the V3 side match key)
# ---------------------------------------------------------------------------

class TestCrossLedgerIdentity:
    def test_v3_side_identity_equals_upstream_identity(self):
        event = build_source_event(
            _candidate(), activation_freeze_ts=FREEZE, captured_at=NOW,
        )
        v3_style = normalized_identity_values(
            scanner="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
            symbol="BTCUSDT",
            direction="LONG",
            entry_timeframe="5m",
            signal_candle_open_ms=1_700_000_000_000,
        )
        assert event.source_identity == v3_style

    def test_activation_fingerprint_dependency_is_outside_identity(self):
        # The event key never embeds the capture protocol fingerprint or
        # version: the same signal captured under a future protocol still
        # hashes to the same upstream identity.
        event = build_source_event(
            _candidate(), activation_freeze_ts=FREEZE, captured_at=NOW,
        )
        assert protocol_fingerprint() not in event.source_event_key
        assert EXPERIMENT_ID not in event.source_event_key
        assert "1.0.0" not in event.source_event_key
