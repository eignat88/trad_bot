"""Issue #181 mandatory test matrix (unit): the causal closed-Bybit 5m source.

Covers, per the issue:
  1. a forming 5m candle is never used;
  2. the boundary candle becomes available only after CONFIRMED closure;
  3. pagination of 200+ candles returns a correct contiguous history;
  4. a missing candle, a duplicate and an OHLC conflict all fail closed;
  5. API errors, rate limits and incomplete pagination fail closed;
  plus: explicit request bounds, verified evaluation ASOF (no local clock),
  missing server-time evidence, history floor, read-only public endpoints.
"""
from __future__ import annotations

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
    BAR_MS,
    BybitCandleSourceError,
    BybitPhase2CandleSource,
    REASON_API_ERROR,
    REASON_CONFLICTING,
    REASON_DUPLICATE,
    REASON_INCOMPLETE_COVERAGE,
    REASON_INCOMPLETE_HISTORY,
    REASON_INVALID_OHLC,
    REASON_MISSING_SERVER_TIME,
    REASON_NOT_ALIGNED,
    REASON_OUT_OF_WINDOW,
    REASON_RATE_LIMITED,
    REASON_REQUEST_WINDOW_INVALID,
    SOURCE_NAME,
)

BAR = BAR_MS
HOUR0 = 1_759_996_800_000
PUBLIC_ENDPOINTS = {"/v5/market/kline", "/v5/market/time"}


def make_candles(count: int, start_ms: int = HOUR0) -> list[Candle]:
    out: list[Candle] = []
    for i in range(count):
        ts = start_ms + i * BAR
        base = 100.0 + (i % 7) * 0.01
        out.append(
            Candle(ts, base, base + 0.05, base - 0.05, base + 0.01, 1.0 + (i % 3) * 0.1)
        )
    return out


def row_of(candle: Candle) -> list[str]:
    return [
        str(int(candle.timestamp)),
        str(candle.open),
        str(candle.high),
        str(candle.low),
        str(candle.close),
        str(candle.volume),
    ]


class FakeKlineClient:
    """Deterministic read-only Bybit v5 fake (public endpoints only)."""

    def __init__(
        self,
        candles: list[Candle],
        *,
        server_time_ms: int,
        drop_opens_once=(),
        drop_opens_twice=(),
        conflict_on_second_serving=(),
        drop_opens_forever=(),
        duplicate_rows=(),
        conflicting_rows=(),
        out_of_window_rows=(),
        misaligned_rows=(),
        invalid_ohlc_rows=(),
        empty_pages=(),
        page_times: dict[int, int] | None = None,
        fail_with=None,
        include_live_tail: bool = True,
        no_server_time: bool = False,
    ):
        self.rows = {int(c.timestamp): row_of(c) for c in candles}
        self.server_time_ms = int(server_time_ms)
        self.drop_opens_once = set(drop_opens_once)
        self.drop_opens_twice = set(drop_opens_twice)
        self.conflict_on_second_serving = set(conflict_on_second_serving)
        self.drop_opens_forever = set(drop_opens_forever)
        self.duplicate_rows = set(duplicate_rows)
        self.conflicting_rows = set(conflicting_rows)
        self.out_of_window_rows = set(out_of_window_rows)
        self.misaligned_rows = set(misaligned_rows)
        self.invalid_ohlc_rows = set(invalid_ohlc_rows)
        self.empty_pages = set(empty_pages)
        self.page_times = dict(page_times or {})
        self.fail_with = fail_with
        self.include_live_tail = include_live_tail
        self.no_server_time = no_server_time
        self.calls: list[dict] = []
        self.kline_calls: list[tuple[int, int]] = []
        self._served_once: set[int] = set()
        self._offer_counts: dict[int, int] = {}
        self._served_counts: dict[int, int] = {}

    def _public_get(self, endpoint: str, **params):
        self.calls.append({"endpoint": endpoint, **params})
        if self.fail_with is not None:
            raise self.fail_with
        if endpoint == "/v5/market/time":
            if self.no_server_time:
                return {"retCode": 0, "retMsg": "OK", "result": {}}
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {"timeSecond": str(self.server_time_ms // 1000)},
                "time": self.server_time_ms,
            }
        assert endpoint == "/v5/market/kline", endpoint
        start, end = int(params["start"]), int(params["end"])
        page_index = len(self.kline_calls)
        self.kline_calls.append((start, end))
        if page_index in self.empty_pages:
            rows: list = []
        else:
            rows = []
            for open_ms in sorted(self.rows):
                in_window = start <= open_ms <= end
                # a live kline response always carries the forming bar, even
                # when it is outside the requested window
                live_tail = (
                    self.include_live_tail
                    and open_ms > end
                    and open_ms + BAR > self.server_time_ms
                )
                if not (in_window or live_tail):
                    continue
                offer = self._offer_counts.get(open_ms, 0)
                self._offer_counts[open_ms] = offer + 1
                if open_ms in self.drop_opens_forever:
                    continue
                if open_ms in self.drop_opens_twice and offer < 2:
                    continue
                if open_ms in self.drop_opens_once and open_ms not in self._served_once:
                    self._served_once.add(open_ms)
                    continue
                row = list(self.rows[open_ms])
                served = self._served_counts.get(open_ms, 0)
                self._served_counts[open_ms] = served + 1
                if open_ms in self.conflict_on_second_serving and served >= 1:
                    row[2] = str(float(row[2]) + 1.0)  # coherent, but different
                    row[4] = str(float(row[4]) + 1.0)
                if open_ms in self.duplicate_rows:
                    rows.append(list(row))
                if open_ms in self.conflicting_rows:
                    conflict = list(row)
                    conflict[2] = str(float(conflict[2]) + 1.0)  # coherent, but different
                    conflict[4] = str(float(conflict[4]) + 1.0)
                    rows.append(conflict)
                if open_ms in self.out_of_window_rows:
                    shifted = list(row)
                    shifted[0] = str(end + BAR)
                    rows.append(shifted)
                    continue
                if open_ms in self.misaligned_rows:
                    row[0] = str(open_ms + 1_000)
                if open_ms in self.invalid_ohlc_rows:
                    row[2] = str(float(row[3]) - 1.0)  # high below low
                rows.append(row)
        rows.reverse()  # Bybit returns newest first
        page_time = self.page_times.get(page_index, self.server_time_ms)
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"symbol": "BTCUSDT", "list": rows},
            "time": page_time,
        }


def build_source(client, **kwargs) -> BybitPhase2CandleSource:
    kwargs.setdefault("sleep", lambda _s: None)
    return BybitPhase2CandleSource(client, **kwargs)


# ------------------------------------------------------ 1. forming candle


def test_forming_5m_candle_is_never_used():
    history = make_candles(60)
    forming = Candle(
        history[-1].timestamp + BAR, 100.0, 100.1, 99.9, 100.05, 0.5
    )
    all_rows = history + [forming]
    server_time_ms = history[-1].timestamp + BAR  # the last bar just closed
    client = FakeKlineClient(all_rows, server_time_ms=server_time_ms)
    source = build_source(client, min_history_bars=50)

    candles = source.load("BTCUSDT")

    assert source.last_evaluation_asof_ms == server_time_ms
    assert [int(c.timestamp) for c in candles] == [int(c.timestamp) for c in history[-50:]]
    assert forming.timestamp not in {int(c.timestamp) for c in candles}
    assert source.last_load_metrics["unclosed_candles_excluded"] >= 1


# --------------------------------- 2. boundary candle needs confirmed closure


def test_boundary_candle_available_only_after_confirmed_closure():
    history = make_candles(60)
    boundary = history[-1]
    boundary_close = boundary.timestamp + BAR
    evaluation_asof_ms = boundary_close  # operator ASOF says "closed"

    # (a) the exchange has NOT confirmed the close yet -> boundary excluded
    unconfirmed = FakeKlineClient(
        history, server_time_ms=boundary_close - 1, include_live_tail=True
    )
    source_a = build_source(unconfirmed, min_history_bars=50, evaluation_asof_ms=evaluation_asof_ms)
    candles_a = source_a.load("BTCUSDT")
    assert boundary.timestamp not in {int(c.timestamp) for c in candles_a}
    assert source_a.last_evaluation_asof_ms == boundary_close - 1

    # (b) the exchange confirms the close -> boundary becomes available
    confirmed = FakeKlineClient(
        history, server_time_ms=boundary_close, include_live_tail=False
    )
    source_b = build_source(confirmed, min_history_bars=50, evaluation_asof_ms=evaluation_asof_ms)
    candles_b = source_b.load("BTCUSDT")
    assert boundary.timestamp in {int(c.timestamp) for c in candles_b}
    assert int(candles_b[-1].timestamp) == boundary.timestamp
    assert source_b.last_evaluation_asof_ms == boundary_close


# ------------------------------------------------------ 3. pagination 200+


def test_pagination_of_200_plus_bars_returns_contiguous_history():
    history = make_candles(700)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(history, server_time_ms=server_time_ms, include_live_tail=False)
    source = build_source(client, page_limit=200, max_pages=3, min_history_bars=450)

    candles = source.load("BTCUSDT")

    opens = [int(c.timestamp) for c in candles]
    assert len(opens) == 450
    assert opens == list(range(opens[0], opens[-1] + BAR, BAR))  # contiguous, ascending
    assert all(int(c.timestamp) % BAR == 0 for c in candles)

    kline_calls = client.kline_calls
    assert len(kline_calls) == 3  # bounded pagination: exactly 3 pages
    expected_start = opens[0]
    for index, (start, end) in enumerate(kline_calls):
        assert start % BAR == 0 and end % BAR == 0
        assert (end - start) // BAR + 1 <= 200  # request bounds honored
        assert start == expected_start  # strictly advancing, no overlap
        expected_start = end + BAR
    assert kline_calls[-1][1] == opens[-1]
    assert all(call["endpoint"] in PUBLIC_ENDPOINTS for call in client.calls)


# ------------------------- 4. missing / duplicate / conflicting -> fail closed


def test_missing_candle_is_recovered_by_bounded_rerequest():
    history = make_candles(700)
    server_time_ms = history[-1].timestamp + BAR
    dropped = history[-120].timestamp
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        drop_opens_once={dropped},
        include_live_tail=False,
    )
    source = build_source(client, page_limit=200, max_pages=3, min_history_bars=450)

    candles = source.load("BTCUSDT")

    assert dropped in {int(c.timestamp) for c in candles}
    assert source.last_load_metrics["gap_recovery_requests"] >= 1


def test_missing_candle_fails_closed_when_not_recoverable():
    history = make_candles(700)
    server_time_ms = history[-1].timestamp + BAR
    dropped = history[-120].timestamp
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        drop_opens_forever={dropped},
        include_live_tail=False,
    )
    source = build_source(client, page_limit=200, max_pages=3, min_history_bars=450)

    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_INCOMPLETE_COVERAGE


def test_duplicate_candle_fails_closed():
    history = make_candles(70)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        duplicate_rows={history[-10].timestamp},
        include_live_tail=False,
    )
    source = build_source(client, min_history_bars=50)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_DUPLICATE


def test_ohlc_conflict_fails_closed():
    history = make_candles(70)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        conflicting_rows={history[-10].timestamp},
        include_live_tail=False,
    )
    source = build_source(client, min_history_bars=50)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_CONFLICTING


def test_gap_recovery_retry_overlap_is_idempotent_not_a_false_duplicate():
    """Review 5615772746 FIX-2: recovery attempt 1 returns only a PART of the
    missing candles, attempt 2 re-fetches the FULL range including the already
    recovered bar. The identical overlap must be accepted (idempotent retry),
    never reported as ``DUPLICATE_CANDLE``."""
    history = make_candles(700)
    server_time_ms = history[-1].timestamp + BAR
    gap = [history[-120].timestamp, history[-119].timestamp, history[-118].timestamp]
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        drop_opens_once={gap[0]},  # served again from recovery attempt 1
        drop_opens_twice={gap[1], gap[2]},  # served only from attempt 2
        include_live_tail=False,
    )
    source = build_source(client, page_limit=200, max_pages=3, min_history_bars=450)

    candles = source.load("BTCUSDT")

    opens = {int(c.timestamp) for c in candles}
    assert set(gap) <= opens  # every missing bar recovered
    assert len(candles) == 450  # complete, no dropped history
    assert source.last_load_metrics["gap_recovery_requests"] >= 2
    assert source.last_load_metrics["bars_returned"] == 450


def test_gap_recovery_retry_overlap_conflict_still_fails_closed():
    """An identical cross-attempt overlap is accepted, but a CONFLICTING candle
    returned by a later recovery attempt stays fail-closed."""
    history = make_candles(700)
    server_time_ms = history[-1].timestamp + BAR
    recovered = history[-120].timestamp
    still_missing = history[-119].timestamp
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        drop_opens_once={recovered},
        drop_opens_twice={still_missing},
        conflict_on_second_serving={recovered},  # attempt 2 contradicts attempt 1
        include_live_tail=False,
    )
    source = build_source(client, page_limit=200, max_pages=3, min_history_bars=450)

    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_CONFLICTING


# ------------------------------------ 5. API / rate limit / incomplete pages


def test_api_error_fails_closed_with_single_logical_request():
    history = make_candles(60)
    client = FakeKlineClient(
        history, server_time_ms=history[-1].timestamp + BAR, fail_with=RuntimeError("connection reset")
    )
    source = build_source(client, min_history_bars=50, max_attempts=2)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_API_ERROR
    # BYBIT_RETRY_BUDGET_HARDENING_V1 (Issue #229): the hidden source-level
    # retry layer is COLLAPSED — exactly one logical request per fetch (the
    # transport owns the <=3 HTTP attempt budget), so a persistent failure
    # can no longer amplify into max_attempts x budget attempts.
    assert len(client.calls) == 1


def test_rate_limit_fails_closed_and_creates_nothing():
    history = make_candles(60)
    client = FakeKlineClient(
        history,
        server_time_ms=history[-1].timestamp + BAR,
        fail_with=RuntimeError("Too many visits, please keep rate limit"),
    )
    source = build_source(client, min_history_bars=50, max_attempts=2)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_RATE_LIMITED
    assert source.last_load_metrics["bars_returned"] == 0


def test_incomplete_pagination_fails_closed():
    history = make_candles(700)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        # the third page is truncated and every bounded recovery request too
        empty_pages={2, 3, 4},
        include_live_tail=False,
    )
    source = build_source(client, page_limit=200, max_pages=3, min_history_bars=450)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_INCOMPLETE_COVERAGE


def test_history_floor_blocks_incomplete_history():
    # page server times regress -> the strict closure evidence moves back and
    # the tail is no longer provably closed at evaluation time
    history = make_candles(700)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        page_times={2: server_time_ms - 20 * BAR},
        include_live_tail=False,
    )
    source = build_source(client, page_limit=200, max_pages=3, min_history_bars=450)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_INCOMPLETE_HISTORY


# ------------------------------------------- request bounds / row validation


def test_row_outside_requested_window_fails_closed():
    history = make_candles(70)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        # a CLOSED bar returned outside the window it was requested for
        out_of_window_rows={history[-45].timestamp},
        include_live_tail=False,
    )
    source = build_source(client, page_limit=30, max_pages=2, min_history_bars=50)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_OUT_OF_WINDOW


def test_unaligned_candle_fails_closed():
    history = make_candles(70)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        misaligned_rows={history[-30].timestamp},
        include_live_tail=False,
    )
    source = build_source(client, min_history_bars=50)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_NOT_ALIGNED


def test_invalid_ohlc_fails_closed():
    history = make_candles(70)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(
        history,
        server_time_ms=server_time_ms,
        invalid_ohlc_rows={history[-30].timestamp},
        include_live_tail=False,
    )
    source = build_source(client, min_history_bars=50)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason == REASON_INVALID_OHLC


# --------------------------------- verified evaluation ASOF / closure evidence


def test_missing_server_time_evidence_fails_closed():
    history = make_candles(60)
    client = FakeKlineClient(
        history, server_time_ms=history[-1].timestamp + BAR, no_server_time=True
    )
    source = build_source(client, min_history_bars=50)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source.load("BTCUSDT")
    assert excinfo.value.reason in (REASON_MISSING_SERVER_TIME, REASON_API_ERROR)


def test_local_system_clock_is_never_closure_evidence():
    """The exchange clock is the only clock the source trusts."""

    class _BombClock:
        def __getattr__(self, name):
            raise AssertionError(f"local clock consulted: time.{name}")

    import app.research.htf_baseline_short_phase2_bybit_candle_source_v1 as mod

    history = make_candles(60)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(history, server_time_ms=server_time_ms, include_live_tail=False)
    source = build_source(client, min_history_bars=50)

    real_time = mod.time
    mod.time = _BombClock()
    try:
        candles = source.load("BTCUSDT")
    finally:
        mod.time = real_time

    assert len(candles) == 50
    assert source.last_evaluation_asof_ms == server_time_ms
    assert source.last_server_time_ms == server_time_ms


def test_explicit_evaluation_asof_is_intersected_with_exchange_time():
    history = make_candles(60)
    server_time_ms = history[-1].timestamp + BAR
    # operator asks for a cutoff 5 bars in the FUTURE: never evaluate ahead of
    # what the exchange has confirmed
    client = FakeKlineClient(history, server_time_ms=server_time_ms, include_live_tail=False)
    source = build_source(
        client, min_history_bars=50, evaluation_asof_ms=server_time_ms + 5 * BAR
    )
    candles = source.load("BTCUSDT")
    assert source.last_evaluation_asof_ms == server_time_ms
    assert all(int(c.timestamp) + BAR <= source.last_evaluation_asof_ms for c in candles)

    # and an EARLIER cutoff is honored exactly
    cutoff = history[-6].timestamp + BAR
    client_early = FakeKlineClient(history, server_time_ms=server_time_ms, include_live_tail=False)
    source_early = build_source(client_early, min_history_bars=50, evaluation_asof_ms=cutoff)
    candles_early = source_early.load("BTCUSDT")
    assert source_early.last_evaluation_asof_ms == cutoff
    assert int(candles_early[-1].timestamp) + BAR == cutoff


# ------------------------------------------------------------- safety / scope


def test_source_is_public_read_only_and_never_authenticates():
    history = make_candles(700)
    server_time_ms = history[-1].timestamp + BAR
    client = FakeKlineClient(history, server_time_ms=server_time_ms, include_live_tail=False)
    source = build_source(client, page_limit=200, max_pages=3, min_history_bars=450)
    source.load("BTCUSDT")
    for call in client.calls:
        assert call["endpoint"] in PUBLIC_ENDPOINTS
        assert not any(
            key in call for key in ("api_key", "api_secret", "sign", "signature")
        )


def test_source_module_never_references_trading_apis():
    import inspect

    import app.research.htf_baseline_short_phase2_bybit_candle_source_v1 as mod

    text = inspect.getsource(mod)
    for forbidden in (
        "create_order",
        "place_order",
        "/v5/order",
        "X-BAPI",
        "api_secret",
        "api_key",
        "app.trading",
        "app.execution",
    ):
        assert forbidden not in text
    assert SOURCE_NAME == "BybitPhase2CandleSource"


def test_pagination_budget_is_bounded_by_construction():
    client = FakeKlineClient(make_candles(10), server_time_ms=HOUR0 + BAR)
    with pytest.raises(ValueError):
        BybitPhase2CandleSource(client, page_limit=100, max_pages=2, min_history_bars=450)
    with pytest.raises(ValueError):
        BybitPhase2CandleSource(client, page_limit=0)
    with pytest.raises(ValueError):
        BybitPhase2CandleSource(client, evaluation_asof_ms=-5)


def test_request_window_validation_rejects_misconfiguration():
    client = FakeKlineClient(make_candles(10), server_time_ms=HOUR0 + BAR)
    source = build_source(client, min_history_bars=5, page_limit=10)
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source._check_request_bounds(HOUR0, HOUR0 + 30 * BAR, HOUR0 + 30 * BAR)
    assert excinfo.value.reason == REASON_REQUEST_WINDOW_INVALID
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source._check_request_bounds(HOUR0 + BAR, HOUR0, HOUR0)
    assert excinfo.value.reason == REASON_REQUEST_WINDOW_INVALID
    with pytest.raises(BybitCandleSourceError) as excinfo:
        source._check_request_bounds(HOUR0 + 1, HOUR0 + BAR, HOUR0 + BAR)
    assert excinfo.value.reason == REASON_REQUEST_WINDOW_INVALID
