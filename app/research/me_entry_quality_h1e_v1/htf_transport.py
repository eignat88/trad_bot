"""Issue #324 / H1-E — C.3: HTF transport adapter over the frozen H1-D
``240``/native ``D`` research fetcher.

RESEARCH_ONLY adapter: it wraps the EXISTING H1-D bounded kline fetcher
(``app.research.me_entry_quality_h1e_v1`` is the single sanctioned
consumer of the frozen H1-D contracts) behind the frozen H1-E
``HtfTransport`` contract.  Design constraints:

* frozen ASOF semantics — a window is built for the ORIGINAL
  ``signal_time`` (``asof_us``) and only candles that were CLOSED at that
  instant are ever returned (``close_ms <= asof_ms``); later candles are
  dropped and counted, never used;
* 60/60 coverage stays the classifier's verdict — this adapter never
  fabricates rows and never pads windows (a short fetch is returned
  short and fails the coverage check downstream);
* the SHARED Bybit limiter / retry budget / circuit breaker are reused
  UNCHANGED — the injected client is the standard Bybit client object and
  every logical request goes through its limiter-gated entry point;
* a cumulative request budget bounds the TOTAL API usage of the adapter;
  exhaustion defers (bounded), it never bypasses the limiter;
* every fetch failure maps to an EXPLICIT H1-E transport error —
  unavailable or corrupted data fails closed.

No live Bybit request is ever made by the offline tests; the client is
always injected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_BYBIT_KLINE_V5,
)
from app.research.me_entry_quality_h1d_v1.htf_fetch import (
    HtfFetchApiError,
    HtfFetchBreakerDeniedError,
    HtfFetchLimiterDeniedError,
    HtfFetchRateLimitError,
    HtfFetchRequestCapExceeded,
    HtfFetchResult,
    HtfFetchRowConflictError,
    HtfFetchTimeoutError,
    INTERVAL_BY_TIMEFRAME,
    STEP_MS_BY_TIMEFRAME,
    fetch_htf_history,
)
from app.research.me_entry_quality_h1e_v1.classification import (
    CANONICAL_LOOKBACK,
    HtfBreakerDeniedError,
    HtfLimiterDeniedError,
    HtfPayload,
    HtfRateLimitError,
    HtfTimeoutError,
    HtfTransportApiError,
    INTERVAL_1D,
    INTERVAL_4H,
)
from app.research.me_entry_quality_h1e_v1.contracts import FIXTURE_LABEL_REAL

H1E_HTF_TRANSPORT_VERSION = "H1E_HTF_TRANSPORT_V1"

#: frozen H1-E interval id -> frozen H1-D timeframe id
TIMEFRAME_BY_INTERVAL: dict[str, str] = {
    INTERVAL_4H: "4H",
    INTERVAL_1D: "1D",
}


class H1EHtfApiBudgetExceeded(HtfRateLimitError):
    """The adapter's cumulative request budget is spent (bounded deferral).

    Surfaces as :class:`HtfRateLimitError` so the classifier's bounded
    deferral budget handles it exactly like a rate limit."""


@dataclass
class H1EHtfTransport:
    """``HtfTransport`` implementation over the frozen H1-D fetcher.

    ``client`` is the shared Bybit client object (its limiter-gated
    ``_public_get`` is the ONLY network entry point — the adapter never
    bypasses the shared limiter).  ``fetch`` is injectable for offline
    tests (defaults to the frozen H1-D ``fetch_htf_history``).
    """

    client: Any
    #: total API requests this adapter may ever consume (None = unbounded
    #: at THIS layer; the shared limiter still bounds per-request usage)
    request_budget: int | None = None
    max_requests_per_call: int = 20
    page_size: int = 200
    call_timeout_s: float = 10.0
    fixture_label: str = FIXTURE_LABEL_REAL
    source_label: str = "h1d-kline"
    fetch: Callable[..., HtfFetchResult] = fetch_htf_history
    requests_used_total: int = field(default=0, init=False)

    # ─── HtfTransport contract ─────────────────────────────────────────
    def fetch_htf_window(
        self,
        *,
        symbol: str,
        interval: str,
        asof_us: int,
        lookback: int = CANONICAL_LOOKBACK,
    ) -> HtfPayload:
        if interval not in TIMEFRAME_BY_INTERVAL:
            raise ValueError(f"unknown interval {interval!r}")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if isinstance(asof_us, bool) or not isinstance(asof_us, int):
            raise ValueError("asof_us must be a real integer")
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
            raise ValueError("lookback must be a positive integer")

        timeframe = TIMEFRAME_BY_INTERVAL[interval]
        step_ms = int(STEP_MS_BY_TIMEFRAME[timeframe])
        asof_ms = int(asof_us) // 1000

        # last candle CLOSED at the frozen ASOF: close_ms <= asof_ms
        last_open_ms = (asof_ms // step_ms) * step_ms
        if last_open_ms + step_ms > asof_ms:
            last_open_ms -= step_ms
        # one spare window of margin: rejected/short rows never trigger an
        # extra API round-trip, and no row can leak past the ASOF filter
        start_ms = last_open_ms - (int(lookback) + 1) * step_ms
        if start_ms < 0:
            start_ms = 0

        allowed = self._remaining_requests()
        if allowed < 1:
            raise H1EHtfApiBudgetExceeded(
                f"{self.source_label}: cumulative HTF API budget spent "
                f"({self.requests_used_total} requests used) — bounded "
                "deferral, the shared limiter is never bypassed"
            )
        result = self.fetch(
            self.client,
            symbol=symbol,
            timeframe=timeframe,
            start_ms=start_ms,
            end_ms=last_open_ms,
            max_requests=min(int(self.max_requests_per_call), allowed),
            page_size=int(self.page_size),
            call_timeout_s=float(self.call_timeout_s),
        )
        self.requests_used_total += int(result.requests_used)

        # STRICT ASOF filter: only candles closed at/before the frozen
        # signal time; anything later is dropped and reported, never used
        kept: list[Mapping[str, Any]] = []
        dropped_future = 0
        for row in result.rows:
            open_ms = int(row["open_ms"])
            if open_ms + step_ms > asof_ms:
                dropped_future += 1
                continue
            if open_ms > last_open_ms:
                dropped_future += 1
                continue
            kept.append(dict(row))
        return HtfPayload(
            rows=tuple(kept),
            source_kind=SOURCE_KIND_BYBIT_KLINE_V5,
            source_label=f"{self.source_label}:{symbol}:{interval}",
            fixture_label=self.fixture_label,
            fetched_at_ms=None,
        )

    # ─── internals ─────────────────────────────────────────────────────
    def _remaining_requests(self) -> int:
        if self.request_budget is None:
            return int(self.max_requests_per_call)
        return int(self.request_budget) - int(self.requests_used_total)


def map_h1d_fetch_error(exc: BaseException) -> BaseException:
    """Map a frozen H1-D fetch error to the EXPLICIT H1-E error class."""
    if isinstance(exc, H1EHtfApiBudgetExceeded):
        return exc
    if isinstance(exc, HtfFetchLimiterDeniedError):
        return HtfLimiterDeniedError(str(exc))
    if isinstance(exc, HtfFetchBreakerDeniedError):
        return HtfBreakerDeniedError(str(exc))
    if isinstance(exc, HtfFetchRateLimitError):
        return HtfRateLimitError(str(exc))
    if isinstance(exc, HtfFetchTimeoutError):
        return HtfTimeoutError(str(exc))
    if isinstance(exc, (
        HtfFetchApiError,
        HtfFetchRequestCapExceeded,
        HtfFetchRowConflictError,
    )):
        return HtfTransportApiError(str(exc))
    return HtfTransportApiError(f"unrecognized HTF fetch failure: {exc}")


class MappedH1EHtfTransport(H1EHtfTransport):
    """Same adapter, but every fetch failure surfaces as its H1-E class."""

    def fetch_htf_window(self, **kwargs: Any) -> HtfPayload:
        try:
            return super().fetch_htf_window(**kwargs)
        except H1EHtfApiBudgetExceeded:
            raise
        except ValueError:
            raise  # caller contract violations stay ValueError (fail closed)
        except Exception as exc:  # noqa: BLE001 — explicit fail-closed map
            raise map_h1d_fetch_error(exc) from exc
