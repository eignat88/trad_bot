"""ISSUE244 — SRR Shadow Evaluator Candle Coverage Fail-Closed regression tests.

Parent: #244 ``OOS_GENERIC_CANDLE_COVERAGE_FAIL_CLOSED_V1``.

Core invariant::

    INCOMPLETE CANDLE COVERAGE
        -> NO evaluated_*_at WRITE
        -> NO is_final

The SRR shadow evaluator (``app.shadow.srr_research_evaluator``, the
``trad-bot-srr-evaluator.timer`` entrypoint for ``SRR_LONG_OUTCOME_V1``) must:

* fetch candles through the shared ``app.research.candle_coverage_guard_v1``
  (classified source statuses, bounded pagination for >1000-candle windows,
  NO fallback request after a Global Limiter deny, NO silent truncation);
* prove the full closed 5m path of a horizon before writing
  ``evaluated_*_at``;
* prove the full 240m TP/SL path before setting ``is_final=True``;
* emit structured diagnostics (``source_blocked`` / ``coverage_blocked`` /
  ``horizons_pending`` / ``finalization_blocked`` + reason_code, signal_id,
  symbol, experiment_id) without any DB schema change;
* stay idempotent across restarts and never rewrite finalized outcomes.

Deterministic fixtures, in-memory fake sources and in-memory persistence only:
no network, no real Bybit, no PostgreSQL, no production DB.

The frozen SRR formulas (``_calculate_long_mfe_mae`` /
``_check_long_target_sl``) are NOT modified by this issue — T01 proves
complete-path parity by re-computing every written value with the untouched
reference functions.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.exchange.bybit_client import BybitError, BybitRateLimitError
from app.models import Candle
from app.research import candle_coverage_guard_v1 as guard
from app.shadow import srr_research_evaluator as srr_module
from app.shadow.srr_research_evaluator import (
    EXPERIMENT_ID,
    HORIZONS,
    SRRResearchEvaluator,
    _calculate_long_mfe_mae,
    _check_long_target_sl,
)

UTC = timezone.utc
INTERVAL_MS = 300_000
SYMBOL = "BTCUSDT"

# Grid-aligned signal so every horizon cutoff lands on the 5-minute grid.
SIGNAL = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

# Shared LONG geometry: entry 100, stop below (98), target above (103).
ENTRY = 100.0
STOP = 98.0
TARGET = 103.0
CANDLE_OPEN = 100.0
CANDLE_HIGH = 101.0
CANDLE_LOW = 99.0
CANDLE_CLOSE = 100.5

# Reference values of the COMPLETE path (frozen formulas, unchanged):
#   mfe  = (high - entry) / entry * 100 = 1.0
#   mae  = (entry - low)  / entry * 100 = 1.0
#   risk = |entry - stop| / entry * 100 = 2.0  ->  mfe_r = mae_r = 0.5
#   TP (high >= 103) never hit, SL (low <= 98) never hit.
EXP_MFE = 1.0
EXP_MAE = 1.0
EXP_MFE_R = 0.5
EXP_MAE_R = 0.5

ALL_EVALUATED_FIELDS = [f"evaluated_{label}_at" for label, _ in HORIZONS]
TP_SL_FIELDS = ("tp_hit", "sl_hit", "tp_before_sl", "sl_before_tp")


# ── candle fixtures ────────────────────────────────────────────────


def ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def first_required_open_ms(signal_ms: int) -> int:
    """First 5-minute open strictly after the signal time."""
    return (signal_ms // INTERVAL_MS + 1) * INTERVAL_MS


def required_opens(signal_ms: int, cutoff_ms: int) -> list[int]:
    """Every 5-minute open the evaluator may read inside (signal, cutoff)."""
    return list(range(first_required_open_ms(signal_ms), cutoff_ms, INTERVAL_MS))


def candle(open_ms: int, *, high: float = CANDLE_HIGH, low: float = CANDLE_LOW,
           close: float = CANDLE_CLOSE, open_: float = CANDLE_OPEN) -> Candle:
    return Candle(timestamp=open_ms, open=open_, high=high, low=low,
                  close=close, volume=1.0)


def full_path(signal: datetime, horizon_minutes: int = 240, **kw) -> list[Candle]:
    """Complete ascending path covering [signal, signal + horizon_minutes)."""
    signal_ms = ms(signal)
    cutoff_ms = ms(signal + timedelta(minutes=horizon_minutes))
    return [candle(t, **kw) for t in required_opens(signal_ms, cutoff_ms)]


def drop(candles: list[Candle], offset_minutes: int,
         signal: datetime = SIGNAL) -> list[Candle]:
    """The complete path WITHOUT the candle opening at signal+offset."""
    target = ms(signal) + offset_minutes * 60_000
    return [c for c in candles if c.timestamp != target]


def corrupt(candles: list[Candle], offset_minutes: int, field: str,
            value: float, *, signal: datetime = SIGNAL) -> list[Candle]:
    """Complete path with one candle's price replaced by ``value``."""
    target = ms(signal) + offset_minutes * 60_000
    param = {"open": "open_", "close": "close",
             "high": "high", "low": "low"}[field]
    return [
        candle(c.timestamp, **{param: value}) if c.timestamp == target else c
        for c in candles
    ]


def swap_candles(candles: list[Candle], offset_a: int, offset_b: int,
                 *, signal: datetime = SIGNAL) -> list[Candle]:
    """The path with the two candles at the given offsets swapped."""
    a = ms(signal) + offset_a * 60_000
    b = ms(signal) + offset_b * 60_000
    out = list(candles)
    ia = next(i for i, c in enumerate(out) if c.timestamp == a)
    ib = next(i for i, c in enumerate(out) if c.timestamp == b)
    out[ia], out[ib] = out[ib], out[ia]
    return out


def shifted_path(signal: datetime, horizon_minutes: int = 240, *,
                 shift_minutes: int = 1) -> list[Candle]:
    """A complete-looking path whose opens are shifted off the UTC grid."""
    signal_ms = ms(signal)
    cutoff_ms = ms(signal + timedelta(minutes=horizon_minutes))
    shift = shift_minutes * 60_000
    return [
        candle(t + shift)
        for t in required_opens(signal_ms, cutoff_ms)
        if signal_ms < t + shift < cutoff_ms
    ]


# ── signal fixtures ────────────────────────────────────────────────


def _srr_signal(signal_time: datetime = SIGNAL, **overrides) -> dict:
    """A row exactly as ``SRRResearchEvaluator.get_eligible_signals`` returns.

    Deliberately WITHOUT ``experiment_id`` (the production SELECT does not
    return it): diagnostics must fall back to the module-level
    ``EXPERIMENT_ID``.
    """
    sig = {
        "signal_id": 1,
        "symbol": SYMBOL,
        "signal_time": signal_time,
        "entry_price": ENTRY,
        "invalidation_price": STOP,
        "target_1": TARGET,
        "outcome_id": None,
        **{field: None for field in ALL_EVALUATED_FIELDS},
        "is_final": False,
    }
    sig.update(overrides)
    return sig


def fresh_stats() -> dict:
    """The fail-closed diagnostics stats shape produced by run_evaluation_cycle."""
    return {
        "experiment_id": EXPERIMENT_ID,
        "horizons_updated": {h: 0 for h, _ in HORIZONS},
        "outcomes_created": 0,
        "outcomes_updated": 0,
        "finalized": 0,
        "errors": 0,
        "source_blocked": {
            "source_fetch_error": 0,
            "source_incomplete": 0,
            "source_not_yet_available": 0,
        },
        "coverage_blocked": {},
        "horizons_pending": {h: 0 for h, _ in HORIZONS},
        "finalization_blocked": 0,
    }


# ── fake candle sources ────────────────────────────────────────────


class FakeBybitClient:
    """Deterministic Bybit-like 5-minute source.

    ``get_klines`` returns the latest-N candles (ascending, including the
    forming bar, like the real client).  ``get_klines_range`` honours the
    explicit [start_ms, end_ms] bounds and the bounded page budget — it
    RAISES rather than silently truncating.

    ``sort=False`` keeps candles exactly in arrival order (unordered source).
    """

    def __init__(self, candles, *, now: datetime | None = None,
                 error: Exception | None = None, range_slice: int = 0,
                 sort: bool = True) -> None:
        self.candles = (sorted(candles, key=lambda c: c.timestamp)
                        if sort else list(candles))
        self.now = now
        self.error = error
        self.range_slice = range_slice
        self.kline_calls = 0
        self.range_calls = 0

    def get_klines(self, symbol: str, interval: str = "5", limit: int = 200):
        self.kline_calls += 1
        if self.error is not None:
            raise self.error
        bars = self.candles
        if self.now is not None:
            horizon = ms(self.now)
            bars = [c for c in bars if c.timestamp <= horizon]
        return list(bars[-limit:])

    def get_klines_range(self, symbol: str, interval: str = "5", *,
                         start_ms: int, end_ms: int, page_size: int = 1000,
                         max_pages: int = 200):
        self.range_calls += 1
        if self.error is not None:
            raise self.error
        step_ms = int(interval) * 60_000
        pages = max(1, (int(end_ms) - int(start_ms))
                    // (int(page_size) * step_ms) + 1)
        if pages > max_pages:
            raise BybitError(
                f"{symbol}: kline range fetch exceeded {max_pages} pages")
        out = [c for c in self.candles
               if start_ms <= c.timestamp <= end_ms]
        if self.range_slice:
            out = out[self.range_slice:]
        return out


class NoRangeClient(FakeBybitClient):
    """Client without a historical range source (>1000-bar windows fail)."""

    get_klines_range = None


# ── harnesses ──────────────────────────────────────────────────────


@dataclass
class EvalResult:
    writes: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def write(self) -> dict | None:
        return self.writes[-1] if self.writes else None

    def finalized(self) -> bool:
        return any(w.get("is_final") for w in self.writes)

    def evaluated(self, label: str) -> bool:
        return any(f"evaluated_{label}_at" in w for w in self.writes)


class UnitHarness:
    """SRRResearchEvaluator._evaluate_signal (unit level, explicit ASOF)."""

    name = "SRRResearchEvaluator"

    @staticmethod
    def evaluate(candles, now, *, signal_time: datetime = SIGNAL,
                 **overrides) -> EvalResult:
        evaluator = SRRResearchEvaluator(conn=None, client=None)
        writes: list[dict] = []

        def _record(signal_id, symbol, updates):
            writes.append(dict(updates))
            return True

        evaluator._save_outcome_partial = _record  # type: ignore[method-assign]
        stats = fresh_stats()
        evaluator._evaluate_signal(
            _srr_signal(signal_time, **overrides), list(candles), now, stats)
        return EvalResult(writes=writes, stats=stats)

    @staticmethod
    def reference_mfe(candles, minutes: int, signal_time: datetime = SIGNAL):
        post = [c for c in candles if c.timestamp > ms(signal_time)]
        return _calculate_long_mfe_mae(post, ENTRY, minutes, signal_time)

    @staticmethod
    def reference_tpsl(candles, signal_time: datetime = SIGNAL):
        post = [c for c in candles if c.timestamp > ms(signal_time)]
        return _check_long_target_sl(post, ENTRY, STOP, TARGET, 240, signal_time)


class SrrCycle:
    """Full SRRResearchEvaluator.run_evaluation_cycle with in-memory state.

    Mirrors the production eligibility SQL semantics (no outcome -> eligible
    after 15m; outcome -> eligible only while ``is_final = FALSE`` AND at
    least one MATURE horizon is unevaluated) so idempotency (T11) and
    finalized-row protection (T15) are observable end to end.
    """

    def __init__(self, signal_time: datetime) -> None:
        self.signal_time = signal_time
        self.outcomes: dict[int, dict] = {}
        self.writes: list[dict] = []
        self.evaluator = self._new_evaluator()

    def _new_evaluator(self) -> SRRResearchEvaluator:
        evaluator = SRRResearchEvaluator(conn=None, client=None)
        evaluator.get_eligible_signals = self._eligible  # type: ignore[method-assign]
        evaluator._save_outcome_partial = self._save  # type: ignore[method-assign]
        return evaluator

    def restart(self) -> None:
        """Simulate a systemd restart: fresh object, same persistent state."""
        self.evaluator = self._new_evaluator()

    def _eligible(self, limit: int = 5000) -> list[dict]:
        now = datetime.now(UTC)
        outcome = self.outcomes.get(1)
        if outcome is None:
            if self.signal_time > now - timedelta(minutes=15):
                return []
            row_outcome: dict | None = None
        else:
            if outcome.get("is_final"):
                return []
            pending = any(
                outcome.get(f"evaluated_{label}_at") is None
                and self.signal_time + timedelta(minutes=minutes) <= now
                for label, minutes in HORIZONS
            )
            if not pending:
                return []
            row_outcome = outcome
        row = _srr_signal(self.signal_time)
        if row_outcome is not None:
            row["outcome_id"] = 1
            for label, _ in HORIZONS:
                row[f"evaluated_{label}_at"] = row_outcome.get(
                    f"evaluated_{label}_at")
            row["is_final"] = bool(row_outcome.get("is_final"))
        return [row][:limit]

    def _save(self, signal_id: int, symbol: str, updates: dict) -> bool:
        # Mirrors _save_outcome_partial: NULL values are dropped.
        filtered = {k: v for k, v in updates.items() if v is not None}
        self.writes.append(dict(filtered))
        self.outcomes.setdefault(signal_id, {}).update(filtered)
        return True

    def run(self, client) -> EvalResult:
        self.evaluator.client = client
        stats = self.evaluator.run_evaluation_cycle()
        return EvalResult(writes=list(self.writes), stats=stats)

    @property
    def eligible_count(self) -> int:
        return len(self.evaluator.get_eligible_signals(limit=5000))


def cycle_signal_time() -> datetime:
    """Grid-aligned signal ~250 minutes ago: every horizon mature + closed."""
    floor = int(datetime.now(UTC).timestamp()) // 300 * 300
    return datetime.fromtimestamp(floor - 250 * 60, UTC)


def old_signal_time() -> datetime:
    """Grid-aligned signal 10 days ago: the window exceeds one Bybit page."""
    floor = int(datetime.now(UTC).timestamp()) // 300 * 300
    return datetime.fromtimestamp(floor - 10 * 24 * 3600, UTC)


NOW_FULL = SIGNAL + timedelta(minutes=245)   # all horizons mature, all closed


# ══════════════════════════════════════════════════════════════════
# T01 — complete path: unchanged results + complete-path parity
# ══════════════════════════════════════════════════════════════════


def test_t01_complete_path_writes_expected_results():
    candles = full_path(SIGNAL, 240)

    result = UnitHarness.evaluate(candles, NOW_FULL)

    assert result.write is not None, "complete path must be evaluated"
    for label, minutes in HORIZONS:
        assert result.evaluated(label), f"{label} must be evaluated"
        assert result.write[f"mfe_{label}"] == pytest.approx(EXP_MFE)
        assert result.write[f"mae_{label}"] == pytest.approx(EXP_MAE)
        assert result.write[f"mfe_r_{label}"] == pytest.approx(EXP_MFE_R)
        assert result.write[f"mae_r_{label}"] == pytest.approx(EXP_MAE_R)

        # Complete-path parity with the untouched reference formulas.
        ref_mfe, ref_mae = UnitHarness.reference_mfe(candles, minutes)
        assert result.write[f"mfe_{label}"] == pytest.approx(ref_mfe)
        assert result.write[f"mae_{label}"] == pytest.approx(ref_mae)

    # TP/SL verdict over the complete 240m path: nothing hit.
    assert result.write.get("tp_hit") is False
    assert result.write.get("sl_hit") is False
    assert result.write.get("tp_before_sl") is False
    assert result.write.get("sl_before_tp") is False

    ref_tpsl = UnitHarness.reference_tpsl(candles)
    for key in TP_SL_FIELDS:
        assert result.write.get(key) is ref_tpsl[key]

    assert result.finalized() is True
    assert result.stats["finalization_blocked"] == 0
    assert result.stats["coverage_blocked"] == {}
    assert not result.stats.get("diagnostics")


def test_t01_complete_path_cycle_finalizes():
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)

    result = cycle.run(FakeBybitClient(full_path(signal_time, 240)))

    assert result.stats["signals_checked"] == 1
    assert result.stats["errors"] == 0
    assert result.write is not None
    assert result.write["mfe_60m"] == pytest.approx(EXP_MFE)
    assert result.write["mae_60m"] == pytest.approx(EXP_MAE)
    assert result.write["mfe_r_60m"] == pytest.approx(EXP_MFE_R)
    assert result.write["mae_r_60m"] == pytest.approx(EXP_MAE_R)
    assert result.finalized() is True
    assert result.stats["source_blocked"] == {
        "source_fetch_error": 0,
        "source_incomplete": 0,
        "source_not_yet_available": 0,
    }
    assert result.stats["coverage_blocked"] == {}
    assert result.stats["finalization_blocked"] == 0


# ══════════════════════════════════════════════════════════════════
# T02 — first required candle missing -> no evaluation write
# ══════════════════════════════════════════════════════════════════


def test_t02_missing_first_required_candle_no_write():
    candles = drop(full_path(SIGNAL, 240), offset_minutes=5)

    result = UnitHarness.evaluate(candles, NOW_FULL)

    assert result.writes == [], "no evaluation write without the first candle"
    assert result.stats["coverage_blocked"].get("FIRST_REQUIRED_CANDLE_MISSING")
    for label, _ in HORIZONS:
        assert result.stats["horizons_pending"].get(label) == 1
    assert result.finalized() is False

    events = result.stats["diagnostics"]
    assert len(events) == 5
    for event in events:
        assert event["event"] == "coverage_blocked"
        assert event["reason_code"] == "FIRST_REQUIRED_CANDLE_MISSING"
        assert event["signal_id"] == 1
        assert event["symbol"] == SYMBOL
        assert event["experiment_id"] == EXPERIMENT_ID


def test_t02_cycle_missing_first_candle_stays_retry_eligible():
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)
    candles = drop(full_path(signal_time, 240), offset_minutes=5,
                   signal=signal_time)

    result = cycle.run(FakeBybitClient(candles))

    assert result.writes == []
    assert result.stats["finalized"] == 0
    assert result.stats["errors"] == 0, "coverage block is not a fetch error"
    assert result.stats["coverage_blocked"].get("FIRST_REQUIRED_CANDLE_MISSING")
    assert cycle.eligible_count == 1, "signal must stay retry-eligible"


# ══════════════════════════════════════════════════════════════════
# T03 — gap in the middle -> no false finalization
# ══════════════════════════════════════════════════════════════════


def test_t03_middle_gap_no_false_finalization():
    # Candle at signal+30m: inside the 60m/120m/240m windows only.
    candles = drop(full_path(SIGNAL, 240), offset_minutes=30)

    result = UnitHarness.evaluate(candles, NOW_FULL)

    assert result.evaluated("15m") and result.evaluated("30m")
    assert not result.evaluated("60m")
    assert not result.evaluated("120m")
    assert not result.evaluated("240m")
    assert result.finalized() is False, "gap must block finalization"
    assert result.stats["coverage_blocked"].get("CANDLE_GAP") == 3
    assert result.stats["finalization_blocked"] == 0  # all_done never reached
    # No TP/SL verdict may be written without the proven final window.
    for write in result.writes:
        for key in TP_SL_FIELDS:
            assert key not in write


# ══════════════════════════════════════════════════════════════════
# T04 — last required candle missing -> horizon stays pending
# ══════════════════════════════════════════════════════════════════


def test_t04_missing_last_required_candle_horizon_pending():
    # Candle at signal+235m is the LAST required candle of the 240m window.
    candles = drop(full_path(SIGNAL, 240), offset_minutes=235)

    result = UnitHarness.evaluate(candles, NOW_FULL)

    for label in ("15m", "30m", "60m", "120m"):
        assert result.evaluated(label), f"{label} must still be evaluated"
    assert not result.evaluated("240m"), "240m must stay pending"
    assert result.finalized() is False
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")
    assert result.stats["horizons_pending"].get("240m") == 1
    for write in result.writes:
        for key in TP_SL_FIELDS:
            assert key not in write


# ══════════════════════════════════════════════════════════════════
# T05 — an unclosed required candle is never used
# ══════════════════════════════════════════════════════════════════


def test_t05_unclosed_required_candle_not_used():
    # Off-grid signal (10:02) at ASOF 10:17: the 15m horizon is mature, but
    # the required 10:15 candle only closes at 10:20 — it is still forming.
    signal = datetime(2026, 10, 1, 10, 2, tzinfo=UTC)
    asof_unmatured = signal + timedelta(minutes=15)          # 10:17
    asof_closed = signal + timedelta(minutes=18)             # 10:20
    candles = [
        candle(ms(signal) + m * 60_000)
        for m in (3, 8, 13)                                  # 10:05, 10:10, 10:15
    ]

    blocked = UnitHarness.evaluate(candles, asof_unmatured, signal_time=signal)
    assert blocked.writes == [], "forming candle must block the horizon"
    assert blocked.stats["coverage_blocked"].get("REQUIRED_CANDLE_NOT_CLOSED")
    assert blocked.stats["horizons_pending"].get("15m") == 1
    event = blocked.stats["diagnostics"][0]
    assert event["reason_code"] == "REQUIRED_CANDLE_NOT_CLOSED"
    assert event["status"] == guard.SOURCE_NOT_YET_AVAILABLE

    # Once the same candle is closed, the horizon is evaluated normally.
    closed = UnitHarness.evaluate(candles, asof_closed, signal_time=signal)
    assert closed.evaluated("15m"), "closed path must be evaluated"
    assert closed.write["mfe_15m"] == pytest.approx(EXP_MFE)
    assert not closed.evaluated("30m"), "30m not mature at 10:20"


# ══════════════════════════════════════════════════════════════════
# T06 — duplicated timestamp -> fail-closed
# ══════════════════════════════════════════════════════════════════


def test_t06_duplicate_timestamp_fail_closed():
    candles = full_path(SIGNAL, 240)
    # Bogus duplicate of the first required candle with an absurd range.
    candles.append(candle(ms(SIGNAL) + 5 * 60_000, high=999.0, low=1.0,
                          close=500.0))

    result = UnitHarness.evaluate(candles, NOW_FULL)

    assert result.writes == [], "duplicate timestamps must block every horizon"
    assert result.stats["coverage_blocked"].get("DUPLICATE_TIMESTAMPS")
    assert result.finalized() is False


# ══════════════════════════════════════════════════════════════════
# T07 — invalid OHLC / off-grid / unordered -> fail-closed
# ══════════════════════════════════════════════════════════════════


def test_t07_invalid_ohlc_fail_closed():
    candles = full_path(SIGNAL, 240)
    # low > high: structurally impossible OHLC (first required candle).
    candles[0] = Candle(timestamp=candles[0].timestamp, open=100.0,
                        high=99.0, low=101.0, close=100.0, volume=1.0)

    result = UnitHarness.evaluate(candles, NOW_FULL)

    assert result.writes == [], "invalid OHLC must block every horizon"
    assert result.stats["coverage_blocked"].get("INVALID_OHLC")
    assert result.finalized() is False


def test_t07_non_finite_price_fail_closed():
    candles = corrupt(full_path(SIGNAL, 240), 5, "close", float("nan"))

    result = UnitHarness.evaluate(candles, NOW_FULL)

    assert result.writes == []
    assert result.stats["coverage_blocked"].get("INVALID_OHLC")
    assert result.finalized() is False


def test_t07_off_grid_shifted_path_fail_closed():
    """10:01/10:06/10:11 must never validate against its own anchor."""
    candles = shifted_path(SIGNAL, 240, shift_minutes=1)

    result = UnitHarness.evaluate(candles, NOW_FULL)

    assert result.writes == [], "shifted opens must not be evaluated"
    assert result.stats["coverage_blocked"].get("CANDLE_TIMESTAMP_OFF_GRID")
    assert result.finalized() is False
    for label, _ in HORIZONS:
        assert not result.evaluated(label), f"{label} must stay pending"


def test_t07_interval_shifted_path_fail_closed():
    """On-grid but shifted series: the FIRST required candle is missing."""
    candles = shifted_path(SIGNAL, 240, shift_minutes=5)

    result = UnitHarness.evaluate(candles, NOW_FULL)

    assert result.writes == []
    assert result.stats["coverage_blocked"].get("FIRST_REQUIRED_CANDLE_MISSING")
    assert result.finalized() is False


def test_t07_shuffled_path_fail_closed():
    candles = swap_candles(full_path(SIGNAL, 240), 60, 65)

    result = UnitHarness.evaluate(candles, NOW_FULL)

    assert result.writes == [], "an unordered path must not be evaluated"
    assert result.stats["coverage_blocked"].get("CANDLE_ORDER_INVALID")
    assert result.finalized() is False


# ══════════════════════════════════════════════════════════════════
# T08 — windows above 1000 candles: bounded pagination, never truncation
# ══════════════════════════════════════════════════════════════════


def test_t08_guard_paginates_rather_than_truncating():
    signal_time = old_signal_time()
    client = FakeBybitClient(full_path(signal_time, 240))

    result = guard.fetch_candles_for_evaluation(
        client, SYMBOL, signal_time, datetime.now(UTC))

    assert result.diagnostics["required_candle_count"] > 1000
    assert client.range_calls == 1, "must use explicit range pagination"
    assert client.kline_calls == 0, "latest-N must never serve a >1000 window"
    assert result.status == guard.SOURCE_VALIDATED


def test_t08_over_1000_candles_full_coverage_finalizes():
    signal_time = old_signal_time()
    cycle = SrrCycle(signal_time)
    client = FakeBybitClient(full_path(signal_time, 240))

    result = cycle.run(client)

    assert client.range_calls >= 1 and client.kline_calls == 0
    assert result.stats["errors"] == 0
    assert result.finalized() is True
    assert result.write["mfe_60m"] == pytest.approx(EXP_MFE)


def test_t08_over_1000_candles_without_range_source_is_pending():
    signal_time = old_signal_time()
    cycle = SrrCycle(signal_time)

    result = cycle.run(NoRangeClient(full_path(signal_time, 240)))

    assert result.writes == [], "no silent latest-N truncation is allowed"
    assert result.stats["errors"] >= 1
    assert result.stats["source_blocked"]["source_incomplete"] >= 1
    assert cycle.eligible_count == 1, "signal must stay retry-eligible"
    fetch = cycle.evaluator._last_fetch_result
    assert fetch.status == guard.SOURCE_INCOMPLETE_COVERAGE
    assert fetch.reason_code == "PAGINATION_UNAVAILABLE"


def test_t08_truncated_range_never_finalizes():
    signal_time = old_signal_time()
    cycle = SrrCycle(signal_time)
    # Range source answers, but with the head of the window cut off.
    client = FakeBybitClient(full_path(signal_time, 240), range_slice=10)

    result = cycle.run(client)

    assert result.finalized() is False
    assert not result.evaluated("240m")
    assert result.stats["coverage_blocked"], "truncation must be diagnosed"
    assert cycle.eligible_count == 1


# ══════════════════════════════════════════════════════════════════
# T09 — API error / 10006 / limiter deny -> no write, no finalization
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "error, reason",
    [
        (BybitError("retCode=10006, retMsg=rate limit exceeded"),
         "BYBIT_RATE_LIMIT_EXHAUSTED"),
        (BybitRateLimitError("rate limiter denied: BYBIT_API_SKIP"),
         "BYBIT_RATE_LIMITER_DENY"),
        (BybitError("retCode=0, retMsg=Bad Request"), "SOURCE_FETCH_EXCEPTION"),
    ],
    ids=["api_10006", "limiter_deny", "api_error"],
)
def test_t09_source_error_no_write_retry_eligible(error, reason):
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)
    client = FakeBybitClient(full_path(signal_time, 240), error=error)

    result = cycle.run(client)

    assert result.writes == [], "source error must not produce any write"
    assert result.stats["errors"] >= 1
    assert result.stats["source_blocked"]["source_fetch_error"] >= 1
    assert result.stats["finalized"] == 0
    assert cycle.eligible_count == 1, "retry eligibility must be preserved"

    # Diagnostics classify the failure distinctly from incomplete coverage.
    status = cycle.evaluator._last_fetch_result
    assert status is not None and status.status == guard.SOURCE_FETCH_ERROR
    assert status.reason_code == reason

    # Structured source_blocked event carries the full identity.
    events = [e for e in result.stats["diagnostics"]
              if e["event"] == "source_blocked"]
    assert events and events[0]["reason_code"] == reason
    assert events[0]["signal_id"] == 1
    assert events[0]["symbol"] == SYMBOL
    assert events[0]["experiment_id"] == EXPERIMENT_ID


def test_t09_limiter_deny_makes_no_fallback_request():
    """After a Global Limiter deny there is exactly ONE attempt: no fallback
    kline call, no range call, no direct-HTTP retry."""
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)
    client = FakeBybitClient(
        full_path(signal_time, 240),
        error=BybitRateLimitError("rate limiter denied: BYBIT_API_SKIP"),
    )

    result = cycle.run(client)

    assert result.writes == []
    assert client.kline_calls == 1, "exactly one attempt, then fail closed"
    assert client.range_calls == 0, "no fallback range request on deny"
    assert cycle.evaluator._last_fetch_result.reason_code == (
        "BYBIT_RATE_LIMITER_DENY")


# ══════════════════════════════════════════════════════════════════
# T10 — deny, then source recovery -> correct evaluation afterwards
# ══════════════════════════════════════════════════════════════════


def test_t10_deny_then_recovery_evaluates_correctly():
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)
    candles = full_path(signal_time, 240)

    denied = cycle.run(
        FakeBybitClient(candles, error=BybitRateLimitError("limiter deny")))
    assert denied.writes == []
    assert denied.stats["source_blocked"]["source_fetch_error"] >= 1

    recovered = cycle.run(FakeBybitClient(candles))
    assert recovered.write is not None, "recovery must evaluate the signal"
    for label, _ in HORIZONS:
        assert recovered.write[f"mfe_{label}"] == pytest.approx(EXP_MFE)
        assert recovered.write[f"mae_{label}"] == pytest.approx(EXP_MAE)
        assert recovered.write[f"mfe_r_{label}"] == pytest.approx(EXP_MFE_R)
        assert recovered.write[f"mae_r_{label}"] == pytest.approx(EXP_MAE_R)
    assert recovered.finalized() is True
    assert recovered.stats["signals_checked"] == 1
    assert recovered.stats["errors"] == 0


def test_t10_coverage_gap_then_recovered_path_evaluates():
    """A coverage-blocked cycle followed by complete data produces exactly the
    results the uninterrupted complete path would have produced."""
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)
    complete = full_path(signal_time, 240)
    broken = drop(complete, offset_minutes=90, signal=signal_time)

    blocked = cycle.run(FakeBybitClient(broken))
    # Only the PROVEN horizons are written; the unproven ones stay pending.
    assert blocked.evaluated("60m")
    assert not blocked.evaluated("120m")
    assert not blocked.evaluated("240m")
    assert blocked.finalized() is False
    assert blocked.stats["coverage_blocked"]

    recovered = cycle.run(FakeBybitClient(complete))
    reference_cycle = SrrCycle(signal_time)
    reference_cycle.run(FakeBybitClient(complete))

    def _state(outcome: dict) -> dict:
        # evaluated_*_at carry the wall-clock ASOF; everything else must be
        # byte-identical between a recovered and an uninterrupted run.
        return {k: v for k, v in outcome.items()
                if not k.startswith("evaluated_")}

    assert recovered.evaluated("120m") and recovered.evaluated("240m")
    assert recovered.finalized() is True
    # Merged outcome state identical to an uninterrupted complete-path run.
    assert _state(cycle.outcomes[1]) == _state(reference_cycle.outcomes[1])
    assert cycle.outcomes[1].get("is_final") is True


# ══════════════════════════════════════════════════════════════════
# T11 — restart / rerun: no duplication, no finalized rewrite
# ══════════════════════════════════════════════════════════════════


def test_t11_rerun_is_idempotent():
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)
    client = FakeBybitClient(full_path(signal_time, 240))

    first = cycle.run(client)
    assert first.write is not None and first.finalized() is True
    first_writes = list(first.writes)

    second = cycle.run(client)

    assert second.stats["signals_checked"] == 0, "finalized row is not eligible"
    assert second.writes == first_writes, "rerun must not write again"
    assert len(second.writes) == len(first_writes)


def test_t11_restart_never_duplicates_or_rewrites_finalized():
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)
    client = FakeBybitClient(full_path(signal_time, 240))

    first = cycle.run(client)
    assert first.finalized() is True
    first_writes = list(first.writes)
    first_outcome = dict(cycle.outcomes[1])

    cycle.restart()   # fresh SRRResearchEvaluator object, same DB state
    after_restart = cycle.run(client)

    assert after_restart.stats["signals_checked"] == 0
    assert after_restart.writes == first_writes
    assert cycle.outcomes[1] == first_outcome, "finalized row is byte-stable"


def test_t11_rerun_never_rewrites_proven_horizons():
    candles = full_path(SIGNAL, 240)
    partial = drop(full_path(SIGNAL, 240), offset_minutes=90)

    first = UnitHarness.evaluate(candles, NOW_FULL)
    assert first.write is not None

    already = {field: first.write.get(field) for field in ALL_EVALUATED_FIELDS}
    second = UnitHarness.evaluate(candles, NOW_FULL, **already, is_final=True)

    assert second.writes == [], "proven + finalized row must not be rewritten"

    # ...and a blocked rerun adds no new horizon markers either.
    blocked_again = UnitHarness.evaluate(partial, NOW_FULL, **already,
                                         is_final=False)
    assert not blocked_again.evaluated("240m")
    assert blocked_again.stats["coverage_blocked"].get("CANDLE_GAP")


# ══════════════════════════════════════════════════════════════════
# T12 — TP/SL event inside a missing candle: no false verdict
# ══════════════════════════════════════════════════════════════════


def test_t12_sl_inside_missing_candle_no_false_verdict():
    # The candle at signal+45m would hit the LONG stop (low 97.0 <= 98.0).
    complete = full_path(SIGNAL, 240)
    sl_index = next(
        i for i, c in enumerate(complete)
        if c.timestamp == ms(SIGNAL) + 45 * 60_000)
    complete_with_sl = list(complete)
    complete_with_sl[sl_index] = candle(ms(SIGNAL) + 45 * 60_000, low=97.0)

    # Control: with the candle present the SL verdict is produced.
    control = UnitHarness.evaluate(complete_with_sl, NOW_FULL)
    assert control.finalized() is True
    assert control.write.get("sl_hit") is True
    assert control.write.get("tp_hit") is False
    assert control.write.get("sl_before_tp") is True
    ref = UnitHarness.reference_tpsl(complete_with_sl)
    for key in TP_SL_FIELDS:
        assert control.write.get(key) is ref[key]

    # Same window, but the candle carrying the SL is missing (a gap):
    # no verdict at all may be produced.
    without = UnitHarness.evaluate(
        drop(complete_with_sl, offset_minutes=45), NOW_FULL)
    assert not without.evaluated("60m")
    assert without.finalized() is False
    for write in without.writes:
        for key in TP_SL_FIELDS:
            assert key not in write
    assert without.stats["coverage_blocked"].get("CANDLE_GAP")


# ══════════════════════════════════════════════════════════════════
# T13 — several horizons, coverage only for the early ones
# ══════════════════════════════════════════════════════════════════


def test_t13_only_proven_horizons_are_written():
    # History stops at signal+90m: 15m/30m/60m covered, 120m/240m not.
    cutoff_ms = ms(SIGNAL) + 90 * 60_000
    truncated = [c for c in full_path(SIGNAL, 240) if c.timestamp <= cutoff_ms]

    result = UnitHarness.evaluate(truncated, NOW_FULL)

    for label in ("15m", "30m", "60m"):
        assert result.evaluated(label), f"{label} coverage is proven"
    for label in ("120m", "240m"):
        assert not result.evaluated(label), f"{label} must stay pending"
        assert result.stats["horizons_pending"].get(label) == 1
    assert result.finalized() is False
    for write in result.writes:
        for key in TP_SL_FIELDS:
            assert key not in write


# ══════════════════════════════════════════════════════════════════
# T14 — cutoff / ASOF boundary: no lookahead, forming candle excluded
# ══════════════════════════════════════════════════════════════════


def test_t14_cutoff_boundary_no_lookahead():
    boundary = SIGNAL + timedelta(minutes=15)   # ASOF == 15m horizon cutoff
    candles = [
        candle(ms(SIGNAL) + 5 * 60_000),
        candle(ms(SIGNAL) + 10 * 60_000),
        # Candle opening exactly at the cutoff with absurd extremes — using
        # it would produce MFE = 899% instead of 1%.
        candle(ms(SIGNAL) + 15 * 60_000, high=999.0, low=1.0, close=500.0),
    ]

    # One millisecond before the cutoff the horizon is simply immature.
    immature = UnitHarness.evaluate(candles, boundary - timedelta(milliseconds=1))
    assert immature.writes == [], "immature horizon must not be written"

    result = UnitHarness.evaluate(candles, boundary)

    assert result.evaluated("15m"), "mature horizon with a closed path evaluates"
    assert result.write["mfe_15m"] == pytest.approx(EXP_MFE), (
        "the boundary candle must not contribute to MFE")
    assert result.write["mae_15m"] == pytest.approx(EXP_MAE)
    assert not result.evaluated("30m"), "later horizons are not mature yet"
    assert result.finalized() is False


# ══════════════════════════════════════════════════════════════════
# T15 — previously finalized records are never touched
# ══════════════════════════════════════════════════════════════════


def test_t15_finalized_record_is_never_rewritten():
    finalized = {field: NOW_FULL for field in ALL_EVALUATED_FIELDS}

    # (a) Complete source: a finalized row produces no write at all.
    complete = UnitHarness.evaluate(full_path(SIGNAL, 240), NOW_FULL,
                                    **finalized, is_final=True)
    assert complete.writes == []

    # (b) Even with an incomplete source nothing is written or un-finalized.
    broken = UnitHarness.evaluate([], NOW_FULL, **finalized, is_final=True)
    assert broken.writes == []
    assert broken.finalized() is False


def test_t15_eligibility_sql_excludes_finalized_rows():
    source = inspect.getsource(SRRResearchEvaluator.get_eligible_signals)
    assert "o.is_final = FALSE" in source, (
        "finalized rows must never be re-selected for evaluation")


# ══════════════════════════════════════════════════════════════════
# Finalization gate: stored marks are not proof of the TP/SL path
# ══════════════════════════════════════════════════════════════════


def test_finalization_gate_blocks_incomplete_tpsl_path():
    # Every horizon already proven (stored marks), but the TP/SL window
    # itself has a gap: stored marks alone must never finalize the row.
    marks = {f: NOW_FULL for f in ALL_EVALUATED_FIELDS}
    candles = drop(full_path(SIGNAL, 240), offset_minutes=90)

    result = UnitHarness.evaluate(candles, NOW_FULL, **marks, is_final=False)

    assert result.writes == [], "no write when the TP/SL window is incomplete"
    assert result.finalized() is False
    assert result.stats["finalization_blocked"] == 1
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")
    events = [e for e in result.stats["diagnostics"]
              if e["event"] == "finalization_blocked"]
    assert events and events[0]["reason_code"] == "CANDLE_GAP"
    assert events[0]["signal_id"] == 1
    assert events[0]["symbol"] == SYMBOL
    assert events[0]["experiment_id"] == EXPERIMENT_ID


def test_finalization_gate_blocks_invalid_tpsl_window():
    # Same with a structurally impossible candle inside the 240m window.
    marks = {f: NOW_FULL for f in ALL_EVALUATED_FIELDS}
    candles = corrupt(full_path(SIGNAL, 240), 120, "close", float("inf"))

    result = UnitHarness.evaluate(candles, NOW_FULL, **marks, is_final=False)

    assert result.writes == []
    assert result.finalized() is False
    assert result.stats["finalization_blocked"] == 1
    assert result.stats["coverage_blocked"].get("INVALID_OHLC")


# ══════════════════════════════════════════════════════════════════
# Diagnostics contract (stats + logs only, NO DB schema change)
# ══════════════════════════════════════════════════════════════════


def test_diagnostics_stats_shape_is_stable():
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)
    candles = drop(full_path(signal_time, 240), offset_minutes=90,
                   signal=signal_time)

    stats = cycle.run(FakeBybitClient(candles)).stats

    assert stats["experiment_id"] == EXPERIMENT_ID
    assert set(stats["source_blocked"]) == {
        "source_fetch_error", "source_incomplete", "source_not_yet_available",
    }
    assert stats["coverage_blocked"], "coverage_blocked counter required"
    assert set(stats["horizons_pending"]) == {h for h, _ in HORIZONS}
    assert stats["finalization_blocked"] == 0   # all_done never reached
    # Every structured event carries the required identity fields.
    for event in stats["diagnostics"]:
        for field in srr_module.DIAGNOSTIC_EVENT_FIELDS:
            assert field in event, f"diagnostic event lacks {field}"
        assert event["event"] in srr_module.DIAGNOSTIC_BLOCK_EVENTS
        assert event["experiment_id"] == EXPERIMENT_ID


def test_diagnostics_reason_codes_are_the_guard_vocabulary():
    """Reason codes are the shared guard's byte-stable vocabulary — the
    agreed future persistence marker set (still NO DB column)."""
    assert srr_module.DIAGNOSTIC_BLOCK_EVENTS == (
        "source_blocked", "coverage_blocked", "finalization_blocked",
    )
    assert guard.SOURCE_VALIDATED == "SOURCE_VALIDATED"
    assert guard.SOURCE_FETCH_ERROR == "SOURCE_FETCH_ERROR"
    assert guard.SOURCE_INCOMPLETE_COVERAGE == "SOURCE_INCOMPLETE_COVERAGE"
    assert guard.SOURCE_NOT_YET_AVAILABLE == "SOURCE_NOT_YET_AVAILABLE"
    assert guard.fetch_status_stats_key(
        guard.SOURCE_NOT_YET_AVAILABLE) == "source_not_yet_available"

    coverage = guard.CandleCoverageResult(
        guard.SOURCE_INCOMPLETE_COVERAGE, "CANDLE_GAP", {})
    stats: dict[str, Any] = {}
    guard.record_coverage_block(stats, coverage, horizon="60m")
    guard.record_finalization_block(stats, coverage)
    assert stats["coverage_blocked"]["CANDLE_GAP"] == 2
    assert stats["horizons_pending"]["60m"] == 1
    assert stats["finalization_blocked"] == 1


def test_diagnostics_never_reach_the_database_schema():
    """Until the reason-code schema is separately agreed, the evaluator must
    persist NOTHING beyond the frozen outcome upsert."""
    source = inspect.getsource(srr_module)
    assert "ALTER TABLE" not in source
    assert "CREATE TABLE" not in source
    assert "DROP " not in source

    save_source = inspect.getsource(SRRResearchEvaluator._save_outcome_partial)
    assert "diagnostics" not in save_source
    assert "block_reason" not in save_source
    assert "source_status" not in save_source
    # The frozen writeable column set is unchanged (migration 046).
    for col in ("mfe_240m", "evaluated_240m_at", "tp_hit", "sl_hit",
                "tp_before_sl", "sl_before_tp", "is_final"):
        assert f'"{col}"' in save_source


def test_diagnostics_event_list_is_bounded():
    stats: dict[str, Any] = {}
    coverage = guard.CandleCoverageResult(
        guard.SOURCE_INCOMPLETE_COVERAGE, "CANDLE_GAP", {})
    for i in range(srr_module.MAX_DIAGNOSTIC_EVENTS + 25):
        srr_module._record_block_event(
            stats, event="coverage_blocked", status=coverage.status,
            reason_code=coverage.reason_code, horizon="60m",
            signal_id=i, symbol=SYMBOL)
    assert len(stats["diagnostics"]) == srr_module.MAX_DIAGNOSTIC_EVENTS
    assert stats["diagnostics_truncated"] == 25


# ══════════════════════════════════════════════════════════════════
# Source wiring contracts
# ══════════════════════════════════════════════════════════════════


def test_source_uses_shared_guard_and_lost_the_1000_cap():
    with open("app/shadow/srr_research_evaluator.py", encoding="utf-8") as fh:
        source = fh.read()
    assert "candle_coverage_guard_v1" in source, (
        "the shared coverage guard must be the candle source")
    # The legacy silent-truncation pattern must be gone.
    assert "300_000 + 10, 1000" not in source
    assert "fetch_candles_for_evaluation" in source
    assert "validate_candle_coverage" in source


def test_source_keeps_the_empty_candle_guard_literal():
    """Contract locked in by tests/test_issue253_shared_limiter_alignment_v1:
    the literal ``if not candles`` chain must stay in the cycle."""
    with open("app/shadow/srr_research_evaluator.py", encoding="utf-8") as fh:
        source = fh.read()
    assert "if not candles" in source


def test_fetch_statuses_survive_a_full_cycle():
    signal_time = cycle_signal_time()
    cycle = SrrCycle(signal_time)
    candles = full_path(signal_time, 240)

    healthy = cycle.run(FakeBybitClient(candles))
    assert healthy.stats["errors"] == 0
    assert cycle.evaluator._last_fetch_result.status == (
        guard.SOURCE_VALIDATED)

    broken = SrrCycle(signal_time).run(NoRangeClient([]))
    assert broken.stats["source_blocked"]["source_incomplete"] == 1
