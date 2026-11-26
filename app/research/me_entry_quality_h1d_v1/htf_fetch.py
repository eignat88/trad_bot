"""Issue #314 / H1-D — bounded research-only historical HTF kline fetch.

``H1D_HTF_4H_FETCH_V1`` (``interval=240``) and ``H1D_HTF_1D_FETCH_V1``
(native Bybit ``interval=D``) — RESEARCH-ONLY, READ-ONLY_SOURCE.

Hard contract (Issue #314):

* the ONLY request path is ``BybitClient._public_get()`` — the shared
  global limiter, retry budget and circuit breaker are reused unchanged.
  This module NEVER performs direct HTTP and NEVER calls
  ``BybitClient.get_klines_range()`` (whose minute-only semantics stay
  frozen); native ``D`` support lives ONLY in this research adapter;
* explicit ``start_ms`` / ``end_ms`` bounds and bounded pagination with a
  HARD request cap — pagination can never spin and can never silently
  return half a window;
* fail-closed error propagation with explicit error types:
  HTTP 429 / Bybit ``retCode=10006`` → :class:`HtfFetchRateLimitError`;
  shared-limiter denial → :class:`HtfFetchLimiterDeniedError`; open
  breaker cooldown / storm guard → :class:`HtfFetchBreakerDeniedError`;
  transport / API / malformed payload → :class:`HtfFetchApiError` /
  :class:`HtfFetchTimeoutError`; request-cap exhaustion →
  :class:`HtfFetchRequestCapExceeded`.  NOTHING is ever retried at this
  layer (the attempt budget is owned by ``_public_get``) and on ANY error
  NO rows are returned — rows are never fabricated, never synthesized;
* output rows are strictly ascending, de-duplicated on the candle OPEN
  time; identical overlaps between pages are collapsed (counted), while
  two rows with the same open but different content are an explicit
  :class:`HtfFetchRowConflictError` (never resolved silently);
* malformed / off-grid / out-of-bounds rows are rejected and COUNTED in
  the fetch diagnostics — never repaired, never invented.

Per-call timeout: every logical request is issued with an explicit
``deadline_s`` so the client's retry envelope can only shrink, never grow.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.exchange.bybit_client import (
    BybitError,
    BybitRateLimitError,
    BybitTimeoutError,
)

H1D_HTF_4H_FETCH_VERSION = "H1D_HTF_4H_FETCH_V1"
H1D_HTF_1D_FETCH_VERSION = "H1D_HTF_1D_FETCH_V1"
H1D_HTF_FETCH_ADAPTER_VERSION = "H1D_HTF_FETCH_1.0.0"

TIMEFRAME_4H = "4H"
TIMEFRAME_1D = "1D"

#: research-only interval mapping — native ``D`` NEVER reaches the
#: production ``get_klines_range()`` (which accepts minute intervals only).
INTERVAL_BY_TIMEFRAME: dict[str, str] = {
    TIMEFRAME_4H: "240",
    TIMEFRAME_1D: "D",
}

STEP_MS_BY_TIMEFRAME: dict[str, int] = {
    TIMEFRAME_4H: 240 * 60_000,      # 14_400_000
    TIMEFRAME_1D: 1_440 * 60_000,    # 86_400_000
}

#: Bybit V5 hard cap on rows per kline request.
BYBIT_MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 200

#: hard pagination bound per fetch call (research budget).  The absolute
#: ceiling can never be exceeded regardless of the caller's argument.
HARD_REQUEST_CAP = 50
DEFAULT_MAX_REQUESTS = 20

#: per-logical-request timeout (seconds) forwarded as ``deadline_s``.
DEFAULT_CALL_TIMEOUT_S = 10.0

KLINE_ENDPOINT = "/v5/market/kline"

_ROW_FIELDS = ("open_ms", "open", "high", "low", "close", "volume")


# ─── explicit fail-closed error types ────────────────────────────────────────


class HtfFetchError(RuntimeError):
    """Base class — a historical HTF fetch failure (no rows are returned)."""


class HtfFetchApiError(HtfFetchError):
    """API unavailability / transient or malformed-payload failure."""


class HtfFetchTimeoutError(HtfFetchError):
    """Per-call timeout / transport timeout (fail closed, no partial rows)."""


class HtfFetchRateLimitError(HtfFetchError):
    """HTTP 429 / Bybit ``retCode=10006`` / exhausted rate-limit budget."""

    def __init__(self, message: str, *, ret_code: int | None = None) -> None:
        super().__init__(message)
        self.ret_code = ret_code


class HtfFetchLimiterDeniedError(HtfFetchError):
    """Shared cross-process limiter DENIED the request (fail closed)."""

    def __init__(self, message: str, *, limiter_result: str | None = None,
                 limiter_reason: str | None = None) -> None:
        super().__init__(message)
        self.limiter_result = limiter_result
        self.limiter_reason = limiter_reason


class HtfFetchBreakerDeniedError(HtfFetchError):
    """Circuit breaker / retry-storm guard deferred the request (fail closed)."""

    def __init__(self, message: str, *, limiter_result: str | None = None,
                 limiter_reason: str | None = None) -> None:
        super().__init__(message)
        self.limiter_result = limiter_result
        self.limiter_reason = limiter_reason


class HtfFetchRequestCapExceeded(HtfFetchError):
    """Bounded pagination exhausted its hard request cap (fail closed)."""


class HtfFetchRowConflictError(HtfFetchError):
    """Two rows share one open time with different content — never resolved."""


@dataclass(frozen=True)
class HtfFetchResult:
    """Strictly ascending, de-duplicated raw rows for ONE timeframe."""

    timeframe: str
    interval: str
    symbol: str
    start_ms: int
    end_ms: int
    rows: tuple[dict[str, Any], ...]
    requests_used: int
    rejected_rows: int
    deduped_rows: int
    adapter_version: str = H1D_HTF_FETCH_ADAPTER_VERSION

    @property
    def first_open_ms(self) -> int | None:
        return self.rows[0]["open_ms"] if self.rows else None

    @property
    def last_open_ms(self) -> int | None:
        return self.rows[-1]["open_ms"] if self.rows else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_version": self.adapter_version,
            "timeframe": self.timeframe,
            "interval": self.interval,
            "symbol": self.symbol,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "row_count": len(self.rows),
            "requests_used": self.requests_used,
            "rejected_rows": self.rejected_rows,
            "deduped_rows": self.deduped_rows,
            "first_open_ms": self.first_open_ms,
            "last_open_ms": self.last_open_ms,
        }


# ─── row normalization (never fabricates) ───────────────────────────────────


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_kline_row(row: Any, *, step_ms: int) -> dict[str, Any] | None:
    """Normalize ONE raw Bybit kline row; ``None`` = reject, never synthesize.

    A row must be a 6+ element sequence ``[open, o, h, l, c, v, ...]`` with
    an integer open timestamp on the timeframe grid and finite numeric
    OHLCV values.  Content validity (positive prices, ``high >= low``, ...)
    is the coverage layer's job — this layer only guarantees well-formed,
    on-grid, finite rows.
    """
    if isinstance(row, (str, bytes)):
        return None
    if isinstance(row, Mapping):
        try:
            open_ms = int(row.get("open_ms"))  # type: ignore[arg-type]
            values = [_finite(row.get(name)) for name in _ROW_FIELDS[1:]]
        except (TypeError, ValueError):
            return None
    elif isinstance(row, Sequence):
        if len(row) < 6:
            return None
        try:
            open_ms = int(row[0])
        except (TypeError, ValueError):
            return None
        values = [_finite(value) for value in row[1:6]]
    else:
        return None
    if any(value is None for value in values):
        return None
    if open_ms < 0 or open_ms % step_ms != 0:
        return None
    return {
        "open_ms": open_ms,
        "open": values[0],
        "high": values[1],
        "low": values[2],
        "close": values[3],
        "volume": values[4],
    }


# ─── error propagation (fail closed, explicit) ──────────────────────────────

_BREAKER_RESULTS = ("BREAKER_OPEN",)
_LIMITER_RESULTS = ("SKIPPED", "STATE_UNAVAILABLE", "REVOKED")


def _translate_client_error(exc: BaseException) -> HtfFetchError:
    """Map a ``_public_get`` failure to an EXPLICIT fetch error (from exc)."""
    if isinstance(exc, HtfFetchError):
        return exc
    if isinstance(exc, BybitRateLimitError):
        limiter_result = getattr(exc, "limiter_result", None)
        limiter_reason = getattr(exc, "limiter_reason", None)
        if limiter_result in _BREAKER_RESULTS:
            return HtfFetchBreakerDeniedError(
                f"breaker denied request: {exc}",
                limiter_result=limiter_result,
                limiter_reason=limiter_reason,
            )
        if limiter_result in _LIMITER_RESULTS:
            return HtfFetchLimiterDeniedError(
                f"shared limiter denied request: {exc}",
                limiter_result=limiter_result,
                limiter_reason=limiter_reason,
            )
        return HtfFetchRateLimitError(
            f"rate limited (429/10006/budget): {exc}",
            ret_code=getattr(exc, "ret_code", None),
        )
    if isinstance(exc, BybitTimeoutError):
        return HtfFetchTimeoutError(f"bybit timeout: {exc}")
    if isinstance(exc, (TimeoutError,)):
        return HtfFetchTimeoutError(f"timeout: {exc}")
    if isinstance(exc, BybitError):
        return HtfFetchApiError(f"bybit error: {exc}")
    return HtfFetchApiError(f"unexpected transport error: {exc}")


def _raise_on_payload_error(payload: Any) -> None:
    """A non-OK envelope is an EXPLICIT error — never an empty row list."""
    if not isinstance(payload, Mapping):
        raise HtfFetchApiError(
            f"malformed payload: expected mapping, got {type(payload).__name__}")
    ret_code = payload.get("retCode", 0)
    if ret_code in (0, "0", None):
        return
    ret_msg = payload.get("retMsg", "")
    try:
        code_int: int | None = int(ret_code)
    except (TypeError, ValueError):
        code_int = None
    if code_int == 10006:
        raise HtfFetchRateLimitError(
            f"bybit retCode=10006 rate limit: {ret_msg}", ret_code=code_int)
    message = f"bybit retCode={ret_code}: {ret_msg}"
    if isinstance(ret_msg, str) and "rate" in ret_msg.lower() and "limit" in ret_msg.lower():
        raise HtfFetchRateLimitError(message, ret_code=code_int)
    raise HtfFetchApiError(message)


# ─── the bounded research fetch ─────────────────────────────────────────────


def fetch_htf_history(
    client: Any,
    *,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    page_size: int = DEFAULT_PAGE_SIZE,
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
) -> HtfFetchResult:
    """Bounded historical kline fetch for ONE timeframe (research-only).

    Exactly one logical ``_public_get`` per page, ``interval=240`` for 4H
    and native ``interval=D`` for 1D (research adapter ONLY).  Raises an
    explicit :class:`HtfFetchError` on every failure and NEVER returns
    partial or fabricated rows.
    """
    if timeframe not in INTERVAL_BY_TIMEFRAME:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if not _is_int(start_ms) or not _is_int(end_ms):
        raise ValueError("start_ms / end_ms must be real integers")
    if not _is_int(max_requests) or max_requests < 1:
        raise ValueError("max_requests must be a positive integer")
    if not _is_int(page_size) or page_size < 1:
        raise ValueError("page_size must be a positive integer")
    if isinstance(call_timeout_s, bool) or not isinstance(call_timeout_s, (int, float)):
        raise ValueError("call_timeout_s must be a number")
    if call_timeout_s <= 0:
        raise ValueError("call_timeout_s must be positive")

    start_ms, end_ms = int(start_ms), int(end_ms)
    if start_ms < 0:
        raise ValueError("start_ms must be non-negative")
    if end_ms < start_ms:
        return HtfFetchResult(
            timeframe=timeframe,
            interval=INTERVAL_BY_TIMEFRAME[timeframe],
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            rows=(),
            requests_used=0,
            rejected_rows=0,
            deduped_rows=0,
        )

    interval = INTERVAL_BY_TIMEFRAME[timeframe]
    step_ms = STEP_MS_BY_TIMEFRAME[timeframe]
    hard_cap = min(int(max_requests), HARD_REQUEST_CAP)
    page_size = min(int(page_size), BYBIT_MAX_PAGE_SIZE)

    out: dict[int, dict[str, Any]] = {}
    rejected = 0
    deduped = 0
    requests_used = 0

    page_start = start_ms
    while page_start <= end_ms:
        if requests_used >= hard_cap:
            raise HtfFetchRequestCapExceeded(
                f"{symbol} {timeframe}: hard request cap {hard_cap} reached "
                f"with [{page_start}, {end_ms}] still unfetched — "
                "fail-closed, no partial history returned")
        page_end = min(end_ms, page_start + page_size * step_ms - 1)
        limit = min(page_size, (page_end - page_start) // step_ms + 1)
        try:
            payload = client._public_get(
                KLINE_ENDPOINT,
                category="linear",
                symbol=symbol,
                interval=interval,
                start=page_start,
                end=page_end,
                limit=limit,
                deadline_s=float(call_timeout_s),
            )
        except Exception as exc:  # noqa: BLE001 — translated explicitly below
            raise _translate_client_error(exc) from exc
        requests_used += 1

        _raise_on_payload_error(payload)
        result = payload.get("result")
        raw_rows = result.get("list") if isinstance(result, Mapping) else None
        if not isinstance(raw_rows, list):
            raise HtfFetchApiError(
                f"{symbol} {timeframe}: malformed payload: result.list must "
                "be a JSON array")

        for row in raw_rows:
            normalized = normalize_kline_row(row, step_ms=step_ms)
            if normalized is None:
                rejected += 1
                continue
            open_ms = normalized["open_ms"]
            if open_ms < start_ms or open_ms > end_ms:
                # outside the EXPLICIT request bounds — rejected, never
                # smuggled in (page bounds only drive pagination)
                rejected += 1
                continue
            existing = out.get(open_ms)
            if existing is not None:
                if existing == normalized:
                    deduped += 1
                    continue
                raise HtfFetchRowConflictError(
                    f"{symbol} {timeframe}: conflicting rows for open "
                    f"{open_ms} — never resolved silently")
            out[open_ms] = normalized

        page_start = page_end + 1

    return HtfFetchResult(
        timeframe=timeframe,
        interval=interval,
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        rows=tuple(out[open_ms] for open_ms in sorted(out)),
        requests_used=requests_used,
        rejected_rows=rejected,
        deduped_rows=deduped,
    )
