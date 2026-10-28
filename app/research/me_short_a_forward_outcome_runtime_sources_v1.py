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
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.research.me_short_a_forward_outcome_runtime_v1 import (
    Candle,
    Candidate,
    CandidatePage,
    CandidateSource,
    DatabaseAccessError,
    canonical_price,
    event_key_for,
    utcnow,
)

__all__ = [
    "CANDLE_MINUTES_MS",
    "DEFAULT_PAGE_SIZE",
    "SCANNER_NAME",
    "DIRECTION",
    "READ_ONLY_LABEL",
    "READ_ONLY_GUC",
    "ReadOnlyViolation",
    "ReadOnlyCursor",
    "assert_read_only_statement",
    "assert_read_only_session",
    "canonical_price",
    "ms_to_datetime",
    "datetime_to_ms",
    "to_runtime_candle",
    "reconstruct_recent_high_window",
    "BybitClosedCandleSource",
    "DbScannerSetupCandidateSource",
    "DbSourceIntakeCandidateSource",
    "CompositeCandidateSource",
]

log = logging.getLogger(__name__)

CANDLE_MINUTES_MS = 5 * 60 * 1000
SCANNER_NAME = "MOMENTUM_EXHAUSTION"
DIRECTION = "SHORT"
# One keyset page of candidate rows. Pages are drained to exhaustion inside a
# cycle, so this is a batching parameter, never a cap on observed candidates.
DEFAULT_PAGE_SIZE = 500

WINDOW_SIZE = 5

# ── read-only transaction contract ───────────────────────────────────
#
# The candidate intake is a pure reader of two dds tables. Two independent
# mechanisms keep it that way:
#
#   1. PostgreSQL level — the intake runs on a session whose
#      `default_transaction_read_only` is ON, so every transaction opened on
#      it is a READ ONLY transaction and the server itself rejects any write
#      with "cannot execute INSERT in a read-only transaction";
#   2. statement level — this module refuses to even hand a non-read
#      statement to a cursor (see :func:`assert_read_only_statement`).
#
# Neither depends on the grants file, and the grants file in turn never gives
# the runtime role a write privilege on dds. Three layers, independently
# verifiable.

READ_ONLY_LABEL = "ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_READ_ONLY_INTAKE"
READ_ONLY_GUC = "default_transaction_read_only"

#: Statement kinds a read-only adapter may run.
_READ_KINDS = ("SELECT", "WITH", "SHOW", "EXPLAIN", "VALUES", "TABLE")

#: Data-modifying CTE: `WITH x AS (INSERT ...)` starts with WITH and mutates.
_MUTATING_CTE = re.compile(
    r"\bAS\s*\(\s*(?:INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)

#: Bare mutating statements, searched in literal-stripped text.
_MUTATING_STMT = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|TRUNCATE\s|MERGE\s+INTO"
    r"|COPY\s+\w|CALL\s+\w|CREATE\s|ALTER\s|DROP\s|GRANT\s|REVOKE\s)\b",
    re.IGNORECASE)

_SQL_LITERAL = re.compile(r"'(?:''|[^'])*'")


class ReadOnlyViolation(DatabaseAccessError):
    """A read-only adapter was asked to run or to commit a mutating action."""


def _strip_literals(sql: str) -> str:
    """Blank out single-quoted literals so keyword scans ignore string data."""
    return _SQL_LITERAL.sub("''", sql)


def assert_read_only_statement(sql: str) -> str:
    """Return the normalised statement kind, or raise ``ReadOnlyViolation``.

    Accepts only read statements. Both a plain ``INSERT`` and a
    data-modifying ``WITH ... AS (INSERT ...)`` are rejected, so a candidate
    source can never write even by accident.
    """
    text = (sql or "").strip()
    if not text:
        raise ReadOnlyViolation("empty statement is not a read")
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    kind = text.split(None, 1)[0].upper().rstrip("(")
    if kind not in _READ_KINDS:
        raise ReadOnlyViolation(
            f"read-only intake refused non-read statement kind={kind!r}")
    body = _strip_literals(text)
    if _MUTATING_CTE.search(body):
        raise ReadOnlyViolation(
            "read-only intake refused a data-modifying WITH clause")
    if _MUTATING_STMT.search(body):
        raise ReadOnlyViolation(
            "read-only intake refused a statement containing a mutating "
            "clause")
    return kind


class ReadOnlyCursor:
    """Cursor wrapper that refuses to execute a mutating statement.

    Every read path of the runtime goes through this wrapper (see
    :func:`assert_read_only_statement`), so the read-only property holds even
    if the session-level guarantee is ever lost.
    """

    def __init__(self, cursor: Any) -> None:
        self._cur = cursor

    # -- reads are allowed -------------------------------------------------
    def execute(self, sql: Any, params: Any = None) -> Any:
        assert_read_only_statement(sql if isinstance(sql, str) else str(sql))
        try:
            if params is None:
                return self._cur.execute(sql)
            return self._cur.execute(sql, params)
        except (ReadOnlyViolation, DatabaseAccessError):
            raise
        except Exception as exc:  # noqa: BLE001 - normalise driver errors
            raise DatabaseAccessError(
                f"read-only query failed: {type(exc).__name__}: {exc}") from exc

    def fetchall(self) -> Any:
        return self._cur.fetchall()

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchmany(self, size: int | None = None) -> Any:
        if size is None:
            return self._cur.fetchmany()
        return self._cur.fetchmany(size)

    def close(self) -> Any:
        return self._cur.close()

    # -- observability ----------------------------------------------------
    @property
    def description(self) -> Any:
        return self._cur.description

    @property
    def rowcount(self) -> Any:
        return self._cur.rowcount

    def __iter__(self) -> Any:
        return iter(self._cur)

    def __enter__(self) -> "ReadOnlyCursor":
        return self

    def __exit__(self, *exc: Any) -> Any:
        self.close()
        return False


def assert_read_only_session(conn: Any) -> None:
    """Verify the connection really is a READ ONLY session, else raise.

    This is the runtime-side check of the read-only transaction contract: the
    intake refuses to run on a session where a write would be accepted.
    """
    cursor_obj = conn.cursor()
    try:
        cursor_obj.execute(f"SHOW {READ_ONLY_GUC}")
        row = cursor_obj.fetchone()
    except Exception as exc:  # noqa: BLE001 - normalise driver errors
        raise DatabaseAccessError(
            f"could not verify the read-only session: "
            f"{type(exc).__name__}: {exc}") from exc
    finally:
        cursor_obj.close()
    value = row[0] if row else None
    if str(value).strip().lower() not in ("on", "true", "1", "yes", "t"):
        raise ReadOnlyViolation(
            f"candidate intake requires a READ ONLY session "
            f"({READ_ONLY_GUC}={value!r}); refusing to scan dds read-write")


def _unabort(conn: Any) -> None:
    """Return the shared transaction to a usable state after a failed read.

    In PostgreSQL one failed statement aborts the WHOLE transaction, and
    every later statement on the same connection then fails with "current
    transaction is aborted". Both transports share one READ ONLY connection,
    so without this a failure of the OPTIONAL research intake read (missing
    table before migration 080, revoked SELECT, a typo) would permanently
    break the MANDATORY ``dds.scanner_setup`` stream. Rolling back is safe
    here: this module only ever reads, and every scan position lives in the
    callers' keyset cursors — never in the transaction — so no page, row or
    progress is lost by it.
    """
    rollback = getattr(conn, "rollback", None)
    if rollback is None:
        return
    try:
        rollback()
    except Exception:  # noqa: BLE001 - best effort, never masks the read error
        log.debug("ME SHORT A read-only rollback failed", exc_info=True)


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


def reconstruct_recent_high_window(
    candle_source: Any,
    symbol: str,
    signal_time: datetime,
    signal_candle_open_time: Any,
    reference_price: Decimal | None,
    missing: list[str],
    flags: list[str],
) -> tuple[list[Candle], Decimal | None]:
    """The five CLOSED 5m candles behind the frozen A ``reference_price``.

    Shared by every candidate source (``dds.scanner_setup`` and the research
    source intake): the window is only accepted when it is closed before
    ``signal_time`` AND reproduces ``reference_price``; otherwise nothing is
    attached and the gap is named explicitly instead of being imputed.

    ``signal_candle_open_time`` may be epoch milliseconds (the dds column) or
    a datetime (the intake column).
    """
    try:
        if isinstance(signal_candle_open_time, datetime):
            opened = signal_candle_open_time
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
        elif signal_candle_open_time:
            opened = ms_to_datetime(signal_candle_open_time)
        else:
            opened = None
        end = opened + timedelta(minutes=5) if opened else signal_time
    except (TypeError, ValueError):
        end = signal_time
    if end > signal_time:
        end = signal_time
    start = end - timedelta(minutes=5 * WINDOW_SIZE)

    try:
        rows = candle_source.closed_candles(symbol, start=start, end=end)
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
        # Historical range, not the exchange's latest-N candles. Clamp
        # the query to the observation clock; never request future bars.
        observed_at = self._clock()
        if observed_at.tzinfo is None:
            raise ValueError("observation clock must be timezone-aware")
        upper = min(end, observed_at)
        if upper <= start:
            return []
        raw_rows = self._client.get_klines_range(
            symbol, self._interval,
            start_ms=datetime_to_ms(start),
            end_ms=datetime_to_ms(upper) - 1,
            page_size=self._max_limit,
        )
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


class DbScannerSetupCandidateSource(CandidateSource):
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

    Pagination contract
    -------------------

    Intake is a **keyset** scan on the composite key ``(detected_at,
    setup_id)`` with the immutable ``freeze_ts`` as a strict lower bound:

    * ``detected_at > freeze_ts`` is applied **inside the query**, so the
      pre-freeze backlog is never read back, never occupies a page and can
      never push a post-freeze row out of one. This is what stops thousands
      of historical rows from blocking the selection.
    * ``(detected_at, setup_id) > (cursor_detected_at, cursor_setup_id)`` is
      applied as ``detected_at > ? OR (detected_at = ? AND setup_id > ?)`` so
      rows sharing the very same ``detected_at`` are all returned exactly
      once — including at a page boundary, where a plain ``detected_at > ?``
      would silently drop the remaining siblings.
    * ``setup_id`` is the tie-breaker, so the order is a total order and the
      scan is stable across pages, re-runs and restarts.
    * ``LIMIT`` only bounds one page; the caller drains pages to exhaustion,
      so a batch larger than the page size is fully observed.
    """

    _PROJECTION = (
        "SELECT s.setup_id, i.symbol, s.detected_at, s.reference_price, "
        "s.invalidation_price, s.target_1, s.entry_zone_low, "
        "s.entry_zone_high, s.signal_candle_open_time, s.features, "
        "s.market_regime, s.status "
        "FROM dds.scanner_setup s "
        "JOIN dds.instrument i ON i.instrument_id = s.instrument_id "
        "WHERE s.scanner_name=%s AND s.direction=%s "
        "AND s.detected_at > %s "
    )
    _ORDER_LIMIT = "ORDER BY s.detected_at ASC, s.setup_id ASC LIMIT %s"

    #: This source is a pure reader and must run on a READ ONLY session.
    requires_read_only_session = True
    #: Exactly these two tables are read; nothing else in dds is touched.
    read_tables = ("dds.scanner_setup", "dds.instrument")

    SELECT_SQL = _PROJECTION + _ORDER_LIMIT
    SELECT_AFTER_CURSOR_SQL = (
        _PROJECTION
        + "AND (s.detected_at > %s OR (s.detected_at = %s AND s.setup_id > %s)) "
        + _ORDER_LIMIT
    )

    def __init__(
        self,
        conn: Any,
        candle_source: Any,
        *,
        scanner_name: str = SCANNER_NAME,
        direction: str = DIRECTION,
        clock: Callable[[], datetime] = utcnow,
        max_rows: int = DEFAULT_PAGE_SIZE,
        page_size: int | None = None,
    ) -> None:
        self._conn = conn
        self._candles = candle_source
        self._scanner_name = scanner_name
        self._direction = direction
        self._clock = clock
        self._page_size = int(page_size if page_size is not None else max_rows)
        if self._page_size < 1:
            raise ValueError("page_size must be >= 1")

    # ── plumbing ──────────────────────────────────────────────────
    def __call__(self, freeze_ts: datetime | None = None) -> list[Candidate]:
        return self.fetch(freeze_ts)

    def verify_session(self) -> None:
        """Assert the intake connection is a READ ONLY session.

        Called before the first scan so a misconfigured connection is a loud,
        fail-closed error instead of a silently read-write intake.
        """
        assert_read_only_session(self._conn)

    def fetch_page(
        self,
        cursor: tuple[datetime, Any] | None,
        *,
        freeze_ts: datetime,
        page_size: int | None = None,
    ) -> CandidatePage:
        """Read exactly one keyset page after ``cursor``.

        ``freeze_ts`` is REQUIRED and is the immutable activation freeze: the
        runtime never reads a row at or before it, and a source is never used
        without one.
        """
        if freeze_ts is None:
            raise ValueError(
                "fetch_page requires the immutable freeze_ts; refusing to "
                "read candidates without a freeze bound")
        if cursor is not None and len(tuple(cursor)) != 2:
            raise ValueError("cursor must be (detected_at, setup_id)")
        limit = int(page_size if page_size is not None else self._page_size)
        if limit < 1:
            raise ValueError("page_size must be >= 1")
        lower = self._as_utc(freeze_ts)
        if lower is None:
            raise ValueError("freeze_ts must be a datetime")

        cursor_detected_at = None
        cursor_setup_id = None
        if cursor is not None:
            cursor_detected_at = self._as_utc(cursor[0])
            cursor_setup_id = cursor[1]
            if cursor_detected_at is None:
                raise ValueError("cursor detected_at must be a datetime")

        if cursor is None:
            sql, params = self.SELECT_SQL, (
                self._scanner_name, self._direction, lower, limit)
        else:
            sql, params = self.SELECT_AFTER_CURSOR_SQL, (
                self._scanner_name, self._direction, lower,
                cursor_detected_at, cursor_detected_at, cursor_setup_id, limit)

        cursor_obj = ReadOnlyCursor(self._conn.cursor())
        try:
            cursor_obj.execute(sql, params)
            rows = cursor_obj.fetchall()
        except Exception:
            # Keep the fail-loud contract (the caller stalls this page and
            # retries), but un-abort the shared transaction first — otherwise
            # the retry would hit "current transaction is aborted" forever.
            # The keyset cursor is unchanged, so the retry re-reads the same
            # page and loses nothing.
            _unabort(self._conn)
            raise
        finally:
            cursor_obj.close()

        candidates: list[Candidate] = []
        rejected: list[str] = []
        last: tuple[datetime, Any] | None = None
        for row in rows:
            detected_at = self._as_utc(row[2]) if len(row) > 2 else None
            setup_id = row[0]
            # The cursor always tracks the LAST RAW ROW read — including rows
            # that cannot be mapped. Advancing past a rejected row is correct
            # (it can never become a candidate) and is what keeps one broken
            # row from blocking the whole intake.
            if detected_at is not None:
                last = (detected_at, setup_id)
            try:
                candidate = self._map_row(row)
            except Exception:  # pragma: no cover - defensive
                candidate = None
            if candidate is None:
                rejected.append(str(setup_id))
                continue
            candidates.append(candidate)

        rows_read = len(rows)
        return CandidatePage(
            candidates=tuple(candidates),
            rejected=tuple(rejected),
            rows_read=rows_read,
            page_size=limit,
            cursor=last,
            exhausted=rows_read < limit,
        )

    def fetch(
        self, freeze_ts: datetime | None = None, *, limit: int | None = None,
    ) -> list[Candidate]:
        """Drain keyset pages into one list (tests and small batches).

        The runtime itself walks pages so that it can stop mid-stream without
        losing anything; this helper is for callers that genuinely want the
        whole post-freeze set.
        """
        if freeze_ts is None:
            raise ValueError("fetch requires the immutable freeze_ts")
        out: list[Candidate] = []
        cursor: tuple[datetime, Any] | None = None
        budget = self._page_size if limit is None else max(1, int(limit))
        while True:
            page = self.fetch_page(
                cursor, freeze_ts=freeze_ts,
                page_size=min(self._page_size, budget - len(out))
                if limit is not None else self._page_size)
            out.extend(page.candidates)
            if page.rows_read <= 0:
                break
            if limit is not None and len(out) >= budget:
                break
            if page.exhausted:
                break
            cursor = page.next_cursor
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
        """Delegates to :func:`reconstruct_recent_high_window`."""
        return reconstruct_recent_high_window(
            self._candles, symbol, signal_time, signal_candle_open_time,
            reference_price, missing, flags)

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


class DbSourceIntakeCandidateSource(CandidateSource):
    """READ-ONLY reader of the research-only ME SHORT A source intake.

    Issue #208 corrective: this source observes the ORIGINAL
    ``MOMENTUM_EXHAUSTION`` ``SHORT`` events captured before/independent of
    the trading regime filter (``research.me_short_a_source_intake_v1``,
    migration 080).  Events the trading filters dropped — the confirmed
    incident class (``SHORT`` in ``TREND_UP``) — reach the forward outcome
    runtime through here, so the ``source signal -> Phase 1 -> lineage ->
    outcome`` chain can never be silently truncated by a trading decision.

    Contract
    --------
    * Pure reader, on a READ ONLY session, of exactly two research tables.
      It never writes anywhere and never calls scanner code.
    * Keyset-paginated on ``(signal_time, event_key)`` with the same lossless
      cursor semantics as :class:`DbScannerSetupCandidateSource`.
    * Strict forward-only: the effective lower bound is
      ``max(freeze_ts, intake_freeze_ts)`` where ``intake_freeze_ts`` is the
      immutable activation boundary of the fixed mechanism
      (``research.me_short_a_source_intake_activation_v1``).  Pre-activation
      history — including the confirmed incident event — can never be read
      back.  When the intake is not ``RUNNING`` this source yields nothing
      (fail-closed).
    * Read failures fail SOFT and lossless: an empty page is returned and the
      caller's cursor stays put, so the same rows are re-read next cycle.
      Read errors are counted in ``error_count`` / ``last_error`` for
      separate source-capture diagnostics and never stall the sibling
      ``dds.scanner_setup`` stream.
    * The deterministic ``event_key`` column IS the frozen identity
      (``sha256(symbol|SHORT|<utc iso signal_time>|<ref>|<stop>|<target>)``,
      byte-identical to Phase 1 and Issue #208), so no identity is ever
      derived from mutable trading statuses.
    """

    _PROJECTION = (
        "SELECT r.event_key, r.symbol, r.signal_time, r.reference_price, "
        "r.stop_price, r.target_price, r.target_2, r.trigger_5m_close, "
        "r.entry_zone_low, r.entry_zone_high, r.signal_candle_open_time, "
        "r.recent_high_window, r.market_context, r.signal_time_quote, "
        "r.capture_route, r.blocked_stage, r.blocked_reason, "
        "r.market_regime, r.setup_id, r.missing_fields, "
        "r.source_quality_flags, r.no_lookahead_flags "
        "FROM research.me_short_a_source_intake_v1 r "
        "WHERE r.scanner_name=%s AND r.direction=%s "
        "AND r.signal_time > %s "
    )
    _ORDER_LIMIT = "ORDER BY r.signal_time ASC, r.event_key ASC LIMIT %s"

    #: Pure reader: must run on a READ ONLY session.
    requires_read_only_session = True
    #: Exactly these two research tables are read.
    read_tables = ("research.me_short_a_source_intake_v1",
                   "research.me_short_a_source_intake_activation_v1")

    SELECT_SQL = _PROJECTION + _ORDER_LIMIT
    SELECT_AFTER_CURSOR_SQL = (
        _PROJECTION
        + "AND (r.signal_time > %s OR (r.signal_time = %s AND r.event_key > %s)) "
        + _ORDER_LIMIT
    )

    ACTIVATION_SQL = (
        "SELECT status,intake_freeze_ts "
        "FROM research.me_short_a_source_intake_activation_v1 "
        "WHERE experiment_id=%s"
    )
    ACTIVATION_EXPERIMENT = "ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1"

    def __init__(
        self,
        conn: Any,
        candle_source: Any,
        *,
        scanner_name: str = SCANNER_NAME,
        direction: str = DIRECTION,
        clock: Callable[[], datetime] = utcnow,
        max_rows: int = DEFAULT_PAGE_SIZE,
        page_size: int | None = None,
    ) -> None:
        self._conn = conn
        self._candles = candle_source
        self._scanner_name = scanner_name
        self._direction = direction
        self._clock = clock
        self._page_size = int(page_size if page_size is not None else max_rows)
        if self._page_size < 1:
            raise ValueError("page_size must be >= 1")
        #: Source-capture read diagnostics (never affects scanner execution).
        self.error_count = 0
        self.last_error: str | None = None

    # ── activation (forward-only boundary of the fixed mechanism) ──
    def activation_state(self) -> tuple[str, datetime | None] | None:
        """``(status, intake_freeze_ts)`` of the intake activation row."""
        cursor_obj = ReadOnlyCursor(self._conn.cursor())
        try:
            cursor_obj.execute(self.ACTIVATION_SQL, (self.ACTIVATION_EXPERIMENT,))
            row = cursor_obj.fetchone()
        except Exception:
            # Fail-soft for THIS source, but never leave the shared READ ONLY
            # transaction aborted for the sibling dds transport.
            _unabort(self._conn)
            raise
        finally:
            cursor_obj.close()
        if row is None:
            return None
        freeze = self._as_utc(row[1]) if row[1] is not None else None
        return str(row[0]), freeze

    def lower_bound(self) -> datetime | None:
        """Immutable activation floor of this mechanism, or ``None``.

        This floor applies ONLY inside this source's own reads
        (``max(freeze_ts, intake_freeze_ts)``).  Sibling transports keep
        their own bounds — in particular ``dds.scanner_setup`` keeps the
        ORIGINAL Issue #208 ``freeze_ts`` — and the composite never raises
        a common boundary from this value.  Read failures return ``None``
        (soft): this source still fail-closes its own reads.
        """
        try:
            state = self.activation_state()
        except Exception as exc:  # noqa: BLE001 - fail-soft, lossless
            self.error_count += 1
            self.last_error = f"intake activation read failed: {type(exc).__name__}: {exc}"
            log.warning("ME SHORT A source intake activation read failed",
                        exc_info=True)
            return None
        if state is None or state[0] != "RUNNING" or state[1] is None:
            return None
        return state[1]

    # ── pagination ────────────────────────────────────────────────
    def fetch_page(
        self,
        cursor: tuple[datetime, Any] | None,
        *,
        freeze_ts: datetime,
        page_size: int | None = None,
    ) -> CandidatePage:
        if freeze_ts is None:
            raise ValueError(
                "fetch_page requires the immutable freeze_ts; refusing to "
                "read candidates without a freeze bound")
        if cursor is not None and len(tuple(cursor)) != 2:
            raise ValueError("cursor must be (signal_time, event_key)")
        limit = int(page_size if page_size is not None else self._page_size)
        if limit < 1:
            raise ValueError("page_size must be >= 1")
        lower = self._as_utc(freeze_ts)
        if lower is None:
            raise ValueError("freeze_ts must be a datetime")

        empty = self._empty_page(limit)
        try:
            activation = self.activation_state()
        except Exception as exc:  # noqa: BLE001 - fail-soft, lossless
            self.error_count += 1
            self.last_error = f"intake activation read failed: {type(exc).__name__}: {exc}"
            log.warning("ME SHORT A source intake activation read failed",
                        exc_info=True)
            return empty
        if activation is None or activation[0] != "RUNNING" or activation[1] is None:
            log.debug("ME SHORT A source intake not active; intake source idle")
            return empty
        intake_freeze = activation[1]
        effective_lower = max(lower, intake_freeze)

        cursor_signal_time = None
        cursor_event_key = None
        if cursor is not None:
            cursor_signal_time = self._as_utc(cursor[0])
            cursor_event_key = cursor[1]
            if cursor_signal_time is None:
                raise ValueError("cursor signal_time must be a datetime")

        if cursor is None:
            sql, params = self.SELECT_SQL, (
                self._scanner_name, self._direction, effective_lower, limit)
        else:
            sql, params = self.SELECT_AFTER_CURSOR_SQL, (
                self._scanner_name, self._direction, effective_lower,
                cursor_signal_time, cursor_signal_time, cursor_event_key, limit)

        try:
            cursor_obj = ReadOnlyCursor(self._conn.cursor())
            try:
                cursor_obj.execute(sql, params)
                rows = cursor_obj.fetchall()
            finally:
                cursor_obj.close()
        except Exception as exc:  # noqa: BLE001 - fail-soft, lossless
            # Un-abort the shared transaction so the sibling dds transport
            # keeps working; our own cursor stays put (lossless retry).
            _unabort(self._conn)
            self.error_count += 1
            self.last_error = f"intake page read failed: {type(exc).__name__}: {exc}"
            log.warning("ME SHORT A source intake page read failed",
                        exc_info=True)
            return empty

        candidates: list[Candidate] = []
        rejected: list[str] = []
        last: tuple[datetime, Any] | None = None
        for row in rows:
            signal_time = self._as_utc(row[2]) if len(row) > 2 else None
            event_key = row[0]
            # The cursor tracks the LAST RAW ROW read — including rows that
            # cannot be mapped — so one broken row never blocks the intake.
            if signal_time is not None:
                last = (signal_time, event_key)
            try:
                candidate = self._map_row(row)
            except Exception:  # pragma: no cover - defensive
                candidate = None
            if candidate is None:
                rejected.append(str(event_key))
                continue
            candidates.append(candidate)

        rows_read = len(rows)
        return CandidatePage(
            candidates=tuple(candidates),
            rejected=tuple(rejected),
            rows_read=rows_read,
            page_size=limit,
            cursor=last,
            exhausted=rows_read < limit,
        )

    @staticmethod
    def _empty_page(limit: int) -> CandidatePage:
        return CandidatePage(
            candidates=(), rejected=(), rows_read=0,
            page_size=limit, cursor=None, exhausted=True)

    # ── mapping ───────────────────────────────────────────────────
    def _map_row(self, row: Sequence[Any]) -> Candidate | None:
        (
            event_key, symbol, signal_time, reference_price, stop_price,
            target_price, _target_2, trigger_5m_close, entry_zone_low,
            entry_zone_high, signal_candle_open_time, _recent_high_window,
            market_context, signal_time_quote,
            capture_route, blocked_stage, blocked_reason,
            market_regime, setup_id,
            missing_fields, source_quality_flags, no_lookahead_flags,
        ) = row

        if symbol is None or signal_time is None:
            return None
        if reference_price is None or stop_price is None or target_price is None:
            return None

        signal_dt = self._as_utc(signal_time)
        if signal_dt is None:
            return None

        missing = [str(name) for name in (self._as_list(missing_fields))]
        flags = [str(name) for name in (self._as_list(source_quality_flags))]

        ref = self._as_decimal(reference_price)
        window, signal_candle_close = reconstruct_recent_high_window(
            self._candles, str(symbol), signal_dt, signal_candle_open_time,
            ref, missing, flags)

        trigger = self._as_decimal(trigger_5m_close)
        if trigger is None and signal_candle_close is not None:
            trigger = signal_candle_close
        if trigger is None and "trigger_5m_close" not in missing:
            missing.append("trigger_5m_close")

        context: dict[str, Any] = {}
        for key, value in (
            ("market_regime", market_regime),
            ("capture_route", capture_route),
            ("blocked_stage", blocked_stage),
            ("blocked_reason", blocked_reason),
        ):
            if value not in (None, ""):
                context[key] = value
        context.update(self._as_mapping(market_context))
        quote = self._as_mapping(signal_time_quote)

        try:
            return Candidate(
                symbol=str(symbol),
                signal_time=signal_dt,
                reference_price=ref,
                invalidation_price=self._as_decimal(stop_price),
                target_1=self._as_decimal(target_price),
                trigger_5m_close=trigger,
                entry_zone_low=self._as_decimal(entry_zone_low),
                entry_zone_high=self._as_decimal(entry_zone_high),
                recent_high_window=tuple(window),
                market_context=context,
                signal_time_quote=quote,
                source_observation_id=None,
                source="MOMENTUM_EXHAUSTION_SHORT_CANDIDATES",
                observed_at=self._clock(),
                event_id=f"intake:{event_key}",
                missing_fields=tuple(missing),
                source_quality_flags=tuple(flags),
            )
        except ValueError as exc:
            log.warning(
                "ME SHORT A runtime rejected intake candidate %s/%s: %s",
                symbol, event_key, exc)
            return None

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

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, (str, bytes)):
            try:
                import json

                parsed = json.loads(value)
                return list(parsed) if isinstance(parsed, list) else []
            except Exception:
                return []
        return []


class CompositeCandidateSource(CandidateSource):
    """Merges keyset-paginated sources into one lossless candidate stream.

    The Issue #208 runtime observes its ``MOMENTUM_EXHAUSTION`` ``SHORT``
    candidates through more than one transport (``dds.scanner_setup`` and
    the research source intake), but the handler loop consumes exactly one
    :class:`CandidatePage` stream.  This composite keeps the per-source
    keyset cursors and presents one page at a time, so the runtime's
    "the cursor advances only after a fully handled page" contract holds for
    EVERY sub-source at once:

    * the composite cursor is the tuple of per-sub-source cursors and is
      opaque to the runtime;
    * a sub-source page is only recorded as handled when the composite page
      that contains it is handled in full — a mid-page failure re-reads the
      whole page and can never lose the rows behind it (duplicates are
      removed by the deterministic ``event_key`` within a fetch and by the
      collector's ``ON CONFLICT DO NOTHING`` across fetches);
    * every fetch re-visits every sub-source from its own cursor, so new rows
      in any sub-source are always observed and a drained sub-source never
      blocks the others;
    * every sub-source keeps ITS OWN immutable cohort bound and the
      ``freeze_ts`` handed to it is passed through VERBATIM: the
      ``dds.scanner_setup`` transport reads strictly after the ORIGINAL Issue
      #208 ``freeze_ts`` (its frozen cohort is never shrunk), while the
      research intake reads strictly after ``max(freeze_ts,
      intake_freeze_ts)`` (its own activation floor — backfill is
      impossible).  The composite never raises a common boundary: a raised
      bound would silently exclude legitimate post-#208-freeze ``dds`` rows
      and change the frozen cohort;
    * each fetch gives every sub-source a guaranteed minimum share of the
      page BEFORE any leftover capacity is distributed, the sum of raw rows
      in one composite page never exceeds ``page_size``, and when the page
      cannot hold every transport (e.g. ``page_size=1``) the scarce slot
      ROTATES — carried in the opaque cursor itself — so a constant full
      stream in one transport (e.g. a busy ``dds.scanner_setup``) can never
      starve another (e.g. new research intake rows) at any
      ``page_size >= 1``.  The rotation makes page composition a pure
      function of the resume token, so a stalled page re-read from the same
      cursor returns the same rows.
    """

    def __init__(self, sources: Sequence[CandidateSource]) -> None:
        if not sources:
            raise ValueError("CompositeCandidateSource needs at least one source")
        self._sources = tuple(sources)

    @property
    def requires_read_only_session(self) -> bool:
        return any(
            getattr(source, "requires_read_only_session", False)
            for source in self._sources)

    @property
    def read_tables(self) -> tuple[str, ...]:
        out: list[str] = []
        for source in self._sources:
            out.extend(getattr(source, "read_tables", ()))
        return tuple(out)

    def fetch_page(
        self,
        cursor: tuple[datetime, Any] | None,
        *,
        freeze_ts: datetime,
        page_size: int | None = None,
    ) -> CandidatePage:
        if freeze_ts is None:
            raise ValueError(
                "fetch_page requires the immutable freeze_ts; refusing to "
                "read candidates without a freeze bound")
        limit = int(page_size if page_size is not None else DEFAULT_PAGE_SIZE)
        if limit < 1:
            raise ValueError("page_size must be >= 1")

        state = self._decode_cursor(cursor)
        start, sub_cursors = state

        # ``freeze_ts`` (the ORIGINAL Issue #208 freeze) is passed through
        # VERBATIM to every sub-source; each sub-source applies its own
        # cohort bound on top of it.  The composite deliberately computes NO
        # common boundary: raising it would shrink the frozen ``dds`` cohort.
        candidates: list[Candidate] = []
        rejected: list[str] = []
        rows_read = 0
        sources_total = len(self._sources)

        # Fair share, valid for EVERY ``page_size >= 1``:
        #
        # * the sum of raw rows in one composite page never exceeds
        #   ``page_size`` — every sub-page request is capped by the REMAINING
        #   capacity, including when ``page_size`` is smaller than the number
        #   of sources;
        # * every sub-source is guaranteed a minimum share BEFORE leftover
        #   capacity is distributed, so a constant full stream in one
        #   transport can never starve another;
        # * when the page cannot hold every transport (e.g. ``page_size=1``),
        #   the scarce slot rotates: the rotation start is carried in the
        #   opaque cursor itself and advances by one per consumed page, so it
        #   is a pure function of the resume token — re-reading a stalled
        #   page from the same cursor returns the SAME composition, and no
        #   transport can be skipped forever;
        # * a fetch that consumed nothing returns the cursor unchanged.
        min_share = max(1, limit // sources_total)
        for round_index in (0, 1):
            for step in range(sources_total):
                index = (start + step) % sources_total
                remaining = limit - rows_read
                if remaining <= 0:
                    break
                want = (
                    min(min_share, remaining) if round_index == 0
                    else remaining
                )
                if want <= 0:
                    break
                page = self._sources[index].fetch_page(
                    sub_cursors[index],
                    freeze_ts=freeze_ts,
                    page_size=want,
                )
                rows_read += page.rows_read
                rejected.extend(page.rejected)
                candidates.extend(page.candidates)
                if page.rows_read > 0:
                    sub_cursors[index] = page.next_cursor

        # One event can arrive through several transports. Collapse the
        # duplicates within THIS fetch by the deterministic identity; the
        # runtime's seen-set and the collector's ON CONFLICT cover re-reads.
        deduped: list[Candidate] = []
        seen_keys: set[str] = set()
        for candidate in candidates:
            try:
                key = event_key_for(candidate)
            except Exception:  # noqa: BLE001 - defensive: keep the row
                deduped.append(candidate)
                continue
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(candidate)

        # A page that consumed nothing leaves the resume token exactly where
        # it was (rotation start included); a consumed page rotates the
        # scarce slot by one so every transport is served in turn.
        next_start = (start + 1) % sources_total if rows_read > 0 else start
        return CandidatePage(
            candidates=tuple(deduped),
            rejected=tuple(rejected),
            rows_read=rows_read,
            page_size=limit,
            cursor=(next_start, *sub_cursors),
            exhausted=rows_read < limit,
        )

    def _decode_cursor(
        self, cursor: Any,
    ) -> tuple[int, list[tuple[datetime, Any] | None]]:
        """``(rotation start, one sub-cursor per source)`` from the token.

        The rotation start is part of the opaque resume token on purpose: it
        makes the scarce-slot order a PURE FUNCTION of the cursor, so a
        stalled page re-read from the same cursor returns the same
        composition while the rotation still advances across consumed pages.
        """
        if cursor is None:
            return 0, [None] * len(self._sources)
        parts = tuple(cursor)
        if len(parts) != len(self._sources) + 1:
            raise ValueError(
                "composite cursor does not match the number of sources")
        start = int(parts[0])
        if not 0 <= start < len(self._sources):
            raise ValueError("composite cursor rotation start out of range")
        return start, list(parts[1:])
