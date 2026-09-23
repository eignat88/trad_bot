"""Tests for FVG outcome evaluation: maturity, idempotency, no ensure_schema."""
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

    def test_1h_maturity(self):
        assert _maturity_for_timeframe("1h") == 2880

    def test_4h_maturity(self):
        assert _maturity_for_timeframe("4h") == 11520

    def test_unknown_defaults_to_240(self):
        assert _maturity_for_timeframe("unknown") == 240


class TestMaturityFiltering:
    def test_5m_200min_not_evaluated(self):
        """5m setup at 200min < 240min maturity → skipped."""
        setup = _candidate(timeframe="5m", detected_minutes_ago=200)

        class Repository:
            def get_setups_without_outcomes(self, *, limit, min_age_minutes):
                return [setup]
            def save_signal_outcome(self, outcome):
                raise AssertionError("should not save immature setup")

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300)]

        evaluated, failed = process_pending_outcomes(Repository(), Client(), limit=10)
        assert evaluated == 0

    def test_15m_500min_not_evaluated(self):
        """15m setup at 500min < 720min maturity → skipped."""
        setup = _candidate(timeframe="15m", detected_minutes_ago=500)

        class Repository:
            def get_setups_without_outcomes(self, *, limit, min_age_minutes):
                return [setup]
            def save_signal_outcome(self, outcome):
                raise AssertionError("should not save")

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300)]

        evaluated, failed = process_pending_outcomes(Repository(), Client(), limit=10)
        assert evaluated == 0

    def test_5m_300min_evaluated(self):
        """5m setup at 300min >= 240min maturity → evaluated."""
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)

        class Repository:
            def get_setups_without_outcomes(self, *, limit, min_age_minutes):
                return [setup]
            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        evaluated, failed = process_pending_outcomes(Repository(), Client(), limit=10)
        assert evaluated == 1

    def test_15m_800min_evaluated(self):
        """15m setup at 800min >= 720min maturity → evaluated."""
        setup = _candidate(timeframe="15m", detected_minutes_ago=800)

        class Repository:
            def get_setups_without_outcomes(self, *, limit, min_age_minutes):
                return [setup]
            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        evaluated, failed = process_pending_outcomes(Repository(), Client(), limit=10)
        assert evaluated == 1


class TestNoEnsureSchema:
    def test_ensure_schema_not_called(self):
        """outcome_cli must not contain ensure_schema call."""
        import app.scanners.outcome_cli as mod
        source = inspect.getsource(mod)
        # Check for actual function call, not docstring mentions
        lines = [l.strip() for l in source.split("\n")
                 if not l.strip().startswith("#") and not l.strip().startswith('"')]
        for line in lines:
            assert "ensure_schema()" not in line, f"ensure_schema() call found: {line}"


class TestIdempotency:
    def test_duplicate_setup_id_is_upserted(self):
        setup = _candidate(setup_id=uuid4())
        saved = []

        class Repository:
            def get_setups_without_outcomes(self, *, limit, min_age_minutes):
                return [setup, setup]
            def save_signal_outcome(self, outcome):
                saved.append(outcome)

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        evaluated, failed = process_pending_outcomes(Repository(), Client(), limit=10)
        assert evaluated == 2
        assert len(saved) == 2


class TestOutcomeEvaluation:
    def test_long_tp1(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)
        # Candle timestamps must be AFTER signal_candle_open_time
        sig_ts = setup.signal_candle_open_time
        candles = [
            _candle(sig_ts + 300_000, h=101, l=100),   # entry touch
            _candle(sig_ts + 600_000, h=107, l=101),   # TP1 hit (target_1=106)
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

    def test_no_entry(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)
        sig_ts = setup.signal_candle_open_time
        candles = [
            _candle(sig_ts + 300_000, h=99, l=98),
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=1)
        assert outcome.first_event == "NO_ENTRY"

    def test_expired(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)
        sig_ts = setup.signal_candle_open_time
        candles = [
            _candle(sig_ts + 300_000, h=101, l=100),
            _candle(sig_ts + 600_000, h=103, l=101),
        ]
        outcome = evaluate_setup_outcome(setup, candles, max_bars=2)
        assert outcome.first_event == "EXPIRED"


class TestScannerFilter:
    def test_filters_by_scanner_name(self):
        fvg_setup = _candidate(timeframe="5m", scanner_name="FVG_REACTION_LONG_LOCAL_STRUCT_V1")
        other_setup = _candidate(timeframe="5m", scanner_name="TREND_PULLBACK_V3")

        class Repository:
            def get_setups_without_outcomes(self, *, limit, min_age_minutes):
                return [fvg_setup, other_setup]
            def save_signal_outcome(self, outcome):
                pass

        class Client:
            def get_klines(self, symbol, interval, limit):
                return [_candle(1_300, h=103, l=100, c=102)]

        evaluated, _ = process_pending_outcomes(
            Repository(), Client(), limit=10,
            scanner_filter="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
        )
        assert evaluated == 1
