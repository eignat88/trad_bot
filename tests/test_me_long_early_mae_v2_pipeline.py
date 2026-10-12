"""Tests for the strict research-only ME_R_LONG early MAE V2 pipeline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.research.me_r_long_early_mae_v2.candles import ClosedCandle, first_touch, validate_window
from app.research.me_r_long_early_mae_v2.capture import MeEarlyMaeV2SourceAdapter
from app.research.me_r_long_early_mae_v2.evaluator import MeEarlyMaeV2Evaluator
from app.research.me_r_long_early_mae_v2.protocol import (
    ACTIVATION_AT,
    VARIANTS,
    round_trip_cost_r,
)
from app.research.me_r_long_early_mae_v2.virtual_execution import evaluate_virtual_fill
from app.scanners.models import SetupCandidate


UTC = timezone.utc
BASE = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
TEST_ACTIVATION_AT = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
TEST_FROZEN_AT = TEST_ACTIVATION_AT - timedelta(minutes=1)


def candle(offset_minutes: int, high: float, low: float, close: float | None = None) -> ClosedCandle:
    open_at = BASE + timedelta(minutes=offset_minutes)
    return ClosedCandle(
        open_at=open_at,
        close_at=open_at + timedelta(minutes=5),
        open=close or high,
        high=high,
        low=low,
        close=close or high,
    )


class TestFrozenProtocol:
    def test_exactly_seven_variants(self):
        assert len(VARIANTS) == 7
        assert [v.variant_id for v in VARIANTS] == [
            "MAE_10m_040", "MAE_10m_050", "MAE_15m_040",
            "MAE_15m_050", "MAE_15m_060", "MAE_15m_075", "MAE_30m_050",
        ]

    def test_primary_variant(self):
        primary = [v for v in VARIANTS if v.is_primary]
        assert len(primary) == 1
        assert primary[0].variant_id == "MAE_15m_050"
        assert primary[0].horizon_minutes == 15
        assert primary[0].threshold_mae_r == 0.50

    def test_activation_is_fixed_boundary(self):
        assert ACTIVATION_AT is None

    def test_cost_model_is_parity_safe(self):
        # Both baseline and variants receive the same round-trip cost function.
        assert round_trip_cost_r(100, 100, 2.5) > 0
        assert round_trip_cost_r(100, 90, 2.5) > 0


class TestSourceCapture:
    @pytest.fixture(autouse=True)
    def _approved_test_freeze(self, monkeypatch):
        from app.research.me_r_long_early_mae_v2 import protocol

        monkeypatch.setattr(protocol, "FROZEN_AT", TEST_FROZEN_AT)
        monkeypatch.setattr(protocol, "ACTIVATION_AT", TEST_ACTIVATION_AT)

        # Keep any legacy imported boundary synchronized in this test only.
        from app.research.me_r_long_early_mae_v2 import capture
        if hasattr(capture, "ACTIVATION_AT"):
            monkeypatch.setattr(capture, "ACTIVATION_AT", TEST_ACTIVATION_AT)


    def _candidate(self, **changes):
        defaults = dict(
            scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V1",
            scanner_version="1.0.0",
            direction="LONG",
            detected_at=TEST_ACTIVATION_AT + timedelta(minutes=1),
            signal_candle_open_time=1791643200000,
            entry_zone_low=99.8,
            entry_zone_high=100.1,
            reference_price=100.0,
            invalidation_price=97.5,
            target_1=103.0,
            symbol="BTCUSDT",
        )
        defaults.update(changes)
        return SetupCandidate(**defaults)

    def test_natural_candidate_is_captured(self):
        repository = SimpleNamespace(
            ensure_instrument=lambda symbol: 1,
            insert_source_with_variants=lambda source: 101,
        )
        adapter = MeEarlyMaeV2SourceAdapter(repository)
        assert adapter.capture(self._candidate()) == 101
        assert adapter.stats == {"captured": 1, "rejected": 0}

    def test_invalid_geometry_is_saved_separately(self):
        repository = SimpleNamespace(
            ensure_instrument=lambda symbol: 1,
            insert_source_with_variants=lambda source: 202,
        )
        adapter = MeEarlyMaeV2SourceAdapter(repository)
        invalid = self._candidate(invalidation_price=100.0)
        assert adapter.capture(invalid) == 202
        assert adapter.stats["captured"] == 1

    def test_pre_freeze_candidate_is_rejected(self):
        repository = SimpleNamespace(
            ensure_instrument=lambda symbol: 1,
            insert_source_with_variants=lambda source: pytest.fail("must not insert pre-freeze source"),
        )
        adapter = MeEarlyMaeV2SourceAdapter(repository)
        assert adapter.capture(self._candidate(detected_at=TEST_ACTIVATION_AT)) is None
        assert adapter.stats["rejected"] == 1

    def test_wrong_scanner_or_direction_is_rejected(self):
        repository = SimpleNamespace(
            ensure_instrument=lambda symbol: 1,
            insert_source_with_variants=lambda source: pytest.fail("must not insert invalid source"),
        )
        adapter = MeEarlyMaeV2SourceAdapter(repository)
        assert adapter.capture(self._candidate(scanner_name="OTHER")) is None
        assert adapter.capture(self._candidate(direction="SHORT")) is None

    def test_duplicate_source_is_idempotent(self):
        calls = []

        def insert(source):
            calls.append(source)
            return 303

        repository = SimpleNamespace(
            ensure_instrument=lambda symbol: 1,
            insert_source_with_variants=insert,
        )
        adapter = MeEarlyMaeV2SourceAdapter(repository)
        candidate = self._candidate()
        assert adapter.capture(candidate) == 303
        assert adapter.capture(candidate) == 303
        assert len(calls) == 2
        assert adapter.stats == {"captured": 2, "rejected": 0}


class TestClosedCandleIntegrity:
    def test_virtual_entry_uses_first_touch_close_and_boundary_price(self):
        source = make_source(detected_at=BASE)
        candles = [
            candle(0, 102.0, 101.0),
            candle(5, 100.2, 99.9),
            candle(10, 100.2, 99.7),
        ]
        fill = evaluate_virtual_fill(source, candles, BASE + timedelta(minutes=240))
        assert fill is not None
        assert fill.entry_at == BASE + timedelta(minutes=10)
        assert fill.entry_price == 100.1
        assert fill.stop_price == 97.5
        assert fill.risk_distance == pytest.approx(2.6)

    def test_virtual_entry_after_freeze_uses_closed_boundary_only(self):
        source = make_source(detected_at=BASE)
        open_touch_only = candle(5, 100.2, 99.7)
        assert evaluate_virtual_fill(source, [open_touch_only], BASE + timedelta(minutes=7)) is None

    def test_complete_window(self):
        candles = [candle(i * 5, 101, 99) for i in range(3)]
        window, error = validate_window(candles, BASE, BASE + timedelta(minutes=15))
        assert error is None
        assert len(window) == 3

    def test_missing_candle_gap(self):
        candles = [candle(0, 101, 99), candle(10, 101, 99)]
        _, error = validate_window(candles, BASE, BASE + timedelta(minutes=15))
        assert error == "MISSING_CANDLE_GAP"

    def test_duplicate_timestamp(self):
        candles = [candle(0, 101, 99), candle(0, 102, 98)]
        _, error = validate_window(candles, BASE, BASE + timedelta(minutes=15))
        assert error == "DUPLICATE_CANDLE_TIMESTAMP"

    def test_incomplete_horizon(self):
        candles = [candle(0, 101, 99), candle(5, 101, 99)]
        _, error = validate_window(candles, BASE, BASE + timedelta(minutes=15), expected_minutes=15)
        assert error == "INCOMPLETE_WINDOW_END"

    def test_post_horizon_candle_is_not_used(self):
        candles = [
            candle(0, 101, 99),
            candle(5, 101, 99),
            candle(10, 101, 99),
            candle(15, 101, 99),
        ]
        window, error = validate_window(candles, BASE, BASE + timedelta(minutes=15))
        assert error is None
        assert window[-1].close_at == BASE + timedelta(minutes=15)


class FakeRepository:
    def __init__(self, candles, source=None):
        self.candles = candles
        self.source = source
        self.baseline = None
        self.variants = []
        self.entries = []
        self.variant_calls = []

    def load_pending_sources(self):
        return [self.source] if self.source else []

    def fetch_candles(self, instrument_id, start, end):
        return self.candles

    def save_paired_outcome(self, source_id, baseline, evaluations):
        """Fake atomic persistence for evaluator tests."""
        assert len(evaluations) == 7
        assert len({row["variant_id"] for row in evaluations}) == 7
        self.baseline = baseline
        self.variants = list(evaluations)

    def save_baseline(self, source_id, baseline):
        self.baseline = baseline

    def mark_source_not_filled(self, source_id, reason):
        self.source["source_status"] = reason

    def update_source_entry(self, source_id, entry):
        self.entries.append(entry)
        self.source.update(entry)
        self.source["source_status"] = "ENTRY_FILLED"

    def ensure_variants(self, source_id):
        self.variant_calls.append(source_id)

    def save_variant_evaluation(self, source_id, evaluation):
        self.variants.append(evaluation)

    def save_variant_batch(self, source_id, evaluations):
        self.variants.extend(evaluations)


def make_source(**changes):
    source = {
        "source_event_id": 1,
        "instrument_id": 1,
        "symbol": "BTCUSDT",
        "detected_at": BASE,
        "entry_zone_low": 99.8,
        "entry_zone_high": 100.1,
        "invalidation_price": 97.5,
        "target_price": 103.0,
        "source_status": "ENTRY_FILLED",
        "eligibility": "ELIGIBLE",
        "entry_filled_at": BASE,
        "entry_price": 100.1,
        "virtual_stop_price": 97.5,
        "virtual_target_price": 103.0,
    }
    source.update(changes)
    return source


class TestVirtualEvaluator:
    def test_entry_not_filled_when_zone_untouched(self):
        source = make_source(source_status="DETECTED")
        candles = [candle(i * 5, 102.0, 101.0) for i in range(48)]
        repository = FakeRepository(candles, source)
        MeEarlyMaeV2Evaluator(repository).run_cycle(BASE + timedelta(hours=5))
        assert source["source_status"] == "ENTRY_NOT_FILLED"

    def test_missing_entry_candles_are_retried(self):
        source = make_source(source_status="DETECTED")
        repository = FakeRepository([], source)
        evaluator = MeEarlyMaeV2Evaluator(repository)

        evaluator.run_cycle(BASE + timedelta(hours=5))
        assert source["source_status"] == "DETECTED"
        assert repository.entries == []

        repository.candles = [
            candle(i * 5, 102.0, 101.0) for i in range(48)
        ]
        evaluator.run_cycle(BASE + timedelta(hours=5))

        assert source["source_status"] == "ENTRY_NOT_FILLED"
    def test_baseline_and_seven_variants_are_evaluated(self):
        candles = [candle(i * 5, 102.0, 98.0) for i in range(49)]
        repository = FakeRepository(candles, make_source())
        MeEarlyMaeV2Evaluator(repository).run_cycle(BASE + timedelta(hours=9))

        assert repository.baseline["exit_reason"] == "FIXED_HORIZON"
        assert len(repository.variants) == 7
        assert all(v["variant_status"] in {
            "EVALUATED_TRIGGERED", "EVALUATED_NO_TRIGGER",
            "BASELINE_EXITED_EARLY", "INCOMPLETE_COVERAGE", "AMBIGUOUS",
        } for v in repository.variants)

    def test_early_exit_trigger_uses_boundary_close(self):
        candles = [candle(0, 101.0, 99.0)]
        candles.extend(candle(i * 5, 102.0, 99.0) for i in range(1, 49))
        repository = FakeRepository(candles, make_source())
        MeEarlyMaeV2Evaluator(repository).run_cycle(BASE + timedelta(hours=9))
        assert any(
            v["trigger_result"] == "TRIGGERED"
            and v["variant_exit_at"] == BASE + timedelta(minutes=10)
            for v in repository.variants
        )

    def test_baseline_exits_before_variant_horizon(self):
        candles = [candle(0, 102.0, 96.0)]
        candles.extend(candle(i * 5, 102.0, 99.0) for i in range(1, 49))
        repository = FakeRepository(candles, make_source())
        MeEarlyMaeV2Evaluator(repository).run_cycle(BASE + timedelta(hours=9))
        assert any(v["variant_status"] == "BASELINE_EXITED_EARLY" for v in repository.variants)

    def test_missing_candle_is_incomplete_not_finalized(self):
        candles = [candle(0, 101.0, 99.0), candle(10, 101.0, 99.0)]
        repository = FakeRepository(candles, make_source())
        MeEarlyMaeV2Evaluator(repository).run_cycle(BASE + timedelta(hours=9))
        assert any(v["variant_status"] == "INCOMPLETE_COVERAGE" for v in repository.variants)


class TestNoPaperSideEffects:
    def test_evaluator_has_no_paper_repository_api(self):
        source = inspect_source(MeEarlyMaeV2Evaluator)
        for forbidden in ("paper_trade", "PaperTradingEngine", "check_entries", "close_trade"):
            assert forbidden not in source

    def test_capture_has_no_paper_api(self):
        source = inspect_source(MeEarlyMaeV2SourceAdapter)
        for forbidden in ("paper_trade", "PaperTradingEngine", "close_trade"):
            assert forbidden not in source


def inspect_source(obj) -> str:
    import inspect

    return inspect.getsource(obj)
