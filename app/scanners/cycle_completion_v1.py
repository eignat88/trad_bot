"""SCANNER_AC7 Phase A — structured Scanner cycle-completion diagnostics.

Part of ``SCANNER_AC7_INCOMPLETE_CYCLE_RECOVERY_V1``.

Phase A is **observability only**: no recovery pass, no extra Bybit request,
no change to trading logic, to the shared rate limiter (G/B, breaker, retry
budget) or to the PostgreSQL schema.

Why this exists
---------------
A Bybit ``retCode=10006`` / shared-limiter deny used to be indistinguishable
from an ordinary market-data failure: ``run_scan_cycle()`` counted the symbol
as ``failed``, wrote one generic ``MARKET_DATA_ERROR`` row and the operator
log said only ``cycle #174 done: 44/50 symbols`` — with no word that the
cycle was incomplete, which symbols were dropped, or why.

This module gives every cycle one explicit, structured completion report:

``expected_symbols``
    universe size the cycle was started with;
``processed_symbols``
    symbols whose ``MarketContext`` was actually built — **unchanged**
    semantics of ``scanner_run.symbols_scanned``;
``failed_symbols``
    market data failed for a NON-rate-limit reason (transport, payload, ...);
``skipped_symbols``
    market data was never fetched because Bybit / the shared limiter denied
    the request (``10006``, ``BYBIT_API_SKIP``, ``BREAKER_OPEN``,
    ``deadline``, the process-local rate-limit cooldown, ...);
``scan_error_symbols``
    market data was fine but the scan itself raised — counted as *processed*
    exactly as before Phase A;
``completion_status``
    ``COMPLETE`` / ``PARTIAL`` / ``FAILED``.

Persistence (no migration)
--------------------------
* ``dds.scanner_run.status`` keeps its **existing** vocabulary — the
  ``RUNNING/COMPLETED/FAILED/PARTIAL/ABORTED`` CHECK constraint and every
  SQL mart that reads it are untouched.  See
  :attr:`CycleCompletionReport.run_status`;
* the full report is persisted as a ``CYCLE_COMPLETION`` row in
  ``dds.scanner_event`` (``payload JSONB``, ``event_type`` has no CHECK) for
  every cycle that is not ``COMPLETE``, and summarized in the operator log
  for every cycle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.exchange.bybit_client import BybitRateLimitError

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: payload ``schema`` marker so a future reader can evolve the shape safely.
SCHEMA = "SCANNER_CYCLE_COMPLETION_V1"

#: ``completion_status`` values (issue §5.1).
COMPLETION_COMPLETE = "COMPLETE"
COMPLETION_PARTIAL = "PARTIAL"
COMPLETION_FAILED = "FAILED"

COMPLETION_STATUSES = (
    COMPLETION_COMPLETE, COMPLETION_PARTIAL, COMPLETION_FAILED,
)

#: The ONLY values ever written to ``dds.scanner_run.status`` for a
#: successfully finished scan pass — identical to the pre-Phase-A mapping
#: (``"COMPLETED" if failed == 0 else "PARTIAL"``) and therefore identical
#: for every SQL mart reading that column.
RUN_STATUS_COMPLETED = "COMPLETED"
RUN_STATUS_PARTIAL = "PARTIAL"

#: ``dds.scanner_run.status`` CHECK vocabulary — asserted unchanged by tests.
RUN_STATUSES = ("RUNNING", "COMPLETED", "FAILED", "PARTIAL", "ABORTED")

STAGE_MARKET_DATA = "MARKET_DATA"
STAGE_SCAN = "SCAN"

CATEGORY_FAILED = "FAILED"
CATEGORY_SKIPPED = "SKIPPED"

ERROR_MARKET_DATA = "MARKET_DATA_ERROR"
ERROR_RATE_LIMIT_SKIP = "RATE_LIMIT_SKIP"
ERROR_SCAN = "SCAN_ERROR"

#: ``dds.scanner_event.event_type`` of the per-cycle diagnostic row.
CYCLE_COMPLETION_EVENT = "CYCLE_COMPLETION"
#: ``scanner_name`` stored on that row (a cycle event belongs to no strategy).
CYCLE_COMPLETION_SCANNER = "SCANNER"

#: Telemetry ``result=`` labels emitted by ``BybitClient`` for the same
#: failure (``BYBIT_API_CALL`` log records) — kept on the report so an
#: operator can join a ``CYCLE_COMPLETION`` payload against the raw log.
TELEMETRY_RATE_LIMIT = "RATE_LIMIT"
TELEMETRY_LIMITER_SKIP = "BYBIT_API_SKIP"

#: Longest message kept on a report row (the full text always stays in
#: ``dds.scanner_error.error_message``).
MESSAGE_LIMIT = 600

# --------------------------------------------------------------------------
# Message parsing — the fallback for exceptions raised outside BybitClient
# (wrappers, third-party code, older call sites) that carry no attributes.
# --------------------------------------------------------------------------

#: ``deferred by shared rate limiter (BREAKER_OPEN: breaker_cooldown_exceeds_deadline)``
#: ``deferred by shared rate limiter (SKIPPED: deadline)``
_DEFERRED_RE = re.compile(
    r"deferred by shared rate limiter \("
    r"(?P<result>[A-Z_]+)(?:: (?P<reason>[^)]+))?\)"
)

#: ``request deferred: rate-limit cooldown open (12.34s remaining)``
_COOLDOWN_RE = re.compile(
    r"rate-limit cooldown open \((?P<remaining>[0-9]+(?:\.[0-9]+)?)s remaining\)"
)

#: ``BTCUSDT: Rate limited on /v5/market/kline after 1 attempt(s) (rate-limit retry cap)``
_RATE_LIMITED_RE = re.compile(
    r"Rate limited on \S+ after \d+ attempt\(s\) \((?P<reason>[^)]+)\)"
)

#: ``limiter grant revoked before send``
_REVOKED_RE = re.compile(r"limiter grant revoked before send")

#: ``shared rate limiter unavailable (...)``
_UNAVAILABLE_RE = re.compile(r"shared rate limiter unavailable")


def _truncate(text: str, limit: int = MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def parse_rate_limit_message(message: str) -> tuple[str | None, str | None]:
    """Recover ``(result, reason)`` from a rate-limit denial message.

    Used only when the exception carries no structured attributes; a match
    never invents a value the client did not report.
    """
    match = _DEFERRED_RE.search(message)
    if match:
        return match.group("result"), match.group("reason")
    match = _COOLDOWN_RE.search(message)
    if match:
        # The remaining cooldown time IS the operator-relevant figure here.
        return TELEMETRY_RATE_LIMIT, (
            f"process_local_cooldown:{match.group('remaining')}s")
    if _REVOKED_RE.search(message):
        return "REVOKED", "grant_revoked"
    if _UNAVAILABLE_RE.search(message):
        return "LIMITER_UNAVAILABLE", "limiter_state"
    match = _RATE_LIMITED_RE.search(message)
    if match:
        # Budget exhausted on Bybit's own rate-limit answer (10006 / HTTP 429).
        return TELEMETRY_RATE_LIMIT, match.group("reason")
    # An unrecognised rate-limit message is still RATE_LIMIT-classified — it
    # never degrades to an ordinary market-data failure.
    return TELEMETRY_RATE_LIMIT, None


# --------------------------------------------------------------------------
# One symbol's outcome
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolOutcome:
    """Why exactly one symbol did not (or did not fully) contribute."""

    symbol: str
    stage: str                    # STAGE_MARKET_DATA | STAGE_SCAN
    category: str                 # CATEGORY_FAILED | CATEGORY_SKIPPED
    error_type: str               # value written to dds.scanner_error
    rate_limit: bool = False
    #: ``BREAKER_OPEN`` / ``SKIPPED`` / ``RATE_LIMIT`` / ``REVOKED`` / ...
    result: str | None = None
    #: ``breaker_cooldown_exceeds_deadline`` / ``deadline`` / ``rate-limit retry cap`` / ...
    reason: str | None = None
    #: ``BYBIT_API_SKIP`` / ``RATE_LIMIT`` — the BYBIT_API_CALL telemetry
    #: result recorded for the very same failure (join key into the logs).
    telemetry_result: str | None = None
    #: limiter queue wait in ms, when the client reported it.
    queue_wait_ms: int | None = None
    #: Bybit ``retCode`` (10006) when the failure came from a response.
    ret_code: int | None = None
    message: str = ""

    @property
    def is_skip(self) -> bool:
        return self.category == CATEGORY_SKIPPED

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "stage": self.stage,
            "category": self.category,
            "error_type": self.error_type,
            "rate_limit": self.rate_limit,
            "result": self.result,
            "reason": self.reason,
            "telemetry_result": self.telemetry_result,
            "queue_wait_ms": self.queue_wait_ms,
            "ret_code": self.ret_code,
            "message": self.message,
        }

    def label(self) -> str:
        """Compact one-line form for the operator log."""
        parts = [
            f"{self.symbol}:{self.category}",
            f"result={self.result or self.error_type}",
        ]
        if self.reason:
            parts.append(f"reason={self.reason}")
        if self.queue_wait_ms is not None:
            parts.append(f"queue_wait_ms={self.queue_wait_ms}")
        if self.ret_code is not None:
            parts.append(f"ret_code={self.ret_code}")
        return " ".join(parts)


def classify_symbol_failure(
    symbol: str,
    exc: BaseException,
    *,
    stage: str = STAGE_MARKET_DATA,
) -> SymbolOutcome:
    """Classify one ``run_scan_cycle`` failure into a structured outcome.

    * market-data stage + :class:`BybitRateLimitError` → **SKIPPED**
      (``error_type=RATE_LIMIT_SKIP``): the symbol received no data at all;
    * market-data stage + anything else → **FAILED**
      (``error_type=MARKET_DATA_ERROR``): unchanged historical behaviour;
    * scan stage → **FAILED** with ``error_type=SCAN_ERROR`` (unchanged);
      a rate-limit denial inside the scan is still flagged ``rate_limit``
      with its structured context, but the symbol was already counted as
      processed — exactly as before Phase A.

    Structured context (``result``/``reason``/``queue_wait_ms``/``ret_code``/
    ``telemetry_result``) is read from the exception when present and parsed
    out of its message otherwise.
    """
    message = str(exc)
    rate_limit = isinstance(exc, BybitRateLimitError)

    if stage == STAGE_SCAN:
        category = CATEGORY_FAILED
        error_type = ERROR_SCAN
    elif rate_limit:
        category = CATEGORY_SKIPPED
        error_type = ERROR_RATE_LIMIT_SKIP
    else:
        category = CATEGORY_FAILED
        error_type = ERROR_MARKET_DATA

    result = getattr(exc, "limiter_result", None)
    reason = getattr(exc, "limiter_reason", None)
    queue_wait_ms = getattr(exc, "queue_wait_ms", None)
    ret_code = getattr(exc, "ret_code", None)
    telemetry_result = getattr(exc, "telemetry_result", None)

    if rate_limit and (result is None or reason is None):
        parsed_result, parsed_reason = parse_rate_limit_message(message)
        result = result or parsed_result
        reason = reason or parsed_reason
    if rate_limit and result is None:
        result = TELEMETRY_RATE_LIMIT

    if stage == STAGE_MARKET_DATA and rate_limit and telemetry_result is None:
        telemetry_result = TELEMETRY_LIMITER_SKIP

    return SymbolOutcome(
        symbol=symbol,
        stage=stage,
        category=category,
        error_type=error_type,
        rate_limit=rate_limit,
        result=result,
        reason=reason,
        telemetry_result=telemetry_result,
        queue_wait_ms=queue_wait_ms,
        ret_code=ret_code,
        message=_truncate(message),
    )


# --------------------------------------------------------------------------
# Per-cycle report
# --------------------------------------------------------------------------


@dataclass
class CycleCompletionReport:
    """Accumulates one Scanner cycle's completion facts (issue §5.1)."""

    expected_symbols: int = 0
    #: symbols whose MarketContext was built — ``symbols_scanned`` parity.
    processed: list[str] = field(default_factory=list)
    #: market data failed, non-rate-limit.
    failures: list[SymbolOutcome] = field(default_factory=list)
    #: market data never fetched: Bybit / limiter denied the request.
    skips: list[SymbolOutcome] = field(default_factory=list)
    #: market data was fine, the scan itself raised.
    scan_failures: list[SymbolOutcome] = field(default_factory=list)

    # -- recording ---------------------------------------------------------
    def record_processed(self, symbol: str) -> None:
        self.processed.append(symbol)

    def record_outcome(self, outcome: SymbolOutcome) -> None:
        if outcome.stage == STAGE_SCAN:
            self.scan_failures.append(outcome)
        elif outcome.category == CATEGORY_SKIPPED:
            self.skips.append(outcome)
        else:
            self.failures.append(outcome)

    # -- views -------------------------------------------------------------
    @property
    def processed_count(self) -> int:
        return len(self.processed)

    @property
    def failed_symbols(self) -> list[str]:
        return [o.symbol for o in self.failures]

    @property
    def skipped_symbols(self) -> list[str]:
        return [o.symbol for o in self.skips]

    @property
    def scan_error_symbols(self) -> list[str]:
        return [o.symbol for o in self.scan_failures]

    @property
    def unprocessed_symbols(self) -> list[str]:
        """Symbols that produced NO market-data result this cycle."""
        return self.failed_symbols + self.skipped_symbols

    @property
    def outcomes(self) -> list[SymbolOutcome]:
        return [*self.failures, *self.skips, *self.scan_failures]

    # -- classification ----------------------------------------------------
    @property
    def completion_status(self) -> str:
        """``COMPLETE`` / ``PARTIAL`` / ``FAILED`` (issue §5.1).

        * ``COMPLETE`` — every expected symbol produced a market context and
          nothing failed anywhere in the cycle;
        * ``FAILED``   — the cycle processed nothing at all (and there was
          something to process);
        * ``PARTIAL``  — anything in between, including a cycle that fetched
          50/50 contexts but could not scan one of them.

        ``expected_symbols == 0`` is ``COMPLETE``: that is the historical
        ``failed == 0 → COMPLETED`` behaviour for an empty universe.
        """
        clean = (
            not self.failures and not self.skips and not self.scan_failures
            and self.processed_count == self.expected_symbols
        )
        if clean:
            return COMPLETION_COMPLETE
        if self.processed_count == 0 and self.expected_symbols > 0:
            return COMPLETION_FAILED
        return COMPLETION_PARTIAL

    @property
    def run_status(self) -> str:
        """Value for ``dds.scanner_run.status`` — vocabulary UNCHANGED.

        ``COMPLETE → COMPLETED`` and ``PARTIAL/FAILED → PARTIAL``: byte-for-byte
        the pre-Phase-A mapping (``"COMPLETED" if failed == 0 else "PARTIAL"``),
        so ``mart.scanner_overview.completed_runs`` (``status='COMPLETED'``),
        ``mart.scanner_health.failed_runs`` (``ABORTED|PARTIAL``) and the
        ``status='COMPLETED'`` filters of ``mart.scanner_health_matrix`` /
        ``mart.scanner_candidates_*`` all keep their exact current meaning.
        ``FAILED`` (a cycle that processed nothing) is surfaced in the log and
        in the ``CYCLE_COMPLETION`` payload; ``main()``'s exception path keeps
        writing ``FAILED`` to the run row exactly as before.
        """
        return (
            RUN_STATUS_COMPLETED
            if self.completion_status == COMPLETION_COMPLETE
            else RUN_STATUS_PARTIAL
        )

    @property
    def is_complete(self) -> bool:
        return self.completion_status == COMPLETION_COMPLETE

    # -- persistence / logging --------------------------------------------
    def to_payload(self) -> dict[str, Any]:
        """JSONB-safe ``dds.scanner_event.payload`` document."""
        return {
            "schema": SCHEMA,
            "completion_status": self.completion_status,
            "run_status": self.run_status,
            "expected_symbols": self.expected_symbols,
            "processed_symbols": self.processed_count,
            "failed_count": len(self.failures),
            "skipped_count": len(self.skips),
            "scan_error_count": len(self.scan_failures),
            "failed_symbols": self.failed_symbols,
            "skipped_symbols": self.skipped_symbols,
            "scan_error_symbols": self.scan_error_symbols,
            "unprocessed_symbols": self.unprocessed_symbols,
            "processed": list(self.processed),
            "outcomes": [o.to_payload() for o in self.outcomes],
        }

    def log_summary(self) -> str:
        """One operator-facing line for an incomplete cycle."""
        parts = [
            f"completion_status={self.completion_status}",
            f"run_status={self.run_status}",
            f"expected={self.expected_symbols}",
            f"processed={self.processed_count}",
            f"failed={len(self.failures)}",
            f"skipped={len(self.skips)}",
            f"scan_errors={len(self.scan_failures)}",
        ]
        if self.failures:
            parts.append(
                "failed_symbols=[" + ", ".join(o.label() for o in self.failures) + "]")
        if self.skips:
            parts.append(
                "skipped_symbols=[" + ", ".join(o.label() for o in self.skips) + "]")
        if self.scan_failures:
            parts.append(
                "scan_errors=[" + ", ".join(o.label() for o in self.scan_failures) + "]")
        return " ".join(parts)
