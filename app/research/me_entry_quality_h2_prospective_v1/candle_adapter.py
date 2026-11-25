"""Issue #312 / FR-02 — closed 5m candle provider (bounded research budget).

Research-only: acquires fully CLOSED 5m candles for ONE prospective H2 event
under a DEDICATED, bounded research API budget that can never compete with the
production scanner.  Data verification is delegated unchanged to the pure
PR #311 provider (:func:`provide_5m_candles`): UTC 5m grid, closed-only ASOF
truncation, gap/duplicate/order control, unconditional payload-hash
verification.  This module NEVER fabricates candles and NEVER substitutes
missing data.

Contract (FR-02):

* only candles fully closed at the ACTUAL observation instant are requested
  and used (``evaluation_asof_ms`` — strict ASOF boundary, no look-ahead);
* the canonical RSI warmup is the fixed 120 CLOSED 5m buckets ENDING AT the
  base candle ``O_0`` (``CANONICAL_LOOKBACK_5M`` of PR #311);
* the fetch is bounded: per-cycle and per-hour request caps, bounded pages
  per call (``get_klines_range`` pagination cap), bounded retry/backoff and
  an inter-request pause — all budget accounting is explicit and observable;
* the budget must be CONFIRMED at configuration time; otherwise the provider
  is OFF and the runtime reports ``BLOCKED_CANDLE_SOURCE`` — the production
  scanner is never affected either way;
* API / transport failures fail closed: no partial history is ever treated as
  complete, and no state is finalized from half a window.

Provenance reuses the H1-C identity contract unchanged
(:class:`HtfSourceIdentity` — source kind, label, fetch time, row count,
SHA-256 payload hash), serialized into the H2 result ``candle_source`` block.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from app.models import Candle
from app.research.me_entry_quality_h1_v1.htf_candles import latest_closed_open_ms
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_BYBIT_KLINE_V5,
    HtfSourceIdentity,
    build_source_identity,
)
from app.research.me_entry_quality_h2_v1.candle_provider import (
    CANDLE_5M_MS,
    CANONICAL_LOOKBACK_5M,
)

H2P_CANDLE_ADAPTER_VERSION = "H2P_CANDLE_ADAPTER_1.0.0"
TIMEFRAME_5M = "5"

# ─── outcome reason codes (fail closed, stable) ──────────────────────────────
REASON_OK = "OK"
REASON_PROVIDER_DISABLED = "CANDLE_PROVIDER_DISABLED"
REASON_BUDGET_EXHAUSTED = "CANDLE_API_BUDGET_EXHAUSTED"
REASON_FETCH_ERROR = "CANDLE_FETCH_ERROR"
REASON_EMPTY_PAYLOAD = "CANDLE_EMPTY_PAYLOAD"

#: The grid assumes epoch-millisecond 5m opens; refuse absurd instants
#: (before 1970-01-01 00:05 UTC) instead of producing a negative bucket.
_MIN_SIGNAL_TIME_US = CANDLE_5M_MS * 1000


@dataclass(frozen=True)
class GridAnchors:
    """5m grid anchors of ONE original signal instant (pure, causal).

    ``O_0`` (``base_open_ms``) is the last 5m bucket CLOSED at or before the
    exact ``signal_time`` instant (microsecond precision); ``O_1``
    (``first_obs_open_ms``) is the first bucket closing STRICTLY AFTER it —
    the same semantics as the H2 pipeline's ``_observation_indices``.
    """

    signal_time_us: int
    base_open_ms: int
    first_obs_open_ms: int

    def observation_open_ms(self, offset: int) -> int:
        """Open time of ``O_{1 + offset}`` (post-signal observation)."""
        return self.first_obs_open_ms + int(offset) * CANDLE_5M_MS


def observation_grid(signal_time_us: int) -> GridAnchors:
    """Derive ``(O_0, O_1)`` grid opens for a signal instant (pure)."""
    if (
        not isinstance(signal_time_us, int)
        or isinstance(signal_time_us, bool)
        or signal_time_us < _MIN_SIGNAL_TIME_US
    ):
        raise ValueError("signal_time_us must be an int >= 5 minutes epoch-us")
    # O_0: largest bucket with (open + 300_000) * 1000 <= signal_time_us
    base_ms = (signal_time_us - CANDLE_5M_MS * 1000) // 1000
    base_open = (base_ms // CANDLE_5M_MS) * CANDLE_5M_MS
    return GridAnchors(
        signal_time_us=int(signal_time_us),
        base_open_ms=base_open,
        first_obs_open_ms=base_open + CANDLE_5M_MS,
    )


def observation_window_elapsed(
    grid: GridAnchors, *, observation_window: int, evaluation_asof_ms: int
) -> bool:
    """True iff ALL ``observation_window`` post-signal candles are closed.

    The window's last candle ``O_window`` is closed at ``asof`` iff
    ``O_window.open + 300_000 <= asof`` (inclusive at the close boundary).
    """
    if (
        not isinstance(observation_window, int)
        or isinstance(observation_window, bool)
        or observation_window < 1
    ):
        raise ValueError("observation_window must be a positive integer")
    last_open = grid.observation_open_ms(observation_window - 1)
    return last_open + CANDLE_5M_MS <= int(evaluation_asof_ms)


def required_fetch_range(
    grid: GridAnchors, *, evaluation_asof_ms: int
) -> tuple[int, int]:
    """``(start_ms, end_ms)`` candle-open range required for one evaluation.

    Start: the first bucket of the canonical 120-bucket window ENDING AT
    ``O_0``.  End: the newest bucket CLOSED at ``evaluation_asof_ms`` (the
    only buckets usable at the observation instant — no look-ahead).
    """
    asof_ms = int(evaluation_asof_ms)
    start_ms = grid.base_open_ms - (CANONICAL_LOOKBACK_5M - 1) * CANDLE_5M_MS
    end_ms = latest_closed_open_ms(asof_ms, CANDLE_5M_MS)
    return start_ms, end_ms


# ─── dedicated research API budget ──────────────────────────────────────────

@dataclass
class CandleApiBudget:
    """Explicit, bounded request budget for the research candle source.

    Separate from every production limiter: the counters live in this process
    only and are additionally capped per cycle and per hour so the research
    runtime can never compete with the scanner for Bybit request capacity.

    Review P1-budget — request accounting:

    * ``try_consume(count)`` reserves ``count`` ACTUAL HTTP requests
      (all-or-nothing).  One ``get_klines_range`` page is one HTTP request
      (the research client is built with ``bybit_max_attempts=1`` and
      ``bybit_rate_limit_max_retries=0`` — enforced by the runner factory and
      pinned by tests), so a paged fetch reserves its whole page count BEFORE
      any request leaves and can never exceed the caps even with
      ``max_pages > 1``;
    * ``requests_per_page > 1`` makes the reservation a conservative UPPER
      bound (for clients that retry internally) — it never under-counts;
    * the hourly window is PERSISTED through the store (``hydrate`` /
      ``on_change``) so a restart never resets it unsafely.  Persistence is
      fail-closed: once a save fails, no further request is granted until the
      store recovers.
    """

    max_requests_per_cycle: int = 10
    max_requests_per_hour: int = 240
    max_retries: int = 2
    retry_backoff_s: float = 2.0
    request_pause_s: float = 0.5
    page_size: int = 1000
    max_pages: int = 10
    #: HTTP requests ONE ``get_klines_range`` page may cost (1 = exact).
    requests_per_page: int = 1
    #: injectable monotonic clock (tests)
    clock: Callable[[], float] = field(default=time.monotonic)
    #: injectable wall clock for the PERSISTED hour window (tests)
    wall: Callable[[], float] = field(default=time.time)
    #: injectable sleep (tests never actually sleep)
    sleep: Callable[[float], None] = field(default=time.sleep)
    #: persistence hook (store.save_api_budget_state)
    on_change: Callable[[Mapping[str, Any]], None] | None = None
    #: fail-closed latch: True once a state save failed
    persistence_failed: bool = False
    # ── accounting (telemetry) ──
    api_requests: int = 0
    retries: int = 0
    rate_limit_skips: int = 0
    budget_skips: int = 0
    _cycle_requests: int = 0
    _hour_started_at_ms: int | None = field(default=None)
    _hour_requests: int = 0

    def start_cycle(self) -> None:
        self._cycle_requests = 0

    def state(self) -> dict[str, Any]:
        return {
            "hour_started_at_ms": int(self._hour_started_at_ms or 0),
            "requests_in_hour": int(self._hour_requests),
        }

    def hydrate(self, state: Mapping[str, Any] | None) -> None:
        """Restore the PERSISTED hour window (review P1-budget restart)."""
        if not state:
            return
        self._hour_started_at_ms = (
            int(state.get("hour_started_at_ms") or 0) or None
        )
        self._hour_requests = int(state.get("requests_in_hour") or 0)
        self.persistence_failed = False

    def _persist(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(self.state())
        except Exception:  # noqa: BLE001 - fail closed on persistence errors
            self.persistence_failed = True

    def _roll_hour(self) -> None:
        now_ms = int(self.wall() * 1000)
        if (
            self._hour_started_at_ms is None
            or now_ms - self._hour_started_at_ms >= 3_600_000
        ):
            self._hour_started_at_ms = now_ms
            self._hour_requests = 0

    def try_consume(self, count: int = 1) -> bool:
        """Reserve ``count`` HTTP requests; ``False`` when a cap would be
        exceeded (all-or-nothing — nothing is consumed on failure)."""
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError("count must be a positive integer")
        if self.persistence_failed:
            # Fail closed: an unpersisted budget could reset on restart.
            self.budget_skips += 1
            return False
        self._roll_hour()
        if self._cycle_requests + count > self.max_requests_per_cycle:
            self.budget_skips += 1
            return False
        if self._hour_requests + count > self.max_requests_per_hour:
            self.budget_skips += 1
            return False
        self._cycle_requests += count
        self._hour_requests += count
        self.api_requests += count
        self._persist()
        return True

    def requests_for_pages(self, pages: int) -> int:
        """Reservation size for ``pages`` pagination pages (>= 1)."""
        return max(1, int(pages)) * max(1, int(self.requests_per_page))

    def note_retry(self) -> None:
        self.retries += 1

    def note_rate_limit_skip(self) -> None:
        self.rate_limit_skips += 1

    def wait(self) -> None:
        if self.request_pause_s > 0:
            self.sleep(self.request_pause_s)

    def backoff(self, attempt: int) -> None:
        delay = self.retry_backoff_s * (2 ** max(0, attempt - 1))
        if delay > 0:
            self.sleep(delay)


# ─── fetch outcome ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CandleFetchOutcome:
    """Result of ONE bounded closed-candle acquisition (fail closed)."""

    ok: bool
    reason: str = REASON_OK
    detail: str = ""
    rows: tuple[Candle, ...] = ()
    source_identity: HtfSourceIdentity | None = None
    requested_range: tuple[int, int] = (0, 0)
    api_requests: int = 0
    retries: int = 0
    rate_limit_skips: int = 0
    provider_version: str = H2P_CANDLE_ADAPTER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_version": self.provider_version,
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "row_count": len(self.rows),
            "requested_range": list(self.requested_range),
            "api_requests": self.api_requests,
            "retries": self.retries,
            "rate_limit_skips": self.rate_limit_skips,
            "source_identity": (
                None if self.source_identity is None
                else self.source_identity.to_dict()
            ),
        }


def pages_for_range(start_ms: int, end_ms: int, page_size: int) -> int:
    """Number of ``get_klines_range`` HTTP requests for ``[start_ms, end_ms]``.

    Mirrors ``BybitClient.get_klines_range`` pagination exactly (inclusive
    bounds, at most ``page_size`` 5m bars per request), so one reserved unit
    maps to ONE actual HTTP request (review P1-budget).
    """
    if end_ms < start_ms:
        return 0
    clamped = max(1, min(int(page_size), 1000))
    span = int(end_ms) - int(start_ms) + 1
    per_page_ms = clamped * CANDLE_5M_MS
    return max(1, (span + per_page_ms - 1) // per_page_ms)


def _looks_like_rate_limit(message: str) -> bool:
    text = message.lower()
    return "429" in text or "rate limit" in text or "ratelimit" in text


class BybitClosedCandleProvider:
    """Bounded closed-5m acquisition for the H2 prospective runtime.

    ``client_factory`` must return an object exposing
    ``get_klines_range(symbol, interval, *, start_ms, end_ms, page_size,
    max_pages)`` (``app.exchange.bybit_client.BybitClient`` in production,
    fakes in tests).  When ``enabled`` is False (budget unconfirmed) the
    provider performs NO request at all and reports
    :data:`REASON_PROVIDER_DISABLED`.
    """

    def __init__(
        self,
        *,
        budget: CandleApiBudget,
        enabled: bool,
        client_factory: Callable[[], Any] | None = None,
        source_label_prefix: str = "bybit_kline_v5",
    ) -> None:
        self._budget = budget
        self._enabled = bool(enabled)
        self._client_factory = client_factory
        self._source_label_prefix = source_label_prefix

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def budget(self) -> CandleApiBudget:
        return self._budget

    def fetch_closed_5m(
        self,
        *,
        symbol: str,
        grid: GridAnchors,
        evaluation_asof_ms: int,
    ) -> CandleFetchOutcome:
        """Fetch the closed 5m range required for ONE event evaluation."""
        start_ms, end_ms = required_fetch_range(
            grid, evaluation_asof_ms=evaluation_asof_ms
        )
        requested = (start_ms, end_ms)
        if not self._enabled:
            return CandleFetchOutcome(
                ok=False,
                reason=REASON_PROVIDER_DISABLED,
                detail=(
                    "research candle API budget is not confirmed; provider "
                    "stays disabled (runtime status BLOCKED_CANDLE_SOURCE)"
                ),
                requested_range=requested,
            )
        if self._client_factory is None:
            return CandleFetchOutcome(
                ok=False,
                reason=REASON_PROVIDER_DISABLED,
                detail="no candle client configured",
                requested_range=requested,
            )

        requests_before = self._budget.api_requests
        retries_before = self._budget.retries
        skips_before = self._budget.rate_limit_skips
        # Review P1-budget: reserve the FULL pagination cost of the range
        # BEFORE any HTTP request leaves.  One page == one HTTP request under
        # the documented client invariant (single attempt per page).
        pages_needed = pages_for_range(
            start_ms, end_ms, self._budget.page_size
        )
        if pages_needed > self._budget.max_pages:
            return CandleFetchOutcome(
                ok=False,
                reason=REASON_FETCH_ERROR,
                detail=(
                    f"range needs {pages_needed} pages > max_pages="
                    f"{self._budget.max_pages}; refusing to fetch"
                ),
                requested_range=requested,
            )
        reserve = self._budget.requests_for_pages(pages_needed)
        rows: tuple[Candle, ...] = ()
        last_error = ""
        attempts = 0
        while True:
            attempts += 1
            if not self._budget.try_consume(reserve):
                return CandleFetchOutcome(
                    ok=False,
                    reason=REASON_BUDGET_EXHAUSTED,
                    detail=(
                        "research API request budget exhausted "
                        f"({reserve} requests reserved for {pages_needed} "
                        f"page(s), attempt {attempts}); fail closed, no "
                        "partial data"
                    ),
                    requested_range=requested,
                    api_requests=self._budget.api_requests - requests_before,
                    retries=self._budget.retries - retries_before,
                    rate_limit_skips=self._budget.rate_limit_skips - skips_before,
                )
            try:
                client = self._client_factory()
                fetched = client.get_klines_range(
                    symbol,
                    TIMEFRAME_5M,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    page_size=self._budget.page_size,
                    max_pages=self._budget.max_pages,
                )
                rows = tuple(fetched)
                self._budget.wait()
                break
            except Exception as exc:  # noqa: BLE001 - fail closed on ANY API error
                last_error = f"{type(exc).__name__}: {exc}"
                if _looks_like_rate_limit(last_error):
                    self._budget.note_rate_limit_skip()
                if attempts > self._budget.max_retries:
                    return CandleFetchOutcome(
                        ok=False,
                        reason=REASON_FETCH_ERROR,
                        detail=last_error,
                        requested_range=requested,
                        api_requests=self._budget.api_requests - requests_before,
                        retries=self._budget.retries - retries_before,
                        rate_limit_skips=(
                            self._budget.rate_limit_skips - skips_before
                        ),
                    )
                self._budget.note_retry()
                self._budget.backoff(attempts)

        if not rows:
            return CandleFetchOutcome(
                ok=False,
                reason=REASON_EMPTY_PAYLOAD,
                detail=f"no candle rows returned for {symbol} in {requested}",
                requested_range=requested,
                api_requests=self._budget.api_requests - requests_before,
                retries=self._budget.retries - retries_before,
                rate_limit_skips=self._budget.rate_limit_skips - skips_before,
            )

        # Provenance identity is computed over the EXACT row representation
        # handed to the pure PR #311 provider (``rows_as_mappings``), so its
        # unconditional payload-hash verification stays bit-exact.
        identity = build_source_identity(
            SOURCE_KIND_BYBIT_KLINE_V5,
            f"{self._source_label_prefix}:{symbol}:5m",
            rows_as_mappings(rows),
            fetched_at_ms=int(time.time() * 1000),
        )
        return CandleFetchOutcome(
            ok=True,
            reason=REASON_OK,
            rows=rows,
            source_identity=identity,
            requested_range=requested,
            api_requests=self._budget.api_requests - requests_before,
            retries=self._budget.retries - retries_before,
            rate_limit_skips=self._budget.rate_limit_skips - skips_before,
        )


def rows_as_mappings(rows: Sequence[Candle]) -> list[dict[str, Any]]:
    """Raw row mappings for the pure PR #311 provider (``provide_5m_candles``).

    The payload hash is computed by the SAME pure helpers on these rows, so
    provenance verification inside ``provide_5m_candles`` stays exact.
    """
    return [
        {
            "open_ms": int(candle.timestamp),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": float(candle.volume),
        }
        for candle in rows
    ]
