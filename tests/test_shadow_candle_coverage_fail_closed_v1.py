"""OOS_SHADOW_CANDLE_COVERAGE_FAIL_CLOSED_V1 — T01..T15 regression tests.

Protects the two P1 shadow evaluators:

* ``app.shadow.evaluator``      (ATR WICK REJECTION SHORT V1 shadow,
  ``dds.shadow_signal_outcome``, horizons 15m..240m + EOD);
* ``app.shadow.v2d_evaluator``  (ATR_WICK_FILTER_OOS_V2_D,
  ``dds.v2d_outcome``, horizons 15m..240m, no EOD).

The P0 SRR LONG evaluator is covered byte-for-byte by
``tests/test_srr_shadow_candle_coverage_fail_closed_v1.py``; the source
wiring contracts at the bottom of this module assert that ALL THREE
evaluators share the same guard, the same ``if not candles`` fail-closed
literal and the same FR-06 eligibility retry semantics, and that the
Generic PR #257 evaluator keeps its guard.

Core invariant::

    INCOMPLETE CANDLE COVERAGE
        -> NO evaluated_*_at WRITE
        -> NO is_final

Scenario map (issue table):

=====  ==========================================================
T01    complete history -> results identical to the frozen formulas
T02    missing FIRST required candle -> no write, stays retryable
T03    gap inside the history -> no false finalization, proven
       horizons still written; stored marks alone never finalize
T04    missing LAST required candle -> horizon stays pending
T05    unclosed candle -> SOURCE_NOT_YET_AVAILABLE, never used
T06    duplicates / wrong order -> fail-closed
T07    Bybit error / limiter deny -> no unverified write, no
       fallback request
T08    window > 1000 candles -> bounded pagination is used
T09    incomplete pagination -> no finalization
T10    source recovery -> re-evaluation allowed (incl. the FR-06
       finalization-retry disjunct for V2D)
T11    rerun / restart -> idempotent, no duplicates, no rewrite
T12    TP/SL event inside a skipped candle -> no unproven verdict
T13    cutoff / ASOF boundary -> no look-ahead, forming candle
       excluded
T14    already finalized record -> never touched
T15    frozen protocol parity on a complete path
=====  ==========================================================

Deterministic fixtures, in-memory fake sources and in-memory persistence
only: no network, no real Bybit, no PostgreSQL, no production DB.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from app.exchange.bybit_client import BybitError, BybitRateLimitError
from app.models import Candle
from app.research import candle_coverage_guard_v1 as guard
from app.shadow import evaluator as v1_module
from app.shadow import v2d_evaluator as v2d_module
from app.shadow.evaluator import (
    EXPERIMENT_ID as V1_EXPERIMENT_ID,
    HORIZONS,
    ShadowSignalEvaluator,
    _calculate_mfe_mae_for_window as v1_mfe_mae,
    _check_target_achievement as v1_target_flags,
    _get_eod_exclusive,
    _is_eod_mature,
)
from app.shadow.v2d_evaluator import (
    EXPERIMENT_ID as V2D_EXPERIMENT_ID,
    V2DEvaluator,
    _calculate_mfe_mae_for_window as v2d_mfe_mae,
    _check_target_achievement as v2d_target_flags,
)

UTC = timezone.utc
INTERVAL_MS = 300_000
SYMBOL = "BTCUSDT"
PRICE = 100.0

# Grid-aligned signal so every horizon cutoff lands on the 5-minute grid.
SIGNAL = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
# V1: EOD boundary of SIGNAL's UTC day.
EOD = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
# V1 evaluation ASOF: every horizon AND the EOD boundary matured.
NOW_V1 = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)
# V2D evaluation ASOF: 240m matured + 5m (both on the grid).
NOW_V2D = SIGNAL + timedelta(minutes=250)

# Complete-path reference values of the frozen formulas (default candles:
# open 100, high 100.5, low 99.5, close 100, SHORT at 100):
#   mfe = (entry - low)  / entry * 100 = 0.5
#   mae = (high - entry) / entry * 100 = 0.5
#   targets: low 99.5 <= 99.5 -> reached_minus_0_5 True, deeper False.
EXP_MFE = 0.5
EXP_MAE = 0.5


# ── specs ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Spec:
    """Everything that differs between the two P1 evaluators."""

    key: str
    cls: type
    has_eod: bool
    now: datetime
    experiment_id: str
    verdict_end: datetime
    # Frozen reference functions (untouched by this issue).  They take the
    # signal time explicitly so cycle tests (real wall-clock signals) and
    # unit tests (the fixed SIGNAL fixture) share one parity path.
    ref_mfe: Callable[..., tuple[float | None, float | None]]
    ref_eod: Callable[..., tuple[float | None, float | None]] | None
    ref_flags: Callable[..., dict[str, bool]]


V1_SPEC = Spec(
    key="shadow_v1",
    cls=ShadowSignalEvaluator,
    has_eod=True,
    now=NOW_V1,
    experiment_id=V1_EXPERIMENT_ID,
    verdict_end=EOD,
    ref_mfe=lambda candles, minutes, sig=SIGNAL: v1_mfe_mae(
        candles, PRICE, minutes, sig),
    ref_eod=lambda candles, sig=SIGNAL: v1_mfe_mae(
        candles, PRICE, None, sig, eod_exclusive=_get_eod_exclusive(sig)),
    ref_flags=lambda candles, sig=SIGNAL: v1_target_flags(
        candles, PRICE, eod_exclusive=_get_eod_exclusive(sig)),
)

V2D_SPEC = Spec(
    key="v2d",
    cls=V2DEvaluator,
    has_eod=False,
    now=NOW_V2D,
    experiment_id=V2D_EXPERIMENT_ID,
    verdict_end=NOW_V2D,
    ref_mfe=lambda candles, minutes, sig=SIGNAL: v2d_mfe_mae(
        candles, PRICE, minutes, sig),
    ref_eod=None,
    ref_flags=lambda candles, sig=SIGNAL: v2d_target_flags(candles, PRICE),
)

SPECS = [V1_SPEC, V2D_SPEC]
SPEC_IDS = [s.key for s in SPECS]


def evaluated_fields(spec: Spec) -> list[str]:
    fields = [f"evaluated_{label}_at" for label, _ in HORIZONS]
    if spec.has_eod:
        fields.append("evaluated_eod_at")
    return fields


# ── candle fixtures ──────────────────────────────────────────────────


def ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def candle(open_ms: int, *, high: float = 100.5, low: float = 99.5,
           close: float = 100.0, open_: float = 100.0) -> Candle:
    return Candle(timestamp=open_ms, open=open_, high=high, low=low,
                  close=close, volume=1000.0)


def path(signal: datetime, end: datetime) -> list[Candle]:
    """Complete ascending grid path of every candle in (signal, end)."""
    out: list[Candle] = []
    t = signal + timedelta(minutes=5)
    while t < end:
        out.append(candle(ms(t)))
        t += timedelta(minutes=5)
    return out


def drop(candles: list[Candle], *, offset_minutes: int,
         signal: datetime = SIGNAL) -> list[Candle]:
    target = ms(signal) + offset_minutes * 60_000
    return [c for c in candles if c.timestamp != target]


def corrupt(candles: list[Candle], *, offset_minutes: int, field: str,
            value: float, signal: datetime = SIGNAL) -> list[Candle]:
    target = ms(signal) + offset_minutes * 60_000
    out = []
    for c in candles:
        if c.timestamp == target:
            data = {"timestamp": c.timestamp, "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close, "volume": c.volume}
            data[field] = value
            out.append(Candle(**data))
        else:
            out.append(c)
    return out


def full_path(spec: Spec) -> list[Candle]:
    return path(SIGNAL, spec.verdict_end)


def complete(spec: Spec) -> list[Candle]:
    """Alias: the complete verdict-window path of *spec*."""
    return full_path(spec)


# ── signal + stats factories ─────────────────────────────────────────


def p1_signal(spec: Spec, signal_time: datetime = SIGNAL,
              **overrides: Any) -> dict:
    sig: dict[str, Any] = {
        "signal_id": 1,
        "symbol": SYMBOL,
        "signal_time": signal_time,
        "signal_price": PRICE,
        "outcome_id": None,
        "is_final": False,
    }
    for field in evaluated_fields(spec):
        sig[field] = None
    sig.update(overrides)
    return sig


def fresh_stats(spec: Spec) -> dict[str, Any]:
    """Mirror of run_evaluation_cycle's stats dict (in-memory only)."""
    horizons = {label: 0 for label, _ in HORIZONS}
    if spec.has_eod:
        horizons = {**horizons, "eod": 0}
    return {
        "experiment_id": spec.experiment_id,
        "signals_checked": 0,
        "symbols_processed": 0,
        "horizons_updated": dict(horizons),
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
        "horizons_pending": dict(horizons),
        "finalization_blocked": 0,
    }


def all_marks(spec: Spec, when: datetime) -> dict[str, Any]:
    return {field: when for field in evaluated_fields(spec)}


# ── in-memory persistence ────────────────────────────────────────────


class StatefulRepo:
    """In-memory mirror of get_eligible_signals + save_outcome_partial.

    Mirrors the production eligibility semantics of BOTH repositories:
    no outcome -> eligible after 15m; outcome -> eligible while
    ``is_final = FALSE`` AND at least one MATURE horizon is unevaluated,
    OR (FR-06) all horizon marks are present but the finalization gate
    was blocked.  Finalized rows are never selected.
    """

    def __init__(self, spec: Spec, signal_time: datetime) -> None:
        self.spec = spec
        self.signal_time = signal_time
        self.outcome: dict | None = None
        self.writes: list[dict] = []

    def get_eligible_signals(self, limit: int = 5000) -> list[dict]:
        now = datetime.now(UTC)
        fields = evaluated_fields(self.spec)
        if self.outcome is None:
            if self.signal_time > now - timedelta(minutes=15):
                return []
            row_outcome: dict | None = None
        else:
            if self.outcome.get("is_final"):
                return []
            pending = any(
                self.outcome.get(f"evaluated_{label}_at") is None
                and self.signal_time + timedelta(minutes=minutes) <= now
                for label, minutes in HORIZONS
            )
            if (
                self.spec.has_eod
                and self.outcome.get("evaluated_eod_at") is None
                and _is_eod_mature(self.signal_time, now)
            ):
                pending = True
            all_present = all(
                self.outcome.get(field) is not None for field in fields)
            if not (pending or all_present):
                return []
            row_outcome = self.outcome
        row = p1_signal(self.spec, self.signal_time)
        if row_outcome is not None:
            row["outcome_id"] = 1
            for field in fields:
                row[field] = row_outcome.get(field)
            row["is_final"] = bool(row_outcome.get("is_final"))
        return [row][:limit]

    def save_outcome_partial(self, signal_id: int, symbol: str,
                             **updates: Any) -> bool:
        # Mirrors _save_outcome_partial: NULL values are dropped and
        # is_final is only ever written when True.
        filtered = {k: v for k, v in updates.items() if v is not None}
        self.writes.append(dict(filtered))
        if self.outcome is None:
            self.outcome = {}
        self.outcome.update(filtered)
        return True

    @property
    def eligible_count(self) -> int:
        return len(self.get_eligible_signals(limit=5000))


# ── fake clients ─────────────────────────────────────────────────────


class FakeClient:
    """Fake Bybit source with classified errors and truncation hooks."""

    def __init__(self, candles: list[Candle], *, error: Exception | None = None,
                 range_keep: int = 0, sort: bool = True) -> None:
        self.candles = sorted(candles, key=lambda c: c.timestamp) if sort \
            else list(candles)
        self.error = error
        self.range_keep = range_keep
        self.kline_calls = 0
        self.range_calls = 0

    def get_klines(self, symbol: str, interval: str = "5",
                   limit: int = 200) -> list[Candle]:
        self.kline_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.candles[-limit:])

    def get_klines_range(self, symbol: str, interval: str = "5", *,
                         start_ms: int, end_ms: int, page_size: int = 1000,
                         max_pages: int = 200) -> list[Candle]:
        self.range_calls += 1
        if self.error is not None:
            raise self.error
        step = int(interval) * 60_000
        pages = (int(end_ms) - int(start_ms)) // (int(page_size) * step) + 1
        if pages > max_pages:
            raise BybitError(
                f"{symbol}: kline range fetch exceeded {max_pages} pages")
        out = [c for c in self.candles
               if start_ms <= c.timestamp <= end_ms]
        if self.range_keep:
            out = out[:self.range_keep]   # drop the TAIL of the window
        return out


class NoRangeClient(FakeClient):
    """A client without a genuine historical range source (MagicMock-style)."""

    get_klines_range = None


# ── harnesses ────────────────────────────────────────────────────────


@dataclass
class EvalResult:
    writes: list[dict]
    stats: dict[str, Any]

    @property
    def write(self) -> dict:
        return self.writes[-1] if self.writes else {}

    def evaluated(self, label: str) -> bool:
        return any(f"evaluated_{label}_at" in w for w in self.writes)

    def finalized(self) -> bool:
        return any(w.get("is_final") for w in self.writes)

    def wrote_target_flags(self) -> bool:
        return any("reached_minus_0_5" in w for w in self.writes)


class UnitHarness:
    """Direct _evaluate_signal_incremental runs with a frozen ``now``."""

    @staticmethod
    def evaluate(spec: Spec, candles: list[Candle],
                 now: datetime | None = None, *,
                 signal_time: datetime = SIGNAL,
                 **overrides: Any) -> EvalResult:
        repo = StatefulRepo(spec, signal_time)
        evaluator = spec.cls(client=None, repo=repo)
        stats = fresh_stats(spec)
        sig = p1_signal(spec, signal_time, **overrides)
        evaluator._evaluate_signal_incremental(
            sig, list(candles), now or spec.now, stats)
        return EvalResult(writes=list(repo.writes), stats=stats)


class CycleHarness:
    """Full run_evaluation_cycle with in-memory state (real wall clock)."""

    def __init__(self, spec: Spec, signal_time: datetime) -> None:
        self.spec = spec
        self.signal_time = signal_time
        self.repo = StatefulRepo(spec, signal_time)
        self.evaluator = spec.cls(client=None, repo=self.repo)

    def restart(self) -> None:
        """Simulate a systemd restart: fresh object, same persistent state."""
        self.evaluator = self.spec.cls(client=None, repo=self.repo)

    def run(self, client: FakeClient) -> EvalResult:
        before = len(self.repo.writes)
        self.evaluator.client = client
        stats = self.evaluator.run_evaluation_cycle()
        # Only the writes produced BY THIS RUN: rerun tests must observe
        # "no new write", not the cumulative history of the row.
        return EvalResult(writes=list(self.repo.writes[before:]), stats=stats)


def cycle_signal(spec: Spec) -> datetime:
    now = datetime.now(UTC)
    if spec.has_eod:
        # Yesterday 12:00 UTC: every horizon AND the EOD boundary matured
        # regardless of when the suite runs.
        return datetime(now.year, now.month, now.day, 12, 0,
                        tzinfo=UTC) - timedelta(days=1)
    # Grid-aligned 250 minutes ago: all five horizons matured and closed.
    floor = int(now.timestamp()) // 300 * 300
    return datetime.fromtimestamp(floor - 250 * 60, UTC)


def old_cycle_signal(spec: Spec) -> datetime:
    """Grid-aligned signal 4 days ago: the fetch window exceeds one page."""
    floor = int(datetime.now(UTC).timestamp()) // 300 * 300
    return datetime.fromtimestamp(floor - 4 * 24 * 3600, UTC)


def cycle_candles(spec: Spec, signal_time: datetime) -> list[Candle]:
    if spec.has_eod:
        # V1 verdict window ends at EOD; nothing beyond EOD is demanded.
        eod = _get_eod_exclusive(signal_time)
        return path(signal_time, eod)
    # V2D verdict window runs to the evaluation ASOF: include the CURRENT
    # grid open as well — the fetch drops it while still unclosed, and the
    # extra candle keeps the path race-free if the wall clock crosses a
    # 5-minute boundary while the test runs.
    floor = int(datetime.now(UTC).timestamp()) // 300 * 300
    return path(signal_time, datetime.fromtimestamp(floor + 300, UTC))


def old_cycle_candles(spec: Spec, signal_time: datetime) -> list[Candle]:
    if spec.has_eod:
        return path(signal_time, _get_eod_exclusive(signal_time))
    floor = int(datetime.now(UTC).timestamp()) // 300 * 300
    return path(signal_time, datetime.fromtimestamp(floor + 300, UTC))


# ════════════════════════════════════════════════════════════════════
# T01 — complete path: frozen results + complete-path parity
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t01_complete_path_writes_expected_results(spec: Spec):
    candles = complete(spec)

    result = UnitHarness.evaluate(spec, candles)

    # Every mature horizon is written exactly once, with the complete path.
    for label, _ in HORIZONS:
        assert result.evaluated(label), f"{label} must be evaluated"
        assert result.write[f"mfe_{label}"] == pytest.approx(EXP_MFE)
        assert result.write[f"mae_{label}"] == pytest.approx(EXP_MAE)
    if spec.has_eod:
        assert result.evaluated("eod")
        assert result.write["mfe_eod"] == pytest.approx(EXP_MFE)
        assert result.write["mae_eod"] == pytest.approx(EXP_MAE)
    else:
        assert "evaluated_eod_at" not in result.write

    # Finalization produced the frozen target flags.
    assert result.finalized() is True
    assert result.write["reached_minus_0_5"] is True
    assert result.write["reached_minus_1_5"] is False
    assert result.stats["coverage_blocked"] == {}
    assert result.stats["finalization_blocked"] == 0
    assert result.stats["errors"] == 0


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t01_complete_path_cycle_finalizes(spec: Spec):
    signal_time = cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)
    candles = cycle_candles(spec, signal_time)

    result = harness.run(FakeClient(candles))

    assert result.stats["signals_checked"] == 1
    assert result.stats["outcomes_created"] == 1
    assert result.stats["errors"] == 0
    for label, _ in HORIZONS:
        assert result.stats["horizons_updated"][label] == 1
    if spec.has_eod:
        assert result.stats["horizons_updated"]["eod"] == 1
    assert result.stats["finalized"] == 1
    # Parity: every written value equals the untouched frozen reference
    # computed over the very same complete path.
    for label, minutes in HORIZONS:
        ref_mfe, ref_mae = spec.ref_mfe(candles, minutes, signal_time)
        assert result.write[f"mfe_{label}"] == pytest.approx(ref_mfe)
        assert result.write[f"mae_{label}"] == pytest.approx(ref_mae)
    if spec.has_eod:
        ref_mfe, ref_mae = spec.ref_eod(candles, signal_time)
        assert result.write["mfe_eod"] == pytest.approx(ref_mfe)
        assert result.write["mae_eod"] == pytest.approx(ref_mae)
    ref_flags = spec.ref_flags(candles, signal_time)
    for key, value in ref_flags.items():
        assert result.write[key] is value


# ════════════════════════════════════════════════════════════════════
# T02 — missing FIRST required candle
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t02_missing_first_required_candle_no_write(spec: Spec):
    candles = drop(complete(spec), offset_minutes=5)

    result = UnitHarness.evaluate(spec, candles)

    assert result.writes == [], "an incomplete path writes nothing"
    assert result.stats["coverage_blocked"].get(
        "FIRST_REQUIRED_CANDLE_MISSING")
    assert result.stats["horizons_pending"]["15m"] == 1
    if spec.has_eod:
        assert result.stats["horizons_pending"]["eod"] == 1
    assert result.finalized() is False


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t02_cycle_missing_first_candle_stays_retry_eligible(spec: Spec):
    signal_time = cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)
    candles = drop(cycle_candles(spec, signal_time), offset_minutes=5,
                   signal=signal_time)

    result = harness.run(FakeClient(candles))

    assert result.writes == []
    assert result.stats["source_blocked"] == {
        "source_fetch_error": 0, "source_incomplete": 0,
        "source_not_yet_available": 0,
    }
    assert result.stats["coverage_blocked"]
    # The row was never touched -> it is still selected for a retry.
    assert harness.repo.eligible_count == 1
    assert harness.repo.outcome is None


# ════════════════════════════════════════════════════════════════════
# T03 — gap inside the history: no false finalization
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t03_middle_gap_no_false_finalization(spec: Spec):
    # The candle at +90m is inside the 120m/240m (and V1 EOD) windows but
    # AFTER the 15m/30m/60m windows.
    candles = drop(complete(spec), offset_minutes=90)

    result = UnitHarness.evaluate(spec, candles)

    for label in ("15m", "30m", "60m"):
        assert result.evaluated(label), f"{label} coverage is proven"
    for label in ("120m", "240m"):
        assert not result.evaluated(label), f"{label} must stay pending"
        assert result.stats["horizons_pending"][label] == 1
    if spec.has_eod:
        assert not result.evaluated("eod")
        assert result.stats["horizons_pending"]["eod"] == 1
    assert result.finalized() is False
    assert not result.wrote_target_flags()
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")
    # Confirmed horizons were written despite the other horizon failing.
    assert result.stats["horizons_updated"]["15m"] == 1
    assert result.stats["horizons_updated"]["60m"] == 1


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t03_stored_marks_are_not_finalization_proof(spec: Spec):
    """All horizon marks present (stored) but the verdict window has a gap:
    stored marks alone must never finalize the row (FR-04)."""
    if spec.has_eod:
        # V1: gap AFTER 240m, still inside the EOD verdict window.
        gap_offset = 300
    else:
        # V2D: gap AFTER 240m, inside the to-ASOF verdict window.
        gap_offset = 245
    candles = drop(complete(spec), offset_minutes=gap_offset)

    result = UnitHarness.evaluate(spec, candles,
                                  **all_marks(spec, spec.now))

    assert result.writes == [], "no write when the verdict path is incomplete"
    assert result.finalized() is False
    assert result.stats["finalization_blocked"] == 1
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")
    events = [e for e in result.stats["diagnostics"]
              if e["event"] == "finalization_blocked"]
    assert events and events[0]["reason_code"] == "CANDLE_GAP"
    assert events[0]["signal_id"] == 1
    assert events[0]["symbol"] == SYMBOL
    assert events[0]["experiment_id"] == spec.experiment_id


# ════════════════════════════════════════════════════════════════════
# T04 — missing LAST required candle -> horizon pending
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t04_missing_last_required_candle_horizon_pending(spec: Spec):
    boundary = SIGNAL + timedelta(minutes=15)
    # Only the first required candle exists: the grid open just below the
    # 15m cutoff (signal+10m) is missing while already closed.
    candles = [candle(ms(SIGNAL + timedelta(minutes=5)))]

    result = UnitHarness.evaluate(spec, candles, now=boundary)

    assert result.writes == []
    assert not result.evaluated("15m")
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")
    assert result.stats["horizons_pending"]["15m"] == 1
    assert result.stats["horizons_updated"]["15m"] == 0


# ════════════════════════════════════════════════════════════════════
# T05 — unclosed candle: never used, evaluated only after it closes
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t05_unclosed_required_candle_not_used(spec: Spec):
    # Off-grid signal 10:02: the 15m window (10:02, 10:17) requires the
    # GRID opens 10:05 / 10:10 / 10:15 — and 10:15 only closes at 10:20.
    signal_time = datetime(2026, 10, 1, 10, 2, tzinfo=UTC)
    candles = [
        candle(ms(datetime(2026, 10, 1, 10, minute, tzinfo=UTC)))
        for minute in (5, 10, 15)
    ]

    # At maturity (10:17) the last required candle is still forming.
    blocked = UnitHarness.evaluate(
        spec, candles, now=datetime(2026, 10, 1, 10, 17, tzinfo=UTC),
        signal_time=signal_time)
    assert blocked.writes == []
    assert blocked.stats["coverage_blocked"].get(
        "REQUIRED_CANDLE_NOT_CLOSED")
    assert blocked.stats["horizons_pending"]["15m"] == 1

    # Once it closes (10:20) the very same window evaluates normally.
    done = UnitHarness.evaluate(
        spec, candles, now=datetime(2026, 10, 1, 10, 20, tzinfo=UTC),
        signal_time=signal_time)
    assert done.evaluated("15m")
    assert done.write["mfe_15m"] == pytest.approx(EXP_MFE)
    assert done.stats["coverage_blocked"] == {}


# ════════════════════════════════════════════════════════════════════
# T06 — duplicates / wrong order
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t06_duplicate_timestamp_fail_closed(spec: Spec):
    candles = complete(spec)
    duplicated = candles + [candle(ms(SIGNAL + timedelta(minutes=10)))]

    result = UnitHarness.evaluate(spec, duplicated)

    assert result.writes == []
    assert result.stats["coverage_blocked"].get("DUPLICATE_TIMESTAMPS")
    assert result.stats["horizons_pending"]["15m"] == 1


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t06_shuffled_path_fail_closed(spec: Spec):
    candles = list(reversed(complete(spec)))

    result = UnitHarness.evaluate(spec, candles)

    assert result.writes == []
    assert result.stats["coverage_blocked"].get("CANDLE_ORDER_INVALID")
    assert result.stats["horizons_pending"]["15m"] == 1


# ════════════════════════════════════════════════════════════════════
# T07 — Bybit error / limiter deny: no unverified write, no fallback
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
@pytest.mark.parametrize(
    "error, reason_code",
    [
        (BybitRateLimitError("rate limiter denied: BYBIT_API_SKIP"),
         "BYBIT_RATE_LIMITER_DENY"),
        (BybitError("retCode=0, retMsg=Bad Request"),
         "SOURCE_FETCH_EXCEPTION"),
    ],
    ids=["limiter_deny", "bybit_error"],
)
def test_t07_source_error_no_write_retry_eligible(
    spec: Spec, error: Exception, reason_code: str,
):
    signal_time = cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)

    result = harness.run(FakeClient(cycle_candles(spec, signal_time),
                                    error=error))

    assert result.writes == [], "no outcome write on a failed source"
    assert result.stats["errors"] == 1
    assert result.stats["source_blocked"]["source_fetch_error"] == 1
    assert result.stats["coverage_blocked"] == {}
    # NO fallback request after the failure: exactly one attempt.
    assert result.stats["symbols_processed"] == 1
    assert harness.repo.eligible_count == 1
    assert harness.repo.outcome is None

    events = [e for e in result.stats["diagnostics"]
              if e["event"] == "source_blocked"]
    assert events and events[0]["status"] == guard.SOURCE_FETCH_ERROR
    assert events[0]["reason_code"] == reason_code
    assert events[0]["experiment_id"] == spec.experiment_id
    for field in guard.DIAGNOSTIC_EVENT_FIELDS:
        assert field in events[0]


# ════════════════════════════════════════════════════════════════════
# T08 — window > 1000 candles: bounded pagination, never truncation
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t08_over_1000_candles_uses_range_pagination(spec: Spec):
    signal_time = old_cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)
    candles = old_cycle_candles(spec, signal_time)
    if not spec.has_eod:
        # V2D's verdict window runs to ASOF: its demanded path spans the
        # whole 4-day window (>1000 candles).  V1's verdict window ends
        # at EOD (frozen protocol), so its demanded path is at most one
        # day — the FETCH window is still 4 days, which is what forces
        # pagination (proven by kline_calls == 0 below).
        assert len(candles) > 900, "the fixture window must exceed one page"

    client = FakeClient(candles)
    result = harness.run(client)

    # The >1000 window went through the bounded historical source...
    assert client.range_calls == 1
    assert client.kline_calls == 0, "no silent latest-N truncation"
    # ...and the full path was proven end to end.
    assert result.stats["errors"] == 0
    assert result.stats["source_blocked"] == {
        "source_fetch_error": 0, "source_incomplete": 0,
        "source_not_yet_available": 0,
    }
    assert result.stats["coverage_blocked"] == {}
    for label, _ in HORIZONS:
        assert result.stats["horizons_updated"][label] == 1
    if spec.has_eod:
        assert result.stats["horizons_updated"]["eod"] == 1
    assert result.stats["finalized"] == 1


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t08_window_without_range_source_is_pending(spec: Spec):
    """A >1000 window on a client without get_klines_range is never
    silently answered by a latest-1000 response."""
    signal_time = old_cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)
    candles = old_cycle_candles(spec, signal_time)

    client = NoRangeClient(candles)
    result = harness.run(client)

    assert client.kline_calls == 0, "no latest-N fallback for a big window"
    assert result.writes == []
    assert result.stats["source_blocked"]["source_incomplete"] == 1
    assert result.stats["errors"] == 1
    assert harness.repo.outcome is None


# ════════════════════════════════════════════════════════════════════
# T09 — incomplete pagination: NO finalization
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t09_truncated_range_never_finalizes(spec: Spec):
    signal_time = old_cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)
    candles = old_cycle_candles(spec, signal_time)
    # The range source returns only the first ~5 hours of a 4-day window.
    client = FakeClient(candles, range_keep=60)

    result = harness.run(client)

    assert result.stats["errors"] == 0, "fetch succeeded — coverage blocks"
    # Proven horizons are written (FR-03) ...
    for label in ("15m", "30m", "60m", "120m", "240m"):
        assert result.stats["horizons_updated"][label] == 1
    # ... but the incomplete verdict window never finalizes.
    assert result.finalized() is False
    assert not result.wrote_target_flags()
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")
    if spec.has_eod:
        # V1: the EOD verdict window itself is truncated.
        assert result.stats["horizons_updated"]["eod"] == 0
        assert result.stats["horizons_pending"]["eod"] == 1
        assert result.stats["finalization_blocked"] == 0
    else:
        # V2D: every horizon is proven, the to-ASOF verdict window is not.
        assert result.stats["finalization_blocked"] == 1

    # The blocked finalization stays retry-eligible (FR-06): a second run
    # with the SAME truncated source must not rewrite proven horizons.
    writes_before = len(harness.repo.writes)
    again = harness.run(FakeClient(candles, range_keep=60))
    assert len(harness.repo.writes) == writes_before, "idempotent rerun"
    assert again.finalized() is False


# ════════════════════════════════════════════════════════════════════
# T10 — source recovery: re-evaluation becomes possible again
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t10_deny_then_recovery_evaluates_correctly(spec: Spec):
    signal_time = cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)
    candles = cycle_candles(spec, signal_time)

    blocked = harness.run(
        FakeClient(candles, error=BybitRateLimitError("rate limiter deny")))
    assert blocked.writes == []
    assert blocked.stats["source_blocked"]["source_fetch_error"] == 1

    recovered = harness.run(FakeClient(candles))
    assert recovered.stats["source_blocked"]["source_fetch_error"] == 0
    assert recovered.stats["outcomes_created"] == 1
    assert recovered.stats["finalized"] == 1
    assert recovered.write["mfe_15m"] == pytest.approx(EXP_MFE)


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t10_truncated_source_then_recovery_finalizes(spec: Spec):
    """T09's blocked row is re-evaluated after the source recovers.

    For V2D this exercises the FR-06 eligibility disjunct: all horizon
    marks are already present, only the finalization gate was blocked.
    """
    signal_time = old_cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)
    candles = old_cycle_candles(spec, signal_time)

    blocked = harness.run(FakeClient(candles, range_keep=60))
    assert blocked.finalized() is False
    assert harness.repo.eligible_count == 1, "blocked row stays retryable"

    recovered = harness.run(FakeClient(candles))
    assert recovered.stats["finalized"] == 1
    assert harness.repo.outcome.get("is_final") is True
    assert harness.repo.eligible_count == 0, "finalized row is never re-selected"


# ════════════════════════════════════════════════════════════════════
# T11 — rerun / restart idempotency
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t11_rerun_is_idempotent(spec: Spec):
    signal_time = cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)
    candles = cycle_candles(spec, signal_time)

    first = harness.run(FakeClient(candles))
    assert first.stats["finalized"] == 1
    writes_after_first = len(harness.repo.writes)

    # Same source, fresh evaluator object (systemd restart): no new write.
    harness.restart()
    second = harness.run(FakeClient(candles))
    assert len(harness.repo.writes) == writes_after_first
    assert second.writes == []
    assert harness.repo.eligible_count == 0

    # Even a BROKEN source after finalization changes nothing.
    harness.restart()
    third = harness.run(FakeClient([], error=BybitError("boom")))
    assert len(harness.repo.writes) == writes_after_first
    assert third.writes == []
    assert harness.repo.outcome.get("is_final") is True


# ════════════════════════════════════════════════════════════════════
# T12 — TP/SL (target-flag) event inside a skipped candle
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t12_target_event_inside_missing_candle_no_false_verdict(spec: Spec):
    # The candle at +45m dips to 98.4 (-1.6%) — it would carry
    # reached_minus_1_5 = True in the frozen verdict.
    dipped = [
        candle(c.timestamp, low=98.4) if c.timestamp ==
        ms(SIGNAL + timedelta(minutes=45)) else c
        for c in complete(spec)
    ]
    marks = all_marks(spec, spec.now)

    # Control: with the candle present the verdict is produced.
    control = UnitHarness.evaluate(spec, dipped, **marks)
    assert control.finalized() is True
    assert control.write["reached_minus_1_5"] is True
    assert control.write["reached_minus_2_0"] is False

    # Same window, but the candle carrying the event is missing: no
    # verdict at all may be produced from an incomplete path.
    without = UnitHarness.evaluate(
        spec, drop(dipped, offset_minutes=45), **marks)
    assert without.writes == []
    assert without.finalized() is False
    assert without.stats["finalization_blocked"] == 1
    assert without.stats["coverage_blocked"].get("CANDLE_GAP")


# ════════════════════════════════════════════════════════════════════
# T13 — cutoff / ASOF boundary: no look-ahead, forming candle excluded
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t13_cutoff_boundary_no_lookahead(spec: Spec):
    boundary = SIGNAL + timedelta(minutes=15)
    candles = [
        candle(ms(SIGNAL + timedelta(minutes=5))),
        candle(ms(SIGNAL + timedelta(minutes=10))),
        # Candle opening exactly at the cutoff with absurd extremes —
        # using it would produce MFE = 99% instead of 0.5%.
        candle(ms(SIGNAL + timedelta(minutes=15)),
               high=999.0, low=1.0, close=500.0),
    ]

    # One millisecond before the cutoff the horizon is simply immature.
    immature = UnitHarness.evaluate(
        spec, candles, now=boundary - timedelta(milliseconds=1))
    assert immature.writes == [], "immature horizon must not be written"

    result = UnitHarness.evaluate(spec, candles, now=boundary)

    assert result.evaluated("15m"), "mature horizon with a closed path runs"
    assert result.write["mfe_15m"] == pytest.approx(EXP_MFE), (
        "the cutoff candle must not contribute to MFE")
    assert result.write["mae_15m"] == pytest.approx(EXP_MAE)
    assert not result.evaluated("30m"), "later horizons are not mature yet"
    assert result.finalized() is False


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t13_invalid_ohlc_inside_window_blocks(spec: Spec):
    candles = corrupt(complete(spec), offset_minutes=10,
                      field="close", value=float("inf"))

    result = UnitHarness.evaluate(spec, candles)

    assert result.writes == []
    assert result.stats["coverage_blocked"].get("INVALID_OHLC")
    assert result.stats["horizons_pending"]["15m"] == 1


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t13_asof_never_demands_the_forming_candle(spec: Spec):
    """The ASOF boundary (V2D finalization) demands only CLOSED candles:
    a still-forming grid candle is neither required nor readable."""
    signal_time = cycle_signal(spec)
    if spec.has_eod:
        pytest.skip("V1's verdict window ends at EOD (closed by maturity)")
    harness = CycleHarness(spec, signal_time)
    candles = cycle_candles(spec, signal_time)
    # cycle_candles includes the current grid open — the fetch must drop
    # it (unclosed) while every closed candle of the path is proven.
    forming = candles[-1]
    assert forming.timestamp + INTERVAL_MS > ms(datetime.now(UTC)), (
        "fixture must end at the current grid open")

    result = harness.run(FakeClient(candles))

    assert result.stats["finalized"] == 1
    assert result.stats["coverage_blocked"] == {}
    assert all(c.timestamp != forming.timestamp
               for c in harness.evaluator._last_fetch_result.candles), (
        "the forming candle never reaches evaluation")


# ════════════════════════════════════════════════════════════════════
# T14 — already finalized record: never touched
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t14_finalized_record_is_never_rewritten(spec: Spec):
    finalized = {**all_marks(spec, spec.now), "is_final": True}

    # (a) Complete source: a finalized row produces no write at all.
    complete_result = UnitHarness.evaluate(
        spec, complete(spec), **finalized)
    assert complete_result.writes == []

    # (b) Even a completely broken source writes nothing and never
    # un-finalizes the record.
    broken = UnitHarness.evaluate(spec, [], **finalized)
    assert broken.writes == []
    assert broken.finalized() is False
    assert broken.stats["coverage_blocked"] == {}
    assert broken.stats["finalization_blocked"] == 0


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t14_finalized_cycle_row_is_not_selected(spec: Spec):
    signal_time = cycle_signal(spec)
    harness = CycleHarness(spec, signal_time)
    harness.repo.outcome = {**all_marks(spec, datetime.now(UTC)),
                            "is_final": True}

    result = harness.run(FakeClient(cycle_candles(spec, signal_time)))

    assert result.writes == []
    assert result.stats["signals_checked"] == 0
    assert harness.repo.eligible_count == 0


# ════════════════════════════════════════════════════════════════════
# T15 — frozen protocol parity on a complete path
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_t15_frozen_protocol_parity(spec: Spec):
    """On a complete path every written value equals the untouched frozen
    reference function computed over the very same candles — the guard
    changes WHEN evaluation may run, never the economics."""
    candles = complete(spec)

    result = UnitHarness.evaluate(spec, candles)
    post = [c for c in candles if c.timestamp > ms(SIGNAL)]

    assert len(post) == len(candles), "fixture starts after the signal"

    for label, minutes in HORIZONS:
        ref_mfe, ref_mae = spec.ref_mfe(candles, minutes)
        assert result.write[f"mfe_{label}"] == pytest.approx(ref_mfe)
        assert result.write[f"mae_{label}"] == pytest.approx(ref_mae)
        # Window boundary rule is frozen: candles with ts <= signal or
        # ts >= cutoff never contribute.
        direct = v1_mfe_mae(post, PRICE, minutes, SIGNAL) if spec.has_eod \
            else v2d_mfe_mae(post, PRICE, minutes, SIGNAL)
        assert direct == (ref_mfe, ref_mae)
    if spec.has_eod:
        ref_mfe, ref_mae = spec.ref_eod(candles)
        assert result.write["mfe_eod"] == pytest.approx(ref_mfe)
        assert result.write["mae_eod"] == pytest.approx(ref_mae)
        # EOD is the half-open [signal, next midnight) interval.
        beyond = candles + [candle(ms(EOD))]
        assert spec.ref_eod(beyond) == (ref_mfe, ref_mae), (
            "a candle at the EOD boundary must not contribute")

    ref_flags = spec.ref_flags(candles, SIGNAL)
    for key, value in ref_flags.items():
        assert key in result.write, f"frozen flag {key} must be written"
        assert result.write[key] is value


# ════════════════════════════════════════════════════════════════════
# Diagnostics contract (stats + logs only, NO DB schema change)
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_diagnostics_stats_shape_is_stable(spec: Spec):
    candles = drop(complete(spec), offset_minutes=90)

    stats = UnitHarness.evaluate(spec, candles).stats

    assert stats["experiment_id"] == spec.experiment_id
    assert set(stats["source_blocked"]) == {
        "source_fetch_error", "source_incomplete", "source_not_yet_available",
    }
    assert stats["coverage_blocked"], "coverage_blocked counter required"
    assert set(stats["horizons_pending"]) == set(stats["horizons_updated"])
    assert stats["finalization_blocked"] == 0   # all_done never reached
    for event in stats["diagnostics"]:
        for field in guard.DIAGNOSTIC_EVENT_FIELDS:
            assert field in event, f"diagnostic event lacks {field}"
        assert event["event"] in guard.DIAGNOSTIC_BLOCK_EVENTS
        assert event["experiment_id"] == spec.experiment_id


def test_diagnostics_use_the_shared_guard_vocabulary():
    """Reason codes and counters come from the shared guard — the same
    byte-stable vocabulary the Generic PR #257 evaluator uses."""
    assert guard.DIAGNOSTIC_BLOCK_EVENTS == (
        "source_blocked", "coverage_blocked", "finalization_blocked",
    )
    assert guard.fetch_status_stats_key(
        guard.SOURCE_NOT_YET_AVAILABLE) == "source_not_yet_available"
    assert guard.fetch_status_stats_key(
        guard.SOURCE_INCOMPLETE_COVERAGE) == "source_incomplete"

    stats: dict[str, Any] = {}
    coverage = guard.CandleCoverageResult(
        guard.SOURCE_INCOMPLETE_COVERAGE, "CANDLE_GAP", {})
    guard.record_coverage_block(stats, coverage, horizon="60m")
    guard.record_finalization_block(stats, coverage)
    assert stats["coverage_blocked"]["CANDLE_GAP"] == 2
    assert stats["horizons_pending"]["60m"] == 1
    assert stats["finalization_blocked"] == 1


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_diagnostics_never_reach_the_database_schema(spec: Spec):
    """Until the reason-code schema is separately agreed, the evaluators
    persist NOTHING beyond the frozen outcome upsert."""
    module_source = __import__(spec.cls.__module__, fromlist=["x"]).__file__
    with open(module_source, encoding="utf-8") as fh:
        source = fh.read()
    assert "ALTER TABLE" not in source
    assert "CREATE TABLE" not in source
    assert "DROP TABLE" not in source


def _code_only(source: str) -> str:
    """Full-line comments stripped (design notes may NAME a column)."""
    return "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )


# ════════════════════════════════════════════════════════════════════
# Source wiring contracts — ALL THREE shadow evaluators
# ════════════════════════════════════════════════════════════════════

SHADOW_EVALUATOR_SOURCES = [
    "app/shadow/evaluator.py",
    "app/shadow/v2d_evaluator.py",
    "app/shadow/srr_research_evaluator.py",
]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("path", SHADOW_EVALUATOR_SOURCES)
def test_source_uses_shared_guard_and_lost_the_1000_cap(path: str):
    source = _read(path)
    assert "candle_coverage_guard_v1" in source, (
        "the shared coverage guard must be the candle source")
    # The legacy silent-truncation pattern must be gone.
    assert "300_000 + 10, 1000" not in source
    assert "fetch_candles_for_evaluation" in source
    assert "validate_candle_coverage" in source


@pytest.mark.parametrize("path", SHADOW_EVALUATOR_SOURCES)
def test_source_keeps_the_empty_candle_guard_literal(path: str):
    """Contract locked in by tests/test_issue253_shared_limiter_alignment_v1:
    the literal ``if not candles`` chain must stay in the cycle."""
    assert "if not candles" in _read(path)


@pytest.mark.parametrize("path", SHADOW_EVALUATOR_SOURCES)
def test_source_has_no_ddl_and_no_diagnostic_columns(path: str):
    source = _read(path)
    assert "ALTER TABLE" not in source
    assert "CREATE TABLE" not in source
    # Column names may appear in explanatory comments, never in code.
    code = _code_only(source)
    assert "block_reason" not in code
    assert "source_status" not in code


@pytest.mark.parametrize(
    "path, all_marks_marker",
    [
        # FR-06 eligibility disjunct: non-final rows with every horizon
        # mark present stay retry-eligible until the verdict is proven.
        ("app/shadow/repository.py", "o.evaluated_eod_at IS NOT NULL"),
        ("app/shadow/v2d_repository.py", "o.evaluated_240m_at IS NOT NULL"),
        ("app/shadow/srr_research_evaluator.py",
         "o.evaluated_240m_at IS NOT NULL"),
    ],
    ids=["shadow_v1", "v2d", "srr"],
)
def test_eligibility_keeps_finalized_out_and_retries_blocked_finalization(
    path: str, all_marks_marker: str,
):
    source = _read(path)
    assert "o.is_final = FALSE" in source, (
        "finalized rows must never be re-selected for evaluation")
    assert all_marks_marker in source, (
        "a finalization-blocked row must stay retry-eligible (FR-06)")


def test_srr_eligibility_keeps_target_less_rows_dormant():
    source = _read("app/shadow/srr_research_evaluator.py")
    assert "s.target_1 IS NOT NULL" in source, (
        "rows that can never finalize must not be woken every cycle")


@pytest.mark.parametrize(
    "path",
    ["app/research/evaluator.py", "app/research/prospective_evaluator.py"],
    ids=["generic", "prospective"],
)
def test_generic_evaluator_keeps_its_pr257_guard(path: str):
    """Regression check for Generic PR #257: the generic evaluators still
    fetch through the shared guard (this issue only EXTENDS it)."""
    source = _read(path)
    assert "candle_coverage_guard_v1" in source
    assert "fetch_candles_for_evaluation" in source
