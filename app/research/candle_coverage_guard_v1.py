"""Generic candle-coverage guard for generic OOS evaluators (FIX-1).

GENERIC_OOS_CANDLE_COVERAGE_FINALIZATION_GUARD_V1
Related: GLOBAL_BYBIT_RATE_LIMITER_V1 (Issue #231), risk R1 + R2.

R1 in one sentence: a *non-empty but truncated* Bybit kline response used to be
treated as a complete evaluation path, so a generic OOS outcome could receive
``is_final=True`` (and ``evaluated_*_at`` marks) computed from a partial candle
path — an irreversible distortion of OOS statistics.

This module is the single place that answers two questions for every generic
evaluation path:

1. *Was the data obtainable at all?*  ``fetch_candles_for_evaluation``
   distinguishes an API/transport/limiter failure (``SOURCE_FETCH_ERROR``)
   from a successful-but-insufficient response
   (``SOURCE_INCOMPLETE_COVERAGE``) and from data that has not closed yet
   (``SOURCE_NOT_YET_AVAILABLE``).  A non-empty response is never treated as
   proof of completeness.

2. *Is the candle path for this exact evaluation window complete?*
   ``validate_candle_coverage`` verifies the required 5-minute path for one
   ``[signal_time, cutoff)`` window: the required opens are the **absolute
   UTC 5-minute grid** points inside the window (never the provider's own
   first timestamp), the first required candle is present, no gaps, no
   duplicate timestamps, no off-grid opens (a response shifted to
   10:01/10:06/10:11 is rejected), the **used path is strictly ascending**
   (every open after the signal, in array order — the evaluator formulas
   read arrival order, so a shuffled array is rejected, never sorted
   silently), all required candles closed at evaluation time, and every OHLC
   value finite.

Status naming is reused from the existing frozen SRR route
(``app.research.srr_short_execution_r_expansion_prospective_evaluator``) so
that generic and SRR diagnostics stay comparable.  This module never changes
trading geometry: it only decides whether already-existing evaluation formulas
may run on the data at hand.

Finalization rule enforced by the callers (see FIX-1 §5)::

    finalization_eligible = source_validated
                            AND required_path_covered
                            AND horizon_matured
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.models import Candle

logger = logging.getLogger(__name__)

# 5-minute candles: the only interval the generic evaluators fetch.
INTERVAL = "5"
CANDLE_MS = 300_000
# Bybit V5 hard cap per /v5/market/kline request.
PAGE_LIMIT = 1000

# ── source statuses (FIX-1 §4.3) ────────────────────────────────
SOURCE_VALIDATED = "SOURCE_VALIDATED"
SOURCE_INCOMPLETE_COVERAGE = "SOURCE_INCOMPLETE_COVERAGE"
SOURCE_FETCH_ERROR = "SOURCE_FETCH_ERROR"
SOURCE_NOT_YET_AVAILABLE = "SOURCE_NOT_YET_AVAILABLE"

SOURCE_STATUSES = frozenset({
    SOURCE_VALIDATED,
    SOURCE_INCOMPLETE_COVERAGE,
    SOURCE_FETCH_ERROR,
    SOURCE_NOT_YET_AVAILABLE,
})

# Worst-first ordering used to collapse several blocked windows into the
# single source status persisted for an observation.
_STATUS_SEVERITY = {
    SOURCE_FETCH_ERROR: 3,
    SOURCE_INCOMPLETE_COVERAGE: 2,
    SOURCE_NOT_YET_AVAILABLE: 1,
    SOURCE_VALIDATED: 0,
}


def merge_source_status(statuses: Sequence[str]) -> str | None:
    """Collapse per-window statuses into one observation-level status.

    Returns ``None`` when every checked window is validated (nothing to
    report), otherwise the most severe status seen.
    """
    worst = SOURCE_VALIDATED
    seen = False
    for status in statuses:
        if status == SOURCE_VALIDATED:
            continue
        seen = True
        if _STATUS_SEVERITY.get(status, 0) > _STATUS_SEVERITY.get(worst, 0):
            worst = status
    return worst if seen else None


# ── results ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CandleCoverageResult:
    """Outcome of validating one evaluation window's candle path."""

    status: str
    reason_code: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == SOURCE_VALIDATED


@dataclass(frozen=True)
class CandleFetchResult:
    """Outcome of fetching candles for one symbol/interval request."""

    candles: tuple[Candle, ...]
    status: str
    reason_code: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == SOURCE_VALIDATED


# ── small helpers ──────────────────────────────────────────────


def datetime_to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def checked_at_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def latest_closed_grid_open_ms(asof_ms: int, interval_ms: int = CANDLE_MS) -> int:
    """Open time of the newest *closed* grid-aligned candle at ``asof_ms``.

    A candle ``[open, open + interval)`` is closed iff ``open + interval
    <= asof_ms``.  For grid-aligned candles that is exactly
    ``floor(asof / interval) * interval - interval``.
    """
    return (asof_ms // interval_ms) * interval_ms - interval_ms


def candle_is_closed(open_ms: int, asof_ms: int, interval_ms: int = CANDLE_MS) -> bool:
    return open_ms + interval_ms <= asof_ms


def _finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number)


def _candle_ts(candle: Any) -> int | None:
    try:
        return int(candle.timestamp)
    except (AttributeError, TypeError, ValueError):
        return None


# ── coverage validation (FIX-1 §4.1) ───────────────────────────


def validate_candle_coverage(
    candles: Sequence[Candle] | None,
    *,
    signal_ms: int,
    cutoff_ms: int,
    asof_ms: int,
    interval_ms: int = CANDLE_MS,
    demand_closed: bool = True,
    scope: str = "HORIZON",
) -> CandleCoverageResult:
    """Validate the required candle path for one evaluation window.

    The window is the half-open interval ``(signal_ms, cutoff_ms)`` — exactly
    the range the existing evaluator formulas scan (they skip candles with
    ``timestamp <= signal_time`` and stop at ``timestamp >= cutoff``).

    The required path is anchored on the **absolute UTC grid** derived from
    ``signal_ms`` —
    ``first_required_open = (signal_ms // interval_ms + 1) * interval_ms`` —
    never on the first timestamp of the provider response, so a consistently
    shifted response (10:01/10:06/10:11) cannot define its own anchor
    (review P2-1):

    * an observed open off the absolute grid is rejected outright
      (``CANDLE_TIMESTAMP_OFF_GRID``);
    * a first observed open later than the first grid open means the FIRST
      required candle is missing (T2);
    * any missing grid open is a gap (T3);
    * the grid open just below ``cutoff_ms`` must exist (T4);
    * duplicates, off-anchor candles and invalid OHLC reject the window;
    * the **used path** — every candle after ``signal_ms`` in array order —
      must be strictly ascending.  The evaluator formulas consume candles in
      arrival order (``return_at_horizon`` and the TP/SL first-hit sequence
      both stop at the first candle ``>= cutoff``), so a shuffled array is
      rejected with ``CANDLE_ORDER_INVALID`` and is never sorted silently
      (review P2-3); candles before the signal are filtered out by the
      formulas and are therefore not part of the used path;
    * every price must be a finite number: NaN / +Infinity / -Infinity (or a
      missing field) in ``open``/``close`` — and in ``high``/``low`` — is
      ``INVALID_OHLC`` (review P2-2); such values must never reach
      MFE/MAE/return/TP-SL math;
    * a required candle that has not closed yet yields
      ``SOURCE_NOT_YET_AVAILABLE`` instead of a false negative (T12).

    ``demand_closed=False`` is used for "scan whatever is closed" style checks
    (V2D target flags): only candles that have already closed are demanded.
    """
    interval_ms = int(interval_ms)
    base: dict[str, Any] = {
        "scope": scope,
        "signal_ms": int(signal_ms),
        "cutoff_ms": int(cutoff_ms),
        "asof_ms": int(asof_ms),
        "interval_ms": interval_ms,
        "demand_closed": bool(demand_closed),
        "checked_at": checked_at_iso(),
    }

    # P2-1: the required opens are absolute grid points of the evaluation
    # window, computed from the signal — never from the provider response.
    first_required_open = (int(signal_ms) // interval_ms + 1) * interval_ms
    base["first_required_open_ms"] = first_required_open

    if cutoff_ms <= signal_ms:
        return CandleCoverageResult(
            SOURCE_VALIDATED, "WINDOW_EMPTY", base,
        )
    if first_required_open >= cutoff_ms:
        # No grid open falls strictly inside (signal, cutoff): there is no
        # provider candle the formulas could read in this window.
        return CandleCoverageResult(
            SOURCE_VALIDATED, "WINDOW_WITHOUT_REQUIRED_CANDLE", base,
        )

    in_window: list[Any] = []
    unparsable = 0
    for candle in candles or ():
        ts = _candle_ts(candle)
        if ts is None:
            unparsable += 1
            continue
        if signal_ms < ts < cutoff_ms:
            in_window.append(candle)

    if unparsable:
        return CandleCoverageResult(
            SOURCE_INCOMPLETE_COVERAGE,
            "CANDLE_TIMESTAMP_INVALID",
            {**base, "unparsable_candles": unparsable},
        )

    if not in_window:
        return CandleCoverageResult(
            SOURCE_INCOMPLETE_COVERAGE,
            "NO_CANDLES_IN_WINDOW",
            {**base, "present_count": 0, "available_range": None},
        )

    timestamps = [_candle_ts(c) for c in in_window]
    counts = Counter(t for t in timestamps if t is not None)
    duplicates = sorted(t for t, n in counts.items() if n > 1)
    if duplicates:
        return CandleCoverageResult(
            SOURCE_INCOMPLETE_COVERAGE,
            "DUPLICATE_TIMESTAMPS",
            {
                **base,
                "duplicate_open_time_ms": duplicates[:10],
                "duplicate_count": len(duplicates),
            },
        )

    # P2-3: the evaluator formulas consume candles in ARRIVAL order —
    # return_at_horizon and the TP/SL first-hit sequence both stop at the
    # first candle >= cutoff — so the USED path (every candle after the
    # signal, exactly what those loops iterate) must be strictly ascending.
    # Never sorted silently: an unordered source fails closed instead of
    # silently producing a wrong return tail or a wrong TP/SL verdict.
    prev_ts: int | None = None
    order_violations: list[int] = []
    for candle in candles or ():
        ts = _candle_ts(candle)
        if ts is None or ts <= signal_ms:
            # Pre-signal candles are filtered out by every formula and are
            # not part of the used path.
            continue
        if prev_ts is not None and ts <= prev_ts:
            order_violations.append(ts)
        prev_ts = ts
    if order_violations:
        return CandleCoverageResult(
            SOURCE_INCOMPLETE_COVERAGE,
            "CANDLE_ORDER_INVALID",
            {
                **base,
                "order_violation_open_time_ms": order_violations[:10],
                "order_violation_count": len(order_violations),
            },
        )

    present = set(counts)

    # P2-1: every observed open must sit on the absolute UTC grid.  A
    # response whose timestamps are all shifted by a non-multiple of the
    # interval (10:01/10:06/10:11) is rejected instead of validating
    # against its own shifted anchor.
    off_grid = sorted(t for t in present if t % interval_ms != 0)
    if off_grid:
        return CandleCoverageResult(
            SOURCE_INCOMPLETE_COVERAGE,
            "CANDLE_TIMESTAMP_OFF_GRID",
            {
                **base,
                "off_grid_open_time_ms": off_grid[:10],
                "off_grid_count": len(off_grid),
                "present_count": len(present),
                "available_range": [min(present), max(present)],
            },
        )

    first_open = min(present)
    if first_open > first_required_open:
        # The earliest required grid candle (anchor) is absent.
        return CandleCoverageResult(
            SOURCE_INCOMPLETE_COVERAGE,
            "FIRST_REQUIRED_CANDLE_MISSING",
            {
                **base,
                "first_present_open_ms": first_open,
                "present_count": len(present),
                "available_range": [first_open, max(present)],
            },
        )

    full_expected = list(range(first_required_open, cutoff_ms, interval_ms))
    expected = full_expected if demand_closed else [
        t for t in full_expected if candle_is_closed(t, asof_ms, interval_ms)
    ]

    missing_closed: list[int] = []
    not_closed: list[int] = []
    for open_ms in expected:
        if open_ms not in present:
            if candle_is_closed(open_ms, asof_ms, interval_ms):
                missing_closed.append(open_ms)
            else:
                not_closed.append(open_ms)
        elif not candle_is_closed(open_ms, asof_ms, interval_ms):
            not_closed.append(open_ms)

    if missing_closed:
        return CandleCoverageResult(
            SOURCE_INCOMPLETE_COVERAGE,
            "CANDLE_GAP",
            {
                **base,
                "missing_count": len(missing_closed),
                "missing_first_open_ms": missing_closed[0],
                "missing_last_open_ms": missing_closed[-1],
                "missing_open_time_ms": missing_closed[:10],
                "present_count": len(present),
                "available_range": [min(present), max(present)],
                "required_range": [full_expected[0], full_expected[-1]],
            },
        )

    if not_closed:
        return CandleCoverageResult(
            SOURCE_NOT_YET_AVAILABLE,
            "REQUIRED_CANDLE_NOT_CLOSED",
            {
                **base,
                "unclosed_count": len(not_closed),
                "unclosed_open_time_ms": not_closed[:10],
                "present_count": len(present),
                "required_range": [full_expected[0], full_expected[-1]],
            },
        )

    expected_set = set(full_expected)
    extras = sorted(t for t in present if t not in expected_set)
    if extras:
        return CandleCoverageResult(
            SOURCE_INCOMPLETE_COVERAGE,
            "CANDLE_SPACING_INVALID",
            {
                **base,
                "off_anchor_open_time_ms": extras[:10],
                "off_anchor_count": len(extras),
                "present_count": len(present),
                "available_range": [min(present), max(present)],
            },
        )

    invalid_ohlc: list[int] = []
    for candle in in_window:
        ts = _candle_ts(candle) or 0
        high = getattr(candle, "high", None)
        low = getattr(candle, "low", None)
        open_ = getattr(candle, "open", None)
        close_ = getattr(candle, "close", None)
        # P2-2: every price of the candle must be a finite number.
        # NaN / +Infinity / -Infinity — and a missing field — are rejected
        # here so they can never reach MFE/MAE/return/TP-SL math.
        if not (
            _finite(high)
            and _finite(low)
            and _finite(open_)
            and _finite(close_)
        ):
            invalid_ohlc.append(ts)
            continue
        high_f, low_f = float(high), float(low)
        open_f, close_f = float(open_), float(close_)
        if low_f > high_f:
            invalid_ohlc.append(ts)
            continue
        if not (low_f <= open_f <= high_f and low_f <= close_f <= high_f):
            invalid_ohlc.append(ts)
            continue

    if invalid_ohlc:
        return CandleCoverageResult(
            SOURCE_INCOMPLETE_COVERAGE,
            "INVALID_OHLC",
            {
                **base,
                "invalid_ohlc_open_time_ms": invalid_ohlc[:10],
                "invalid_ohlc_count": len(invalid_ohlc),
            },
        )

    return CandleCoverageResult(
        SOURCE_VALIDATED,
        "SOURCE_VALIDATED",
        {
            **base,
            "present_count": len(present),
            "required_range": [full_expected[0], full_expected[-1]],
            "available_range": [min(present), max(present)],
        },
    )


def terminal_event_open_ms(
    tp_sl: Mapping[str, Any] | None,
    candles: Sequence[Candle] | None,
    *,
    signal_ms: int,
) -> int | None:
    """Open time of the first TP/SL hit recorded by the frozen checker.

    ``_check_tp_sl`` reports ``time_to_tp`` / ``time_to_sl`` in minutes from
    the signal (rounded to 0.1).  The first terminal event is the smaller of
    the two; its candle open is recovered by matching the same rounded value.
    Returns ``None`` when no hit was recorded or it cannot be located.
    """
    if not tp_sl:
        return None
    if not (tp_sl.get("tp_hit") or tp_sl.get("sl_hit")):
        return None
    times = [
        float(v)
        for v in (tp_sl.get("time_to_tp"), tp_sl.get("time_to_sl"))
        if v is not None
    ]
    if not times:
        return None
    target_minutes = round(min(times), 1)
    for candle in candles or ():
        ts = _candle_ts(candle)
        if ts is None or ts <= signal_ms:
            continue
        if round((ts - signal_ms) / 60_000.0, 1) == target_minutes:
            return ts
    return None


# ── fetch with explicit status (FIX-1 §4.2) ────────────────────


def _classify_fetch_exception(exc: BaseException, *, pagination: bool) -> tuple[str, str]:
    """Separate API/limiter failures from pagination truncation."""
    message = str(exc)
    lowered = message.lower()
    if pagination and "exceeded" in lowered and "page" in lowered:
        # The bounded page budget was exhausted: the history we could read is
        # truncated → incomplete coverage, NOT a validated source.
        return SOURCE_INCOMPLETE_COVERAGE, "SOURCE_PAGINATION_TRUNCATED"
    if type(exc).__name__ == "BybitRateLimitError" or "rate limiter" in lowered:
        # GLOBAL_BYBIT_RATE_LIMITER_V1 deny (telemetry label BYBIT_API_SKIP):
        # fail-closed, no HTTP fallback, retried on a later cycle.
        return SOURCE_FETCH_ERROR, "BYBIT_RATE_LIMITER_DENY"
    if "10006" in lowered:
        return SOURCE_FETCH_ERROR, "BYBIT_RATE_LIMIT_EXHAUSTED"
    return SOURCE_FETCH_ERROR, "SOURCE_FETCH_EXCEPTION"


def fetch_candles_for_evaluation(
    client: Any,
    symbol: str,
    from_time: datetime,
    to_time: datetime,
    *,
    interval: str = INTERVAL,
    page_limit: int = PAGE_LIMIT,
) -> CandleFetchResult:
    """Fetch 5-minute candles for ``[from_time, to_time]`` with a status.

    Guarantees:

    * only candles that are already closed are returned (T12);
    * a window larger than one Bybit page is fetched through the existing
      bounded historical source ``BybitClient.get_klines_range`` (explicit
      start/end pagination, never fabricates gaps, raises instead of returning
      half a window);
    * if pagination is impossible (no historical source on the client) the
      result is ``SOURCE_INCOMPLETE_COVERAGE`` — never a silently truncated
      latest-N response (T6);
    * any exception (transport, retCode, retry-budget exhaustion, shared
      limiter deny) is ``SOURCE_FETCH_ERROR`` with no fallback attempt (T8,
      T9);
    * a successful response that does not contain the requested range is
      ``SOURCE_INCOMPLETE_COVERAGE`` (T5), not a validated source.
    """
    interval_minutes = int(interval)
    interval_ms = interval_minutes * 60_000
    page_limit = max(1, min(int(page_limit), 1000))

    started_ms = datetime_to_ms(from_time) - interval_ms
    asof_ms = datetime_to_ms(to_time)
    aligned_start = (started_ms // interval_ms) * interval_ms
    grid_end = latest_closed_grid_open_ms(asof_ms, interval_ms)

    base_diag: dict[str, Any] = {
        "symbol": symbol,
        "interval": interval,
        "requested_start_ms": aligned_start,
        "requested_end_ms": grid_end,
        "asof_ms": asof_ms,
        "checked_at": checked_at_iso(),
        "provider": "BYBIT_V5_KLINE",
    }

    if grid_end < aligned_start:
        return CandleFetchResult(
            (), SOURCE_NOT_YET_AVAILABLE, "NO_CLOSED_CANDLES_YET", base_diag,
        )

    needed = (grid_end - aligned_start) // interval_ms + 1
    base_diag["required_candle_count"] = needed

    def _closed_only(raw: Any) -> list[Candle]:
        out: list[Candle] = []
        for candle in raw:
            ts = _candle_ts(candle)
            if ts is None:
                continue
            if aligned_start <= ts and candle_is_closed(ts, asof_ms, interval_ms):
                out.append(candle)
        return out

    if needed > page_limit:
        # Window exceeds one Bybit page → explicit historical pagination.
        # Only a *real* class method counts: mock clients without a genuine
        # range source must never be mistaken for one.
        if getattr(type(client), "get_klines_range", None) is None:
            return CandleFetchResult(
                (), SOURCE_INCOMPLETE_COVERAGE, "PAGINATION_UNAVAILABLE",
                {**base_diag, "page_limit": page_limit},
            )
        max_pages = needed // page_limit + 2
        try:
            raw = client.get_klines_range(
                symbol, interval,
                start_ms=aligned_start, end_ms=grid_end,
                page_size=page_limit, max_pages=max_pages,
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            status, reason = _classify_fetch_exception(exc, pagination=True)
            logger.warning(
                "candle guard: historical range fetch failed for %s (%s/%s)",
                symbol, status, reason,
            )
            return CandleFetchResult(
                (), status, reason,
                {**base_diag, "error_type": type(exc).__name__,
                 "error": str(exc)[:200], "max_pages": max_pages},
            )
        if not isinstance(raw, (list, tuple)):
            return CandleFetchResult(
                (), SOURCE_FETCH_ERROR, "SOURCE_FETCH_RESULT_TYPE_INVALID",
                {**base_diag, "actual_type": type(raw).__name__},
            )
        candles = _closed_only(raw)
        if not candles:
            return CandleFetchResult(
                (), SOURCE_INCOMPLETE_COVERAGE, "EMPTY_KLINE_RESPONSE",
                {**base_diag, "raw_count": len(raw)},
            )
        return CandleFetchResult(
            tuple(candles), SOURCE_VALIDATED, "SOURCE_VALIDATED",
            {**base_diag, "provider": "BYBIT_V5_KLINE_RANGE_PAGED",
             "pages": max_pages, "raw_count": len(raw),
             "returned_count": len(candles)},
        )

    limit = min(needed + 10, 1000)
    try:
        raw = client.get_klines(symbol, interval, limit)
    except Exception as exc:  # noqa: BLE001 - classified below
        status, reason = _classify_fetch_exception(exc, pagination=False)
        logger.warning(
            "candle guard: kline fetch failed for %s (%s/%s)",
            symbol, status, reason,
        )
        return CandleFetchResult(
            (), status, reason,
            {**base_diag, "error_type": type(exc).__name__,
             "error": str(exc)[:200], "limit": limit},
        )
    if not isinstance(raw, (list, tuple)):
        return CandleFetchResult(
            (), SOURCE_FETCH_ERROR, "SOURCE_FETCH_RESULT_TYPE_INVALID",
            {**base_diag, "actual_type": type(raw).__name__},
        )
    if not raw:
        return CandleFetchResult(
            (), SOURCE_INCOMPLETE_COVERAGE, "EMPTY_KLINE_RESPONSE", base_diag,
        )
    candles = _closed_only(raw)
    if not candles:
        return CandleFetchResult(
            (), SOURCE_INCOMPLETE_COVERAGE, "NO_CANDLES_IN_RANGE",
            {**base_diag, "raw_count": len(raw)},
        )
    return CandleFetchResult(
        tuple(candles), SOURCE_VALIDATED, "SOURCE_VALIDATED",
        {**base_diag, "raw_count": len(raw), "returned_count": len(candles)},
    )


def fetch_status_stats_key(status: str) -> str:
    """Map a source status onto the evaluator stats counter name."""
    return {
        SOURCE_INCOMPLETE_COVERAGE: "source_incomplete",
        SOURCE_FETCH_ERROR: "source_fetch_error",
        SOURCE_NOT_YET_AVAILABLE: "source_not_yet_available",
        SOURCE_VALIDATED: "source_validated",
    }.get(status, "source_fetch_error")


# ── stats recording (PR-1 diagnostics; no DB schema change) ──


def record_coverage_block(
    stats: dict[str, Any],
    coverage: CandleCoverageResult,
    *,
    horizon: str | None = None,
) -> None:
    """Record one fail-closed coverage block in evaluator stats.

    Structured, in-memory diagnostics only — PR 1 intentionally persists no
    schema markers (that is a separate PR).  Every key is created with
    ``setdefault`` so callers whose stats dict predates this helper (or that
    build a partial dict in tests) keep working.
    """
    blocked = stats.setdefault("coverage_blocked", {})
    blocked[coverage.reason_code] = blocked.get(coverage.reason_code, 0) + 1
    if horizon is not None:
        pending = stats.setdefault("horizons_pending", {})
        pending[horizon] = pending.get(horizon, 0) + 1


def record_finalization_block(
    stats: dict[str, Any], coverage: CandleCoverageResult,
) -> None:
    """Record a blocked ``is_final`` attempt (the horizon stays pending)."""
    stats["finalization_blocked"] = stats.get("finalization_blocked", 0) + 1
    blocked = stats.setdefault("coverage_blocked", {})
    blocked[coverage.reason_code] = blocked.get(coverage.reason_code, 0) + 1
