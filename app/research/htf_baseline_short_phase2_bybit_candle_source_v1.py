"""Issue #181 — causal closed-Bybit 5m candle source for the HTF baseline SHORT
Phase-2 observer (``HTF_BASELINE_SHORT_PHASE2_LIVE_BYBIT_SOURCE_V1``).

RESEARCH_ONLY / OBSERVE_ONLY. Production activation is BLOCKED behind the
issue #181 gate. This module only ever reads PUBLIC market data (``/v5/market/*``)
through the injected read-only transport; it never authenticates, never signs a
request, never touches paper/live execution and never writes to any database.

``BybitPhase2CandleSource.load(symbol)`` returns ONLY CLOSED 5m candles with the
same shape as the frozen CSV source (``app.models.Candle``, ascending by open).

Causal closure contract (blocker requirement 3):

* the evaluation ASOF is explicit and verified — either supplied by the operator
  (``evaluation_asof_ms``) or obtained from the EXCHANGE clock
  (``/v5/market/time``); the local system clock is never evidence of closure and
  is never consulted by this module;
* a bar is usable only when ``open + BAR_MS <= min(evaluation_asof_ms,
  exchange_server_time_ms)`` — the boundary bar becomes available only after
  the exchange confirms its close;
* a forming (unclosed) bar returned by the API is EXCLUDED and counted, never
  consumed.

Pagination contract (blocker requirement 2/4):

* requests are strictly bounded: aligned 5m windows, at most ``page_limit``
  bars per request, at most ``max_pages`` page requests per load, explicit
  request-bound validation of every returned row (``CANDLE_OUT_OF_WINDOW``);
* missing bars trigger BOUNDED targeted re-requests (gap recovery) with bounded
  retry/backoff; unrecovered gaps, duplicates (``DUPLICATE_CANDLE``), OHLC
  conflicts (``CONFLICTING_CANDLE``), rate limits and API errors all fail closed
  (:class:`BybitCandleSourceError`) so the runner can block the symbol and emit
  nothing;
* at least ``min_history_bars`` closed bars are returned so the causal 1H
  resistance universe (200 closed hours) and the 200-bar ATR window are always
  computable.

Pure research surface: no production state, no OOS activation, no migrations.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable

from app.models import Candle
from app.research.htf_baseline_short_phase2_contracts_v1 import BAR_MS

SOURCE_NAME = "BybitPhase2CandleSource"
PROTOCOL_TAG = "HTF_BASELINE_SHORT_PHASE2_LIVE_BYBIT_SOURCE_V1"

KLINE_ENDPOINT = "/v5/market/kline"
SERVER_TIME_ENDPOINT = "/v5/market/time"

DEFAULT_PAGE_LIMIT = 200
DEFAULT_MAX_PAGES = 20
#: 200 closed 1H bars (level universe) + 200-bar ATR window + default 400-bar
#: warmup: enough history for a canonical causal computation on every bar.
DEFAULT_MIN_HISTORY_BARS = 3_000
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_GAP_RECOVERY_ATTEMPTS = 2
DEFAULT_MAX_GAP_WINDOWS = 8
DEFAULT_RETRY_BACKOFF_S = 0.5
#: Maximum NEW closed bars delivered per catch-up batch when the durable
#: cursor falls outside the steady window (bounded historical backfill).
DEFAULT_MAX_BACKFILL_BARS = 800

# --------------------------------------------------------------- reason codes
REASON_API_ERROR = "BYBIT_API_ERROR"
REASON_RATE_LIMITED = "RATE_LIMITED"
REASON_INVALID_API_RESPONSE = "INVALID_API_RESPONSE"
REASON_INVALID_ROW = "INVALID_CANDLE_ROW"
REASON_INVALID_VALUE = "INVALID_CANDLE_VALUE"
REASON_NOT_ALIGNED = "CANDLE_NOT_ALIGNED"
REASON_INVALID_OHLC = "INVALID_OHLC"
REASON_OUT_OF_WINDOW = "CANDLE_OUT_OF_WINDOW"
REASON_DUPLICATE = "DUPLICATE_CANDLE"
REASON_CONFLICTING = "CONFLICTING_CANDLE"
REASON_INCOMPLETE_COVERAGE = "INCOMPLETE_CANDLE_COVERAGE"
REASON_INCOMPLETE_HISTORY = "INCOMPLETE_HISTORY"
REASON_MAX_PAGES = "MAX_PAGES_EXCEEDED"
REASON_MISSING_SERVER_TIME = "MISSING_SERVER_TIME"
REASON_REQUEST_WINDOW_INVALID = "REQUEST_WINDOW_INVALID"
REASON_EVALUATION_ASOF_INVALID = "EVALUATION_ASOF_INVALID"


class CandleSourceError(RuntimeError):
    """Fail-closed candle-source failure: the symbol must not be processed.

    The runner turns this into SYMBOL BLOCKING (no bars processed, no cursor
    advance, no events). It is never a partial or best-effort result.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class BybitCandleSourceError(CandleSourceError):
    """Bybit-backed source failure (identical fail-closed contract)."""


_RATE_LIMIT_MARKERS = ("too many visits", "exceeded the api rate limit", "rate limit")


def _is_rate_limit_text(text: str) -> bool:
    lowered = str(text).lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _extract_server_time_ms(payload: Any) -> int:
    """Extract the EXCHANGE server time (ms) from a Bybit v5 payload.

    This is the verified closure evidence; a payload without it is rejected
    (``MISSING_SERVER_TIME``) instead of falling back to the local clock.
    """
    if not isinstance(payload, dict):
        raise BybitCandleSourceError(REASON_INVALID_API_RESPONSE, "payload is not a mapping")
    raw = payload.get("time")
    if raw is None:
        result = payload.get("result")
        if isinstance(result, dict):
            raw = result.get("timeSecond")
            if raw is not None:
                try:
                    return int(raw) * 1000
                except (TypeError, ValueError, OverflowError):
                    raw = None
            if raw is None:
                raw = result.get("time")
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise BybitCandleSourceError(REASON_MISSING_SERVER_TIME, "no exchange time in payload") from None
    if value <= 0:
        raise BybitCandleSourceError(REASON_MISSING_SERVER_TIME, f"implausible exchange time {value}")
    # ``result.time`` is seconds on some deployments and ms on others
    if raw == payload.get("time") and value < 1_000_000_000_000:
        value *= 1000
    return value


class BybitPhase2CandleSource:
    """Bounded, fail-closed, causal closed-5m Bybit history source.

    ``client`` is any object exposing the read-only ``_public_get(endpoint,
    **params)`` transport used by :class:`app.exchange.bybit_client.BybitClient`
    (the same reuse pattern as
    ``app/shadow/me_r_close_location_historical_source.py``). Only public
    ``/v5/market/*`` endpoints are ever called.
    """

    def __init__(
        self,
        client: Any,
        *,
        evaluation_asof_ms: int | None = None,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_pages: int = DEFAULT_MAX_PAGES,
        min_history_bars: int = DEFAULT_MIN_HISTORY_BARS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        gap_recovery_attempts: int = DEFAULT_GAP_RECOVERY_ATTEMPTS,
        max_gap_windows: int = DEFAULT_MAX_GAP_WINDOWS,
        max_backfill_bars: int = DEFAULT_MAX_BACKFILL_BARS,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
        category: str = "linear",
        interval: str = "5",
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if client is None:
            raise ValueError("client is required")
        for name, value in (
            ("page_limit", page_limit),
            ("max_pages", max_pages),
            ("min_history_bars", min_history_bars),
            ("max_attempts", max_attempts),
            ("gap_recovery_attempts", gap_recovery_attempts),
            ("max_gap_windows", max_gap_windows),
            ("max_backfill_bars", max_backfill_bars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
        if retry_backoff_s < 0:
            raise ValueError("retry_backoff_s must be >= 0")
        if (min_history_bars + page_limit - 1) // page_limit > max_pages:
            raise ValueError(
                "max_pages cannot cover min_history_bars: pagination must stay bounded"
            )
        if evaluation_asof_ms is not None and (
            isinstance(evaluation_asof_ms, bool)
            or not isinstance(evaluation_asof_ms, int)
            or evaluation_asof_ms <= 0
        ):
            raise ValueError(f"evaluation_asof_ms must be a positive int, got {evaluation_asof_ms!r}")

        self.client = client
        self._evaluation_asof_ms = evaluation_asof_ms
        self.page_limit = int(page_limit)
        self.max_pages = int(max_pages)
        self.min_history_bars = int(min_history_bars)
        self.max_attempts = int(max_attempts)
        self.gap_recovery_attempts = int(gap_recovery_attempts)
        self.max_gap_windows = int(max_gap_windows)
        self.max_backfill_bars = int(max_backfill_bars)
        self.retry_backoff_s = float(retry_backoff_s)
        self.category = str(category)
        self.interval = str(interval)
        self._sleep = sleep if sleep is not None else time.sleep

        # audit surface (per load, bounded)
        self.last_evaluation_asof_ms: int | None = None
        self.last_server_time_ms: int | None = None
        self.last_load_metrics: dict[str, Any] = {}
        self.request_log: list[dict] = []

    # ------------------------------------------------------------------ load
    def _plan_window(
        self, end_open: int, resume_open_ms: int | None
    ) -> tuple[int, int, bool, int]:
        """Plan the bounded delivery window around the durable timestamp cursor.

        Steady state: the last ``min_history_bars`` closed bars. When the durable
        resume point falls OUTSIDE that window (observer downtime longer than
        the window) a bounded catch-up batch is planned instead: the window
        starts ``min_history_bars`` before the resume point (the full causal
        lookback for 1H resistance and the ATR window) and ends at most
        ``batch_bars`` past it, so the observer advances sequentially from the
        old cursor toward the current ASOF over several bounded cycles. The
        cursor is never silently moved and nothing is ever skipped.
        """
        steady_start = end_open - (self.min_history_bars - 1) * BAR_MS
        if resume_open_ms is None or resume_open_ms >= steady_start:
            return steady_start, end_open, False, self.min_history_bars
        # bounded catch-up: window = causal lookback + a capped batch of new bars
        capacity = self.max_pages * self.page_limit - self.min_history_bars
        batch_bars = min(self.max_backfill_bars, capacity)
        if batch_bars < 1:
            raise BybitCandleSourceError(
                REASON_REQUEST_WINDOW_INVALID,
                "no backfill capacity within the page budget "
                f"(max_pages={self.max_pages} page_limit={self.page_limit} "
                f"min_history_bars={self.min_history_bars})",
            )
        start_open = resume_open_ms - (self.min_history_bars - 1) * BAR_MS
        window_end = min(end_open, resume_open_ms + (batch_bars - 1) * BAR_MS)
        return start_open, window_end, True, batch_bars

    def load(self, symbol: str, *, resume_open_ms: int | None = None) -> list[Candle]:
        """Return the verified closed 5m history for ``symbol`` (ascending).

        ``resume_open_ms`` is the durable timestamp-cursor resume point (the
        open time of the first not-yet-processed bar). When it lies outside the
        steady window, a bounded historical backfill batch covering it is
        returned instead of the latest window, so a long observer downtime can
        be recovered without skipping history or moving the cursor.
        """
        symbol = str(symbol or "").strip()
        if not symbol:
            raise BybitCandleSourceError(REASON_REQUEST_WINDOW_INVALID, "empty symbol")
        if resume_open_ms is not None and (
            isinstance(resume_open_ms, bool)
            or not isinstance(resume_open_ms, int)
            or resume_open_ms <= 0
            or resume_open_ms % BAR_MS
        ):
            raise BybitCandleSourceError(
                REASON_REQUEST_WINDOW_INVALID,
                f"resume_open_ms must be a positive aligned timestamp, got {resume_open_ms!r}",
            )
        self.request_log = []
        metrics: dict[str, Any] = {
            "symbol": symbol,
            "pages_fetched": 0,
            "page_requests": 0,
            "gap_recovery_requests": 0,
            "retries": 0,
            "unclosed_candles_excluded": 0,
            "bars_returned": 0,
            "backfill": False,
            "resume_open_ms": resume_open_ms,
        }
        self.last_load_metrics = metrics

        server_time_ms = self._fetch_server_time_ms()
        asof_ms = self._resolve_asof(server_time_ms)
        end_open = (asof_ms - BAR_MS) // BAR_MS * BAR_MS
        start_open, window_end, backfill, batch_bars = self._plan_window(
            end_open, resume_open_ms
        )
        metrics.update(
            {
                "backfill": backfill,
                "batch_bars": batch_bars,
                "window_start_open_ms": start_open,
                "window_end_open_ms": window_end,
            }
        )

        by_open: dict[int, Candle] = {}
        unclosed_opens: set[int] = set()
        cursor = start_open
        pages = 0
        while cursor <= window_end:
            pages += 1
            if pages > self.max_pages:
                raise BybitCandleSourceError(
                    REASON_MAX_PAGES, f"page budget {self.max_pages} exhausted"
                )
            page_end = min(window_end, cursor + (self.page_limit - 1) * BAR_MS)
            rows, page_time_ms = self._fetch_page(
                symbol, cursor, page_end, window_end=window_end, attempts=self.max_attempts
            )
            metrics["pages_fetched"] += 1
            server_time_ms = min(server_time_ms, page_time_ms)
            strict_asof = min(asof_ms, server_time_ms)
            metrics["unclosed_candles_excluded"] += self._merge_rows(
                symbol, rows, cursor, page_end, strict_asof, by_open, unclosed_opens
            )
            cursor = page_end + BAR_MS

        # final closure sweep under the strictest evidence observed
        strict_asof = min(asof_ms, server_time_ms)
        for open_ms in [o for o in by_open if o + BAR_MS > strict_asof]:
            del by_open[open_ms]
            unclosed_opens.add(open_ms)
            metrics["unclosed_candles_excluded"] += 1

        if not by_open:
            raise BybitCandleSourceError(REASON_INCOMPLETE_COVERAGE, "no closed candles returned")
        # every bar of the requested window must be accounted for: present or
        # explicitly excluded as unclosed. Anything else is an incomplete page
        # or a dropped bar -> bounded recovery -> fail closed.
        expected = [
            open_ms
            for open_ms in range(start_open, window_end + BAR_MS, BAR_MS)
            if open_ms not in unclosed_opens
        ]
        missing = [open_ms for open_ms in expected if open_ms not in by_open]
        if missing:
            metrics["gap_recovery_requests"] = self._recover_gaps(
                symbol, missing, by_open, unclosed_opens, strict_asof, metrics
            )
            missing = [open_ms for open_ms in expected if open_ms not in by_open]
            if missing:
                raise BybitCandleSourceError(
                    REASON_INCOMPLETE_COVERAGE,
                    f"{len(missing)} bar(s) missing after bounded recovery "
                    f"(first={missing[0]}, last={missing[-1]})",
                )

        candles = [by_open[open_ms] for open_ms in sorted(by_open)]
        if len(candles) < self.min_history_bars:
            raise BybitCandleSourceError(
                REASON_INCOMPLETE_HISTORY,
                f"only {len(candles)} closed bars, need {self.min_history_bars}",
            )

        metrics["bars_returned"] = len(candles)
        metrics["evaluation_asof_ms"] = asof_ms
        metrics["server_time_ms"] = server_time_ms
        self.last_evaluation_asof_ms = asof_ms
        self.last_server_time_ms = server_time_ms
        self.last_load_metrics = metrics
        return candles

    # ------------------------------------------------------------- internals
    def _resolve_asof(self, server_time_ms: int) -> int:
        """Verified evaluation ASOF: operator cutoff AND exchange time agree."""
        if self._evaluation_asof_ms is None:
            return server_time_ms
        if self._evaluation_asof_ms > server_time_ms:
            # never evaluate ahead of what the exchange has confirmed
            return server_time_ms
        return self._evaluation_asof_ms

    def _fetch_server_time_ms(self) -> int:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._log_request(SERVER_TIME_ENDPOINT, attempt=attempt)
            try:
                payload = self.client._public_get(SERVER_TIME_ENDPOINT)
            except Exception as exc:  # transport/rate-limit/timeout: bounded retry
                last_error = exc
                self.last_load_metrics["retries"] = self.last_load_metrics.get("retries", 0) + 1
                if attempt < self.max_attempts:
                    self._backoff(attempt)
                continue
            if isinstance(payload, dict) and payload.get("retCode", 0) != 0:
                last_error = BybitCandleSourceError(
                    REASON_API_ERROR, str(payload.get("retMsg", ""))[:120]
                )
                if attempt < self.max_attempts:
                    self._backoff(attempt)
                continue
            try:
                return _extract_server_time_ms(payload)
            except BybitCandleSourceError as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    self._backoff(attempt)
                continue
        if isinstance(last_error, BybitCandleSourceError):
            raise last_error
        text = f"{type(last_error).__name__}: {last_error}" if last_error else "no attempt"
        reason = REASON_RATE_LIMITED if _is_rate_limit_text(text) else REASON_API_ERROR
        raise BybitCandleSourceError(reason, f"server time unavailable ({text[:160]})") from last_error

    def _fetch_page(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        window_end: int,
        attempts: int,
    ) -> tuple[list, int]:
        self._check_request_bounds(start_ms, end_ms, window_end)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._log_request(
                KLINE_ENDPOINT,
                attempt=attempt,
                symbol=symbol,
                start=start_ms,
                end=end_ms,
                limit=self.page_limit,
            )
            try:
                payload = self.client._public_get(
                    KLINE_ENDPOINT,
                    category=self.category,
                    symbol=symbol,
                    interval=self.interval,
                    start=start_ms,
                    end=end_ms,
                    limit=self.page_limit,
                )
            except Exception as exc:
                last_error = exc
                self.last_load_metrics["retries"] = self.last_load_metrics.get("retries", 0) + 1
                if attempt < attempts:
                    self._backoff(attempt)
                continue
            if not isinstance(payload, dict):
                raise BybitCandleSourceError(REASON_INVALID_API_RESPONSE, "payload is not a mapping")
            if payload.get("retCode", 0) != 0:
                last_error = BybitCandleSourceError(
                    REASON_API_ERROR, str(payload.get("retMsg", ""))[:120]
                )
                if attempt < attempts:
                    self._backoff(attempt)
                continue
            result = payload.get("result")
            rows = result.get("list") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise BybitCandleSourceError(REASON_INVALID_API_RESPONSE, "missing result.list")
            return rows, _extract_server_time_ms(payload)
        text = f"{type(last_error).__name__}: {last_error}" if last_error else "no attempt"
        reason = REASON_RATE_LIMITED if _is_rate_limit_text(text) else REASON_API_ERROR
        raise BybitCandleSourceError(
            reason, f"page {start_ms}..{end_ms} failed after {attempts} attempt(s) ({text[:160]})"
        ) from last_error

    def _check_request_bounds(self, start_ms: int, end_ms: int, window_end: int) -> None:
        if start_ms % BAR_MS or end_ms % BAR_MS:
            raise BybitCandleSourceError(
                REASON_REQUEST_WINDOW_INVALID, f"unaligned window {start_ms}..{end_ms}"
            )
        if start_ms > end_ms:
            raise BybitCandleSourceError(
                REASON_REQUEST_WINDOW_INVALID, f"inverted window {start_ms}..{end_ms}"
            )
        if end_ms > window_end:
            raise BybitCandleSourceError(
                REASON_REQUEST_WINDOW_INVALID,
                f"window end {end_ms} beyond the closed window {window_end}",
            )
        bars = (end_ms - start_ms) // BAR_MS + 1
        if bars > self.page_limit:
            raise BybitCandleSourceError(
                REASON_REQUEST_WINDOW_INVALID, f"window asks {bars} bars > page_limit {self.page_limit}"
            )

    def _merge_rows(
        self,
        symbol: str,
        rows: list,
        page_start: int,
        page_end: int,
        strict_asof_ms: int,
        by_open: dict[int, Candle],
        unclosed_opens: set[int],
    ) -> int:
        """Validate and merge one page; returns the number of unclosed bars excluded."""
        excluded = 0
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise BybitCandleSourceError(REASON_INVALID_ROW, f"malformed row in {symbol}")
            try:
                open_ms = int(row[0])
                open_, high, low, close, volume = (float(v) for v in row[1:6])
            except (TypeError, ValueError, OverflowError):
                raise BybitCandleSourceError(REASON_INVALID_VALUE, f"unparsable row in {symbol}") from None

            if open_ms % BAR_MS:
                raise BybitCandleSourceError(REASON_NOT_ALIGNED, f"open {open_ms} in {symbol}")
            if (
                not all(math.isfinite(v) and v > 0 for v in (open_, high, low, close))
                or high < max(open_, close)
                or low > min(open_, close)
                or low > high
                or not math.isfinite(volume)
                or volume < 0
            ):
                raise BybitCandleSourceError(REASON_INVALID_OHLC, f"invalid OHLC at {open_ms} in {symbol}")

            if open_ms + BAR_MS > strict_asof_ms:
                # forming / not-yet-confirmed bar: NEVER consumed (requirement 3)
                excluded += 1
                unclosed_opens.add(open_ms)
                continue

            if open_ms < page_start or open_ms > page_end:
                raise BybitCandleSourceError(
                    REASON_OUT_OF_WINDOW,
                    f"bar {open_ms} outside requested window {page_start}..{page_end}",
                )

            candle = Candle(open_ms, open_, high, low, close, volume)
            previous = by_open.get(open_ms)
            if previous is not None:
                identical = (
                    float(previous.open) == open_
                    and float(previous.high) == high
                    and float(previous.low) == low
                    and float(previous.close) == close
                    and float(previous.volume) == volume
                )
                raise BybitCandleSourceError(
                    REASON_DUPLICATE if identical else REASON_CONFLICTING,
                    f"repeated open {open_ms} in {symbol}",
                )
            by_open[open_ms] = candle
        return excluded

    def _recover_gaps(
        self,
        symbol: str,
        missing: list[int],
        by_open: dict[int, Candle],
        unclosed_opens: set[int],
        strict_asof_ms: int,
        metrics: dict[str, Any],
    ) -> int:
        """Bounded targeted re-requests for missing bars (requirement 4)."""
        runs: list[list[int]] = []
        for open_ms in missing:
            if runs and open_ms == runs[-1][-1] + BAR_MS:
                runs[-1].append(open_ms)
            else:
                runs.append([open_ms])
        if len(runs) > self.max_gap_windows:
            raise BybitCandleSourceError(
                REASON_INCOMPLETE_COVERAGE,
                f"{len(runs)} gap windows exceed the bounded recovery budget {self.max_gap_windows}",
            )
        budget = self.max_gap_windows * self.gap_recovery_attempts
        requests = 0
        for run in runs:
            cursor = run[0]
            while cursor <= run[-1]:
                page_end = min(run[-1], cursor + (self.page_limit - 1) * BAR_MS)
                recovered = False
                for attempt in range(1, self.gap_recovery_attempts + 1):
                    if requests >= budget:
                        raise BybitCandleSourceError(
                            REASON_INCOMPLETE_COVERAGE,
                            f"gap recovery request budget {budget} exhausted",
                        )
                    if attempt > 1:
                        self._backoff(attempt)
                    rows, page_time_ms = self._fetch_page(
                        symbol, cursor, page_end, window_end=page_end, attempts=1
                    )
                    requests += 1
                    strict = min(strict_asof_ms, page_time_ms)
                    self._merge_rows(symbol, rows, cursor, page_end, strict, by_open, unclosed_opens)
                    if all(open_ms in by_open for open_ms in range(cursor, page_end + BAR_MS, BAR_MS)):
                        recovered = True
                        break
                if not recovered:
                    break  # outer coverage check reports the exact failure
                cursor = page_end + BAR_MS
        return requests

    def _backoff(self, attempt: int) -> None:
        if self.retry_backoff_s <= 0:
            return
        self._sleep(self.retry_backoff_s * (2 ** (attempt - 1)))

    def _log_request(self, endpoint: str, *, attempt: int, **params: Any) -> None:
        self.request_log.append({"endpoint": endpoint, "attempt": attempt, **params})
