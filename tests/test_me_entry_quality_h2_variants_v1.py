"""Issue #298 — H2 variants: TURN_1 / TURN_2 / PEAK_REVERSAL + state machine.

Rule-level tests use explicit closed-observation lists (exact expected
indices).  Pipeline-level tests use a crafted 5m close sequence whose Wilder
RSI(14) series is:

    idx  14..17 -> 100.0   (rising closes, no losses)
    idx  18    -> 77.6892  (down close)
    idx  19    -> 80.6022  (up close)
    idx  20    -> 67.3454  (down close)
    idx  21    -> 62.5939  (down close)
    idx  22    -> 68.2269  (up close)

with the original signal fired mid-candle idx 18 (a FORMING candle), so
``O_0 = idx 17`` and ``O_1..O_5 = idx 18..22``.

Offline: no DB, no network, no filesystem, no clock.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h2_v1.candle_provider import (
    CANDLE_5M_MS,
    build_source_identity,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_OFFLINE_SNAPSHOT,
)
from app.research.me_entry_quality_h2_v1.pipeline import (
    REASON_INSUFFICIENT_RSI_WARMUP,
    run_h2_event,
)
from app.research.me_entry_quality_h2_v1.result import (
    ENTRY_PRICE_UNDETERMINED,
    ENTRY_REFERENCE_LABEL,
    EXPIRY_REASON_WINDOW_EXHAUSTED,
    H2Policy,
    H2PolicyError,
    ORIGINAL_PRICE_BASIS,
    STATE_CONFIRMED_DELAYED,
    STATE_EXPIRED,
    STATE_INCOMPLETE_COVERAGE,
    STATE_NOT_READY,
    STATE_PENDING_CONFIRMATION,
    STATE_INVALID_SOURCE,
    VARIANT_PEAK_REVERSAL,
    VARIANT_TURN_1,
    VARIANT_TURN_2,
    REASON_FEATURE_CONFIRMATION_FLAG_INVALID_TYPE,
    REASON_FEATURE_RSI14_OUT_OF_RANGE,
    REASON_FEATURE_RSI_DELTA_3_INVALID_TYPE,
    REASON_INCOMPLETE_FEATURES,
    SourceFeatureSnapshot,
)
from app.research.me_entry_quality_h2_v1.rsi_confirmation import (
    PEAK_RULE_V1,
    peak_reversal_confirmation,
    turn_1_confirmation,
    turn_2_confirmation,
)

BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MS = CANDLE_5M_MS

# crafted closes (see module docstring)
CRAFTED = (
    [100.0 + i for i in range(17)]  # idx 0..16
    + [118.0, 114.0, 116.5, 113.0, 111.5, 115.0]  # idx 17..22
)
# reference Wilder RSI(14) at the observation candles (parity-tested)
RSI_AT = {17: 100.0, 18: 77.6892, 19: 80.6022, 20: 67.3454,
          21: 62.5939, 22: 68.2269}


def rows_from_closes(closes, start_index=0):
    rows = []
    for i, close in enumerate(closes):
        open_ms = BASE_MS + (start_index + i) * MS
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


def event_row(signal_ms, *, key="evt-1", features=None, price=1234.5):
    from datetime import datetime, timedelta, timezone
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    signal = epoch + timedelta(milliseconds=signal_ms)
    return {
        "source_event_key": key,
        "source_identity": (
            f"MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|BTCUSDT|LONG|5m|{signal_ms // MS * MS}"
        ),
        "symbol": "BTCUSDT",
        "lineage_hash": "ab" * 32,
        "signal_time": signal,
        "captured_at": signal + timedelta(seconds=20),
        "activation_freeze_ts": signal,
        "source_fingerprint": "fp-test-1",
        "source_reference_price": price,
        "features": features if features is not None else {
            "rsi_14": 71.25,
            "rsi_delta_3": 4.5,
            "entry_confirmation_rsi_rising": True,
        },
    }


def run(closes, signal_ms, policies, asof_ms, *, key="evt-1", features=None):
    rows = rows_from_closes(closes)
    identity = build_source_identity(
        SOURCE_KIND_OFFLINE_SNAPSHOT, "crafted", rows
    )
    return run_h2_event(
        event_row(signal_ms, key=key, features=features),
        rows_5m=rows,
        source_identity=identity,
        policies=policies,
        evaluation_asof_ms=asof_ms,
    )


# signal mid-candle idx 18 -> O_0 = 17, O_1 = 18 ...
SIGNAL_MS = BASE_MS + 18 * MS + 60_000


class TestRuleFunctions:
    OBS = [50.0, 40.0, 55.0, 45.0, 35.0, 38.0]  # r0..r5 synthetic

    def test_turn_1_first_decline(self):
        assert turn_1_confirmation(self.OBS) == 1

    def test_turn_1_none_without_decline(self):
        assert turn_1_confirmation([50.0, 51.0, 52.0]) is None

    def test_turn_1_flat_is_not_decline(self):
        assert turn_1_confirmation([50.0, 50.0, 50.0]) is None

    def test_turn_2_two_consecutive_declines(self):
        # declines at k=3 and k=4 -> first valid k is 4
        assert turn_2_confirmation(self.OBS) == 4

    def test_turn_2_single_decline_is_not_enough(self):
        assert turn_2_confirmation([50.0, 60.0, 45.0, 55.0]) is None

    def test_turn_2_requires_both_declines_post_signal(self):
        # r0->r1 rises; declines begin only at k=2,3 -> k=3
        obs = [50.0, 60.0, 45.0, 35.0]
        assert turn_1_confirmation(obs) == 2
        assert turn_2_confirmation(obs) == 3

    def test_peak_reversal_strict_local_max(self):
        # peak at r2 (55 > 40 and 55 > 45); reversal confirmed at k=3
        assert peak_reversal_confirmation(self.OBS) == 3

    def test_peak_reversal_none_without_peak(self):
        # strictly decreasing sequence: no strict local max anywhere
        assert peak_reversal_confirmation([50.0, 45.0, 40.0, 35.0]) is None

    def test_peak_reversal_plateau_is_not_strict_peak(self):
        assert peak_reversal_confirmation([50.0, 55.0, 55.0, 54.0]) is None

    def test_peak_rule_must_be_prespecified(self):
        with pytest.raises(ValueError):
            peak_reversal_confirmation([50.0, 45.0, 50.0, 40.0], peak_rule=None)
        with pytest.raises(ValueError):
            peak_reversal_confirmation(
                [50.0, 45.0, 50.0, 40.0], peak_rule="INVENTED_RULE"
            )

    @pytest.mark.parametrize(
        "fn", [turn_1_confirmation, turn_2_confirmation,
               lambda obs: peak_reversal_confirmation(obs)]
    )
    def test_rules_are_causal_prefix_stable(self, fn):
        full = [50.0, 40.0, 55.0, 45.0, 35.0, 38.0, 30.0, 20.0]
        for n in range(1, len(full) + 1):
            prefix_answer = fn(full[:n])
            if prefix_answer is not None:
                assert fn(full) == prefix_answer


class TestPipelineTurn1:
    def test_confirms_at_first_post_signal_decline(self):
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_TURN_1, observation_window=3)],
            asof_ms=BASE_MS + 19 * MS,
        )
        (result,) = outcome.results
        assert outcome.ok
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.confirmation_candle_open_ms == BASE_MS + 18 * MS
        assert result.confirmation_time_ms == BASE_MS + 19 * MS
        assert result.h2_rsi_at_confirmation == pytest.approx(RSI_AT[18], abs=1e-3)
        assert result.observed_window_candles == 1

    def test_confirmed_entry_is_not_a_fictional_execution(self):
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_TURN_1, observation_window=3)],
            asof_ms=BASE_MS + 19 * MS,
        )
        (result,) = outcome.results
        assert result.entry_candidate_price is None
        assert result.entry_price_basis == ENTRY_PRICE_UNDETERMINED
        assert result.entry_reference_label == ENTRY_REFERENCE_LABEL
        assert result.entry_reference_close_price == pytest.approx(114.0)
        assert result.original_price == pytest.approx(1234.5)
        assert result.original_price_basis == ORIGINAL_PRICE_BASIS

    def test_entry_delay_is_measured_from_signal_instant(self):
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_TURN_1, observation_window=3)],
            asof_ms=BASE_MS + 19 * MS,
        )
        (result,) = outcome.results
        assert result.entry_candidate_time_us == result.confirmation_time_us
        assert result.entry_delay_us == (
            result.confirmation_time_us - result.signal_time_us
        )
        assert result.entry_delay_us == (
            (BASE_MS + 19 * MS) * 1000 - SIGNAL_MS * 1000
        )
        assert result.entry_delay_ms == result.entry_delay_us // 1000

    def test_pending_before_confirmation_candle_closes(self):
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_TURN_1, observation_window=3)],
            asof_ms=BASE_MS + 19 * MS - 1,
        )
        (result,) = outcome.results
        assert result.status == STATE_PENDING_CONFIRMATION
        assert result.observed_window_candles == 0
        assert result.entry_candidate_time_ms is None

    def test_window_cap_expires_before_later_decline(self):
        # with signal mid idx 15 the first decline lands at obs k=4:
        # window 3 misses it (EXPIRED), window 6 catches it (CONFIRMED)
        sig = BASE_MS + 15 * MS + 60_000
        expired = run(
            CRAFTED, sig,
            [H2Policy(VARIANT_TURN_1, observation_window=3)],
            asof_ms=BASE_MS + 18 * MS,
        ).results[0]
        assert expired.status == STATE_EXPIRED
        assert expired.expiry_reason == EXPIRY_REASON_WINDOW_EXHAUSTED
        assert expired.observed_window_candles == 3

        confirmed = run(
            CRAFTED, sig,
            [H2Policy(VARIANT_TURN_1, observation_window=6)],
            asof_ms=BASE_MS + 19 * MS,
        ).results[0]
        assert confirmed.status == STATE_CONFIRMED_DELAYED
        assert confirmed.confirmation_candle_open_ms == BASE_MS + 18 * MS

    def test_pending_inside_open_window(self):
        sig = BASE_MS + 15 * MS + 60_000
        result = run(
            CRAFTED, sig,
            [H2Policy(VARIANT_TURN_1, observation_window=3)],
            asof_ms=BASE_MS + 17 * MS,
        ).results[0]
        assert result.status == STATE_PENDING_CONFIRMATION
        assert result.observed_window_candles == 2


class TestPipelineTurn2:
    def test_confirms_after_two_consecutive_declines(self):
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_TURN_2, observation_window=6)],
            asof_ms=BASE_MS + 22 * MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.confirmation_candle_open_ms == BASE_MS + 21 * MS
        assert result.h2_rsi_at_confirmation == pytest.approx(RSI_AT[21], abs=1e-3)

    def test_single_decline_does_not_confirm_turn2(self):
        # at obs 3 only one decline exists (r3 < r2 but r2 > r1)
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_TURN_2, observation_window=6)],
            asof_ms=BASE_MS + 21 * MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_PENDING_CONFIRMATION
        assert result.observed_window_candles == 3

    def test_turn2_window_cap(self):
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_TURN_2, observation_window=3)],
            asof_ms=BASE_MS + 21 * MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_EXPIRED
        assert result.expiry_reason == EXPIRY_REASON_WINDOW_EXHAUSTED


class TestPipelinePeakReversal:
    def test_confirms_at_causal_peak_reversal(self):
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_PEAK_REVERSAL, observation_window=6,
                      peak_rule=PEAK_RULE_V1)],
            asof_ms=BASE_MS + 21 * MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_CONFIRMED_DELAYED
        assert result.confirmation_candle_open_ms == BASE_MS + 20 * MS
        assert result.h2_rsi_at_confirmation == pytest.approx(RSI_AT[20], abs=1e-3)

    def test_not_ready_without_prespecified_peak_rule(self):
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_PEAK_REVERSAL, observation_window=6)],
            asof_ms=BASE_MS + 21 * MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_NOT_READY
        assert result.confirmation_candle_open_ms is None
        assert result.entry_candidate_time_ms is None

    def test_pending_before_peak_is_recognizable(self):
        # at obs 2 the strict local max at r2 needs r3 closed -> still pending
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_PEAK_REVERSAL, observation_window=6,
                      peak_rule=PEAK_RULE_V1)],
            asof_ms=BASE_MS + 20 * MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_PENDING_CONFIRMATION
        assert result.observed_window_candles == 2


class TestStateMachineTerminalStates:
    def test_invalid_feature_snapshot(self):
        for bad_features, expected_reason in (
            ({"rsi_delta_3": 1.0, "entry_confirmation_rsi_rising": True},
             REASON_INCOMPLETE_FEATURES),
            ({"rsi_14": 150.0, "rsi_delta_3": 1.0,
              "entry_confirmation_rsi_rising": True},
             REASON_FEATURE_RSI14_OUT_OF_RANGE),
            ({"rsi_14": 65.0, "rsi_delta_3": "x",
              "entry_confirmation_rsi_rising": True},
             REASON_FEATURE_RSI_DELTA_3_INVALID_TYPE),
            ({"rsi_14": 65.0, "rsi_delta_3": 1.0,
              "entry_confirmation_rsi_rising": 1},
             REASON_FEATURE_CONFIRMATION_FLAG_INVALID_TYPE),
        ):
            outcome = run(
                CRAFTED, SIGNAL_MS,
                [H2Policy(VARIANT_TURN_1, observation_window=3)],
                asof_ms=BASE_MS + 19 * MS,
                features=bad_features,
            )
            (result,) = outcome.results
            assert result.status == STATE_INVALID_SOURCE, bad_features
            assert result.reason == expected_reason, bad_features
            assert not outcome.ok

    def test_insufficient_rsi_warmup_is_incomplete_coverage(self):
        # signal right after the payload start: the series has plenty of
        # candles overall (>= RSI seed) but NO warmup before O_0
        short_closes = [100.0 + i for i in range(20)]
        sig = BASE_MS + 1 * MS + 60_000
        outcome = run(
            short_closes, sig,
            [H2Policy(VARIANT_TURN_1, observation_window=3)],
            asof_ms=BASE_MS + 20 * MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_INCOMPLETE_COVERAGE
        assert result.reason == REASON_INSUFFICIENT_RSI_WARMUP
        assert not outcome.ok

    def test_gap_in_ohlcv_is_incomplete_coverage(self):
        rows = rows_from_closes(CRAFTED)
        del rows[17]  # gap inside the closed/observation range (O_0 bucket)
        identity = build_source_identity(
            SOURCE_KIND_OFFLINE_SNAPSHOT, "crafted", rows
        )
        outcome = run_h2_event(
            event_row(SIGNAL_MS),
            rows_5m=rows,
            source_identity=identity,
            policies=[H2Policy(VARIANT_TURN_1, observation_window=3)],
            evaluation_asof_ms=BASE_MS + 19 * MS,
        )
        (result,) = outcome.results
        assert result.status == STATE_INCOMPLETE_COVERAGE
        assert result.reason == "CANDLE_GAP"
        assert not outcome.ok

    def test_evaluation_before_signal_is_rejected(self):
        with pytest.raises(ValueError):
            run(
                CRAFTED, SIGNAL_MS,
                [H2Policy(VARIANT_TURN_1, observation_window=3)],
                asof_ms=SIGNAL_MS - 1,
            )


class TestFeatureSnapshotVerbatim:
    def test_snapshot_is_verbatim_and_distinct_from_recomputed_rsi(self):
        outcome = run(
            CRAFTED, SIGNAL_MS,
            [H2Policy(VARIANT_TURN_1, observation_window=3)],
            asof_ms=BASE_MS + 19 * MS,
        )
        (result,) = outcome.results
        assert result.source_rsi_14_snapshot == 71.25  # verbatim detector value
        assert result.source_rsi_delta_3_snapshot == 4.5
        assert result.source_entry_confirmation_rsi_rising is True
        assert result.h2_rsi_at_confirmation == pytest.approx(RSI_AT[18], abs=1e-3)
        assert result.h2_rsi_at_confirmation != result.source_rsi_14_snapshot

    def test_snapshot_validation_is_type_strict(self):
        with pytest.raises(ValueError):
            SourceFeatureSnapshot.from_mapping(
                {"rsi_14": "71.25", "rsi_delta_3": 1.0,
                 "entry_confirmation_rsi_rising": True}
            )
        with pytest.raises(ValueError):
            SourceFeatureSnapshot.from_mapping(
                {"rsi_14": float("nan"), "rsi_delta_3": 1.0,
                 "entry_confirmation_rsi_rising": True}
            )


class TestPolicyContract:
    def test_unknown_variant_rejected(self):
        with pytest.raises(H2PolicyError):
            H2Policy("H2_MADE_UP", observation_window=3)

    def test_non_positive_window_rejected(self):
        with pytest.raises(H2PolicyError):
            H2Policy(VARIANT_TURN_1, observation_window=0)
        with pytest.raises(H2PolicyError):
            H2Policy(VARIANT_TURN_1, observation_window=True)

    def test_peak_rule_only_for_peak_variant(self):
        with pytest.raises(H2PolicyError):
            H2Policy(VARIANT_TURN_1, observation_window=3, peak_rule=PEAK_RULE_V1)

    def test_unknown_peak_rule_rejected(self):
        with pytest.raises(H2PolicyError):
            H2Policy(VARIANT_PEAK_REVERSAL, observation_window=3,
                     peak_rule="NOT_A_RULE")

    def test_candidate_windows_are_not_frozen_policy(self):
        policy = H2Policy(VARIANT_TURN_1, observation_window=6)
        assert policy.policy_status == "PRELIMINARY_CANDIDATE_NOT_FROZEN"
        with pytest.raises(H2PolicyError):
            H2Policy(VARIANT_TURN_1, observation_window=6,
                     policy_status="FROZEN")
