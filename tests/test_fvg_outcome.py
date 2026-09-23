"""Tests for FVG outcome evaluation: maturity, idempotency, parity."""
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


# --- Maturity ---

class TestMaturity:
    def test_5m_is_240(self):
        assert _maturity_for_timeframe("5m") == 240

    def test_15m_is_720(self):
        assert _maturity_for_timeframe("15m") == 720

    def test_1h_is_2880(self):
        assert _maturity_for_timeframe("1h") == 2880

    def test_4h_is_11520(self):
        assert _maturity_for_timeframe("4h") == 11520

    def test_unknown_defaults(self):
        assert _maturity_for_timeframe("unknown") == 240


class TestMaturityFiltering:
    def test_5m_200min_skipped(self):
        """5m setup at 200min < 240min maturity. DB query with min_age=240
        should NOT return it. In the test we simulate this by returning empty."""
        setup = _candidate(timeframe="5m", detected_minutes_ago=200)

        class Repo:
            def get_setups_without_outcomes(self, **kw):
                # SQL maturity filter: 200min < 240min → not returned
                if kw.get("min_age_minutes", 0) >= 240 and setup.detected_at > datetime.now(timezone.utc) - timedelta(minutes=240):
                    return []
                return [setup]
            def save_signal_outcome(self, o):
                raise AssertionError("immature")

        class Cli:
            def get_klines(self, s, i, l):
                return [_candle(1_300)]

        ev, fl = process_pending_outcomes(Repo(), Cli(), limit=10, min_age_minutes=240)
        assert ev == 0

    def test_15m_500min_skipped(self):
        """15m setup at 500min < 720min maturity."""
        setup = _candidate(timeframe="15m", detected_minutes_ago=500)

        class Repo:
            def get_setups_without_outcomes(self, **kw):
                if kw.get("min_age_minutes", 0) >= 720:
                    return []
                return [setup]
            def save_signal_outcome(self, o):
                raise AssertionError("immature")

        class Cli:
            def get_klines(self, s, i, l):
                return [_candle(1_300)]

        ev, fl = process_pending_outcomes(Repo(), Cli(), limit=10, min_age_minutes=720)
        assert ev == 0

    def test_5m_300min_ok(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)

        class Repo:
            def get_setups_without_outcomes(self, **kw):
                return [setup]
            def save_signal_outcome(self, o):
                pass

        class Cli:
            def get_klines(self, s, i, l):
                return [_candle(1_300, h=103, l=100, c=102)]

        ev, _ = process_pending_outcomes(Repo(), Cli(), limit=10, min_age_minutes=240)
        assert ev == 1

    def test_15m_800min_ok(self):
        setup = _candidate(timeframe="15m", detected_minutes_ago=800)

        class Repo:
            def get_setups_without_outcomes(self, **kw):
                return [setup]
            def save_signal_outcome(self, o):
                pass

        class Cli:
            def get_klines(self, s, i, l):
                return [_candle(1_300, h=103, l=100, c=102)]

        ev, _ = process_pending_outcomes(Repo(), Cli(), limit=10, min_age_minutes=720)
        assert ev == 1


# --- No ensure_schema ---

class TestNoEnsureSchema:
    def test_not_called(self):
        source = inspect.getsource(
            __import__("app.scanners.outcome_cli", fromlist=["outcome_cli"])
        )
        for line in source.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            assert "ensure_schema()" not in stripped


# --- Idempotency ---

class TestIdempotency:
    def test_upsert(self):
        setup = _candidate(setup_id=uuid4())
        saved = []

        class Repo:
            def get_setups_without_outcomes(self, **kw):
                return [setup, setup]
            def save_signal_outcome(self, o):
                saved.append(o)

        class Cli:
            def get_klines(self, s, i, l):
                return [_candle(1_300, h=103, l=100, c=102)]

        ev, _ = process_pending_outcomes(Repo(), Cli(), limit=10, min_age_minutes=240)
        assert ev == 2
        assert len(saved) == 2


# --- Generic outcome ---

class TestGenericOutcome:
    def test_long_tp1(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)
        sig = setup.signal_candle_open_time
        outcome = evaluate_setup_outcome(setup, [
            _candle(sig + 300_000, h=101, l=100),
            _candle(sig + 600_000, h=107, l=101),
        ], max_bars=2)
        assert outcome.first_event == "TP1"
        assert outcome.result_r > 0

    def test_long_sl(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)
        sig = setup.signal_candle_open_time
        outcome = evaluate_setup_outcome(setup, [
            _candle(sig + 300_000, h=101, l=100),
            _candle(sig + 600_000, h=101, l=97),
        ], max_bars=2)
        assert outcome.first_event == "SL"

    def test_expired(self):
        setup = _candidate(timeframe="5m", detected_minutes_ago=300)
        sig = setup.signal_candle_open_time
        outcome = evaluate_setup_outcome(setup, [
            _candle(sig + 300_000, h=101, l=100),
            _candle(sig + 600_000, h=103, l=101),
        ], max_bars=2)
        assert outcome.first_event == "EXPIRED"


# --- Scanner filter in SQL ---

class TestFiltersInSQL:
    def test_scanner_filter_passed(self):
        calls = []

        class Repo:
            def get_setups_without_outcomes(self, **kw):
                calls.append(kw)
                return []
            def save_signal_outcome(self, o):
                pass

        class Cli:
            def get_klines(self, s, i, l):
                return []

        process_pending_outcomes(Repo(), Cli(), limit=50,
                                scanner_filter="FVG_REACTION_LONG_LOCAL_STRUCT_V1")
        assert calls[0]["scanner_name"] == "FVG_REACTION_LONG_LOCAL_STRUCT_V1"

    def test_other_scanners_dont_starve(self):
        fvg = _candidate(timeframe="5m")

        class Repo:
            def get_setups_without_outcomes(self, **kw):
                if kw.get("scanner_name") == "FVG_REACTION_LONG_LOCAL_STRUCT_V1":
                    return [fvg]
                return []
            def save_signal_outcome(self, o):
                pass

        class Cli:
            def get_klines(self, s, i, l):
                return [_candle(1_300, h=103, l=100, c=102)]

        ev, _ = process_pending_outcomes(
            Repo(), Cli(), limit=2, min_age_minutes=240,
            scanner_filter="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
        )
        assert ev == 1

    def test_timeframe_filter_passed(self):
        calls = []

        class Repo:
            def get_setups_without_outcomes(self, **kw):
                calls.append(kw)
                return []
            def save_signal_outcome(self, o):
                pass

        class Cli:
            def get_klines(self, s, i, l):
                return []

        process_pending_outcomes(Repo(), Cli(), limit=50)
        assert len(calls) >= 2
        for c in calls:
            assert "entry_timeframe" in c

    def test_scanner_filter_only(self):
        fvg = _candidate(scanner_name="FVG_REACTION_LONG_LOCAL_STRUCT_V1")
        other = _candidate(scanner_name="TREND_PULLBACK_V3")

        class Repo:
            def get_setups_without_outcomes(self, **kw):
                if kw.get("scanner_name") == "FVG_REACTION_LONG_LOCAL_STRUCT_V1":
                    return [fvg]
                return [other]
            def save_signal_outcome(self, o):
                pass

        class Cli:
            def get_klines(self, s, i, l):
                return [_candle(1_300, h=103, l=100, c=102)]

        ev, _ = process_pending_outcomes(
            Repo(), Cli(), limit=10, min_age_minutes=240,
            scanner_filter="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
        )
        assert ev == 1


# --- Dry run ---

class TestDryRun:
    def test_no_save(self):
        class Repo:
            def get_setups_without_outcomes(self, **kw):
                return [_candidate()]
            def save_signal_outcome(self, o):
                raise AssertionError("dry-run must not save")

        class Cli:
            def get_klines(self, s, i, l):
                return [_candle(1_300, h=103, l=100, c=103)]

        assert process_pending_outcomes(Repo(), Cli(), dry_run=True, min_age_minutes=10) == (1, 0)


# ===========================================================================
# FVG Outcome Parity: ENTRY = CONFIRMATION CLOSE
# ===========================================================================
#
# Frozen research semantics:
#   T0     FVG created (C3 close)
#   T0+1   touch (price enters FVG zone)
#   T0+2   confirmation (close > swing_high)
#          ENTRY = close(T0+2) = reference_price
#          bars_to_entry = 0
#   T0+3   first candle for SL/TP/MFE/MAE evaluation
#
# Key: T0+1 touch is IGNORED. Entry is pre-determined at confirmation.
# ===========================================================================

class TestFVGOutcomeParity:
    """FVG entry at confirmation close, not re-touch."""

    def _fvg(self, fvg_ts=1000, confirm_ts=2000, entry_price=100.0):
        return SetupCandidate(
            setup_id=uuid4(),
            scanner_name="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
            symbol="BTCUSDT", direction="LONG",
            entry_timeframe="5m", setup_timeframe="5m",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=fvg_ts,
            reference_price=entry_price,
            entry_zone_low=entry_price, entry_zone_high=entry_price,
            invalidation_price=98.0, target_1=106.0,
            score=80.0, state=SetupState.READY_TO_TRADE,
            features={
                "fvg_created_at": fvg_ts,
                "confirmation_at": confirm_ts,
                "entry_price": entry_price,
            },
        )

    def test_entry_at_confirmation_close(self):
        """Entry = confirmation close. bars_to_entry = 0. T0+1 touch IGNORED."""
        s = self._fvg(fvg_ts=1000, confirm_ts=2000, entry_price=100.0)
        candles = [
            _candle(500, h=99, l=98),
            _candle(1000, h=101, l=100),        # T0
            _candle(1500, h=101, l=99),         # T0+1: TOUCHES entry_zone -> IGNORE
            _candle(2000, h=103, l=100, c=100), # T0+2: confirmation
            _candle(2500, h=107, l=101),        # T0+3: first SL/TP candle
        ]
        o = evaluate_setup_outcome(s, candles, max_bars=5)
        assert o.entry_touched is True
        assert o.bars_to_entry == 0
        assert o.entry_price == 100.0

    def test_no_touch_search(self):
        """Entry pre-determined. T0+1 touch ignored."""
        s = self._fvg(fvg_ts=1000, confirm_ts=2000, entry_price=100.0)
        candles = [
            _candle(1000, h=101, l=100),
            _candle(1500, h=101, l=100),        # T0+1: ignored
            _candle(2000, h=103, l=100, c=100), # T0+2: confirmation
            _candle(2500, h=97, l=95),          # T0+3: SL
        ]
        o = evaluate_setup_outcome(s, candles, max_bars=5)
        assert o.first_event == "SL"
        assert o.bars_to_entry == 0
        assert o.entry_price == 100.0

    def test_tp_at_first_eval(self):
        """TP1 at T0+3 (first SL/TP candle)."""
        s = self._fvg(fvg_ts=1000, confirm_ts=2000, entry_price=100.0)
        candles = [
            _candle(1000, h=101, l=100),
            _candle(1500, h=101, l=100),        # T0+1: ignored
            _candle(2000, h=103, l=100, c=100), # T0+2: confirmation
            _candle(2500, h=107, l=101),        # T0+3: TP1 (target=106)
        ]
        o = evaluate_setup_outcome(s, candles, max_bars=5)
        assert o.first_event == "TP1"
        assert o.entry_price == 100.0
        assert o.bars_to_entry == 0
        assert o.result_r > 0

    def test_mfe_only_from_eval_candles(self):
        """T0+1 has huge range (80-120) but is IGNORED. MFE from T0+3 only."""
        s = self._fvg(fvg_ts=1000, confirm_ts=2000, entry_price=100.0)
        candles = [
            _candle(1000, h=101, l=100),
            _candle(1500, h=120, l=80),         # T0+1: IGNORED
            _candle(2000, h=103, l=100, c=100), # T0+2: confirmation
            _candle(2500, h=103, l=99),         # T0+3: MFE = (103-100)/2 = 1.5
            _candle(3000, h=100, l=98),         # T0+4: SL
        ]
        o = evaluate_setup_outcome(s, candles, max_bars=5)
        risk = 100.0 - 98.0  # = 2.0
        assert o.mfe_r == (103 - 100) / risk  # 1.5, NOT 10.0 from T0+1
        assert o.bars_to_entry == 0

    def test_non_fvg_backward_compat(self):
        """Non-FVG scanners: touch-based entry unchanged."""
        s = SetupCandidate(
            setup_id=uuid4(), scanner_name="TREND_PULLBACK_V3",
            symbol="BTCUSDT", direction="LONG",
            entry_timeframe="5m", setup_timeframe="5m",
            detected_at=datetime.now(timezone.utc),
            signal_candle_open_time=1000,
            entry_zone_low=100.0, entry_zone_high=100.0,
            invalidation_price=98.0, target_1=106.0,
            score=80.0, state=SetupState.READY_TO_TRADE, features={},
        )
        o = evaluate_setup_outcome(s, [
            _candle(1000, h=101, l=100),
            _candle(1500, h=101, l=100),
            _candle(2000, h=107, l=100),
        ], max_bars=3)
        assert o.first_event == "TP1"
        assert o.bars_to_entry == 1
