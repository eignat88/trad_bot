"""Tests for FVG outcome evaluation: maturity, idempotency, filter-in-SQL."""
from __future__ import annotations

import inspect
import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from app.scanners.outcome_cli import (
    process_pending_outcomes,
    _maturity_for_timeframe,
    TIMEFRAME_MATURITY_MINUTES,
)
from app.scanners.outcome import SignalOutcome, evaluate_setup_outcome
from app.scanners.models import SetupCandidate, SetupState


def _candidate(
    timeframe: str = "5m",
    detected_minutes_ago: int = 300,
    setup_id=None,
    scanner_name: str = "FVG_REACTION_LONG_LOCAL_STRUCT_V1",
):
    now = datetime.now(timezone.utc)
    return SetupCandidate(
        setup_id=setup_id or uuid4(),
        scanner_name=scanner_name,
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe=timeframe,
        setup_timeframe=timeframe,
        detected_at=now - timedelta(minutes=detected_minutes_ago),
        signal_candle_open_time=int((now - timedelta(minutes=detected_minutes_ago + 5)).timestamp() * 1000),
        entry_zone_low=100.0,
        entry_zone_high=100.0,
        invalidation_price=98.0,
        target_1=106.0,
        score=80.0,
        state=SetupState.READY_TO_TRADE,
    )


def _candle(ts, o=100, h=105, l=99, c=102, v=100):
    from app.models import Candle
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


# ---------------------------------------------------------------------------
# Maturity
# ---------------------------------------------------------------------------

class TestMaturity:
    def test_5m_maturity_is_240_minutes(self):
        assert _maturity_for_timeframe("5m") == 240

    def test_15m_maturity_is_720_minutes(self):
        assert _maturity_for_timeframe("15m") == 720

    def test_unknown_defaults_to_240(self):
        assert _maturity_for_timeframe("unknown") == 240


class TestMaturityFiltering:
    def test_5m_200min_not_evaluated(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=200)

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                return [setup]
            def save_signal_outcome(self, outcome):
                raise AssertionError("should not save immature setup")

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300)]

        evaluated, _ = process_pending_outcomes(Repository(), Client(), limit=10)
        assert evaluated == 0

    def test_15m_500min_not_evaluated(self):
        setup = _candidate(timeframe="15m", detected_minutes_ago=500)

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                return [setup]
            def save_signal_outcome(self, outcome):
                raise AssertionError("should not save")

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300)]

        evaluated, _ = process_pending_outcomes(Repository(), Client(), limit=10)
        assert evaluated == 0

    def test_5m_300min_evaluated(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                return [setup]
            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        # Use legacy mode (explicit min_age_minutes) → single TF pass
        evaluated, _ = process_pending_outcomes(
            Repository(), Client(), limit=10, min_age_minutes=240,
        )
        assert evaluated == 1

    def test_15m_800min_evaluated(self):
        setup = _candidate(timeframe="15m", detected_minutes_ago=800)

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                return [setup]
            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        # Legacy mode
        evaluated, _ = process_pending_outcomes(
            Repository(), Client(), limit=10, min_age_minutes=720,
        )
        assert evaluated == 1


# ---------------------------------------------------------------------------
# Filters applied in SQL, not Python
# ---------------------------------------------------------------------------

class TestFiltersInSQL:
    def test_scanner_filter_passed_to_repository(self):
        """scanner_filter is passed as SQL parameter, not post-filtered."""
        calls = []

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                calls.append(kw)
                return []
            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return []

        process_pending_outcomes(
            Repository(), Client(), limit=50,
            scanner_filter="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
        )
        assert len(calls) >= 1
        assert calls[0]["scanner_name"] == "FVG_REACTION_LONG_LOCAL_STRUCT_V1"

    def test_timeframe_filter_passed_to_repository(self):
        calls = []

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                calls.append(kw)
                return []
            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return []

        # Per-TF maturity: 5m maturity=240 is passed
        process_pending_outcomes(Repository(), Client(), limit=50)

        # Should be called 4 times (5m, 15m, 1h, 4h)
        assert len(calls) >= 2
        # Each call should have entry_timeframe set
        for call in calls:
            assert "entry_timeframe" in call

    def test_other_scanners_do_not_starve_fvg(self):
        """FVG setups are returned even if other scanners have more pending."""
        fvg_setup = _candidate(timeframe="5m")
        other_setup = _candidate(timeframe="5m", scanner_name="TREND_PULLBACK_V3")

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                # SQL filters by scanner_name BEFORE limit
                if kw.get("scanner_name") == "FVG_REACTION_LONG_LOCAL_STRUCT_V1":
                    return [fvg_setup]
                return []
            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        evaluated, _ = process_pending_outcomes(
            Repository(), Client(), limit=2,
            min_age_minutes=240,  # legacy mode, single pass
            scanner_filter="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
        )
        assert evaluated == 1


# ---------------------------------------------------------------------------
# No ensure_schema
# ---------------------------------------------------------------------------

class TestNoEnsureSchema:
    def test_ensure_schema_not_called(self):
        import app.scanners.outcome_cli as mod
        source = inspect.getsource(mod)
        lines = [l.strip() for l in source.split("\n")
                 if not l.strip().startswith("#") and not l.strip().startswith('"')]
        for line in lines:
            assert "ensure_schema()" not in line


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_duplicate_setup_id_is_upserted(self):
        setup = _candidate(setup_id=uuid4())
        saved = []

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                return [setup, setup]
            def save_signal_outcome(self, outcome):
                saved.append(outcome)

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        evaluated, _ = process_pending_outcomes(
            Repository(), Client(), limit=10, min_age_minutes=240,
        )
        assert evaluated == 2
        assert len(saved) == 2


# ---------------------------------------------------------------------------
# Outcome evaluation
# ---------------------------------------------------------------------------

class TestOutcomeEvaluation:
    def test_long_tp1(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)
        sig_ts = setup.signal_candle_open_time
        candles = [
            _candle(sig_ts + 300_000, h=101, l=100),
            _candle(sig_ts + 600_000, h=107, l=101),
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=2)
        assert outcome.first_event == "TP1"
        assert outcome.entry_touched is True
        assert outcome.result_r > 0

    def test_long_sl(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)
        sig_ts = setup.signal_candle_open_time
        candles = [
            _candle(sig_ts + 300_000, h=101, l=100),
            _candle(sig_ts + 600_000, h=101, l=97),
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=2)
        assert outcome.first_event == "SL"
        assert outcome.result_r < 0

    def test_expired(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)
        sig_ts = setup.signal_candle_open_time
        candles = [
            _candle(sig_ts + 300_000, h=101, l=100),
            _candle(sig_ts + 600_000, h=103, l=101),
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=2)
        assert outcome.first_event == "EXPIRED"


# ---------------------------------------------------------------------------
# Scanner filter
# ---------------------------------------------------------------------------

class TestScannerFilter:
    def test_filters_by_scanner_name(self):
        fvg_setup = _candidate(timeframe="5m", scanner_name="FVG_REACTION_LONG_LOCAL_STRUCT_V1")
        other_setup = _candidate(timeframe="5m", scanner_name="TREND_PULLBACK_V3")

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                if kw.get("scanner_name") == "FVG_REACTION_LONG_LOCAL_STRUCT_V1":
                    return [fvg_setup]
                return [other_setup]

            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        evaluated, _ = process_pending_outcomes(
            Repository(), Client(), limit=10,
            min_age_minutes=240,
            scanner_filter="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
        )
        assert evaluated == 1


# ---------------------------------------------------------------------------
# FVG Outcome Parity: confirmation_at prevents pre-confirmation entry
# ---------------------------------------------------------------------------

class TestFVGOutcomeParity:
    """Regression: candles between fvg_created_at and confirmation_at
    must NOT count as entry or SL/TP triggers.

    FVG created at T0, touch at T0+1, confirmation at T0+2.
    Outcome evaluation starts strictly after T0+2.
    """

    def _fvg_candidate(self, fvg_ts=1000, confirmation_ts=2000):
        now = datetime.now(timezone.utc)
        return SetupCandidate(
            setup_id=uuid4(),
            scanner_name="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
            symbol="BTCUSDT",
            direction="LONG",
            entry_timeframe="5m",
            setup_timeframe="5m",
            detected_at=now,
            signal_candle_open_time=fvg_ts,
            entry_zone_low=100.0,
            entry_zone_high=100.0,
            invalidation_price=98.0,
            target_1=106.0,
            score=80.0,
            state=SetupState.READY_TO_TRADE,
            features={
                "fvg_created_at": fvg_ts,
                "confirmation_at": confirmation_ts,
            },
        )

    def test_pre_confirmation_candle_not_entry(self):
        """T0+1 (touch) must NOT count as entry. Entry at T0+3 (first eval candle)."""
        setup = self._fvg_candidate(fvg_ts=1000, confirmation_ts=2000)
        candles = [
            _candle(500, h=99, l=98),
            _candle(1000, h=101, l=100),        # T0
            _candle(1500, h=101, l=100),        # T0+1: touch BEFORE confirmation
            _candle(2000, h=103, l=100, c=100), # T0+2: confirmation candle
            _candle(2500, h=101, l=100),        # T0+3: first eval candle, touches entry_zone
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=5)
        # T0+1 (ts=1500) is excluded because 1500 < confirmation_at=2000
        # T0+2 (ts=2000) is excluded because 2000 is NOT > confirmation_at=2000
        # T0+3 (ts=2500) is first eval candle, low=100 touches entry_zone
        assert outcome.entry_touched is True
        assert outcome.bars_to_entry == 1  # first eval candle
        assert outcome.entry_price == 100.0

    def test_no_entry_before_confirmation(self):
        """Touch at T0+1 is ignored. T0+3 doesn't touch entry_zone → NO_ENTRY."""
        setup = self._fvg_candidate(fvg_ts=1000, confirmation_ts=2000)
        candles = [
            _candle(1000, h=101, l=100),
            _candle(1500, h=101, l=100),         # T0+1: touches FVG but before confirmation
            _candle(2000, h=101, l=100, c=100),  # T0+2: confirmation
            _candle(2500, h=97, l=95, c=96),     # T0+3: high=97 < entry_zone_low=100 → no touch
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=5)
        assert outcome.first_event == "NO_ENTRY"
        assert outcome.entry_touched is False

    def test_tp_after_confirmation(self):
        """TP1 hit in first eval candle after confirmation."""
        setup = self._fvg_candidate(fvg_ts=1000, confirmation_ts=2000)
        candles = [
            _candle(1000, h=101, l=100),
            _candle(1500, h=101, l=100),         # T0+1: pre-confirmation touch
            _candle(2000, h=103, l=100, c=100),  # T0+2: confirmation
            _candle(2500, h=107, l=100),         # T0+3: first eval candle, TP1 (target=106)
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=5)
        assert outcome.first_event == "TP1"
        assert outcome.entry_price == 100.0
        assert outcome.result_r > 0

    def test_without_confirmation_at_uses_signal_candle(self):
        """Non-FVG: no confirmation_at → uses signal_candle_open_time."""
        setup = SetupCandidate(
            setup_id=uuid4(),
            scanner_name="TREND_PULLBACK_V3",
            symbol="BTCUSDT",
            direction="LONG",
            entry_timeframe="5m",
            setup_timeframe="5m",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=1000,
            entry_zone_low=100.0,
            entry_zone_high=100.0,
            invalidation_price=98.0,
            target_1=106.0,
            score=80.0,
            state=SetupState.READY_TO_TRADE,
            features={},
        )
        candles = [
            _candle(1000, h=101, l=100),
            _candle(1500, h=101, l=100),
            _candle(2000, h=107, l=101),
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=3)
        assert outcome.first_event == "TP1"
        assert outcome.bars_to_entry == 1

    def test_bars_to_entry_one_at_first_eval(self):
        """First eval candle after confirmation → bars_to_entry = 1."""
        setup = self._fvg_candidate(fvg_ts=1000, confirmation_ts=2000)
        candles = [
            _candle(1000, h=101, l=100),
            _candle(1500, h=101, l=100),         # T0+1: pre-confirmation
            _candle(2000, h=103, l=100, c=100),  # T0+2: confirmation (excluded from eval)
            _candle(2500, h=101, l=100),         # T0+3: first eval candle → touches entry
            _candle(3000, h=107, l=101),         # T0+4: TP1
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=5)
        assert outcome.bars_to_entry == 1  # first eval candle
        assert outcome.entry_price == 100.0


# ---------------------------------------------------------------------------
# Scanner filter
# ---------------------------------------------------------------------------

class TestScannerFilter:
    def test_filters_by_scanner_name(self):
        fvg_setup = _candidate(timeframe="5m", scanner_name="FVG_REACTION_LONG_LOCAL_STRUCT_V1")
        other_setup = _candidate(timeframe="5m", scanner_name="TREND_PULLBACK_V3")

        class Repository:
            def get_setups_without_outcomes(self, **kw):
                if kw.get("scanner_name") == "FVG_REACTION_LONG_LOCAL_STRUCT_V1":
                    return [fvg_setup]
                return [other_setup]
            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        evaluated, _ = process_pending_outcomes(
            Repository(), Client(), limit=10,
            min_age_minutes=240,
            scanner_filter="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
        )
        assert evaluated == 1
