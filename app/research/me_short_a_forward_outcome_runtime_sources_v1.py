"""Real observe-only data sources for
``ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1``.

These adapters are the only place where the runtime touches the outside
world, and every one of them is **read-only**:

* :class:`DbScannerSetupCandidateSource` reads already-persisted
  ``MOMENTUM_EXHAUSTION`` ``SHORT`` candidates from ``dds.scanner_setup``
  (joined to ``dds.instrument`` for the symbol). It never inserts, updates or
  deletes anything there, and it never calls into
  :mod:`app.scanners`, so scanner logic and scanner direction gates are
  untouched.
* :class:`BybitClosedCandleSource` reads CLOSED 5m klines through the existing
  ``BybitClient`` and drops every candle that has not closed yet
  (``candle_close_time <= observed_at``), so no future information can enter
  the pipeline.

Neither adapter has any order, paper or live surface.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.research.me_short_a_forward_outcome_runtime_v1 import (
    Candle,
    Candidate,
    canonical_price,
    utcnow,
)

__all__ = [
    "CANDLE_MINUTES_MS",
    "SCANNER_NAME",
    "DIRECTION",
    "canonical_price",
    "ms_to_datetime",
    "datetime_to_ms",
    "to_runtime_candle",
    "BybitClosedCandleSource",
    "DbScannerSetupCandidateSource",
]

log = logging.getLogger(__name__)

CANDLE_MINUTES_MS = 5 * 60 * 1000
SCANNER_NAME = "MOMENTUM_EXHAUSTION"
DIRECTION = "SHORT"

WINDOW_SIZE = 5


def ms_to_datetime(value: int | float) -> datetime:
    """Epoch milliseconds -> timezone-aware UTC datetime."""
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)


def datetime_to_ms(value: datetime) -> int:
    """Timezone-aware datetime -> epoch milliseconds."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return int(value.timestamp() * 1000)


def to_runtime_candle(
    raw: Any,
    *,
    symbol: str,
    retrieved_at: datetime,
    source: str = "CLOSED_5M_KLINES",
) -> Candle | None:
    """Convert an ``app.models.Candle``-shaped row to a runtime :class:`Candle`.

    Returns ``None`` when the candle has not closed yet — a forming candle is
    never observable under this protocol.
    """
    open_time = ms_to_datetime(raw.timestamp)
    close_time = open_time + timedelta(minutes=5)
    if close_time > retrieved_at:
        return None
    try:
        return Candle(
            symbol=symbol,
            open_time=open_time,
            close_time=close_time,
            open=Decimal(str(raw.open)),
            high=Decimal(str(raw.high)),
            low=Decimal(str(raw.low)),
            close=Decimal(str(raw.close)),
            retrieved_at=retrieved_at,
            source=source,
        )
    except (ValueError, ArithmeticError):
        log.warning("ME SHORT A runtime rejected malformed candle %s", symbol)
        return None


class BybitClosedCandleSource:
    """CLOSED 5m candles from the exchange, ASOF-verified.

    The forming candle is dropped before it ever reaches the collector, so the
    runtime can only ever observe ``candle_close_time <= observed_at``.
    """

    def __init__(
        self,
        client: Any,
        *,
        interval: str = "5",
        clock: Callable[[], datetime] = utcnow,
        max_limit: int = 1000,
    ) -> None:
        self._client = client
        self._interval = interval
        self._clock = clock
        self._max_limit = int(max_limit)

    def closed_candles(
        self, symbol: str, *, start: datetime, end: datetime,
    ) -> list[Candle]:
        if end <= start:
            return []
        requested = int((end - start) / timedelta(minutes=5)) + 3
        limit = max(1, min(requested, self._max_limit))
        raw_rows = self._client.get_klines(symbol, self._interval, limit)
        observed_at = self._clock()
        out: list[Candle] = []
        for raw in raw_rows:
            candle = to_runtime_candle(
                raw, symbol=symbol, retrieved_at=observed_at)
            if candle is None:
                continue
            if start <= candle.open_time < end:
                out.append(candle)
        out.sort(key=lambda c: c.open_time)
        return out


class DbScannerSetupCandidateSource:
    """READ-ONLY reader of the scanner's persisted ``MOMENTUM_EXHAUSTION``
    ``SHORT`` candidates.

    This is a genuinely independent observer: it consumes what the scanner
    already wrote and never invokes scanner code, so the scanner's gates,
    filters and scoring stay exactly as they are.

    The deterministic identity is built from the SAME fields Phase 1 uses
    (``detected_at`` -> ``signal_time``, ``reference_price``,
    ``invalidation_price``, ``target_1``), which is what makes the
    ``event_key`` shared across ``source signal -> Phase 1 snapshot ->
    outcome``.
    """

    SELECT_SQL = (
        "SELECT s.setup_id, i.symbol, s.detected_at, s.reference_price, "
        "s.invalidation_price, s.target_1, s.entry_zone_low, "
        "s.entry_zone_high, s.signal_candle_open_time, s.features, "
        "s.market_regime, s.status "
        "FROM dds.scanner_setup s "
        "JOIN dds.instrument i ON i.instrument_id = s.instrument_id "
        "WHERE s.scanner_name=%s AND s.direction=%s AND s.detected_at > %s "
        "ORDER BY s.detected_at ASC, s.setup_id ASC "
        "LIMIT %s"
    )

    def __init__(
        self,
        conn: Any,
        candle_source: Any,
        *,
        scanner_name: str = SCANNER_NAME,
        direction: str = DIRECTION,
        clock: Callable[[], datetime] = utcnow,
        max_rows: int = 500,
    ) -> None:
        self._conn = conn
        self._candles = candle_source
        self._scanner_name = scanner_name
        self._direction = direction
        self._clock = clock
        self._max_rows = int(max_rows)

    # ── plumbing ──────────────────────────────────────────────────
    def __call__(self, since: datetime | None = None) -> list[Candidate]:
        return self.fetch(since)

    def fetch(self, since: datetime | None = None) -> list[Candidate]:
        lower = since or datetime(1970, 1, 1, tzinfo=timezone.utc)
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                self.SELECT_SQL,
                (self._scanner_name, self._direction, lower, self._max_rows))
            rows = cursor.fetchall()
        finally:
            cursor.close()
        out: list[Candidate] = []
        for row in rows:
            candidate = self._map_row(row)
            if candidate is not None:
                out.append(candidate)
        return out

    # ── mapping ───────────────────────────────────────────────────
    def _map_row(self, row: Sequence[Any]) -> Candidate | None:
        (
            setup_id, symbol, detected_at, reference_price,
            invalidation_price, target_1, entry_zone_low, entry_zone_high,
            signal_candle_open_time, features, market_regime, _status,
        ) = row

        if symbol is None or detected_at is None:
            return None
        if reference_price is None or invalidation_price is None:
            return None
        if target_1 is None:
            return None

        signal_time = self._as_utc(detected_at)
        if signal_time is None:
            return None

        features_dict = self._as_mapping(features)
        missing: list[str] = []
        flags: list[str] = []

        trigger_5m_close = self._as_decimal(
            features_dict.get("trigger_5m_close"))
        window, signal_candle_close = self._recent_high_window(
            symbol, signal_time, signal_candle_open_time,
            self._as_decimal(reference_price), missing, flags)

        if trigger_5m_close is None and signal_candle_close is not None:
            trigger_5m_close = signal_candle_close
        if trigger_5m_close is None:
            missing.append("trigger_5m_close")

        try:
            return Candidate(
                symbol=str(symbol),
                signal_time=signal_time,
                reference_price=self._as_decimal(reference_price),
                invalidation_price=self._as_decimal(invalidation_price),
                target_1=self._as_decimal(target_1),
                trigger_5m_close=trigger_5m_close,
                entry_zone_low=self._as_decimal(entry_zone_low),
                entry_zone_high=self._as_decimal(entry_zone_high),
                recent_high_window=tuple(window),
                market_context=str(market_regime or ""),
                signal_time_quote=str(features_dict.get("signal_time_quote") or ""),
                source_observation_id=None,
                source="MOMENTUM_EXHAUSTION_SHORT_CANDIDATES",
                observed_at=self._clock(),
                event_id=str(setup_id),
                missing_fields=tuple(missing),
                source_quality_flags=tuple(flags),
            )
        except ValueError as exc:
            log.warning(
                "ME SHORT A runtime rejected candidate %s/%s: %s",
                symbol, setup_id, exc)
            return None

    def _recent_high_window(
        self, symbol: str, signal_time: datetime,
        signal_candle_open_time: Any, reference_price: Decimal | None,
        missing: list[str], flags: list[str],
    ) -> tuple[list[Candle], Decimal | None]:
        """The five CLOSED 5m candles behind the frozen A ``reference_price``.

        The window is only accepted when it is closed before ``signal_time``
        AND reproduces ``reference_price``; otherwise nothing is attached and
        the gap is named explicitly instead of being imputed.
        """
        try:
            end = ms_to_datetime(signal_candle_open_time) \
                + timedelta(minutes=5) if signal_candle_open_time else signal_time
        except (TypeError, ValueError):
            end = signal_time
        if end > signal_time:
            end = signal_time
        start = end - timedelta(minutes=5 * WINDOW_SIZE)

        try:
            rows = self._candles.closed_candles(symbol, start=start, end=end)
        except Exception:
            log.warning("ME SHORT A runtime candle lookup failed for %s", symbol,
                        exc_info=True)
            missing.append("recent_high_window_candles")
            flags.append("source_window_unavailable")
            return [], None

        usable = [c for c in rows if c.close_time <= signal_time][-WINDOW_SIZE:]
        if len(usable) < WINDOW_SIZE:
            missing.append("recent_high_window_candles")
            flags.append("source_window_incomplete")
            return [], None

        if reference_price is not None:
            observed = max(c.high for c in usable)
            if observed != reference_price:
                # The frozen A reference included a candle that had not closed
                # at signal_time (or the scanner context differed). Never
                # silently "fix" the geometry: report it and stay without a
                # window so the row lands in SOURCE_INCOMPLETE.
                missing.append("recent_high_window_candles")
                flags.append("recent_high_window_reference_mismatch")
                return [], None

        signal_candle_close = next(
            (c.close for c in usable
             if c.open_time + timedelta(minutes=5) == end), None)
        return usable, signal_candle_close

    # ── small converters ──────────────────────────────────────────
    @staticmethod
    def _as_utc(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None \
                else value.astimezone(timezone.utc)
        return None

    @staticmethod
    def _as_decimal(value: Any) -> Decimal | None:
        return canonical_price(value)

    @staticmethod
    def _as_mapping(value: Any) -> Mapping[str, Any]:
        if isinstance(value, Mapping):
            return value
        if isinstance(value, (str, bytes)):
            try:
                import json

                parsed = json.loads(value)
                return parsed if isinstance(parsed, Mapping) else {}
            except Exception:
                return {}
        return {}
