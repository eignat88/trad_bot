"""Issue #298 — causal ASOF guarantees: boundaries, microseconds, lookahead.

Covers:

* closed-candle boundary ``T-1ms`` / ``T`` / ``T+1ms`` (inclusive close);
* microsecond signal-time boundaries — two signals one MICROSECOND apart
  can select different observation candles (same-millisecond case);
* original signal fired on a still-FORMING candle;
* appending future candles NEVER changes a result computed at an earlier
  ASOF (even corrupt future rows);
* a decline visible only after the ASOF can never confirm at that ASOF
  (no lookahead);
* delivery time (``captured_at``) never leaks into causality.

Offline: no DB, no network, no filesystem, no clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_OFFLINE_SNAPSHOT,
)
from app.research.me_entry_quality_h2_v1.candle_provider import (
    CANDLE_5M_MS,
    build_source_identity,
)
from app.research.me_entry_quality_h2_v1.pipeline import run_h2_event
from app.research.me_entry_quality_h2_v1.result import (
    H2Policy,
    STATE_CONFIRMED_DELAYED,
    STATE_PENDING_CONFIRMATION,
    VARIANT_TURN_1,
)

BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MS = CANDLE_5M_MS
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# same crafted sequence as test_me_entry_quality_h2_variants_v1
CRAFTED = (
    [100.0 + i for i in range(17)]
    + [118.0, 114.0, 116.5, 113.0, 111.5, 115.0]
)

#: rising warmup buckets before the crafted sequence (canonical window)
WARMUP = 106


def warmup_closes(n):
    return [99.0 - 0.5 * (n - 1 - i) for i in range(n)]


def rows_from_closes(closes, start_index=0, warmup=WARMUP):
    placed = [
        (start_index - warmup + j, c) for j, c in enumerate(warmup_closes(warmup))
    ] + [(start_index + i, c) for i, c in enumerate(closes)]
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


def event_row(signal_us, *, key="evt-asof", captured_shift_s=20):
    signal = EPOCH + timedelta(microseconds=signal_us)
    return {
        "source_event_key": key,
        "source_identity": (
            "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|BTCUSDT|LONG|5m|"
            f"{signal_us // 1000 // MS * MS}"
        ),
        "symbol": "BTCUSDT",
        "lineage_hash": "cd" * 32,
        "signal_time": signal,
        "captured_at": signal + timedelta(seconds=captured_shift_s),
        # #305 cohort contract: signal_time > activation_freeze_ts STRICTLY
        "activation_freeze_ts": signal - timedelta(hours=1),
        "source_fingerprint": "fp-asof-1",
        "source_reference_price": 1000.0,
        "features": {
            "rsi_14": 70.0,
            "rsi_delta_3": 3.0,
            "entry_confirmation_rsi_rising": True,
        },
    }


def run_at(signal_us, asof_ms, rows, *, captured_shift_s=20):
    identity = build_source_identity(
        SOURCE_KIND_OFFLINE_SNAPSHOT, "asof-test", rows
    )
    return run_h2_event(
        event_row(signal_us, captured_shift_s=captured_shift_s),
        rows_5m=rows,
        source_identity=identity,
        policies=[H2Policy(VARIANT_TURN_1, observation_window=6)],
        evaluation_asof_ms=asof_ms,
    )


SIGNAL_US = (BASE_MS + 18 * MS + 60_000) * 1000  # mid-candle idx 18


class TestClosedCandleBoundary:
    def test_confirmation_invisible_at_t_minus_1ms(self):
        rows = rows_from_closes(CRAFTED)
        # candle idx 18 closes exactly at T = BASE + 19*MS
        outcome = run_at(SIGNAL_US, BASE_MS + 19 * MS - 1, rows)
        assert outcome.results[0].status == STATE_PENDING_CONFIRMATION

    def test_confirmation_visible_exactly_at_close_t(self):
        rows = rows_from_closes(CRAFTED)
        outcome = run_at(SIGNAL_US, BASE_MS + 19 * MS, rows)
        assert outcome.results[0].status == STATE_CONFIRMED_DELAYED
        assert outcome.results[0].confirmation_time_ms == BASE_MS + 19 * MS

    def test_confirmation_visible_at_t_plus_1ms(self):
        rows = rows_from_closes(CRAFTED)
        outcome = run_at(SIGNAL_US, BASE_MS + 19 * MS + 1, rows)
        assert outcome.results[0].status == STATE_CONFIRMED_DELAYED


class TestMicrosecondSignalBoundaries:
    def test_signal_at_exact_close_boundary_picks_next_candle(self):
        # signal exactly at close of candle idx 18 -> that close is NOT
        # strictly after the signal; O_1 becomes idx 19
        rows = rows_from_closes(CRAFTED)
        signal_us = (BASE_MS + 19 * MS) * 1000  # == close_us of candle 18
        outcome = run_at(signal_us, BASE_MS + 21 * MS, rows)
        result = outcome.results[0]
        assert result.status == STATE_CONFIRMED_DELAYED
        # O_0 = 18 (RSI 100.0), O_1 = 19 (75.33-ish pattern: up), O_2 = 20
        # (down) — values from the fixed canonical window; first decline at 20
        assert result.confirmation_candle_open_ms == BASE_MS + 20 * MS

    def test_signal_one_microsecond_before_close_picks_that_candle(self):
        rows = rows_from_closes(CRAFTED)
        signal_us = (BASE_MS + 19 * MS) * 1000 - 1  # same millisecond!
        outcome = run_at(signal_us, BASE_MS + 21 * MS, rows)
        result = outcome.results[0]
        assert result.status == STATE_CONFIRMED_DELAYED
        # O_0 = 17 (100.0), O_1 = 18 (down) -> confirms at candle 18
        assert result.confirmation_candle_open_ms == BASE_MS + 18 * MS

    def test_one_microsecond_changes_the_selected_observation(self):
        rows = rows_from_closes(CRAFTED)
        at_close = run_at((BASE_MS + 19 * MS) * 1000, BASE_MS + 21 * MS, rows)
        just_before = run_at(
            (BASE_MS + 19 * MS) * 1000 - 1, BASE_MS + 21 * MS, rows
        )
        assert (
            at_close.results[0].confirmation_candle_open_ms
            != just_before.results[0].confirmation_candle_open_ms
        )


class TestFormingCandleSignal:
    def test_signal_on_forming_candle_observes_its_close(self):
        rows = rows_from_closes(CRAFTED)
        outcome = run_at(SIGNAL_US, BASE_MS + 19 * MS, rows)
        result = outcome.results[0]
        # the forming signal candle (idx 18) is O_1 and its CLOSED values
        # drive the confirmation — never its in-flight state
        assert result.confirmation_candle_open_ms == BASE_MS + 18 * MS
        assert BASE_MS + 18 * MS <= SIGNAL_US // 1000 < BASE_MS + 19 * MS
        assert result.confirmation_time_ms == BASE_MS + 19 * MS


class TestFutureAppendInvariance:
    @staticmethod
    def _semantic(result):
        # the candle-source provenance block legitimately records the
        # SUPPLIED payload (row_count/hash differ when future rows are
        # appended); every semantic result field must stay identical
        payload = result.to_dict()
        payload.pop("candle_source")
        return payload

    def test_appended_future_candles_do_not_change_earlier_result(self):
        base_rows = rows_from_closes(CRAFTED)
        asof = BASE_MS + 19 * MS
        result_before = run_at(SIGNAL_US, asof, base_rows).results[0]

        future_good = rows_from_closes(
            [130.0, 131.0, 132.0], start_index=23, warmup=0
        )
        future_corrupt = [{
            "open_ms": BASE_MS + 26 * MS,
            "open": "junk", "high": None, "low": -9,
            "close": float("nan"), "volume": "x",
        }]
        extended = base_rows + future_good + future_corrupt
        result_after = run_at(SIGNAL_US, asof, extended).results[0]

        assert self._semantic(result_before) == self._semantic(result_after)

    def test_no_lookahead_from_later_decline(self):
        # signal mid idx 15: the first decline (candle idx 18) must NOT be
        # visible before its own close
        rows = rows_from_closes(CRAFTED)
        signal_us = (BASE_MS + 15 * MS + 60_000) * 1000
        before = run_at(signal_us, BASE_MS + 18 * MS, rows).results[0]
        after = run_at(signal_us, BASE_MS + 19 * MS, rows).results[0]
        assert before.status == STATE_PENDING_CONFIRMATION
        assert after.status == STATE_CONFIRMED_DELAYED
        assert after.confirmation_candle_open_ms == BASE_MS + 18 * MS

    def test_captured_at_delivery_time_never_leaks(self):
        rows = rows_from_closes(CRAFTED)
        asof = BASE_MS + 21 * MS
        early = run_at(SIGNAL_US, asof, rows, captured_shift_s=5).results[0]
        late = run_at(SIGNAL_US, asof, rows, captured_shift_s=900).results[0]
        assert early.to_dict() == late.to_dict()


class TestAsofInputContract:
    def test_non_integer_asof_rejected(self):
        rows = rows_from_closes(CRAFTED)
        for bad in (True, 1.5, "123", None):
            with pytest.raises(ValueError):
                run_at(SIGNAL_US, bad, rows)

    def test_negative_asof_rejected(self):
        rows = rows_from_closes(CRAFTED)
        with pytest.raises(ValueError):
            run_at(SIGNAL_US, -1, rows)
