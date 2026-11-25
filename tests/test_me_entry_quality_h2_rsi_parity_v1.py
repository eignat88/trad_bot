"""Issue #298 — Wilder RSI parity: H2 vs the original detector math.

The H2 package must compute Wilder RSI(14) with the SAME semantics as
``app.indicators.technical.rsi_wilder`` (used by
``momentum_exhaustion_reverse_long_v2``) without importing production code
into its runtime path.  These tests verify parity bit-for-bit on identical
close sequences, including the degenerate ``avg_loss == 0`` handling.

Offline: no DB, no network, no filesystem, no clock.
"""
from __future__ import annotations

import random

import pytest

from app.indicators.technical import rsi_wilder as ref_rsi
from app.research.me_entry_quality_h2_v1.rsi_confirmation import (
    RSI_PERIOD,
    RSI_SEED_MIN_CLOSES,
    rsi_wilder_last,
    rsi_wilder_series,
)


def _lcg_closes(seed: int, n: int, start: float = 100.0) -> list[float]:
    rng = random.Random(seed)
    out = [start]
    for _ in range(n - 1):
        out.append(round(out[-1] + rng.uniform(-3.0, 3.0), 4))
    return out


SEQUENCES = {
    "monotone_up": [100.0 + i for i in range(30)],
    "monotone_down": [130.0 - i for i in range(30)],
    "flat": [100.0] * 30,
    "single_dip": [100.0 + i for i in range(20)] + [118.0, 114.0, 116.5],
    "all_gain_then_loss": [10.0, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                           21, 22, 23, 24, 25, 24, 23, 22, 21],
    "seed_exact": [5.0, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
    "seed_plus_one": [5.0, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 18],
    "lcg_1": _lcg_closes(1, 40),
    "lcg_2": _lcg_closes(2, 40),
    "lcg_3": _lcg_closes(3, 60, start=1.5),
}


class TestWilderRsiParity:
    @pytest.mark.parametrize("name", sorted(SEQUENCES))
    def test_last_value_matches_reference(self, name):
        closes = SEQUENCES[name]
        assert rsi_wilder_last(closes) == ref_rsi(closes)

    @pytest.mark.parametrize("name", sorted(SEQUENCES))
    def test_every_prefix_matches_reference(self, name):
        closes = SEQUENCES[name]
        for i in range(RSI_PERIOD, len(closes)):
            assert rsi_wilder_last(closes[: i + 1]) == ref_rsi(closes[: i + 1])

    @pytest.mark.parametrize("name", sorted(SEQUENCES))
    def test_series_matches_prefix_reference_bit_for_bit(self, name):
        closes = SEQUENCES[name]
        series = rsi_wilder_series(closes)
        assert len(series) == len(closes)
        for i, value in enumerate(series):
            if i < RSI_PERIOD:
                assert value is None
            else:
                assert value == ref_rsi(closes[: i + 1])

    def test_incomplete_seed_raises_in_both(self):
        closes = [1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]  # 14 < 15
        with pytest.raises(ValueError):
            ref_rsi(closes)
        with pytest.raises(ValueError):
            rsi_wilder_last(closes)
        assert rsi_wilder_series(closes) == tuple(None for _ in closes)

    def test_degenerate_all_gain_is_100(self):
        closes = [100.0 + i for i in range(25)]
        assert rsi_wilder_last(closes) == ref_rsi(closes) == 100.0

    def test_degenerate_flat_is_50(self):
        closes = [100.0] * 25
        assert rsi_wilder_last(closes) == ref_rsi(closes) == 50.0

    def test_no_loss_floor_is_zero(self):
        # all losses, no gains -> RSI 0 in both implementations
        closes = [100.0 - i for i in range(25)]
        assert rsi_wilder_last(closes) == ref_rsi(closes) == 0.0

    def test_period_and_seed_constants_match_detector(self):
        assert RSI_PERIOD == 14
        assert RSI_SEED_MIN_CLOSES == RSI_PERIOD + 1

    def test_non_positive_period_fails_closed(self):
        closes = [1.0] * 20
        with pytest.raises(ValueError):
            rsi_wilder_series(closes, period=0)
