"""Issue #244 — OOS_GENERIC_CANDLE_COVERAGE_FAIL_CLOSED_V1 regression tests.

Core invariant::

    INCOMPLETE CANDLE COVERAGE
        -> NO EVALUATION WRITE
        -> NO FINALIZATION

Every scenario of the mandatory table (T01..T15) is executed against BOTH
generic evaluators:

* ``ResearchEvaluator``      (research.research_signal     -> research.research_outcome)
* ``ProspectiveOOSEvaluator`` (research.prospective_observation -> research.prospective_outcome)

Deterministic fixtures, fake candle sources and in-memory repositories /
connections only: no network, no real Bybit, no PostgreSQL, no production DB.

The pure evaluation formulas (MFE/MAE/return/TP-SL) are NOT modified by this
issue — T01 proves complete-path parity by re-computing every written value
with the untouched reference functions.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.exchange.bybit_client import BybitError, BybitRateLimitError
from app.models import Candle
from app.research import candle_coverage_guard_v1 as guard
from app.research.constants import HORIZONS
from app.research.evaluator import ResearchEvaluator, _calculate_short_mfe_mae
from app.research.prospective_evaluator import (
    ProspectiveOOSEvaluator,
    _calculate_mfe_mae,
)

UTC = timezone.utc
INTERVAL_MS = 300_000
EXPERIMENT_ID = "GENERIC_CANDLE_COVERAGE_V1"
SYMBOL = "BTCUSDT"

# Grid-aligned signal so every horizon cutoff lands on the 5-minute grid.
SIGNAL = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

# Shared SHORT geometry: entry 100, stop 102.5 (above), target 97.5 (below).
ENTRY = 100.0
STOP = 102.5
TARGET = 97.5
CANDLE_HIGH = 101.0
CANDLE_LOW = 99.0
CANDLE_CLOSE = 100.5

# Reference values of the COMPLETE path (frozen formulas, unchanged):
#   mfe  = (entry - low)  / entry * 100 = 1.0
#   mae  = (high - entry) / entry * 100 = 1.0
#   risk = |entry - stop| / entry * 100 = 2.5  ->  mfe_r = mae_r = 0.4
#   ret  = (close - entry)/ entry * 100 = 0.5
EXP_MFE = 1.0
EXP_MAE = 1.0
EXP_MFE_R = 0.4
EXP_MAE_R = 0.4
EXP_RETURN = 0.5

ALL_EVALUATED_FIELDS = [f"evaluated_{label}_at" for label, _ in HORIZONS]


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
           close: float = CANDLE_CLOSE, open_: float = 100.0) -> Candle:
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


# ── in-memory persistence fakes ────────────────────────────────────


class _FakeRepo:
    """In-memory mirror of ResearchRepository eligibility + upsert semantics."""

    def __init__(self, signals: list[dict] | None = None) -> None:
        self.signals = list(signals or [])
        self.outcomes: dict[int, dict] = {}
        self.upserts: list[dict] = []

    def get_eligible_signals(self, experiment_id: str, limit: int = 5000) -> list[dict]:
        # Mirrors ResearchRepository.get_eligible_signals:
        # no outcome row -> eligible after 15m; outcome row -> eligible only
        # while NOT final AND at least one MATURE horizon is unevaluated.
        now = datetime.now(UTC)
        out: list[dict] = []
        for sig in self.signals:
            if sig["experiment_id"] != experiment_id:
                continue
            outcome = self.outcomes.get(sig["signal_id"])
            row = dict(sig)
            if outcome is None:
                if sig["signal_time"] > now - timedelta(minutes=15):
                    continue
                row["outcome_id"] = None
            else:
                if outcome.get("is_final"):
                    continue
                pending = any(
                    outcome.get(f"evaluated_{label}_at") is None
                    and sig["signal_time"] + timedelta(minutes=minutes) <= now
                    for label, minutes in HORIZONS
                )
                if not pending:
                    continue
                row["outcome_id"] = sig["signal_id"]
            for label, _ in HORIZONS:
                row[f"evaluated_{label}_at"] = (
                    outcome or {}).get(f"evaluated_{label}_at")
            row["is_final"] = bool((outcome or {}).get("is_final"))
            out.append(row)
        return out[:limit]

    def upsert_outcome(self, signal_id: int, experiment_id: str, symbol: str,
                       updates: dict) -> bool:
        # Mirrors ResearchRepository.upsert_outcome: NULL values are dropped.
        filtered = {k: v for k, v in updates.items() if v is not None}
        self.upserts.append(dict(filtered))
        self.outcomes.setdefault(signal_id, {}).update(filtered)
        return True


class _ProbeConn:
    """Records only the prospective outcome INSERT executed by the evaluator."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list]] = []
        self.rollbacks = 0

    def cursor(self) -> "_ProbeCursor":
        return _ProbeCursor(self)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        self.rollbacks += 1


class _ProbeCursor:
    def __init__(self, conn: _ProbeConn) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = None) -> None:
        text = " ".join(str(sql).split())
        if "INSERT INTO research.prospective_outcome" in text:
            self._conn.inserts.append((text, list(params or [])))

    def close(self) -> None:
        return None


class _CycleConn:
    """Scripted connection for ProspectiveOOSEvaluator.run_evaluation_cycle.

    Mimics the three cycle queries plus the outcome upsert, and mirrors the
    ``r.is_final = FALSE`` eligibility filter so idempotency (T11) and
    finalized-row protection (T15) are observable end to end.
    """

    def __init__(self, rows: list[tuple], *, started_at: datetime) -> None:
        self.rows = rows
        self.started_at = started_at
        self.outcomes: dict[int, dict] = {}
        self.inserts: list[tuple[str, list]] = []
        self.sql_log: list[str] = []
        self._mode = "none"

    def cursor(self) -> "_CycleCursor":
        return _CycleCursor(self)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    @property
    def eligible_rows(self) -> list[tuple]:
        return [
            row for row in self.rows
            if not self.outcomes.get(row[0], {}).get("is_final")
        ]


class _CycleCursor:
    def __init__(self, conn: _CycleConn) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = None) -> None:
        text = " ".join(str(sql).split())
        self._conn.sql_log.append(text)
        # Order matters: the outcome upsert is INSERT ... SELECT ... FROM
        # research.prospective_observation, so INSERT must be matched first.
        if "INSERT INTO research.prospective_outcome" in text:
            self._conn._mode = "insert"
            self._conn.inserts.append((text, list(params or [])))
            keys = _update_keys(text)
            obs_id = params[0]
            values = params[1:1 + len(keys)]
            self._conn.outcomes.setdefault(obs_id, {}).update(
                dict(zip(keys, values)))
        elif "SELECT started_at" in text:
            self._conn._mode = "started"
        elif "direction_state" in text:
            self._conn._mode = "directions"
        elif "FROM research.prospective_observation" in text:
            self._conn._mode = "eligible"
        else:
            self._conn._mode = "none"

    def fetchone(self) -> Any:
        if self._conn._mode == "started":
            return (self._conn.started_at,)
        return None

    def fetchall(self) -> list:
        if self._conn._mode == "directions":
            return []
        if self._conn._mode == "eligible":
            return list(self._conn.eligible_rows)
        return []

    def close(self) -> None:
        return None


def _update_keys(sql: str) -> list[str]:
    """Column names written by the prospective outcome upsert (minus ids)."""
    match = re.search(r"prospective_outcome\s*\(([^)]*)\)", sql)
    assert match, f"cannot parse outcome columns from: {sql}"
    columns = [c.strip() for c in match.group(1).split(",")]
    return columns[2:]


def _outcome_updates(sql: str, params: list) -> dict:
    keys = _update_keys(sql)
    return dict(zip(keys, list(params)[1:1 + len(keys)]))


def fresh_stats() -> dict:
    return {
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


def _generic_signal(signal_time: datetime = SIGNAL, **overrides) -> dict:
    sig = {
        "signal_id": 1,
        "experiment_id": EXPERIMENT_ID,
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


def _prospective_row(signal_time: datetime = SIGNAL, **overrides) -> tuple:
    row = (
        1, SYMBOL, signal_time, EXPERIMENT_ID, "SHORT",
        ENTRY, STOP, TARGET, None,      # reference/invalidation/target_1/target_2
        None, None, None, None,         # variant_entry/stop/target, features
        None, None, None, None, None,   # evaluated_*_at
        False,                          # is_final
    )
    if not overrides:
        return row
    data = {
        "observation_id": row[0], "symbol": row[1], "signal_time": row[2],
        "experiment_id": row[3], "direction": row[4],
        "reference_price": row[5], "invalidation_price": row[6],
        "target_1": row[7], "target_2": row[8],
        "variant_entry": row[9], "variant_stop": row[10],
        "variant_target": row[11], "features": row[12],
        "evaluated_15m_at": row[13], "evaluated_30m_at": row[14],
        "evaluated_60m_at": row[15], "evaluated_120m_at": row[16],
        "evaluated_240m_at": row[17], "is_final": row[18],
    }
    data.update(overrides)
    values = list(data.values())
    return tuple(values[:19])


def _prospective_obs(signal_time: datetime = SIGNAL, **overrides) -> dict:
    obs = {
        "observation_id": 1,
        "symbol": SYMBOL,
        "signal_time": signal_time,
        "experiment_id": EXPERIMENT_ID,
        "direction": "SHORT",
        "reference_price": ENTRY,
        "invalidation_price": STOP,
        "target_1": TARGET,
        "target_2": None,
        "variant_entry": None,
        "variant_stop": None,
        "variant_target": None,
        "features": None,
        **{field: None for field in ALL_EVALUATED_FIELDS},
        "is_final": False,
    }
    obs.update(overrides)
    return obs


# ── harnesses: run the same scenario through both evaluators ──────


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


class GenericHarness:
    """ResearchEvaluator._evaluate_signal (unit level, explicit ASOF)."""

    name = "ResearchEvaluator"

    @staticmethod
    def evaluate(candles, now, *, signal_time: datetime = SIGNAL,
                 **overrides) -> EvalResult:
        repo = _FakeRepo()
        evaluator = ResearchEvaluator(conn=None, client=None)
        evaluator._repo = repo
        stats = fresh_stats()
        evaluator._evaluate_signal(
            _generic_signal(signal_time, **overrides), list(candles), now, stats)
        return EvalResult(writes=list(repo.upserts), stats=stats)

    @staticmethod
    def reference_mfe(candles, minutes: int, signal_time: datetime = SIGNAL):
        post = [c for c in candles if c.timestamp > ms(signal_time)]
        return _calculate_short_mfe_mae(post, ENTRY, minutes, signal_time)


class ProspectiveHarness:
    """ProspectiveOOSEvaluator.evaluate_observation (unit level, explicit ASOF)."""

    name = "ProspectiveOOSEvaluator"

    @staticmethod
    def evaluate(candles, now, *, signal_time: datetime = SIGNAL,
                 **overrides) -> EvalResult:
        conn = _ProbeConn()
        evaluator = ProspectiveOOSEvaluator(conn, None, None)
        stats = fresh_stats()
        evaluator.evaluate_observation(
            _prospective_obs(signal_time, **overrides), list(candles), now, stats)
        writes = [_outcome_updates(sql, params) for sql, params in conn.inserts]
        return EvalResult(writes=writes, stats=stats)

    @staticmethod
    def reference_mfe(candles, minutes: int, signal_time: datetime = SIGNAL):
        post = [c for c in candles if c.timestamp > ms(signal_time)]
        return _calculate_mfe_mae(post, ENTRY, minutes, signal_time, True)


HARNESSES = [GenericHarness, ProspectiveHarness]


class GenericCycle:
    """Full ResearchEvaluator.run_evaluation_cycle with a fake source."""

    def __init__(self, signal_time: datetime) -> None:
        self.repo = _FakeRepo([_generic_signal(signal_time)])
        self.evaluator = ResearchEvaluator(conn=None, client=None)
        self.evaluator._repo = self.repo

    def run(self, client) -> EvalResult:
        self.evaluator.client = client
        stats = self.evaluator.run_evaluation_cycle(EXPERIMENT_ID)
        return EvalResult(writes=list(self.repo.upserts), stats=stats)

    @property
    def eligible_count(self) -> int:
        return len(self.repo.get_eligible_signals(EXPERIMENT_ID))


class ProspectiveCycle:
    """Full ProspectiveOOSEvaluator.run_evaluation_cycle with a fake source."""

    def __init__(self, signal_time: datetime) -> None:
        self.conn = _CycleConn(
            [_prospective_row(signal_time)],
            started_at=signal_time - timedelta(minutes=10),
        )
        self.evaluator = ProspectiveOOSEvaluator(self.conn, None, None)

    def run(self, client) -> EvalResult:
        self.evaluator.client = client
        stats = self.evaluator.run_evaluation_cycle(EXPERIMENT_ID)
        writes = [_outcome_updates(sql, params)
                  for sql, params in self.conn.inserts]
        return EvalResult(writes=writes, stats=stats)

    @property
    def eligible_count(self) -> int:
        return len(self.conn.eligible_rows)


CYCLE_HARNESSES = [GenericCycle, ProspectiveCycle]


# ── fake candle sources ────────────────────────────────────────────


class FakeBybitClient:
    """Deterministic Bybit-like 5-minute source.

    ``get_klines`` returns the latest-N candles (ascending, including the
    forming bar, like the real client).  ``get_klines_range`` honours the
    explicit [start_ms, end_ms] bounds and the bounded page budget — it
    RAISES rather than silently truncating.
    """

    def __init__(self, candles, *, now: datetime | None = None,
                 error: Exception | None = None, range_slice: int = 0) -> None:
        self.candles = sorted(candles, key=lambda c: c.timestamp)
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


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t01_complete_path_writes_expected_results(harness):
    candles = full_path(SIGNAL, 240)

    result = harness.evaluate(candles, NOW_FULL)

    assert result.write is not None, "complete path must be evaluated"
    for label, minutes in HORIZONS:
        assert result.evaluated(label), f"{label} must be evaluated"
        assert result.write[f"mfe_{label}"] == pytest.approx(EXP_MFE)
        assert result.write[f"mae_{label}"] == pytest.approx(EXP_MAE)
        assert result.write[f"mfe_r_{label}"] == pytest.approx(EXP_MFE_R)
        assert result.write[f"mae_r_{label}"] == pytest.approx(EXP_MAE_R)
        assert result.write[f"return_at_{label}"] == pytest.approx(EXP_RETURN)

        # Complete-path parity with the untouched reference formulas.
        ref_mfe, ref_mae = harness.reference_mfe(candles, minutes)
        assert result.write[f"mfe_{label}"] == pytest.approx(ref_mfe)
        assert result.write[f"mae_{label}"] == pytest.approx(ref_mae)

    # TP/SL verdict over the complete 240m path: nothing hit.
    assert result.write.get("tp_hit") is False
    assert result.write.get("sl_hit") is False
    assert result.write.get("time_to_tp") in (None, "")
    assert result.finalized() is True
    assert result.stats["finalization_blocked"] == 0
    assert result.stats["coverage_blocked"] == {}


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
def test_t01_complete_path_cycle_finalizes(cycle_cls):
    signal_time = cycle_signal_time()
    cycle = cycle_cls(signal_time)
    candles = full_path(signal_time, 240)

    result = cycle.run(FakeBybitClient(candles))

    assert result.stats["signals_checked"] == 1
    assert result.stats["errors"] == 0
    assert result.write is not None
    assert result.write["mfe_60m"] == pytest.approx(EXP_MFE)
    assert result.write["mae_60m"] == pytest.approx(EXP_MAE)
    assert result.write["mfe_r_60m"] == pytest.approx(EXP_MFE_R)
    assert result.write["return_at_60m"] == pytest.approx(EXP_RETURN)
    assert result.finalized() is True
    assert result.stats["source_blocked"] == {
        "source_fetch_error": 0,
        "source_incomplete": 0,
        "source_not_yet_available": 0,
    }


# ══════════════════════════════════════════════════════════════════
# T02 — first required candle missing -> no evaluation write
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t02_missing_first_required_candle_no_write(harness):
    candles = drop(full_path(SIGNAL, 240), offset_minutes=5)

    result = harness.evaluate(candles, NOW_FULL)

    assert result.writes == [], "no evaluation write without the first candle"
    assert result.stats["coverage_blocked"].get("FIRST_REQUIRED_CANDLE_MISSING")
    for label, _ in HORIZONS:
        assert result.stats["horizons_pending"].get(label) == 1
    assert result.finalized() is False


# ══════════════════════════════════════════════════════════════════
# T03 — gap in the middle -> no false finalization
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t03_middle_gap_no_false_finalization(harness):
    # Candle at signal+30m: inside the 60m/120m/240m windows only.
    candles = drop(full_path(SIGNAL, 240), offset_minutes=30)

    result = harness.evaluate(candles, NOW_FULL)

    assert result.evaluated("15m") and result.evaluated("30m")
    assert not result.evaluated("60m")
    assert not result.evaluated("120m")
    assert not result.evaluated("240m")
    assert result.finalized() is False, "gap must block finalization"
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")
    assert result.stats["finalization_blocked"] == 0  # all_done never reached
    # No TP/SL verdict may be written without the proven final window.
    assert not any("tp_hit" in w for w in result.writes)


# ══════════════════════════════════════════════════════════════════
# T04 — last required candle missing -> horizon stays pending
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t04_missing_last_required_candle_horizon_pending(harness):
    # Candle at signal+235m is the LAST required candle of the 240m window.
    candles = drop(full_path(SIGNAL, 240), offset_minutes=235)

    result = harness.evaluate(candles, NOW_FULL)

    for label in ("15m", "30m", "60m", "120m"):
        assert result.evaluated(label), f"{label} must still be evaluated"
    assert not result.evaluated("240m"), "240m must stay pending"
    assert result.finalized() is False
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")
    assert result.stats["horizons_pending"].get("240m") == 1


# ══════════════════════════════════════════════════════════════════
# T05 — an unclosed required candle is never used
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t05_unclosed_required_candle_not_used(harness):
    # Off-grid signal (10:02) at ASOF 10:17: the 15m horizon is mature, but
    # the required 10:15 candle only closes at 10:20 — it is still forming.
    signal = datetime(2026, 10, 1, 10, 2, tzinfo=UTC)
    asof_unmatured = signal + timedelta(minutes=15)          # 10:17
    asof_closed = signal + timedelta(minutes=18)             # 10:20
    candles = [
        candle(ms(signal) + m * 60_000)
        for m in (3, 8, 13)                                  # 10:05, 10:10, 10:15
    ]

    blocked = harness.evaluate(candles, asof_unmatured, signal_time=signal)
    assert blocked.writes == [], "forming candle must block the horizon"
    assert blocked.stats["coverage_blocked"].get("REQUIRED_CANDLE_NOT_CLOSED")

    # Once the same candle is closed, the horizon is evaluated normally.
    closed = harness.evaluate(candles, asof_closed, signal_time=signal)
    assert closed.evaluated("15m"), "closed path must be evaluated"
    assert closed.write["mfe_15m"] == pytest.approx(EXP_MFE)
    assert not closed.evaluated("30m"), "30m not mature at 10:20"


# ══════════════════════════════════════════════════════════════════
# T06 — duplicated timestamp -> fail-closed
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t06_duplicate_timestamp_fail_closed(harness):
    candles = full_path(SIGNAL, 240)
    # Bogus duplicate of the first required candle with an absurd range.
    candles.append(candle(ms(SIGNAL) + 5 * 60_000, high=999.0, low=1.0,
                          close=500.0))

    result = harness.evaluate(candles, NOW_FULL)

    assert result.writes == [], "duplicate timestamps must block every horizon"
    assert result.stats["coverage_blocked"].get("DUPLICATE_TIMESTAMPS")
    assert result.finalized() is False


# ══════════════════════════════════════════════════════════════════
# T07 — invalid OHLC candle -> fail-closed
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t07_invalid_ohlc_fail_closed(harness):
    candles = full_path(SIGNAL, 240)
    # low > high: structurally impossible OHLC (first required candle).
    candles[0] = Candle(timestamp=candles[0].timestamp, open=100.0,
                        high=99.0, low=101.0, close=100.0, volume=1.0)

    result = harness.evaluate(candles, NOW_FULL)

    assert result.writes == [], "invalid OHLC must block every horizon"
    assert result.stats["coverage_blocked"].get("INVALID_OHLC")
    assert result.finalized() is False


# ══════════════════════════════════════════════════════════════════
# T08 — windows above 1000 candles: full coverage or pending
# ══════════════════════════════════════════════════════════════════


def test_t08_guard_paginates_rather_than_truncating():
    signal_time = old_signal_time()
    candles = full_path(signal_time, 240)
    client = FakeBybitClient(candles)

    result = guard.fetch_candles_for_evaluation(
        client, SYMBOL, signal_time, datetime.now(UTC))

    assert result.diagnostics["required_candle_count"] > 1000
    assert client.range_calls == 1, "must use explicit range pagination"
    assert client.kline_calls == 0, "latest-N must never serve a >1000 window"
    assert result.status == guard.SOURCE_VALIDATED


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
def test_t08_over_1000_candles_full_coverage_finalizes(cycle_cls):
    signal_time = old_signal_time()
    cycle = cycle_cls(signal_time)
    client = FakeBybitClient(full_path(signal_time, 240))

    result = cycle.run(client)

    assert client.range_calls >= 1 and client.kline_calls == 0
    assert result.stats["errors"] == 0
    assert result.finalized() is True
    assert result.write["mfe_60m"] == pytest.approx(EXP_MFE)


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
def test_t08_over_1000_candles_without_range_source_is_pending(cycle_cls):
    signal_time = old_signal_time()
    cycle = cycle_cls(signal_time)

    result = cycle.run(NoRangeClient(full_path(signal_time, 240)))

    assert result.writes == [], "no silent latest-N truncation is allowed"
    assert result.stats["errors"] >= 1
    assert result.stats["source_blocked"]["source_incomplete"] >= 1
    assert cycle.eligible_count == 1, "signal must stay retry-eligible"


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
def test_t08_truncated_range_never_finalizes(cycle_cls):
    signal_time = old_signal_time()
    cycle = cycle_cls(signal_time)
    # Range source answers, but with the head of the window cut off.
    client = FakeBybitClient(full_path(signal_time, 240), range_slice=10)

    result = cycle.run(client)

    assert result.finalized() is False
    assert not result.evaluated("240m")
    assert result.stats["coverage_blocked"], "truncation must be diagnosed"
    assert cycle.eligible_count == 1


# ══════════════════════════════════════════════════════════════════
# T09 — API error / 10006 / limiter denial -> no write, retry preserved
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
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
def test_t09_source_error_no_write_retry_eligible(cycle_cls, error, reason):
    signal_time = cycle_signal_time()
    cycle = cycle_cls(signal_time)
    client = FakeBybitClient(full_path(signal_time, 240), error=error)

    result = cycle.run(client)

    assert result.writes == [], "source error must not produce any write"
    assert result.stats["errors"] >= 1
    assert result.stats["source_blocked"]["source_fetch_error"] >= 1
    assert result.stats["finalized"] == 0
    assert cycle.eligible_count == 1, "retry eligibility must be preserved"

    # Diagnostics classify the failure distinctly from incomplete coverage.
    status = getattr(cycle.evaluator, "_last_fetch_result", None)
    assert status is not None and status.status == guard.SOURCE_FETCH_ERROR
    assert status.reason_code == reason


# ══════════════════════════════════════════════════════════════════
# T10 — deny, then source recovery -> correct evaluation afterwards
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
def test_t10_deny_then_recovery_evaluates_correctly(cycle_cls):
    signal_time = cycle_signal_time()
    cycle = cycle_cls(signal_time)
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
    assert recovered.finalized() is True
    assert recovered.stats["signals_checked"] == 1


# ══════════════════════════════════════════════════════════════════
# T11 — rerunning the evaluator is idempotent
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
def test_t11_rerun_is_idempotent(cycle_cls):
    signal_time = cycle_signal_time()
    cycle = cycle_cls(signal_time)
    client = FakeBybitClient(full_path(signal_time, 240))

    first = cycle.run(client)
    assert first.write is not None and first.finalized() is True
    first_writes = list(first.writes)

    second = cycle.run(client)

    assert second.stats["signals_checked"] == 0, "finalized row is not eligible"
    assert second.writes == first_writes, "rerun must not write again"
    assert len(second.writes) == len(first_writes)


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t11_rerun_never_rewrites_proven_horizons(harness):
    candles = full_path(SIGNAL, 240)
    # Only 60m of coverage proven in cycle 1 (tail missing in cycle 2's data).
    partial = drop(full_path(SIGNAL, 240), offset_minutes=90)

    first = harness.evaluate(candles, NOW_FULL)
    assert first.write is not None

    # Re-evaluate with the SAME signal row: proven horizons are skipped.
    already = {
        field: first.write.get(field) for field in ALL_EVALUATED_FIELDS
    }
    second = harness.evaluate(candles, NOW_FULL, **already, is_final=True)

    assert second.writes == [], "proven + finalized row must not be rewritten"

    # ...and a blocked rerun adds no new horizon markers either.
    blocked_again = harness.evaluate(partial, NOW_FULL, **already,
                                     is_final=False)
    assert not blocked_again.evaluated("240m")
    assert blocked_again.stats["coverage_blocked"].get("CANDLE_GAP")


# ══════════════════════════════════════════════════════════════════
# T12 — TP inside the missing candle: no false verdict
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t12_tp_inside_missing_candle_no_false_verdict(harness):
    # The candle at signal+45m would hit the SHORT target (low 97.0 <= 97.5).
    tp_candle_kwargs = {"low": 97.0}
    complete = full_path(SIGNAL, 240)
    tp_index = next(
        i for i, c in enumerate(complete)
        if c.timestamp == ms(SIGNAL) + 45 * 60_000)
    complete_with_tp = list(complete)
    complete_with_tp[tp_index] = candle(
        ms(SIGNAL) + 45 * 60_000, **tp_candle_kwargs)

    # Control: with the candle present the TP verdict is produced.
    control = harness.evaluate(complete_with_tp, NOW_FULL)
    assert control.finalized() is True
    assert control.write.get("tp_hit") is True
    assert control.write.get("time_to_tp") == pytest.approx(45.0)

    # Same window, but the candle carrying the TP is missing (a gap):
    # no verdict at all may be produced.
    without = harness.evaluate(
        drop(complete_with_tp, offset_minutes=45), NOW_FULL)
    assert not without.evaluated("60m")
    assert without.finalized() is False
    assert not any("tp_hit" in w for w in without.writes)
    assert not any("sl_hit" in w for w in without.writes)
    assert not any("time_to_tp" in w for w in without.writes)
    assert without.stats["coverage_blocked"].get("CANDLE_GAP")


# ══════════════════════════════════════════════════════════════════
# T13 — several horizons, coverage only for the early ones
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t13_only_proven_horizons_are_written(harness):
    # History stops at signal+90m: 15m/30m/60m are covered, 120m/240m are not.
    cutoff_ms = ms(SIGNAL) + 90 * 60_000
    truncated = [c for c in full_path(SIGNAL, 240) if c.timestamp <= cutoff_ms]

    result = harness.evaluate(truncated, NOW_FULL)

    for label in ("15m", "30m", "60m"):
        assert result.evaluated(label), f"{label} coverage is proven"
    for label in ("120m", "240m"):
        assert not result.evaluated(label), f"{label} must stay pending"
        assert result.stats["horizons_pending"].get(label) == 1
    assert result.finalized() is False
    assert not any("tp_hit" in w for w in result.writes)


# ══════════════════════════════════════════════════════════════════
# T14 — cutoff / ASOF boundary: no lookahead, forming candle excluded
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t14_cutoff_boundary_no_lookahead(harness):
    boundary = SIGNAL + timedelta(minutes=15)   # ASOF == 15m horizon cutoff
    candles = [
        candle(ms(SIGNAL) + 5 * 60_000),
        candle(ms(SIGNAL) + 10 * 60_000),
        # Forming 10:15 candle (not closed at 10:15) with absurd extremes —
        # using it would produce MFE = 899% instead of 1%.
        candle(ms(SIGNAL) + 15 * 60_000, high=999.0, low=1.0, close=500.0),
    ]

    # One millisecond before the cutoff the horizon is simply immature.
    immature = harness.evaluate(candles, boundary - timedelta(milliseconds=1))
    assert immature.writes == [], "immature horizon must not be written"

    result = harness.evaluate(candles, boundary)

    assert result.evaluated("15m"), "mature horizon with a closed path evaluates"
    assert result.write["mfe_15m"] == pytest.approx(EXP_MFE), (
        "the forming candle must not contribute to MFE")
    assert result.write["mae_15m"] == pytest.approx(EXP_MAE)
    assert result.write["return_at_15m"] == pytest.approx(EXP_RETURN)
    assert not result.evaluated("30m"), "later horizons are not mature yet"
    assert result.finalized() is False


def test_t14_fetch_drops_the_forming_candle():
    boundary = SIGNAL + timedelta(minutes=15)
    candles = [
        candle(ms(SIGNAL) + m * 60_000)
        for m in (0, 5, 10, 15)
    ]   # includes the forming 10:15 bar and the pre-signal 10:00 bar

    client = FakeBybitClient(candles, now=boundary)
    result = guard.fetch_candles_for_evaluation(
        client, SYMBOL, SIGNAL, boundary)

    assert result.status == guard.SOURCE_VALIDATED
    open_times = [c.timestamp for c in result.candles]
    assert ms(SIGNAL) + 15 * 60_000 not in open_times, (
        "unclosed candle must be dropped by the fetch")
    assert ms(SIGNAL) + 5 * 60_000 in open_times
    assert ms(SIGNAL) + 10 * 60_000 in open_times


# ══════════════════════════════════════════════════════════════════
# T15 — previously finalized records are never touched
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_t15_finalized_record_is_never_rewritten(harness):
    finalized = {field: NOW_FULL for field in ALL_EVALUATED_FIELDS}

    # (a) Complete source: a finalized row produces no write at all.
    complete = harness.evaluate(full_path(SIGNAL, 240), NOW_FULL,
                                **finalized, is_final=True)
    assert complete.writes == []

    # (b) Even with an incomplete source nothing is written or un-finalized.
    broken = harness.evaluate([], NOW_FULL, **finalized, is_final=True)
    assert broken.writes == []
    assert broken.finalized() is False


def test_t15_eligibility_sql_excludes_finalized_rows():
    from app.research.repository import ResearchRepository

    generic_sql = inspect.getsource(ResearchRepository.get_eligible_signals)
    assert "o.is_final = FALSE" in generic_sql

    prospective_sql = inspect.getsource(
        ProspectiveOOSEvaluator.run_evaluation_cycle)
    assert "r.is_final = FALSE" in prospective_sql


# ══════════════════════════════════════════════════════════════════
# Review P2-1 — required path anchored on the ABSOLUTE UTC grid
# ══════════════════════════════════════════════════════════════════

_FIELD_PARAM = {"open": "open_", "close": "close", "high": "high", "low": "low"}


def shifted_path(signal: datetime, horizon_minutes: int = 240, *,
                 shift_minutes: int = 1) -> list[Candle]:
    """A complete-looking path whose opens are shifted off the UTC grid.

    ``shift_minutes=1`` reproduces the review example 10:01/10:06/10:11;
    ``shift_minutes=5`` keeps every open on-grid but moves the whole series,
    so the FIRST required candle disappears.
    """
    signal_ms = ms(signal)
    cutoff_ms = ms(signal + timedelta(minutes=horizon_minutes))
    shift = shift_minutes * 60_000
    return [
        candle(t + shift)
        for t in required_opens(signal_ms, cutoff_ms)
        if signal_ms < t + shift < cutoff_ms
    ]


def corrupt(candles: list[Candle], offset_minutes: int, field: str,
            value: float, *, signal: datetime = SIGNAL) -> list[Candle]:
    """Complete path with one candle's price replaced by ``value``."""
    target = ms(signal) + offset_minutes * 60_000
    param = _FIELD_PARAM[field]
    return [
        candle(c.timestamp, **{param: value}) if c.timestamp == target else c
        for c in candles
    ]


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_p2_1_off_grid_shifted_path_is_rejected(harness):
    """10:01/10:06/10:11 must never validate against its own anchor."""
    candles = shifted_path(SIGNAL, 240, shift_minutes=1)

    result = harness.evaluate(candles, NOW_FULL)

    assert result.writes == [], "shifted opens must not be evaluated"
    assert result.stats["coverage_blocked"].get("CANDLE_TIMESTAMP_OFF_GRID")
    assert result.finalized() is False
    for label, _ in HORIZONS:
        assert not result.evaluated(label), f"{label} must stay pending"


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_p2_1_interval_shifted_path_is_rejected(harness):
    """On-grid but shifted series: the FIRST required candle is missing."""
    candles = shifted_path(SIGNAL, 240, shift_minutes=5)

    result = harness.evaluate(candles, NOW_FULL)

    assert result.writes == []
    assert result.stats["coverage_blocked"].get("FIRST_REQUIRED_CANDLE_MISSING")
    assert result.finalized() is False


def test_p2_1_guard_anchor_is_the_absolute_grid():
    signal_ms = ms(SIGNAL)
    shifted = [candle(signal_ms + (1 + 5 * i) * 60_000) for i in range(5)]

    result = guard.validate_candle_coverage(
        shifted,
        signal_ms=signal_ms,
        cutoff_ms=signal_ms + 30 * 60_000,
        asof_ms=ms(SIGNAL + timedelta(minutes=40)),
    )

    assert result.status == guard.SOURCE_INCOMPLETE_COVERAGE
    assert result.reason_code == "CANDLE_TIMESTAMP_OFF_GRID"
    assert result.diagnostics["first_required_open_ms"] == signal_ms + INTERVAL_MS
    assert result.diagnostics["off_grid_count"] == 5


def test_p2_1_guard_absolute_anchor_accepts_genuine_path():
    """The absolute anchor must not reject a genuine complete path."""
    candles = full_path(SIGNAL, 60)

    result = guard.validate_candle_coverage(
        candles,
        signal_ms=ms(SIGNAL),
        cutoff_ms=ms(SIGNAL) + 60 * 60_000,
        asof_ms=ms(SIGNAL + timedelta(minutes=65)),
    )

    assert result.ok
    assert result.diagnostics["first_required_open_ms"] == ms(SIGNAL) + INTERVAL_MS
    assert result.diagnostics["required_range"][0] == ms(SIGNAL) + INTERVAL_MS


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
def test_p2_1_cycle_rejects_shifted_source(cycle_cls):
    signal_time = cycle_signal_time()
    cycle = cycle_cls(signal_time)
    candles = shifted_path(signal_time, 240, shift_minutes=1)

    result = cycle.run(FakeBybitClient(candles))

    assert result.writes == []
    assert result.stats["coverage_blocked"].get("CANDLE_TIMESTAMP_OFF_GRID")
    assert result.stats["finalized"] == 0
    assert result.stats["errors"] == 0, "coverage block is not a fetch error"
    assert cycle.eligible_count == 1, "signal must stay retry-eligible"


# ══════════════════════════════════════════════════════════════════
# Review P2-2 — NaN / ±Infinity prices fail closed
# ══════════════════════════════════════════════════════════════════

NON_FINITE_VALUES = [float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
@pytest.mark.parametrize("field", ["open", "close"])
@pytest.mark.parametrize("value", NON_FINITE_VALUES,
                         ids=["nan", "pos_inf", "neg_inf"])
def test_p2_2_non_finite_price_blocks_every_horizon(harness, field, value):
    # Corrupt the FIRST required candle: every window contains it, so no
    # evaluated_*_at and no is_final may be written at all.
    candles = corrupt(full_path(SIGNAL, 240), 5, field, value)

    result = harness.evaluate(candles, NOW_FULL)

    assert result.writes == [], f"{field}={value!r} must not be evaluated"
    assert result.stats["coverage_blocked"].get("INVALID_OHLC")
    assert result.finalized() is False
    for label, _ in HORIZONS:
        assert not result.evaluated(label)


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
@pytest.mark.parametrize("value", NON_FINITE_VALUES,
                         ids=["nan", "pos_inf", "neg_inf"])
def test_p2_2_non_finite_price_blocks_finalization(harness, value):
    # Corrupt a candle that only the 240m/TP-SL window reads: the shorter
    # proven horizons stay, but the final verdict must not be produced.
    candles = corrupt(full_path(SIGNAL, 240), 120, "close", value)

    result = harness.evaluate(candles, NOW_FULL)

    for label in ("15m", "30m", "60m", "120m"):
        assert result.evaluated(label), f"{label} window is unaffected"
    assert not result.evaluated("240m")
    assert result.finalized() is False
    assert not any("tp_hit" in w for w in result.writes)
    assert not any("sl_hit" in w for w in result.writes)
    assert result.stats["coverage_blocked"].get("INVALID_OHLC")


def test_p2_2_guard_rejects_every_non_finite_field():
    signal_ms = ms(SIGNAL)
    for field in ("open", "high", "low", "close"):
        for value in NON_FINITE_VALUES:
            candles = corrupt(full_path(SIGNAL, 60), 30, field, value)
            result = guard.validate_candle_coverage(
                candles,
                signal_ms=signal_ms,
                cutoff_ms=signal_ms + 60 * 60_000,
                asof_ms=ms(SIGNAL + timedelta(minutes=65)),
            )
            assert result.status == guard.SOURCE_INCOMPLETE_COVERAGE, (
                f"{field}={value!r} must be rejected")
            assert result.reason_code == "INVALID_OHLC"


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
def test_p2_2_cycle_rejects_non_finite_source(cycle_cls):
    signal_time = cycle_signal_time()
    cycle = cycle_cls(signal_time)
    candles = corrupt(full_path(signal_time, 240), 5, "close",
                      float("nan"), signal=signal_time)

    result = cycle.run(FakeBybitClient(candles))

    assert result.writes == []
    assert result.stats["coverage_blocked"].get("INVALID_OHLC")
    assert result.stats["finalized"] == 0
    assert result.stats["errors"] == 0, "coverage block is not a fetch error"
    assert cycle.eligible_count == 1, "signal must stay retry-eligible"


# ══════════════════════════════════════════════════════════════════
# ASOF / cutoff boundary + false-finalization protection
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_asof_boundary_evaluates_exactly_at_horizon_cutoff(harness):
    candles = [candle(ms(SIGNAL) + 5 * 60_000), candle(ms(SIGNAL) + 10 * 60_000)]
    cutoff = SIGNAL + timedelta(minutes=15)

    # One millisecond before the cutoff the horizon is not mature yet.
    early = harness.evaluate(candles, cutoff - timedelta(milliseconds=1))
    assert early.writes == []

    # Exactly at the cutoff: mature, and the last required candle (10:10,
    # closing at 10:15) satisfies open+5m <= asof — the rule is inclusive.
    at_cutoff = harness.evaluate(candles, cutoff)
    assert at_cutoff.evaluated("15m")
    assert at_cutoff.write["mfe_15m"] == pytest.approx(EXP_MFE)
    assert at_cutoff.stats["coverage_blocked"] == {}
    assert at_cutoff.finalized() is False, "later horizons are not mature"


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_cutoff_boundary_candle_outside_window_is_not_read(harness):
    # The candle opening exactly at the cutoff belongs to the next window:
    # the frozen formulas never read it, so its corrupt value must not block
    # — and, symmetrically, can never influence the result (no lookahead).
    candles = [
        candle(ms(SIGNAL) + 5 * 60_000),
        candle(ms(SIGNAL) + 10 * 60_000),
        candle(ms(SIGNAL) + 15 * 60_000, close=float("nan")),
    ]
    boundary = SIGNAL + timedelta(minutes=15)

    result = harness.evaluate(candles, boundary)

    assert result.evaluated("15m")
    assert result.write["mfe_15m"] == pytest.approx(EXP_MFE)
    assert result.stats["coverage_blocked"] == {}
    assert result.finalized() is False


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_finalization_gate_blocks_invalid_tpsl_window(harness):
    # Every horizon already proven (stored marks), but the TP/SL window
    # itself is corrupt: stored marks alone must never finalize the row.
    marks = {f: NOW_FULL for f in ALL_EVALUATED_FIELDS}
    candles = corrupt(full_path(SIGNAL, 240), 120, "close", float("nan"))

    result = harness.evaluate(candles, NOW_FULL, **marks, is_final=False)

    assert result.writes == [], "no write when the TP/SL window is invalid"
    assert result.finalized() is False
    assert result.stats["finalization_blocked"] == 1
    assert result.stats["coverage_blocked"].get("INVALID_OHLC")


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
def test_finalization_gate_blocks_incomplete_tpsl_path(harness):
    # Same, with a plain gap: stored horizon marks are not proof of the
    # final path, so is_final must stay false (no false finalization).
    marks = {f: NOW_FULL for f in ALL_EVALUATED_FIELDS}
    candles = drop(full_path(SIGNAL, 240), offset_minutes=90)

    result = harness.evaluate(candles, NOW_FULL, **marks, is_final=False)

    assert result.writes == []
    assert result.finalized() is False
    assert result.stats["finalization_blocked"] == 1
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")


@pytest.mark.parametrize("cycle_cls", CYCLE_HARNESSES,
                         ids=lambda c: c.__name__)
def test_cycle_never_finalizes_from_incomplete_path(cycle_cls):
    """End-to-end: a truncated source never produces is_final (T03/T13)."""
    signal_time = cycle_signal_time()
    cycle = cycle_cls(signal_time)
    # Tail truncated at signal+90m: early horizons provable, 240m is not.
    candles = drop(full_path(signal_time, 240), offset_minutes=90,
                   signal=signal_time)

    result = cycle.run(FakeBybitClient(candles))

    assert result.finalized() is False
    assert not any(w.get("is_final") for w in result.writes)
    assert not any("tp_hit" in w for w in result.writes)
    assert result.stats["finalized"] == 0
    assert result.stats["coverage_blocked"].get("CANDLE_GAP")
    assert cycle.eligible_count == 1


# ══════════════════════════════════════════════════════════════════
# Guard contract + shared diagnostics helpers
# ══════════════════════════════════════════════════════════════════


def test_guard_statuses_are_stable():
    assert guard.SOURCE_VALIDATED == "SOURCE_VALIDATED"
    assert guard.SOURCE_FETCH_ERROR == "SOURCE_FETCH_ERROR"
    assert guard.SOURCE_INCOMPLETE_COVERAGE == "SOURCE_INCOMPLETE_COVERAGE"
    assert guard.SOURCE_NOT_YET_AVAILABLE == "SOURCE_NOT_YET_AVAILABLE"
    assert guard.fetch_status_stats_key(guard.SOURCE_FETCH_ERROR) == "source_fetch_error"
    assert guard.fetch_status_stats_key(guard.SOURCE_INCOMPLETE_COVERAGE) == "source_incomplete"


def test_guard_validates_grid_alignment_and_spacing():
    candles = [candle(ms(SIGNAL) + m * 60_000) for m in (5, 10, 15, 20)]
    ok = guard.validate_candle_coverage(
        candles, signal_ms=ms(SIGNAL),
        cutoff_ms=ms(SIGNAL) + 25 * 60_000, asof_ms=ms(SIGNAL + timedelta(minutes=30)))
    assert ok.ok and ok.reason_code == "SOURCE_VALIDATED"

    off_grid = candles + [candle(ms(SIGNAL) + 2 * 60_000)]
    bad = guard.validate_candle_coverage(
        off_grid, signal_ms=ms(SIGNAL),
        cutoff_ms=ms(SIGNAL) + 25 * 60_000, asof_ms=ms(SIGNAL + timedelta(minutes=30)))
    assert not bad.ok
    assert bad.status == guard.SOURCE_INCOMPLETE_COVERAGE


def test_guard_merge_source_status_is_worst_first():
    assert guard.merge_source_status([]) is None
    assert guard.merge_source_status(
        [guard.SOURCE_VALIDATED, guard.SOURCE_VALIDATED]) is None
    assert guard.merge_source_status(
        [guard.SOURCE_VALIDATED,
         guard.SOURCE_INCOMPLETE_COVERAGE]) == guard.SOURCE_INCOMPLETE_COVERAGE
    assert guard.merge_source_status(
        [guard.SOURCE_INCOMPLETE_COVERAGE,
         guard.SOURCE_FETCH_ERROR]) == guard.SOURCE_FETCH_ERROR
    assert guard.merge_source_status(
        [guard.SOURCE_VALIDATED,
         guard.SOURCE_NOT_YET_AVAILABLE]) == guard.SOURCE_NOT_YET_AVAILABLE


def test_guard_terminal_event_open_ms_locates_first_hit():
    signal_ms = ms(SIGNAL)
    candles = [candle(signal_ms + m * 60_000) for m in (5, 10, 15)]
    tp_sl = {"tp_hit": True, "sl_hit": False,
             "time_to_tp": 10.0, "time_to_sl": None}

    assert guard.terminal_event_open_ms(
        tp_sl, candles, signal_ms=signal_ms) == signal_ms + 10 * 60_000
    assert guard.terminal_event_open_ms(
        {"tp_hit": False, "sl_hit": False,
         "time_to_tp": None, "time_to_sl": None},
        candles, signal_ms=signal_ms) is None
    assert guard.terminal_event_open_ms(None, candles, signal_ms=signal_ms) is None


def test_stats_recording_helpers_are_schema_free():
    stats: dict[str, Any] = {}
    coverage = guard.CandleCoverageResult(
        guard.SOURCE_INCOMPLETE_COVERAGE, "CANDLE_GAP", {})
    guard.record_coverage_block(stats, coverage, horizon="60m")
    guard.record_finalization_block(stats, coverage)

    assert stats["coverage_blocked"]["CANDLE_GAP"] == 2
    assert stats["horizons_pending"]["60m"] == 1
    assert stats["finalization_blocked"] == 1
