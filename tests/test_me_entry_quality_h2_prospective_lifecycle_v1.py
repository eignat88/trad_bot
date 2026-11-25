"""Issue #312 — H2 prospective runtime: lifecycle + variant tests (FR-03/FR-04).

Reuses the merged PR #311 engine unchanged; these tests cover the acceptance
criteria that concern the RSI confirmation and the delayed-entry lifecycle:

* 6  — a candle gap is ``INCOMPLETE_COVERAGE`` (never EXPIRED, never
       CONFIRMED_DELAYED);
* 7  — 119/120 canonical candles can never yield an eligible H2 result;
* 8  — 120/120 canonical candles allow the RSI calculation;
* 9  — first RSI decline -> ``H2_RSI_TURN_1`` confirms per the rules;
* 10 — two consecutive declines -> ``H2_RSI_TURN_2`` confirms per the rules;
* 11 — confirmed RSI peak -> ``H2_RSI_PEAK_REVERSAL`` on the causal ASOF;
* 12 — window expiry -> ``EXPIRED`` with NO fictional trade outcome;
plus the lifecycle rules: missing data never fabricates EXPIRED/CONFIRMED,
transient coverage defects finalize only after the bounded retry budget, and
terminal states are locked forever.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h2_prospective_v1.lifecycle import (
    EvaluationObservation,
    decide_next_state,
    is_terminal,
)
from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    STATE_CONFIRMED_DELAYED,
    STATE_EXPIRED,
    STATE_INCOMPLETE_COVERAGE,
    STATE_INVALID_SOURCE,
    STATE_NOT_READY,
    STATE_PENDING_CONFIRMATION,
    is_permanent_coverage_reason,
)
from app.research.me_entry_quality_h2_v1.candle_provider import (
    CANDLE_5M_MS,
    build_source_identity,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_OFFLINE_SNAPSHOT,
)
from app.research.me_entry_quality_h2_v1.pipeline import run_h2_event
from app.research.me_entry_quality_h2_v1.result import (
    ENTRY_PRICE_UNDETERMINED,
    H2Policy,
    STATE_CONFIRMED_DELAYED as H2_CONFIRMED,
    STATE_EXPIRED as H2_EXPIRED,
    STATE_PENDING_CONFIRMATION as H2_PENDING,
    VARIANT_PEAK_REVERSAL,
    VARIANT_TURN_1,
    VARIANT_TURN_2,
)
from app.research.me_entry_quality_h2_v1.rsi_confirmation import PEAK_RULE_V1

BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MS = CANDLE_5M_MS

# crafted closes (identical recipe to the PR #311 variant tests): RSI rises
# to a local peak at bucket 19 and reverses afterwards
CRAFTED = (
    [100.0 + i for i in range(17)]
    + [118.0, 114.0, 116.5, 113.0, 111.5, 115.0]
)
RSI_AT = {17: 100.0, 18: 75.3334, 19: 78.8455, 20: 64.9109,
          21: 60.0154, 22: 66.3857}
WARMUP = 106

# strictly rising: Wilder avg_loss stays 0 -> RSI pinned at 100 (no decline)
RISING = [100.0 + 0.5 * i for i in range(23)]

# signal mid-candle bucket 18 -> O_0 = 17, O_1 = 18 ...
SIGNAL_MS = BASE_MS + 18 * MS + 60_000


def warmup_closes(n):
    return [99.0 - 0.5 * (n - 1 - i) for i in range(n)]


def rows_from_closes(closes, warmup=WARMUP):
    placed = [
        (-warmup + j, c) for j, c in enumerate(warmup_closes(warmup))
    ] + [(i, c) for i, c in enumerate(closes)]
    rows = []
    for bucket, close in placed:
        open_ms = BASE_MS + bucket * MS
        prev = rows[-1]["close"] if rows else float(close)
        rows.append({
            "open_ms": open_ms,
            "open": prev,
            "high": max(prev, float(close)) + 0.5,
            "low": min(prev, float(close)) - 0.5,
            "close": float(close),
            "volume": 1.0,
        })
    return rows


def event_row(signal_ms=SIGNAL_MS, *, key="evt-1"):
    from datetime import datetime, timedelta, timezone
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    signal = epoch + timedelta(milliseconds=signal_ms)
    return {
        "source_event_key": key,
        "source_identity": f"ident-{signal_ms}",
        "symbol": "BTCUSDT",
        "lineage_hash": "ab" * 32,
        "signal_time": signal,
        "captured_at": signal + timedelta(seconds=20),
        "activation_freeze_ts": signal - timedelta(hours=1),
        "source_fingerprint": "fp-test-1",
        "source_reference_price": 1234.5,
        "features": {
            "rsi_14": 71.25,
            "rsi_delta_3": 4.5,
            "entry_confirmation_rsi_rising": True,
        },
    }


def run(closes, policies, asof_ms, *, warmup=WARMUP, rows=None):
    source_rows = rows if rows is not None else rows_from_closes(closes, warmup=warmup)
    identity = build_source_identity(
        SOURCE_KIND_OFFLINE_SNAPSHOT, "crafted", source_rows
    )
    return run_h2_event(
        event_row(),
        rows_5m=source_rows,
        source_identity=identity,
        policies=policies,
        evaluation_asof_ms=asof_ms,
    )


def policy(variant, window, peak_rule=None):
    return H2Policy(
        variant_id=variant, observation_window=window, peak_rule=peak_rule
    )


TURN1_W3 = policy(VARIANT_TURN_1, 3)
TURN2_W6 = policy(VARIANT_TURN_2, 6)
PEAK_W3 = policy(VARIANT_PEAK_REVERSAL, 3, peak_rule=PEAK_RULE_V1)


class TestTurn1:
    def test_first_decline_confirms(self):
        # criterion 9: RSI(O_1) < RSI(O_0) -> confirmed at the O_1 close
        outcome = run(CRAFTED, [TURN1_W3], BASE_MS + 19 * MS)
        result = outcome.results[0]
        assert result.status == H2_CONFIRMED == STATE_CONFIRMED_DELAYED
        assert result.confirmation_candle_open_ms == BASE_MS + 18 * MS
        assert result.confirmation_time_ms == BASE_MS + 19 * MS
        assert result.h2_rsi_at_confirmation == pytest.approx(RSI_AT[18], abs=1e-3)
        assert result.entry_delay_ms == BASE_MS + 19 * MS - SIGNAL_MS

    def test_pending_before_first_decline_is_observed(self):
        # after the signal but before the O_1 candle closes
        outcome = run(CRAFTED, [TURN1_W3], SIGNAL_MS + 1_000)
        result = outcome.results[0]
        assert result.status == H2_PENDING == STATE_PENDING_CONFIRMATION


class TestTurn2:
    def test_two_consecutive_declines_confirm(self):
        # criterion 10: first k=4 (buckets 20 and 21 both declining)
        outcome = run(CRAFTED, [TURN2_W6], BASE_MS + 22 * MS)
        result = outcome.results[0]
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.confirmation_candle_open_ms == BASE_MS + 21 * MS
        assert result.h2_rsi_at_confirmation == pytest.approx(RSI_AT[21], abs=1e-3)

    def test_single_decline_does_not_confirm(self):
        outcome = run(CRAFTED, [TURN2_W6], BASE_MS + 19 * MS)
        assert outcome.results[0].status == STATE_PENDING_CONFIRMATION


class TestPeakReversal:
    def test_confirmed_peak_reversal_on_causal_asof(self):
        # criterion 11: peak at bucket 19 recognized at the O_3 close
        outcome = run(CRAFTED, [PEAK_W3], BASE_MS + 21 * MS)
        result = outcome.results[0]
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.confirmation_candle_open_ms == BASE_MS + 20 * MS
        assert result.h2_rsi_at_confirmation == pytest.approx(RSI_AT[20], abs=1e-3)

    def test_peak_not_recognizable_before_right_neighbour_closes(self):
        # causal ASOF: one candle earlier the reversal is NOT yet knowable
        outcome = run(CRAFTED, [PEAK_W3], BASE_MS + 20 * MS)
        assert outcome.results[0].status == STATE_PENDING_CONFIRMATION

    def test_missing_peak_rule_is_not_ready_never_approximated(self):
        outcome = run(CRAFTED, [policy(VARIANT_PEAK_REVERSAL, 3)],
                      BASE_MS + 21 * MS)
        result = outcome.results[0]
        assert result.status == STATE_NOT_READY
        decision = decide_next_state(
            EvaluationObservation(
                current_state=STATE_PENDING_CONFIRMATION,
                pipeline_status=result.status,
            )
        )
        assert decision.new_state == STATE_NOT_READY and decision.terminal


class TestExpiry:
    def test_window_exhausted_expires_without_fictional_trade(self):
        # criterion 12: rising series -> no decline -> EXPIRED, and NO
        # fictional execution / outcome of any trade
        outcome = run(RISING, [TURN1_W3], BASE_MS + 21 * MS)
        result = outcome.results[0]
        assert result.status == H2_EXPIRED == STATE_EXPIRED
        assert result.expiry_reason == "OBSERVATION_WINDOW_EXHAUSTED_NO_CONFIRMATION"
        # criterion 12: NO fictional trade outcome of any kind
        assert result.entry_candidate_price is None
        assert result.entry_reference_close_price is None
        assert result.entry_price_basis in ("", ENTRY_PRICE_UNDETERMINED)
        assert result.h2_rsi_at_confirmation is None
        decision = decide_next_state(
            EvaluationObservation(
                current_state=STATE_PENDING_CONFIRMATION,
                pipeline_status=result.status,
                observed_window_candles=result.observed_window_candles,
                observation_window=3,
                window_elapsed=True,
            )
        )
        assert decision.new_state == STATE_EXPIRED and decision.terminal

    def test_expired_refused_when_window_not_fully_observed(self):
        decision = decide_next_state(
            EvaluationObservation(
                current_state=STATE_PENDING_CONFIRMATION,
                pipeline_status=STATE_EXPIRED,
                observed_window_candles=2,
                observation_window=3,
                window_elapsed=False,
            )
        )
        assert decision.new_state is None
        assert not decision.terminal


class TestCoverageFailures:
    def test_candle_gap_is_incomplete_coverage_never_expired(self):
        # criterion 6: one missing bucket -> INCOMPLETE_COVERAGE; the
        # lifecycle may NEVER fabricate EXPIRED / CONFIRMED_DELAYED
        rows = rows_from_closes(CRAFTED)
        rows = [r for r in rows if r["open_ms"] != BASE_MS - 50 * MS]
        outcome = run(CRAFTED, [TURN1_W3], BASE_MS + 21 * MS, rows=rows)
        for result in outcome.results:
            assert result.status == STATE_INCOMPLETE_COVERAGE
            assert result.status != STATE_EXPIRED
            assert result.entry_candidate_price is None
        result = outcome.results[0]
        assert result.reason == "CANDLE_GAP"
        assert is_permanent_coverage_reason(result.reason)
        decision = decide_next_state(
            EvaluationObservation(
                current_state=STATE_PENDING_CONFIRMATION,
                pipeline_status=result.status,
                coverage_reason=result.reason,
                observed_window_candles=result.observed_window_candles,
                observation_window=3,
                window_elapsed=True,
                coverage_attempts=1,
                coverage_finalize_after_attempts=3,
            )
        )
        assert decision.new_state == STATE_INCOMPLETE_COVERAGE
        assert decision.terminal

    def test_119_of_120_canonical_candles_never_confirms(self):
        # criterion 7: the canonical window is a cohort-eligibility gate
        outcome = run(CRAFTED, [TURN1_W3], BASE_MS + 19 * MS, warmup=101)
        result = outcome.results[0]
        assert result.status == STATE_INCOMPLETE_COVERAGE
        assert result.reason == "CANONICAL_WINDOW_INCOMPLETE"
        assert result.h2_rsi_at_confirmation is None

    def test_120_of_120_canonical_candles_allow_rsi(self):
        # criterion 8
        outcome = run(CRAFTED, [TURN1_W3], BASE_MS + 19 * MS, warmup=106)
        result = outcome.results[0]
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.h2_rsi_at_confirmation == pytest.approx(RSI_AT[18], abs=1e-3)

    def test_transient_coverage_needs_bounded_attempts_to_finalize(self):
        obs = EvaluationObservation(
            current_state=STATE_PENDING_CONFIRMATION,
            pipeline_status=STATE_INCOMPLETE_COVERAGE,
            coverage_reason="MISSING_LATEST_CLOSED_CANDLE",
            observed_window_candles=3,
            observation_window=3,
            window_elapsed=True,
            coverage_attempts=1,
            coverage_finalize_after_attempts=3,
        )
        assert decide_next_state(obs).new_state is None  # still retrying
        final = decide_next_state(
            EvaluationObservation(
                **{**obs.__dict__, "coverage_attempts": 3}
            )
        )
        assert final.new_state == STATE_INCOMPLETE_COVERAGE and final.terminal

    def test_coverage_before_window_end_is_never_final(self):
        decision = decide_next_state(
            EvaluationObservation(
                current_state=STATE_PENDING_CONFIRMATION,
                pipeline_status=STATE_INCOMPLETE_COVERAGE,
                coverage_reason="CANDLE_GAP",
                window_elapsed=False,
            )
        )
        assert decision.new_state is None and not decision.terminal


class TestLifecycleRules:
    def test_terminal_states_are_locked(self):
        for state in (STATE_CONFIRMED_DELAYED, STATE_EXPIRED,
                      STATE_INCOMPLETE_COVERAGE, STATE_INVALID_SOURCE,
                      STATE_NOT_READY):
            assert is_terminal(state)
            decision = decide_next_state(
                EvaluationObservation(
                    current_state=state,
                    pipeline_status=STATE_PENDING_CONFIRMATION,
                )
            )
            assert decision.new_state is None and decision.terminal

    def test_pending_is_not_terminal(self):
        assert not is_terminal(STATE_PENDING_CONFIRMATION)

    def test_permanent_vs_transient_reason_classes(self):
        assert is_permanent_coverage_reason("CANDLE_GAP")
        assert is_permanent_coverage_reason("CANONICAL_WINDOW_INCOMPLETE")
        assert not is_permanent_coverage_reason("MISSING_LATEST_CLOSED_CANDLE")

    def test_invalid_source_is_terminal_immediately(self):
        decision = decide_next_state(
            EvaluationObservation(
                current_state=STATE_PENDING_CONFIRMATION,
                pipeline_status=STATE_INVALID_SOURCE,
            )
        )
        assert decision.new_state == STATE_INVALID_SOURCE and decision.terminal
