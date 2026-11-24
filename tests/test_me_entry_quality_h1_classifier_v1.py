"""Issue #297 (H1-A.3 / H1-A.4 / H1-B) — HTF classification tests.

Pure, deterministic tests: no PostgreSQL, no Bybit, no network.
Scenario coverage: 1, 2, 3, 4, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19 of the
Issue #297 H1-B matrix, all nine 4H/1D combinations, sensitivity to
``swing_lookback`` 2/3/5 and the research-only isolation guarantees.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.models import Candle
from app.research.me_entry_quality_h1_v1.classifier import (
    CANONICAL_LOOKBACK_CANDLES,
    COVERAGE_INVALID_SOURCE,
    COVERAGE_VALID,
    H1_VERSION,
    REGIME_BEARISH,
    REGIME_BULLISH,
    REGIME_INVALID_SOURCE,
    REGIME_NEUTRAL,
    REASON_INSUFFICIENT_CONFIRMED_STRUCTURE,
    SWING_LOOKBACK_DEFAULT,
    VALID_COMBINED_REGIMES,
    _bos_label,
    classify_combined,
    classify_timeframe,
)
from app.research.me_entry_quality_h1_v1.htf_candles import (
    REASON_CANDLE_GAP,
    REASON_INSUFFICIENT_HISTORY,
    TIMEFRAME_1D,
    TIMEFRAME_1D_MS,
    TIMEFRAME_4H,
    TIMEFRAME_4H_MS,
)
from app.research.me_entry_quality_h1_v1.structure import (
    BOS_BEARISH,
    BOS_BULLISH,
    BOS_NONE,
    BosEvent,
    SWING_LOOKBACK_DEFAULT as STRUCT_LOOKBACK,
)

TF = TIMEFRAME_4H_MS
DAY = 86_400_000
BASE_MS = 31 * DAY  # 1970-02-01T00:00:00Z
N = 17


def _zigzag(points, n):
    xs = [p[0] for p in points]
    ys = [float(p[1]) for p in points]
    out = []
    for i in range(n):
        if i <= xs[0]:
            out.append(ys[0])
            continue
        if i >= xs[-1]:
            out.append(ys[-1])
            continue
        for j in range(len(xs) - 1):
            if xs[j] <= i <= xs[j + 1]:
                span = xs[j + 1] - xs[j]
                t = (i - xs[j]) / span
                out.append(ys[j] + (ys[j + 1] - ys[j]) * t)
                break
    return out


def _bar(open_ms, high, low, *, close=None):
    mid = (high + low) / 2.0
    return Candle(open_ms, mid, high, low, mid if close is None else close, 1.0)


# regime fixtures (index geometry): exactly 2 swing highs and 2 swing lows
BULL_HIGH = [(0, 20), (3, 24), (6, 21), (10, 27), (13, 22), (16, 23)]
BULL_LOW = [(0, 11), (6, 6), (10, 10), (13, 8), (16, 9)]        # HH + HL
BEAR_HIGH = [(0, 20), (3, 27), (6, 22), (10, 24), (13, 21), (16, 22)]
BEAR_LOW = [(0, 16), (6, 11), (10, 14), (13, 8), (16, 10)]      # LH + LL
NEUT_HIGH = BULL_HIGH                                            # HH
NEUT_LOW = BEAR_LOW                                              # LL -> mixed

FIXTURES = {
    REGIME_BULLISH: (BULL_HIGH, BULL_LOW),
    REGIME_BEARISH: (BEAR_HIGH, BEAR_LOW),
    REGIME_NEUTRAL: (NEUT_HIGH, NEUT_LOW),
}


def _series(regime, *, asof, tf=TF, n=N, close_last=None):
    high_pts, low_pts = FIXTURES[regime]
    highs = _zigzag(high_pts, n)
    lows = _zigzag(low_pts, n)
    bars = [_bar(asof - (n - i) * tf, highs[i], lows[i]) for i in range(n)]
    if close_last is not None:
        last = bars[-1]
        bars[-1] = _bar(int(last.timestamp), max(float(last.high), close_last + 0.5), float(last.low), close=close_last)
    return bars


ASOF = BASE_MS + N * TF + TF  # close of candle 16 on the 4H grid


# --- scenarios 1 / 2 / 3 / 4: regimes -------------------------------------


def test_hh_hl_is_bullish():
    state = classify_timeframe(_series(REGIME_BULLISH, asof=ASOF), timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.regime == REGIME_BULLISH
    assert state.coverage_status == COVERAGE_VALID
    assert state.bos == BOS_NONE
    assert len(state.confirmed_swings) == 4


def test_lh_ll_is_bearish():
    state = classify_timeframe(_series(REGIME_BEARISH, asof=ASOF), timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.regime == REGIME_BEARISH
    assert state.coverage_status == COVERAGE_VALID


def test_mixed_structure_is_neutral():
    """Scenario 3: HH + LL is mixed — NEUTRAL, never a fabricated trend."""
    state = classify_timeframe(_series(REGIME_NEUTRAL, asof=ASOF), timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.regime == REGIME_NEUTRAL
    assert state.coverage_status == COVERAGE_VALID
    assert state.reason == "OK"


def test_insufficient_history_is_invalid_source_not_neutral():
    """Scenario 4: too few closed candles -> INVALID_SOURCE / INSUFFICIENT_HISTORY."""
    candles = _series(REGIME_BULLISH, asof=ASOF)[:6]
    asof = int(candles[-1].timestamp) + TF
    state = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert state.regime == REGIME_INVALID_SOURCE
    assert state.reason == REASON_INSUFFICIENT_HISTORY
    assert state.coverage_status == COVERAGE_INVALID_SOURCE
    assert state.bos == "NOT_EVALUATED"


def test_insufficient_confirmed_structure_is_invalid_source():
    """Enough candles but not enough CONFIRMED extrema — never NEUTRAL."""
    highs = _zigzag([(0, 20), (16, 40)], N)  # monotone: no swings at all
    lows = _zigzag([(0, 5), (16, 10)], N)
    candles = [_bar(ASOF - (N - i) * TF, highs[i], lows[i]) for i in range(N)]
    state = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.regime == REGIME_INVALID_SOURCE
    assert state.reason == REASON_INSUFFICIENT_CONFIRMED_STRUCTURE
    assert state.coverage_status == COVERAGE_VALID  # source itself is valid
    assert state.regime != REGIME_NEUTRAL


# --- scenarios 5 / 6 / 10 / 11: causal ASOF semantics ---------------------


def test_unclosed_candle_is_excluded_from_classification():
    """Scenarios 5/6: the unclosed candle never influences the result."""
    candles = _series(REGIME_BULLISH, asof=ASOF)
    asof_before = ASOF - 1  # last candle still open
    with_unclosed = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof_before)
    truncated = classify_timeframe(candles[:-1], timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof_before)
    assert with_unclosed == truncated
    assert with_unclosed.window_candle_count == N - 1


def test_formed_but_unconfirmed_swing_never_influences_classification():
    """Scenario 10: only swings with confirmed_at_ms <= asof are used."""
    candles = _series(REGIME_BULLISH, asof=ASOF)
    asof_early = ASOF - 1  # LOW@13 is formed but its confirmation closes at ASOF
    state = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof_early)
    assert all(s.confirmed_at_ms <= asof_early for s in state.confirmed_swings)
    assert state.regime == REGIME_INVALID_SOURCE  # not enough confirmed structure
    assert state.reason == REASON_INSUFFICIENT_CONFIRMED_STRUCTURE


def test_swing_counts_only_after_confirmation():
    """Scenario 11: the swing appears exactly at its confirmed_at_ms."""
    candles = _series(REGIME_BULLISH, asof=ASOF)
    before = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF - 1)
    after = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert len(before.confirmed_swings) == 3
    assert len(after.confirmed_swings) == 4
    assert after.regime == REGIME_BULLISH


# --- scenarios 12 / 13: BOS diagnostics ----------------------------------


def test_wick_break_gives_no_bos():
    """Scenario 12: no close beyond the level -> NO_BOS."""
    candles = _series(REGIME_BULLISH, asof=ASOF)
    state = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.bos == BOS_NONE
    assert state.bos_events == ()


def test_close_break_gives_bullish_bos():
    """Scenario 13: close beyond a confirmed Swing High -> BULLISH_BOS."""
    candles = _series(REGIME_BULLISH, asof=ASOF, close_last=28.0)  # > 27@10
    state = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.bos == BOS_BULLISH
    assert state.regime == REGIME_BULLISH  # BOS never overrides the HH/HL regime
    events = [e for e in state.bos_events if e.direction == BOS_BULLISH]
    assert len(events) == 1
    assert events[0].level_price == 27.0
    assert events[0].level_confirmed_at_ms < events[0].break_confirmed_at_ms <= ASOF


def test_bos_is_diagnostic_only_and_never_overrides_regime():
    """BULLISH_BOS on a mixed structure keeps the NEUTRAL regime."""
    candles = _series(REGIME_NEUTRAL, asof=ASOF, close_last=28.0)  # > 27@10
    state = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.bos == BOS_BULLISH
    assert state.regime == REGIME_NEUTRAL


def test_bos_label_tie_break_is_deterministic():
    base = dict(
        level_price=1.0, level_type="SWING_HIGH",
        level_formed_at_ms=0, break_open_ms=10, break_confirmed_at_ms=20,
    )
    bearish = BosEvent(direction=BOS_BEARISH, level_confirmed_at_ms=15, **base)
    bullish = BosEvent(direction=BOS_BULLISH, level_confirmed_at_ms=15, **base)
    assert _bos_label((bearish, bullish)) == BOS_BULLISH  # fixed rank
    later = BosEvent(direction=BOS_BEARISH, level_confirmed_at_ms=16, **base)
    assert _bos_label((bullish, later)) == BOS_BEARISH  # more recent level wins
    assert _bos_label(()) == BOS_NONE


# --- scenario 14 + all nine combined regimes ------------------------------


@pytest.mark.parametrize("regime_4h", [REGIME_BULLISH, REGIME_BEARISH, REGIME_NEUTRAL])
@pytest.mark.parametrize("regime_1d", [REGIME_BULLISH, REGIME_BEARISH, REGIME_NEUTRAL])
def test_all_nine_combined_regimes_are_preserved(regime_4h, regime_1d):
    asof = BASE_MS + N * DAY  # UTC midnight: both grids close exactly here
    result = classify_combined(
        _series(regime_4h, asof=asof),
        _series(regime_1d, asof=asof, tf=TIMEFRAME_1D_MS),
        evaluation_asof_ms=asof,
    )
    expected = f"{regime_4h}/{regime_1d}"
    assert result["combined_regime"] == expected
    assert expected in VALID_COMBINED_REGIMES
    assert result["combined_eligible"] is True
    assert result["htf_4h"]["regime"] == regime_4h
    assert result["htf_1d"]["regime"] == regime_1d
    assert len(VALID_COMBINED_REGIMES) == 9


def test_conflicting_htf_modes_are_not_averaged():
    """Scenario 14: 4H and 1D states stay independent and verbatim."""
    asof = BASE_MS + N * DAY
    result = classify_combined(
        _series(REGIME_BULLISH, asof=asof),
        _series(REGIME_BEARISH, asof=asof, tf=TIMEFRAME_1D_MS),
        evaluation_asof_ms=asof,
    )
    assert result["combined_regime"] == "BULLISH/BEARISH"
    assert result["htf_4h"]["regime"] == REGIME_BULLISH
    assert result["htf_1d"]["regime"] == REGIME_BEARISH
    assert result["htf_4h"] != result["htf_1d"]


def test_invalid_timeframe_keeps_the_other_state_but_is_not_eligible():
    asof = BASE_MS + N * DAY
    broken_4h = _series(REGIME_BULLISH, asof=asof)
    del broken_4h[5]  # gap -> INVALID_SOURCE
    result = classify_combined(
        broken_4h, _series(REGIME_BEARISH, asof=asof, tf=TIMEFRAME_1D_MS), evaluation_asof_ms=asof
    )
    assert result["htf_4h"]["regime"] == REGIME_INVALID_SOURCE
    assert result["htf_4h"]["coverage_status"] == COVERAGE_INVALID_SOURCE
    assert result["htf_1d"]["regime"] == REGIME_BEARISH          # independent valid state kept
    assert result["combined_regime"] == "INVALID_SOURCE/BEARISH"
    assert result["combined_eligible"] is False


# --- scenarios 7 / 8 / 9 / 19: source failures ----------------------------


def test_gap_is_invalid_source_and_never_valid_coverage():
    """Scenarios 7/19: incomplete source can never report VALID."""
    candles = _series(REGIME_BULLISH, asof=ASOF)
    del candles[5]
    state = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.regime == REGIME_INVALID_SOURCE
    assert state.reason == REASON_CANDLE_GAP
    assert state.coverage_status != COVERAGE_VALID


def test_duplicate_timestamp_is_invalid_source():
    candles = _series(REGIME_BULLISH, asof=ASOF)
    candles.append(candles[3])
    state = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.regime == REGIME_INVALID_SOURCE
    assert state.reason == "DUPLICATE_CANDLE"


def test_invalid_ohlc_is_invalid_source():
    candles = _series(REGIME_BULLISH, asof=ASOF)
    bad = candles[4]
    candles[4] = Candle(int(bad.timestamp), float(bad.open), float("nan"), float(bad.low), float(bad.close), 1.0)
    state = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert state.regime == REGIME_INVALID_SOURCE
    assert state.reason == "INVALID_OHLC"


# --- scenario 18: exact close boundary ------------------------------------


def test_asof_exactly_at_close_makes_the_candle_available():
    candles = _series(REGIME_BULLISH, asof=ASOF)
    at_close = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    before = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF - 1)
    assert at_close.window_candle_count == N
    assert before.window_candle_count == N - 1
    assert at_close.regime == REGIME_BULLISH
    assert before.regime == REGIME_INVALID_SOURCE  # swing confirming at ASOF not usable yet


# --- scenarios 15 / 16 / 17: reproducibility invariants --------------------


def test_rerun_on_same_asof_is_identical():
    asof = BASE_MS + N * DAY
    candles = _series(REGIME_BULLISH, asof=asof)
    daily = _series(REGIME_BEARISH, asof=asof, tf=TIMEFRAME_1D_MS)
    first = classify_combined(candles, daily, evaluation_asof_ms=asof)
    second = classify_combined(list(candles), list(daily), evaluation_asof_ms=asof)
    assert first == second


def test_appending_future_candles_never_changes_past_asof_result():
    """Scenario 16."""
    candles = _series(REGIME_BULLISH, asof=ASOF)
    before = classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    extended = list(candles) + [
        _bar(int(candles[-1].timestamp) + (i + 1) * TF, 40.0, 30.0) for i in range(6)
    ]
    after = classify_timeframe(extended, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF)
    assert after == before


def test_earlier_history_cannot_change_result_at_fixed_canonical_lookback():
    """Scenario 17: only the canonical window participates."""
    n = 30
    asof = BASE_MS + n * TF + TF
    high_pts = [(0, 18), (10, 20), (13, 24), (16, 21), (20, 27), (23, 22), (26, 23), (29, 23)]
    low_pts = [(0, 8), (10, 11), (16, 6), (20, 10), (23, 8), (26, 9), (29, 9)]
    highs = _zigzag(high_pts, n)
    lows = _zigzag(low_pts, n)
    base_series = [_bar(asof - (n - i) * TF, highs[i], lows[i]) for i in range(n)]

    baseline = classify_timeframe(
        base_series, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof,
        canonical_lookback=20,
    )
    assert baseline.regime == REGIME_BULLISH
    assert baseline.window_candle_count == 20

    # 15 additional older candles (ascending, contiguous): outside the
    # canonical window, they must be inert
    older = [
        _bar(int(base_series[0].timestamp) - (15 - j) * TF, 12.0, 6.0) for j in range(15)
    ]
    extended = classify_timeframe(
        older + base_series, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof,
        canonical_lookback=20,
    )
    assert extended == baseline

    assert CANONICAL_LOOKBACK_CANDLES == 60  # frozen-candidate default


# --- sensitivity to swing_lookback 2/3/5 ----------------------------------


def test_swing_lookback_sensitivity_is_explicit_and_deterministic():
    """Offline sensitivity 2/3/5 on synthetic data — NO winner is selected."""
    n = 37
    asof = BASE_MS + n * TF + TF
    high_pts = [(0, 20), (6, 24), (12, 21), (18, 27), (24, 22), (30, 29), (36, 25)]
    low_pts = [(0, 11), (6, 6), (12, 9), (18, 7), (24, 11), (30, 8), (36, 10)]
    highs = _zigzag(high_pts, n)
    lows = _zigzag(low_pts, n)
    candles = [_bar(asof - (n - i) * TF, highs[i], lows[i]) for i in range(n)]
    results = []
    for lookback in (2, 3, 5):
        state = classify_timeframe(
            candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof, swing_lookback=lookback
        )
        assert state == classify_timeframe(
            candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof, swing_lookback=lookback
        )
        results.append(state)
    # clean synthetic HH/HL structure is recognized identically at 2/3/5
    assert {state.regime for state in results} == {REGIME_BULLISH}
    assert SWING_LOOKBACK_DEFAULT == STRUCT_LOOKBACK == 3  # research default, not frozen


def test_invalid_parameters_raise_value_error():
    candles = _series(REGIME_BULLISH, asof=ASOF)
    with pytest.raises(ValueError):
        classify_timeframe(candles, timeframe="2H", evaluation_asof_ms=ASOF)
    with pytest.raises(ValueError):
        classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF, swing_lookback=0)
    with pytest.raises(ValueError):
        classify_timeframe(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=ASOF, canonical_lookback=6)


# --- contract shape -------------------------------------------------------


def test_combined_contract_shape():
    asof = BASE_MS + N * DAY
    result = classify_combined(
        _series(REGIME_BULLISH, asof=asof),
        _series(REGIME_NEUTRAL, asof=asof, tf=TIMEFRAME_1D_MS),
        evaluation_asof_ms=asof,
    )
    assert result["h1_version"] == H1_VERSION == "1.0.0"
    assert result["evaluation_asof_ms"] == asof
    assert result["combined_regime"] == "BULLISH/NEUTRAL"
    assert set(result) == {
        "h1_version", "evaluation_asof_ms", "htf_4h", "htf_1d",
        "combined_regime", "combined_eligible", "swing_lookback", "canonical_lookback",
    }
    for key in ("htf_4h", "htf_1d"):
        assert set(result[key]) == {"regime", "bos", "coverage_status", "reason", "detail"}


# --- research-only isolation guarantees -----------------------------------

FORBIDDEN_TOKENS = (
    "datetime.now", "utcnow", "time.time", "time.monotonic", "perf_counter",
    "localtime", "mktime", "fromtimestamp", "astimezone", "sleep(",
    "requests", "urllib", "aiohttp", "psycopg", "pg8000", "sqlalchemy",
    "bybit", "socket", "subprocess", "os.environ", "open(", "Path(",
)
ALLOWED_IMPORTS = {"__future__", "math", "dataclasses", "typing", "app.models"}


def _package_dir() -> Path:
    from app.research import me_entry_quality_h1_v1

    return Path(me_entry_quality_h1_v1.__file__).resolve().parent


def test_package_sources_are_pure_no_clock_no_network_no_db_no_io():
    for path in sorted(_package_dir().glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            assert token not in text, f"{path.name} uses forbidden token {token!r}"
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if node.__class__ is ast.ImportFrom and getattr(node, "level", 0) > 0:
                    continue  # intra-package relative import
                assert name in ALLOWED_IMPORTS or root in ALLOWED_IMPORTS, (
                    f"{path.name} imports {name!r}"
                )


def test_package_is_not_wired_into_any_production_surface():
    """Fail-closed isolation: no production module may import this package."""
    root = _package_dir().parents[2]
    offenders = []
    scan = [root / "scanner_runner.py", root / "paper_runner.py", root / "bot.py"]
    scan += sorted((root / "app").rglob("*.py"))
    for path in scan:
        if _package_dir() in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if "me_entry_quality_h1" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"production wiring detected: {offenders}"
