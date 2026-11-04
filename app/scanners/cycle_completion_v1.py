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

Two different rate-limit stories (review P1)
--------------------------------------------
A :class:`BybitRateLimitError` does NOT tell whether the request left the
process, so every rate-limit outcome additionally carries
``request_state``:

``RATE_LIMIT_RESPONSE``
    the request was sent and Bybit answered ``retCode=10006`` (or the HTTP
    status was ``429``) — the retry budget then ran out;
``LIMITER_PRE_SEND_SKIP``
    the shared limiter refused the request *before* ``send``
    (``BREAKER_OPEN``, ``SKIPPED: deadline``, ``REVOKED``,
    ``STATE_UNAVAILABLE``);
``UNKNOWN``
    neither is proven by the client attributes or the message — reported as
    unknown, never guessed.

``telemetry_result`` follows the same rule: it is taken from the client when
present, derived from an unambiguously recognised message otherwise, and
stays ``None`` when the scenario is unknown — a ``RATE_LIMIT`` response is
never labelled ``BYBIT_API_SKIP`` (review P2).

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
#: Written by ``main()``'s exception path for a cycle that died (unchanged
#: historical behaviour). Used only inside an ABORTED ``CYCLE_COMPLETION``
#: payload so the diagnostic states the real status of the run row.
RUN_STATUS_FAILED = "FAILED"

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

#: Was the HTTP request behind a rate-limit denial actually sent? (review P1)
#:
#: The state is NEVER inferred from the exception type alone — a
#: :class:`BybitRateLimitError` is raised both after a real Bybit answer and
#: before any byte leaves the process. It is taken from the structured
#: context attached by ``BybitClient``, otherwise from an unambiguously
#: recognised denial message; anything else stays ``UNKNOWN``.
#:
#: ``RATE_LIMIT_RESPONSE``
#:     the request WAS sent and Bybit answered (``retCode=10006``) or the
#:     HTTP status itself was a rate limit (``429``) — the retry budget then
#:     exhausted;
#: ``LIMITER_PRE_SEND_SKIP``
#:     the request was NEVER sent: the shared limiter denied it
#:     (``BREAKER_OPEN`` / ``SKIPPED: deadline`` / ``REVOKED`` /
#:     ``STATE_UNAVAILABLE``) before ``send`` was called;
#: ``UNKNOWN``
#:     the message proves neither (e.g. the process-local cooldown deferral
#:     does not say whether an earlier attempt of the same logical request
#:     already reached Bybit) — reported as unknown, never guessed.
REQUEST_STATE_RATE_LIMIT_RESPONSE = "RATE_LIMIT_RESPONSE"
REQUEST_STATE_PRE_SEND_SKIP = "LIMITER_PRE_SEND_SKIP"
REQUEST_STATE_UNKNOWN = "UNKNOWN"

REQUEST_STATES = (
    REQUEST_STATE_RATE_LIMIT_RESPONSE, REQUEST_STATE_PRE_SEND_SKIP,
    REQUEST_STATE_UNKNOWN,
)

#: ``limiter_result`` values that BY DEFINITION never reached the wire.
_PRE_SEND_RESULTS = frozenset({
    "BREAKER_OPEN", "SKIPPED", "REVOKED", "LIMITER_UNAVAILABLE",
    "STATE_UNAVAILABLE",
})

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


def parse_rate_limit_denial(
    message: str,
) -> tuple[str | None, str | None, str]:
    """Recover ``(result, reason, request_state)`` from a denial message.

    Used only when the exception carries no structured attributes; a match
    never invents a value the client did not report, and the third element
    is decided ONLY by what the message actually proves:

    * ``deferred by shared rate limiter (...)`` / ``grant revoked before
      send`` / ``shared rate limiter unavailable`` → the client refuses
      *before* ``send`` is called ⇒ ``LIMITER_PRE_SEND_SKIP``;
    * ``Rate limited on ... after N attempt(s)`` ⇒ attempts really happened
      ⇒ ``RATE_LIMIT_RESPONSE`` (the ``retCode`` itself may be unknown, e.g.
      HTTP 429);
    * the process-local cooldown deferral → ``UNKNOWN``: it is raised before
      *this* attempt, but the message does not say whether an earlier
      attempt of the same logical request had already reached Bybit;
    * anything unrecognised → ``UNKNOWN``.
    """
    match = _DEFERRED_RE.search(message)
    if match:
        return match.group("result"), match.group("reason"), REQUEST_STATE_PRE_SEND_SKIP
    match = _COOLDOWN_RE.search(message)
    if match:
        # The remaining cooldown time IS the operator-relevant figure here.
        return TELEMETRY_RATE_LIMIT, (
            f"process_local_cooldown:{match.group('remaining')}s"
        ), REQUEST_STATE_UNKNOWN
    if _REVOKED_RE.search(message):
        return "REVOKED", "grant_revoked", REQUEST_STATE_PRE_SEND_SKIP
    if _UNAVAILABLE_RE.search(message):
        return "LIMITER_UNAVAILABLE", "limiter_state", REQUEST_STATE_PRE_SEND_SKIP
    match = _RATE_LIMITED_RE.search(message)
    if match:
        # Budget exhausted on Bybit's own rate-limit answer (10006 / HTTP 429).
        return TELEMETRY_RATE_LIMIT, match.group(
            "reason"), REQUEST_STATE_RATE_LIMIT_RESPONSE
    # An unrecognised rate-limit message is still RATE_LIMIT-classified — it
    # never degrades to an ordinary market-data failure — but it proves
    # nothing about the transport, so the state stays UNKNOWN.
    return TELEMETRY_RATE_LIMIT, None, REQUEST_STATE_UNKNOWN


def parse_rate_limit_message(message: str) -> tuple[str | None, str | None]:
    """Backward-compatible ``(result, reason)`` view of the denial message."""
    result, reason, _request_state = parse_rate_limit_denial(message)
    return result, reason


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
    #: ``None`` when neither the client nor the message proves one (review
    #: P2: never defaulted to ``BYBIT_API_SKIP``).
    telemetry_result: str | None = None
    #: Did the request reach Bybit? ``REQUEST_STATE_*`` for a rate-limit
    #: denial, ``None`` for an ordinary failure (review P1).
    request_state: str | None = None
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
            "request_state": self.request_state,
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
        if self.request_state:
            parts.append(f"http={self.request_state}")
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

    ``request_state`` (review P1) tells the two rate-limit stories apart:

    * ``RATE_LIMIT_RESPONSE`` — a request really went out and Bybit answered
      (``10006``) or the status was ``429``;
    * ``LIMITER_PRE_SEND_SKIP`` — the shared limiter refused the request
      before it was sent;
    * ``UNKNOWN`` — neither is proven. It is never guessed from the
      exception type, and ``telemetry_result`` stays ``None`` in that case
      (review P2: no fabricated ``BYBIT_API_SKIP``).
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

    request_state: str | None = None
    if rate_limit:
        structured = any(value is not None for value in (
            result, reason, queue_wait_ms, telemetry_result, ret_code,
        ))
        parsed_state: str | None = None
        if result is None or reason is None:
            parsed_result, parsed_reason, parsed_state = parse_rate_limit_denial(
                message)
            result = result or parsed_result
            reason = reason or parsed_reason
        if result is None:
            result = TELEMETRY_RATE_LIMIT

        if telemetry_result == TELEMETRY_RATE_LIMIT:
            # BybitClient records RATE_LIMIT only for an attempt that was
            # really executed (body retCode / HTTP 429).
            request_state = REQUEST_STATE_RATE_LIMIT_RESPONSE
        elif telemetry_result == TELEMETRY_LIMITER_SKIP:
            # BybitClient records BYBIT_API_SKIP only on the pre-send deny.
            request_state = REQUEST_STATE_PRE_SEND_SKIP
        elif structured:
            if result in _PRE_SEND_RESULTS:
                request_state = REQUEST_STATE_PRE_SEND_SKIP
            elif result == TELEMETRY_RATE_LIMIT:
                request_state = REQUEST_STATE_RATE_LIMIT_RESPONSE
            else:
                request_state = REQUEST_STATE_UNKNOWN
        else:
            # Message-only: take exactly what the message proved (or
            # UNKNOWN when it proves nothing).
            request_state = parsed_state or REQUEST_STATE_UNKNOWN

        if telemetry_result is None:
            # P2: derive the label only from the proven transport fact.
            if request_state == REQUEST_STATE_PRE_SEND_SKIP:
                telemetry_result = TELEMETRY_LIMITER_SKIP
            elif request_state == REQUEST_STATE_RATE_LIMIT_RESPONSE:
                telemetry_result = TELEMETRY_RATE_LIMIT

    return SymbolOutcome(
        symbol=symbol,
        stage=stage,
        category=category,
        error_type=error_type,
        rate_limit=rate_limit,
        result=result,
        reason=reason,
        telemetry_result=telemetry_result,
        request_state=request_state,
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
    #: ``True`` once this cycle's ``CYCLE_COMPLETION`` row was persisted —
    #: at most ONE diagnostic row per cycle, even when the cycle both
    #: finished ``run_scan_cycle`` and later died in ``main()``.
    event_persisted: bool = False

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

    # -- transport fact (review P1) ----------------------------------------
    def _state_symbols(self, state: str) -> list[str]:
        return [o.symbol for o in self.outcomes if o.request_state == state]

    @property
    def rate_limit_response_symbols(self) -> list[str]:
        """Symbols whose request DID reach Bybit and was rate-limited there."""
        return self._state_symbols(REQUEST_STATE_RATE_LIMIT_RESPONSE)

    @property
    def limiter_pre_send_skip_symbols(self) -> list[str]:
        """Symbols the limiter refused BEFORE any HTTP request was sent."""
        return self._state_symbols(REQUEST_STATE_PRE_SEND_SKIP)

    @property
    def request_state_unknown_symbols(self) -> list[str]:
        """Rate-limit skips where the transport fact could not be proven."""
        return self._state_symbols(REQUEST_STATE_UNKNOWN)

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
    def to_payload(
        self,
        *,
        aborted: bool = False,
        run_status: str | None = None,
    ) -> dict[str, Any]:
        """JSONB-safe ``dds.scanner_event.payload`` document.

        ``aborted=True`` marks a cycle that died in ``main()`` (the run row
        keeps its historical ``FAILED`` status, which ``run_status`` then
        records explicitly instead of the computed value).
        """
        payload: dict[str, Any] = {
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
            # Transport fact (review P1): which skips really reached Bybit.
            "rate_limit_response_count": len(self.rate_limit_response_symbols),
            "limiter_pre_send_skip_count": len(
                self.limiter_pre_send_skip_symbols),
            "request_state_unknown_count": len(
                self.request_state_unknown_symbols),
            "rate_limit_response_symbols": self.rate_limit_response_symbols,
            "limiter_pre_send_skip_symbols": self.limiter_pre_send_skip_symbols,
            "request_state_unknown_symbols": self.request_state_unknown_symbols,
            "processed": list(self.processed),
            "outcomes": [o.to_payload() for o in self.outcomes],
        }
        if aborted:
            payload["aborted"] = True
        if run_status is not None:
            payload["run_status"] = run_status
        return payload

    def log_summary(self, *, run_status: str | None = None) -> str:
        """One operator-facing line for an incomplete (or aborted) cycle.

        ``run_status`` overrides the computed value so ``main()``'s abort
        path can print the ``FAILED`` status the run row really received.
        """
        parts = [
            f"completion_status={self.completion_status}",
            f"run_status={run_status or self.run_status}",
            f"expected={self.expected_symbols}",
            f"processed={self.processed_count}",
            f"failed={len(self.failures)}",
            f"skipped={len(self.skips)}",
            f"scan_errors={len(self.scan_failures)}",
        ]
        if any(o.rate_limit for o in self.outcomes):
            # Review P1: response-vs-pre-send, side by side in the log.
            parts.append(
                f"rate_limit_response={len(self.rate_limit_response_symbols)}")
            parts.append(
                f"limiter_pre_send_skip={len(self.limiter_pre_send_skip_symbols)}")
            parts.append(
                f"request_state_unknown={len(self.request_state_unknown_symbols)}")
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
