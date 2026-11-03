"""MANAGED OFFLINE FETCH GATE — Issue #253 (offline/manual utilities).

``BybitClient`` is the only *runtime* path to Bybit in this repository.
A handful of one-shot / manual research utilities nevertheless open their
own HTTP connection (``research_snapshot/*``), so they would otherwise be an
UNCLASSIFIED bypass of the shared request budget.

This module gives those utilities a **safe managed path** without changing
how the fetched data is interpreted or stored:

* settings come from the SAME ``load_settings()`` contract as every runtime
  consumer, anchored at the REPOSITORY ROOT rather than the process cwd — a
  configuration error RAISES (there is no silent fallback to limiter-OFF,
  which is exactly the misalignment Issue #253 removes);
* **default-off parity**: while ``global_bybit_rate_limiter_enabled`` is
  false the gate is a pure no-op (zero shared-state I/O, byte-identical
  behaviour to the pre-Issue-#253 scripts);
* when enabled, ONE guarded block == ONE logical HTTP attempt: ``acquire``
  → ``prepare_send`` → caller's request → ``record_result``, the same
  contract ``BybitClient`` uses, at P3 (bulk historical backfill must never
  outrank P0 order submission or P1 trading reads);
* a deny raises :class:`~app.exchange.bybit_client.BybitRateLimitError`
  *before* the request is sent, so the caller's existing per-symbol
  ``FAILED`` handling keeps the checkpoint where it was and the historical
  dataset is resumed later — no partial page is ever marked complete.

Nothing here changes freeze policy, checkpoints or the semantics of
historical data: it only decides whether a request may be sent.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.exchange.global_rate_limiter import VALID_OUTCOMES as _CORE_OUTCOMES

logger = logging.getLogger(__name__)

#: Bulk historical fetches are the LOWEST shared-limiter priority class.
OFFLINE_PRIORITY = "P3"

#: Repository root (``app/exchange/offline_limiter.py`` -> two levels up).
#: Settings are resolved from HERE, never from the process cwd: a raw
#: offline utility launched from an arbitrary directory must not silently
#: fall back to built-in defaults (limiter OFF) just because ``config.yaml``
#: is absent from that directory. That silent-fallback is exactly the class
#: of defect Issue #231 R3 removed from the order-reconcile CLI.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def offline_settings() -> Any:
    """Resolve settings for an offline/manual Bybit attempt.

    Repo-anchored (cwd-independent) twin of every runtime consumer's
    ``load_settings()``; a missing or broken config still raises.
    """
    from app.config import load_settings

    return load_settings(path=_REPO_ROOT / "config.yaml",
                         env_file=_REPO_ROOT / ".env")

#: Outcomes an offline attempt may record — the shared-limiter core's own
#: vocabulary, so breaker accounting is identical for every caller.
VALID_OFFLINE_OUTCOMES = frozenset(
    {"OK", "RATE_LIMIT", "TIMEOUT", "AUTH", "ERROR"} & _CORE_OUTCOMES)


class _OfflineAttempt:
    """Handle yielded for exactly one guarded HTTP attempt."""

    def __init__(self, limiter: Any, grant: Any, priority: str,
                 endpoint: str) -> None:
        self._limiter = limiter
        self._grant = grant
        self._priority = priority
        self._endpoint = endpoint
        #: Caller-settable terminal outcome (``OK``/``RATE_LIMIT``/``TIMEOUT``
        #: ``ERROR``/``AUTH``). Defaults to ``OK`` on a clean exit.
        self.outcome = "OK"

    @property
    def grant_id(self) -> str:
        return "-" if self._grant is None else self._grant.grant_id

    @property
    def waited_ms(self) -> int:
        if self._grant is None:
            return 0
        return int(getattr(self._grant, "waited_ms", 0) or 0)


def _map_exception(exc: BaseException) -> str:
    """Map an exception raised by the caller's own request onto an outcome.

    Mirrors the ``BybitClient`` outcome vocabulary so shared-limiter breaker
    accounting sees the same signal from offline utilities as from runtime
    consumers. Never swallows the exception (it is re-raised by the caller).
    """
    import socket
    from urllib.error import HTTPError, URLError

    from app.exchange.bybit_client import (
        BybitAuthError,
        BybitRateLimitError,
        BybitTimeoutError,
    )

    if isinstance(exc, BybitRateLimitError):
        return "RATE_LIMIT"
    if isinstance(exc, BybitAuthError):
        return "AUTH"
    if isinstance(exc, (BybitTimeoutError, TimeoutError, socket.timeout)):
        return "TIMEOUT"
    # Historical fetchers use requests, not urllib.
    # Keep requests optional for other consumers.
    try:
        from requests.exceptions import HTTPError as RequestsHTTPError
    except ImportError:
        RequestsHTTPError = None

    if RequestsHTTPError is not None and isinstance(exc, RequestsHTTPError):
        response = getattr(exc, "response", None)
        return (
            "RATE_LIMIT"
            if response is not None and response.status_code == 429
            else "ERROR"
        )

    if isinstance(exc, HTTPError):
        # A raised HTTP status: 429 is the rate-limit class, everything
        # else is a plain error (same delivery-independent rule as
        # BybitClient._budgeted_get, review R4-5).
        return "RATE_LIMIT" if getattr(exc, "code", None) == 429 else "ERROR"
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "TIMEOUT"
        return "ERROR"
    if isinstance(exc, OSError):
        return "ERROR"
    return "ERROR"


@contextmanager
def guarded_bybit_attempt(
    endpoint: str,
    *,
    priority: str = OFFLINE_PRIORITY,
    settings: Any = None,
    logical_request_id: str = "",
) -> Iterator[_OfflineAttempt]:
    """Guard ONE raw Bybit HTTP attempt with the shared limiter contract.

    Usage (offline/manual utility)::

        with guarded_bybit_attempt("/v5/market/kline") as attempt:
            response = requests.get(URL, params=params, timeout=30)
            payload = response.json()
            if payload.get("retCode") != 0:
                attempt.outcome = (
                    "RATE_LIMIT" if payload.get("retCode") == 10006
                    else "ERROR")

    Raises :class:`BybitRateLimitError` (fail-closed, request NOT sent) when
    the shared limiter denies the attempt or its state is unavailable.
    """
    from app.exchange.bybit_client import BybitRateLimitError
    from app.exchange.global_rate_limiter import (
        GlobalBybitRateLimiter,
        LimiterConfig,
    )

    if settings is None:
        # Repo-anchored, cwd-independent: never silently degrades to
        # built-in defaults just because the caller's cwd has no config.
        settings = offline_settings()

    config = LimiterConfig.from_settings(settings)
    if not config.enabled:
        # default-off: zero shared-state I/O, exactly the legacy behaviour.
        yield _OfflineAttempt(None, None, priority, endpoint)
        return

    limiter = GlobalBybitRateLimiter(config)
    try:
        grant = limiter.acquire(
            priority, logical_request_id=logical_request_id)
    except Exception as exc:
        raise BybitRateLimitError(
            f"offline fetch: {endpoint} shared rate limiter unavailable "
            f"({type(exc).__name__}) — not sent (fail-closed)"
        ) from exc
    if not grant.granted:
        raise BybitRateLimitError(
            f"offline fetch: {endpoint} deferred by shared rate limiter "
            f"({grant.result.value}"
            + (f": {grant.reason}" if grant.reason else "")
            + ") — not sent (fail-closed)")
    if not limiter.prepare_send(grant.grant_id):
        limiter.record_result(grant.grant_id, priority, "REVOKED")
        raise BybitRateLimitError(
            f"offline fetch: {endpoint} limiter grant revoked before send "
            "(epoch moved) — not sent (fail-closed)")

    attempt = _OfflineAttempt(limiter, grant, priority, endpoint)
    try:
        yield attempt
    except BaseException as exc:
        # Preserve an EXPLICIT caller classification: a fetcher that has
        # already seen Bybit ``retCode == 10006`` sets ``outcome =
        # "RATE_LIMIT"`` and then raises to stop the page loop. Overwriting
        # that with the generic mapping of the wrapper's own RuntimeError
        # would record provider throttling as a plain ERROR and the shared
        # breaker would never see the rate limit. Only an UNCLASSIFIED
        # attempt (still the default "OK") is mapped from the exception.
        if attempt.outcome == "OK":
            attempt.outcome = _map_exception(exc)
        raise
    finally:
        outcome = attempt.outcome
        if outcome not in VALID_OFFLINE_OUTCOMES:
            logger.warning(
                "offline fetch: unknown caller outcome %r on %s — recorded "
                "as ERROR", outcome, endpoint)
            outcome = "ERROR"
        try:
            limiter.record_result(grant.grant_id, priority, outcome)
        except Exception:      # never mask the caller's outcome
            logger.warning(
                "offline fetch: shared limiter record_result failed on %s "
                "(outcome=%s)", endpoint, outcome)


__all__ = ["OFFLINE_PRIORITY", "guarded_bybit_attempt"]
