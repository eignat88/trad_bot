"""Issue #314 / H1-D — bounded research HTF fetch tests
(``H1D_HTF_4H_FETCH_V1`` / ``H1D_HTF_1D_FETCH_V1``).

Covers: ascending de-duplicated output, pagination (multi-page, overlap
dedup, hard request cap), native ``D`` interval ONLY in this research
adapter (``get_klines_range`` must NEVER be called), per-call timeout
forwarding, and fail-closed propagation of API unavailability / transient
errors / HTTP 429 / ``retCode=10006`` / shared-limiter denial / breaker
denial — never fabricating rows.

Deterministic and offline: every client is a stub; NO real Bybit requests.
"""
from __future__ import annotations

import pytest

from app.exchange.bybit_client import (
    BybitError,
    BybitRateLimitError,
    BybitTimeoutError,
)
from app.research.me_entry_quality_h1d_v1.htf_fetch import (
    DEFAULT_CALL_TIMEOUT_S,
    H1D_HTF_1D_FETCH_VERSION,
    H1D_HTF_4H_FETCH_VERSION,
    HARD_REQUEST_CAP,
    HtfFetchApiError,
    HtfFetchBreakerDeniedError,
    HtfFetchError,
    HtfFetchLimiterDeniedError,
    HtfFetchRateLimitError,
    HtfFetchResult,
    HtfFetchRowConflictError,
    HtfFetchRequestCapExceeded,
    HtfFetchTimeoutError,
    fetch_htf_history,
    normalize_kline_row,
)

TF = 14_400_000        # 4H
DAY = 86_400_000       # 1D
BASE_MS = 365 * DAY    # 1971-01-01T00:00:00Z — on both grids


class StubKlineApi:
    """GET /v5/market/kline double honoring the Bybit V5 contract.

    Raw rows are returned inside the inclusive ``[start, end]`` bounds,
    newest-first, truncated to ``limit`` (as the real API does).  The stub
    also enforces that ``get_klines_range`` is NEVER reached — the native
    ``D`` interval must stay inside this research adapter.
    """

    def __init__(self, rows, *, fail_with=None, fail_on_call=None,
                 respect_bounds=True, truncate=True, payload=None,
                 out_of_bounds_rows=()):
        self.rows = list(rows)
        self.calls = []
        self.fail_with = fail_with
        self.fail_on_call = fail_on_call
        self.respect_bounds = respect_bounds
        self.truncate = truncate
        self.payload = payload
        self.out_of_bounds_rows = list(out_of_bounds_rows)

    @staticmethod
    def _ts(row):
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError, KeyError):
            return None

    def _public_get(self, endpoint, **params):
        self.calls.append((endpoint, dict(params)))
        if self.fail_with is not None and (
                self.fail_on_call is None
                or len(self.calls) == self.fail_on_call):
            raise self.fail_with
        if self.payload is not None:
            return self.payload
        start = params.get("start")
        end = params.get("end")
        data = []
        for row in list(self.rows) + list(self.out_of_bounds_rows):
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
        return {"retCode": 0, "retMsg": "OK", "result": {"list": list(data)}}

    def get_klines_range(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError(
            "get_klines_range must NEVER be called by the H1-D research "
            "adapter (native D lives only in htf_fetch)")


def _client(stub):
    return stub  # the adapter only uses ``_public_get``


def _row(ts, open_="100", high="101", low="99", close="100", volume="1"):
    return [str(ts), open_, high, low, close, volume]


def _rows(start, n, step=TF):
    return [_row(start + i * step) for i in range(n)]


def _fetch(stub, *, timeframe="4H", start=BASE_MS, end=BASE_MS + 60 * TF - 1,
           **kwargs):
    return fetch_htf_history(
        _client(stub), symbol="SOXSUSDT", timeframe=timeframe,
        start_ms=start, end_ms=end, **kwargs)


# ─── versioned substreams ───────────────────────────────────────────────────


class TestVersions:
    def test_substream_versions(self):
        assert H1D_HTF_4H_FETCH_VERSION == "H1D_HTF_4H_FETCH_V1"
        assert H1D_HTF_1D_FETCH_VERSION == "H1D_HTF_1D_FETCH_V1"


# ─── happy path: ascending, deduped, bounded ───────────────────────────────


class TestFetchHappyPath:
    def test_single_page_rows_normalized_ascending(self):
        stub = StubKlineApi(_rows(BASE_MS, 10))
        result = _fetch(stub, end=BASE_MS + 10 * TF - 1)
        assert isinstance(result, HtfFetchResult)
        assert [row["open_ms"] for row in result.rows] == [
            BASE_MS + i * TF for i in range(10)]
        assert result.rows[0] == {
            "open_ms": BASE_MS, "open": 100.0, "high": 101.0,
            "low": 99.0, "close": 100.0, "volume": 1.0}
        assert result.requests_used == 1
        assert result.rejected_rows == 0
        assert result.deduped_rows == 0

    def test_4h_uses_interval_240_and_explicit_bounds(self):
        stub = StubKlineApi(_rows(BASE_MS, 5))
        _fetch(stub, end=BASE_MS + 5 * TF - 1)
        _, params = stub.calls[0]
        assert params["interval"] == "240"
        assert params["category"] == "linear"
        assert params["symbol"] == "SOXSUSDT"
        assert params["start"] == BASE_MS
        assert params["end"] == BASE_MS + 5 * TF - 1

    def test_1d_uses_native_interval_d(self):
        stub = StubKlineApi(_rows(BASE_MS, 5, step=DAY))
        result = _fetch(
            stub, timeframe="1D", end=BASE_MS + 5 * DAY - 1)
        _, params = stub.calls[0]
        assert params["interval"] == "D"
        assert result.interval == "D"
        assert [row["open_ms"] for row in result.rows] == [
            BASE_MS + i * DAY for i in range(5)]

    def test_call_timeout_is_forwarded_as_deadline(self):
        stub = StubKlineApi(_rows(BASE_MS, 5))
        _fetch(stub, end=BASE_MS + 5 * TF - 1, call_timeout_s=3.5)
        _, params = stub.calls[0]
        assert params["deadline_s"] == 3.5

    def test_default_call_timeout(self):
        stub = StubKlineApi(_rows(BASE_MS, 5))
        _fetch(stub, end=BASE_MS + 5 * TF - 1)
        _, params = stub.calls[0]
        assert params["deadline_s"] == DEFAULT_CALL_TIMEOUT_S

    def test_multi_page_pagination(self):
        stub = StubKlineApi(_rows(BASE_MS, 30))
        result = _fetch(
            stub, end=BASE_MS + 30 * TF - 1, page_size=10)
        assert result.requests_used == 3
        assert len(result.rows) == 30
        assert result.rows[-1]["open_ms"] == BASE_MS + 29 * TF

    def test_overlap_between_pages_is_deduplicated(self):
        # misbehaving API echoes EVERY row on every page
        stub = StubKlineApi(_rows(BASE_MS, 20),
                            respect_bounds=False, truncate=False)
        result = _fetch(
            stub, end=BASE_MS + 20 * TF - 1, page_size=10)
        assert len(result.rows) == 20
        assert len({row["open_ms"] for row in result.rows}) == 20
        assert result.deduped_rows > 0
        assert result.rejected_rows == 0

    def test_empty_range_without_requests(self):
        stub = StubKlineApi([])
        result = _fetch(stub, start=BASE_MS + 10, end=BASE_MS)
        assert result.rows == ()
        assert result.requests_used == 0

    def test_no_rows_returned_for_empty_exchange_window(self):
        stub = StubKlineApi([])
        result = _fetch(stub, end=BASE_MS + 10 * TF - 1)
        assert result.rows == ()   # empty is reported, never fabricated


# ─── bounded pagination: hard request cap ──────────────────────────────────


class TestRequestCap:
    def test_hard_cap_raises_and_stops(self):
        stub = StubKlineApi(_rows(BASE_MS, 40))
        with pytest.raises(HtfFetchRequestCapExceeded):
            _fetch(stub, end=BASE_MS + 40 * TF - 1,
                   page_size=10, max_requests=2)
        assert len(stub.calls) == 2  # never exceeds the cap

    def test_absolute_hard_cap_cannot_be_raised(self):
        stub = StubKlineApi(_rows(BASE_MS, 200))
        with pytest.raises(HtfFetchRequestCapExceeded):
            _fetch(stub, end=BASE_MS + 200 * TF - 1,
                   page_size=1, max_requests=10 ** 6)
        assert len(stub.calls) == HARD_REQUEST_CAP

    def test_cap_exceeded_returns_no_partial_rows(self):
        stub = StubKlineApi(_rows(BASE_MS, 40))
        with pytest.raises(HtfFetchError):
            _fetch(stub, end=BASE_MS + 40 * TF - 1,
                   page_size=10, max_requests=1)


# ─── fail-closed error propagation (mocked ``_public_get``) ────────────────


class TestFailClosedErrors:
    def test_transient_api_error(self):
        stub = StubKlineApi([], fail_with=BybitError("HTTP 503"))
        with pytest.raises(HtfFetchApiError):
            _fetch(stub)
        assert not stub.calls[0][1].get("_partial")

    def test_api_unavailability_payload_retcode(self):
        stub = StubKlineApi([], payload={"retCode": 10001,
                                         "retMsg": "service unavailable"})
        with pytest.raises(HtfFetchApiError):
            _fetch(stub)

    def test_malformed_payload(self):
        stub = StubKlineApi([], payload={"retCode": 0, "result": {}})
        with pytest.raises(HtfFetchApiError):
            _fetch(stub)

    def test_non_mapping_payload(self):
        stub = StubKlineApi([], payload=["not", "a", "mapping"])
        with pytest.raises(HtfFetchApiError):
            _fetch(stub)

    @pytest.mark.parametrize("error,expected", [
        (TimeoutError("socket timeout"), HtfFetchTimeoutError),
        (BybitTimeoutError("deadline exceeded"), HtfFetchTimeoutError),
    ])
    def test_timeout_is_explicit(self, error, expected):
        stub = StubKlineApi([], fail_with=error)
        with pytest.raises(expected):
            _fetch(stub)

    def test_http_429_propagates_as_rate_limit(self):
        error = BybitRateLimitError(
            "rate limited", limiter_result="RATE_LIMIT", ret_code=10006)
        stub = StubKlineApi([], fail_with=error)
        with pytest.raises(HtfFetchRateLimitError) as excinfo:
            _fetch(stub)
        assert excinfo.value.ret_code == 10006
        assert excinfo.value.__cause__ is error

    def test_retcode_10006_in_payload_is_rate_limit(self):
        stub = StubKlineApi([], payload={"retCode": 10006,
                                         "retMsg": "too many visits"})
        with pytest.raises(HtfFetchRateLimitError):
            _fetch(stub)

    def test_limiter_denial_is_explicit(self):
        error = BybitRateLimitError(
            "denied", limiter_result="SKIPPED", limiter_reason="deadline")
        stub = StubKlineApi([], fail_with=error)
        with pytest.raises(HtfFetchLimiterDeniedError) as excinfo:
            _fetch(stub)
        assert excinfo.value.limiter_result == "SKIPPED"
        assert excinfo.value.__cause__ is error

    @pytest.mark.parametrize("result", [
        "SKIPPED", "STATE_UNAVAILABLE", "REVOKED", "LIMITER_UNAVAILABLE",
    ])
    def test_every_limiter_gate_result_is_a_limiter_denial(self, result):
        """Audit D2: ``LIMITER_UNAVAILABLE`` (shared limiter ERROR) must be a
        limiter denial, never misclassified as a Bybit rate limit."""
        error = BybitRateLimitError(
            "denied", limiter_result=result, limiter_reason="gate")
        stub = StubKlineApi([], fail_with=error)
        with pytest.raises(HtfFetchLimiterDeniedError) as excinfo:
            _fetch(stub)
        assert excinfo.value.limiter_result == result
        assert excinfo.value.__cause__ is error

    def test_process_local_breaker_deferral_is_breaker_denied(self):
        """``retry_budget.raise_bybit_rate_limit_deferred`` carries no
        structured ``limiter_result`` — the cooldown marker classifies it."""
        error = BybitRateLimitError(
            "request deferred: rate-limit cooldown open (2.00s remaining)")
        stub = StubKlineApi([], fail_with=error)
        with pytest.raises(HtfFetchBreakerDeniedError) as excinfo:
            _fetch(stub)
        assert excinfo.value.__cause__ is error

    def test_retry_storm_guard_denial_is_breaker_denied(self):
        error = BybitRateLimitError(
            "SOXSUSDT: Rate limited on /v5/market/kline after 1 attempt(s) "
            "(storm guard: retry share capped)",
            limiter_result="RATE_LIMIT",
            limiter_reason="storm guard: retry share capped")
        stub = StubKlineApi([], fail_with=error)
        with pytest.raises(HtfFetchBreakerDeniedError):
            _fetch(stub)

    @pytest.mark.parametrize("result", ["BREAKER_OPEN"])
    def test_breaker_denial_is_explicit(self, result):
        error = BybitRateLimitError(
            "breaker open", limiter_result=result,
            limiter_reason="breaker_cooldown_exceeds_deadline")
        stub = StubKlineApi([], fail_with=error)
        with pytest.raises(HtfFetchBreakerDeniedError):
            _fetch(stub)

    def test_mid_pagination_failure_returns_no_rows(self):
        stub = StubKlineApi(_rows(BASE_MS, 30),
                            fail_with=BybitError("down"), fail_on_call=2)
        with pytest.raises(HtfFetchApiError):
            _fetch(stub, end=BASE_MS + 30 * TF - 1, page_size=10)

    def test_every_failure_is_a_fetch_error(self):
        stub = StubKlineApi([], fail_with=RuntimeError("surprise"))
        with pytest.raises(HtfFetchError):
            _fetch(stub)


# ─── strict rows: reject + count, never fabricate ──────────────────────────


class TestRowDiscipline:
    def test_malformed_rows_rejected_not_fabricated(self):
        rows = _rows(BASE_MS, 5)
        stub = StubKlineApi(rows, respect_bounds=False, truncate=False,
                            out_of_bounds_rows=[
            ["not-a-ts", "1", "1", "1", "1", "1"],
            ["1", "1"],                        # wrong arity
            ["1", "x", "1", "1", "1", "1"],    # non-numeric
            ["1", "1", "1", "1", "1", "nan"],  # non-finite
        ])
        result = _fetch(stub, end=BASE_MS + 5 * TF - 1)
        assert len(result.rows) == 5
        assert result.rejected_rows == 4

    def test_off_grid_rows_rejected(self):
        stub = StubKlineApi(_rows(BASE_MS, 3), truncate=False,
                            out_of_bounds_rows=[
                                [str(BASE_MS + 1), "1", "1", "1", "1", "1"],
                            ])
        result = _fetch(stub, end=BASE_MS + 3 * TF - 1)
        assert result.rejected_rows == 1
        assert all(row["open_ms"] % TF == 0 for row in result.rows)

    def test_out_of_bounds_rows_rejected(self):
        stub = StubKlineApi(_rows(BASE_MS, 3), respect_bounds=False,
                            truncate=False,
                            out_of_bounds_rows=[
                                _row(BASE_MS - TF),
                                _row(BASE_MS + 10 * TF),
                            ])
        result = _fetch(stub, end=BASE_MS + 3 * TF - 1)
        assert len(result.rows) == 3
        assert result.rejected_rows == 2

    def test_conflicting_duplicate_raises(self):
        rows = _rows(BASE_MS, 3)
        stub = StubKlineApi(rows, respect_bounds=False, truncate=False,
                            out_of_bounds_rows=[
                                [str(BASE_MS), "200", "1", "1", "1", "1"],
                            ])
        with pytest.raises(HtfFetchRowConflictError):
            _fetch(stub, end=BASE_MS + 3 * TF - 1)

    def test_mapping_rows_normalize_too(self):
        row = normalize_kline_row(
            {"open_ms": BASE_MS, "open": 1, "high": 2, "low": 0.5,
             "close": 1.5, "volume": 3}, step_ms=TF)
        assert row == {"open_ms": BASE_MS, "open": 1.0, "high": 2.0,
                       "low": 0.5, "close": 1.5, "volume": 3.0}


# ─── argument validation ───────────────────────────────────────────────────


class TestArguments:
    @pytest.mark.parametrize("kwargs", [
        {"timeframe": "1H"}, {"timeframe": "D"}, {"symbol": ""},
        {"start_ms": True}, {"end_ms": 1.5}, {"max_requests": 0},
        {"page_size": 0}, {"call_timeout_s": 0}, {"call_timeout_s": "x"},
    ])
    def test_invalid_arguments_rejected(self, kwargs):
        params = dict(symbol="SOXSUSDT", timeframe="4H",
                      start_ms=BASE_MS, end_ms=BASE_MS + TF)
        params.update(kwargs)
        with pytest.raises(ValueError):
            fetch_htf_history(_client(StubKlineApi([])), **params)

    def test_negative_start_rejected(self):
        with pytest.raises(ValueError):
            fetch_htf_history(
                _client(StubKlineApi([])), symbol="SOXSUSDT",
                timeframe="4H", start_ms=-1, end_ms=TF)
