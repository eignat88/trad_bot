"""Regression coverage for the ME SHORT A historical 5m candle range fetch.

Root cause (Issue #206 follow-up, task
ME_SHORT_A_HISTORICAL_5M_CANDLE_RANGE_FETCH_FIX_V1): the research adapter
used to ask the exchange for the LATEST N candles and then filter them down
to a historical 240m window. A matured event processed late could therefore
never observe its own post-signal candle path — the DOGEUSDT event of
2026-10-27 21:35 UTC stored only 4/48 candles at ASOF 2026-10-28 05:31 UTC.

These tests use deterministic mock fixtures only — no real Bybit requests.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.exchange.bybit_client import BybitClient, BybitError
from app.research.me_short_a_forward_outcome_lineage_v1 import (
    expected_window_opens,
    window_bounds,
)
from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
    BybitClosedCandleSource,
    datetime_to_ms,
)

# The exact VPS evidence event (2026-10-28): off-grid signal, matured
# horizon, late processing.
DOGE_SIGNAL = datetime(2026, 10, 27, 21, 35, 41, 973578, tzinfo=timezone.utc)
DOGE_HORIZON = datetime(2026, 10, 28, 1, 35, 41, 973578, tzinfo=timezone.utc)
DOGE_ASOF = datetime(2026, 10, 28, 5, 31, 37, 10267, tzinfo=timezone.utc)

STEP_MS = 300_000


class StubBybit:
    """Double for GET /v5/market/kline honoring the Bybit V5 contract.

    Returns raw rows inside the inclusive ``[start, end]`` bounds,
    newest-first, truncated to ``limit`` (as the real API does). Rows whose
    timestamp cannot be parsed are still returned so the client's own row
    rejection is exercised.

    ``respect_bounds=False`` / ``truncate=False`` model a misbehaving API
    (extra rows, duplicated rows) and prove the client enforces bounds and
    de-duplication by itself.
    """

    def __init__(self, rows, *, fail_on_call=None,
                 respect_bounds=True, truncate=True):
        self.rows = list(rows)
        self.calls = []
        self.fail_on_call = fail_on_call
        self.respect_bounds = respect_bounds
        self.truncate = truncate

    @staticmethod
    def _ts(row):
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError, KeyError):
            return None

    def _public_get(self, endpoint, **params):
        self.calls.append((endpoint, dict(params)))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise BybitError("simulated transient Bybit failure")
        start = params.get("start")
        end = params.get("end")
        data = []
        for row in self.rows:
            ts = self._ts(row)
            if self.respect_bounds and ts is not None:
                if start is not None and ts < start:
                    continue
                if end is not None and ts > end:
                    continue
            data.append(row)
        # Newest-first, exactly like Bybit.
        data.sort(key=lambda row: (self._ts(row) is not None, self._ts(row) or 0),
                  reverse=True)
        if self.truncate:
            data = data[:params["limit"]]
        return {"result": {"list": list(data)}}


def _client(stub):
    client = object.__new__(BybitClient)
    client._public_get = stub._public_get
    return client


def _row(ts, open_="100", high="101", low="99", close="100", volume="1"):
    return [str(ts), open_, high, low, close, volume]


def _doge_fixture():
    """The exchange state a late run of the matured DOGE event saw: the 48
    historical window bars plus 40 recent bars after the window, ending at
    the ASOF clock (the window itself is far behind the latest data)."""
    opens = expected_window_opens(DOGE_SIGNAL)
    assert len(opens) == 48
    asof_grid = datetime_to_ms(DOGE_ASOF) // STEP_MS * STEP_MS
    recent = [asof_grid - idx * STEP_MS for idx in range(40)]
    assert min(recent) > datetime_to_ms(opens[-1])
    rows = [_row(datetime_to_ms(op)) for op in opens]
    rows += [_row(ts) for ts in recent]
    return opens, rows


# ── historical window ──────────────────────────────────────────────────


def test_matured_historical_window_48_of_48():
    start = datetime(2026, 10, 27, 21, 35, tzinfo=timezone.utc)
    base = int(start.timestamp() * 1000)
    stub = StubBybit([_row(base + i * STEP_MS) for i in range(48)])
    observer = BybitClosedCandleSource(
        _client(stub), clock=lambda: start + timedelta(hours=8))
    candles = observer.closed_candles(
        "DOGEUSDT", start=start, end=start + timedelta(hours=4))
    assert len(candles) == 48
    assert candles[0].open_time == start
    assert candles[-1].open_time == start + timedelta(minutes=235)
    assert all(call[1]["start"] >= base for call in stub.calls)
    assert all(call[1]["end"] < base + 4 * 60 * 60 * 1000 for call in stub.calls)


def test_doge_regression_matured_window_is_fetched_as_history():
    """27.10.2026 21:35 -> 28.10.2026 01:35 UTC processed at 05:31 UTC:
    all 48 bars of the frozen window, with the query anchored to the EVENT
    window and never to the execution time."""
    opens, rows = _doge_fixture()
    first_open, last_open, _horizon_end = window_bounds(DOGE_SIGNAL)
    stub = StubBybit(rows)
    observer = BybitClosedCandleSource(_client(stub), clock=lambda: DOGE_ASOF)
    candles = observer.closed_candles(
        "DOGEUSDT", start=first_open,
        end=last_open + timedelta(minutes=5))

    assert len(candles) == 48
    assert [c.open_time for c in candles] == opens
    window_start_ms = datetime_to_ms(first_open)
    window_end_ms = datetime_to_ms(last_open + timedelta(minutes=5)) - 1
    assert stub.calls
    for _endpoint, params in stub.calls:
        assert params["start"] >= window_start_ms
        assert params["end"] <= window_end_ms
        assert params["end"] < datetime_to_ms(DOGE_ASOF)


def test_root_cause_repro_latest_n_cannot_reach_the_matured_window():
    """The pre-fix strategy is reproduced verbatim (latest N, then filter):
    it sees at most the window tail (the VPS run stored 4/48) while the range
    fetch observes all 48 bars of the same matured window."""
    opens, rows = _doge_fixture()
    first_open, last_open, _horizon_end = window_bounds(DOGE_SIGNAL)
    start, end = first_open, last_open + timedelta(minutes=5)
    client = _client(StubBybit(rows))

    # pre-fix BybitClosedCandleSource logic
    requested = int((end - start) / timedelta(minutes=5)) + 3
    latest = client.get_klines("DOGEUSDT", "5", requested)
    stale = [c for c in latest
             if datetime_to_ms(start) <= c.timestamp < datetime_to_ms(end)]
    # at best the tail of the window: exactly the DOGE 4/48 defect class
    assert 0 <= len(stale) < 48
    assert datetime_to_ms(opens[0]) not in {c.timestamp for c in stale}

    fetched = client.get_klines_range(
        "DOGEUSDT", start_ms=datetime_to_ms(first_open),
        end_ms=datetime_to_ms(last_open + timedelta(minutes=5)) - 1)
    assert [c.timestamp for c in fetched] == [datetime_to_ms(o) for o in opens]


# ── pagination, order, de-duplication ──────────────────────────────────


def test_pagination_merges_pages_without_gaps_or_duplicates():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    stub = StubBybit([_row(base + i * STEP_MS) for i in range(1002)])
    rows = _client(stub).get_klines_range(
        "BTCUSDT", start_ms=base, end_ms=base + 1002 * STEP_MS - 1)
    assert len(rows) == 1002
    assert rows[0].timestamp == base
    assert rows[-1].timestamp == base + 1001 * STEP_MS
    assert len(stub.calls) == 2
    ts_values = [c.timestamp for c in rows]
    assert ts_values == sorted(set(ts_values))          # merged, no gaps/dups


def test_reverse_order_and_duplicate_rows_collapse_to_unique_ascending():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    opens = [base + idx * STEP_MS for idx in range(10)]
    rows = [_row(ts) for ts in reversed(opens)] * 2     # reversed + duplicated
    stub = StubBybit(rows, truncate=False)              # API returns them all
    got = _client(stub).get_klines_range(
        "BTCUSDT", start_ms=base, end_ms=base + 9 * STEP_MS)
    assert [c.timestamp for c in got] == opens          # unique + ascending


def test_repeat_reads_are_idempotent_and_duplicate_free():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    stub = StubBybit([_row(base + i * STEP_MS) for i in range(30)])
    client = _client(stub)
    first = client.get_klines_range(
        "BTCUSDT", start_ms=base, end_ms=base + 29 * STEP_MS)
    second = client.get_klines_range(
        "BTCUSDT", start_ms=base, end_ms=base + 29 * STEP_MS)
    assert [c.timestamp for c in first] == [c.timestamp for c in second]
    assert len({c.timestamp for c in first}) == len(first) == 30


# ── boundaries, grid and malformed rows ────────────────────────────────


def test_bounds_are_millisecond_precise_and_grid_bound():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    # a misbehaving API returns rows on BOTH sides of the requested bounds
    rows = [_row(base - STEP_MS), _row(base), _row(base + STEP_MS),
            _row(base + 2 * STEP_MS)]
    stub = StubBybit(rows, respect_bounds=False, truncate=False)
    got = _client(stub).get_klines_range(
        "BTCUSDT", start_ms=base, end_ms=base + 2 * STEP_MS - 1)
    # start inclusive, end exclusive, 1ms resolution
    assert [c.timestamp for c in got] == [base, base + STEP_MS]


def test_window_bounds_are_start_inclusive_end_exclusive():
    start = datetime(2026, 10, 27, 21, 40, tzinfo=timezone.utc)
    base = datetime_to_ms(start)
    stub = StubBybit([_row(base + i * STEP_MS) for i in range(5)],
                     respect_bounds=False, truncate=False)
    observer = BybitClosedCandleSource(
        _client(stub), clock=lambda: start + timedelta(hours=2))
    candles = observer.closed_candles(
        "BTCUSDT", start=start,
        end=start + timedelta(minutes=15))
    assert [c.open_time for c in candles] == [
        start, start + timedelta(minutes=5), start + timedelta(minutes=10)]


def test_off_grid_rows_are_rejected_not_reinterpreted():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [_row(base), _row(base + 123), _row(base + STEP_MS)]
    stub = StubBybit(rows, respect_bounds=False, truncate=False)
    got = _client(stub).get_klines_range(
        "BTCUSDT", start_ms=base, end_ms=base + STEP_MS)
    assert [c.timestamp for c in got] == [base, base + STEP_MS]


def test_malformed_rows_are_rejected_without_synthesizing_ohlc():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [
        ["not-a-timestamp", "100", "101", "99", "100", "1"],   # bad timestamp
        [str(base + STEP_MS)],                                # truncated row
        [str(base + 2 * STEP_MS), "x", "101", "99", "100", "1"],   # bad price
        [str(base + 3 * STEP_MS), "nan", "101", "99", "100", "1"],  # non-finite
        {"open": "100"},                                      # not a row
        _row(base),                                           # the real bar
    ]
    stub = StubBybit(rows, truncate=False)
    got = _client(stub).get_klines_range(
        "BTCUSDT", start_ms=base, end_ms=base + 3 * STEP_MS)
    assert [c.timestamp for c in got] == [base]           # nothing invented


# ── ASOF / closed-candle protection ────────────────────────────────────


def test_excludes_unclosed_and_future_candles():
    start = datetime(2026, 10, 27, 21, 35, tzinfo=timezone.utc)
    base = int(start.timestamp() * 1000)
    stub = StubBybit([_row(base + i * STEP_MS) for i in range(5)])
    observer = BybitClosedCandleSource(
        _client(stub), clock=lambda: start + timedelta(minutes=12))
    candles = observer.closed_candles(
        "BTCUSDT", start=start, end=start + timedelta(minutes=25))
    assert len(candles) == 2
    assert candles[-1].close_time == start + timedelta(minutes=10)
    # the request itself never runs past the observation clock
    for _endpoint, params in stub.calls:
        assert params["end"] < int(
            (start + timedelta(minutes=12)).timestamp() * 1000)


def test_missing_history_is_not_synthesized():
    start = datetime(2026, 10, 27, 21, 35, tzinfo=timezone.utc)
    base = int(start.timestamp() * 1000)
    stub = StubBybit([_row(base + i * STEP_MS) for i in range(48) if i != 8])
    observer = BybitClosedCandleSource(
        _client(stub), clock=lambda: start + timedelta(hours=8))
    assert len(observer.closed_candles(
        "XRPUSDT", start=start, end=start + timedelta(hours=4))) == 47


# ── API failure and pagination bounds (fail closed) ────────────────────


def test_api_failure_fails_closed_without_partial_history():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    stub = StubBybit([_row(base + i * STEP_MS) for i in range(1500)],
                     fail_on_call=2)
    client = _client(stub)
    with pytest.raises(BybitError):
        client.get_klines_range(
            "BTCUSDT", start_ms=base, end_ms=base + 1500 * STEP_MS - 1)
    # page 1 was already fetched, yet no partial history is ever returned
    assert len(stub.calls) == 2


def test_empty_pages_terminate_and_request_count_is_bounded():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    stub = StubBybit([])
    client = _client(stub)
    got = client.get_klines_range(
        "BTCUSDT", start_ms=base, end_ms=base + 10_000 * STEP_MS - 1)
    assert got == []
    assert len(stub.calls) == 10        # 10k bars / 1000 per page, no runaway

    with pytest.raises(BybitError):
        client.get_klines_range(
            "BTCUSDT", start_ms=base, end_ms=base + 99 * STEP_MS,
            page_size=10, max_pages=3)
    assert len(stub.calls) == 13        # exactly max_pages requests, then stop


def test_malformed_payload_is_rejected_not_silently_emptied():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    client = object.__new__(BybitClient)
    client._public_get = lambda endpoint, **kw: {"result": {}}
    with pytest.raises(BybitError):
        client.get_klines_range(
            "BTCUSDT", start_ms=base, end_ms=base + STEP_MS)


# ── legacy trading contract ────────────────────────────────────────────


def test_legacy_get_klines_remains_latest_n():
    stub = StubBybit([])
    # Legacy method must remain unchanged: it does not send start/end.
    calls = []
    client = object.__new__(BybitClient)
    client._public_get = lambda endpoint, **kw: (
        calls.append(kw) or {"result": {"list": []}})
    assert client.get_klines("BTCUSDT", "5", 20) == []
    assert "start" not in calls[0] and "end" not in calls[0]
    assert calls[0]["limit"] == 20


@pytest.mark.parametrize("bad_rows", ["bad", {"key": "value"}, 123, None])
def test_wrong_type_kline_list_raises_instead_of_returning_empty(bad_rows):
    base = int(datetime(
        2026, 10, 20, tzinfo=timezone.utc
    ).timestamp() * 1000)

    client = object.__new__(BybitClient)
    client._public_get = lambda endpoint, **kw: {
        "result": {"list": bad_rows}
    }

    with pytest.raises(BybitError, match="result.list must be a JSON array"):
        client.get_klines_range(
            "BTCUSDT",
            start_ms=base,
            end_ms=base + STEP_MS,
        )
