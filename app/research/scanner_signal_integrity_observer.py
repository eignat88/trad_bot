"""Scanner Signal Integrity adapter — Issue #322 (ME_SHORT-scoped).

Why a separate module
---------------------
``app/research/prospective_observer.py`` is protected by the frozen HTF Point-B
contract (see ``tests/test_htf_baseline_short_phase2_isolation_v1.py``): it must
stay byte-identical to the audited base apart from the single documented PR #193
one-line replacement.  Issue #322 therefore adds its signal-identity logic in a
SUBCLASS adapter instead of editing the frozen observer.

What this adapter does
----------------------
``ScannerSignalIntegrityObserver`` extends ``ProspectiveOOSObserver`` and:

* derives a deterministic, restart-stable SHA-256 identity from
  ``(scanner_name, symbol, direction, entry_timeframe, signal_candle_open_time)``
  for the ``MOMENTUM_EXHAUSTION`` / ``SHORT`` family only;
* fails CLOSED (captures nothing) when that family's candle lineage is missing
  or invalid — an unknown signal candle can never mint a fresh identity;
* carries the candle lineage through an isolated :class:`contextvars.ContextVar`
  that is always reset in a ``finally`` block, so identity can never leak across
  sequential or concurrent ``observe()`` calls;
* leaves EVERY other scanner family on the frozen base behaviour: the original
  legacy identity and NO added fail-closed.  HTF Point-B (``_make_htf_source_key``)
  and ME Reverse Long V3 (``v3_source_key``) keep their own specialized identity
  contracts, computed inside the frozen base.

Scope boundary — PARTIAL_SCOPE vs Issue #322
-------------------------------------------
Issue #322 is a general "Scanner signal integrity" task.  This change applies the
new candle identity ONLY to ``MOMENTUM_EXHAUSTION`` / ``SHORT`` (see task
boundary B).  Every other scanner family — SRR, HTF, ME Reverse Long V3, breakout
retest, VC, LR, FVG, trend pullback, … — keeps its existing identity and capture
semantics.  Those families are therefore NOT yet covered by the new stable
identity and remain part of the broader Issue #322 follow-up.  This adapter must
be reported as PARTIAL_SCOPE, not as a complete resolution of Issue #322.

Determinism / BIGINT range
--------------------------
The digest is folded into ``[-(2**62 - 2**31), -1]``-bounded negative values that
sit strictly below ``-2**31``.  This keeps new candle identities disjoint from the
legacy 31-bit identity range while remaining inside a signed PostgreSQL BIGINT
(``-2**63 .. 2**63-1``).  SHA-256 makes the value stable across process restarts
(no ``PYTHONHASHSEED`` dependence).
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.research.prospective_observer import ProspectiveOOSObserver

logger = logging.getLogger(__name__)

#: The ONLY scanner family that receives the new deterministic candle identity.
_ME_SHORT_SCANNER = "MOMENTUM_EXHAUSTION"
_ME_SHORT_DIRECTION = "SHORT"


@dataclass(frozen=True)
class _CandleLineage:
    """Immutable candle lineage carried for the duration of one ``observe()``."""

    entry_timeframe: str
    signal_candle_open_time: int


#: Isolated per-call context.  A value is set only for the duration of a single
#: scoped ``observe()`` call and reset in ``finally``.  Because ``contextvars``
#: are context-local (per thread / per asyncio task), concurrent calls can never
#: observe each other's lineage.
_CANDLE_LINEAGE: ContextVar["_CandleLineage | None"] = ContextVar(
    "issue322_candle_lineage", default=None
)


class ScannerSignalIntegrityObserver(ProspectiveOOSObserver):
    """Prospective OOS observer with ME_SHORT-scoped stable candle identity.

    The frozen :class:`ProspectiveOOSObserver` is never modified.  Non-ME_SHORT
    families are delegated straight to the frozen base so their behaviour is bit
    for bit unchanged.
    """

    # -- deterministic candle identity -------------------------------------
    @staticmethod
    def _make_candle_source_key(
        scanner_name: str,
        symbol: str,
        direction: str,
        entry_timeframe: str,
        signal_candle_open_time: int | None,
    ) -> int | None:
        """Restart-stable ME_SHORT identity for a known signal candle.

        Returns ``None`` (fail-closed) when the candle lineage is missing or
        invalid.  The versioned ``issue322:candle:v1`` namespace prevents
        accidental reuse of legacy identities.
        """
        if (
            type(signal_candle_open_time) is not int
            or signal_candle_open_time <= 0
            or not all(
                isinstance(value, str) and value.strip()
                for value in (scanner_name, symbol, direction, entry_timeframe)
            )
        ):
            return None

        payload = json.dumps(
            [
                "issue322:candle:v1",
                scanner_name,
                symbol,
                direction,
                entry_timeframe,
                signal_candle_open_time,
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        digest = hashlib.sha256(payload).digest()
        # Keep new candle IDs outside the legacy/HTF 31-bit range while staying
        # inside a signed PostgreSQL BIGINT.
        value = int.from_bytes(digest[:8], "big") % (2**62 - 2**31)
        return -(2**31 + 1 + value)

    @staticmethod
    def _is_me_short_scope(scanner_name: str, direction: str) -> bool:
        """True only for the ``MOMENTUM_EXHAUSTION`` / ``SHORT`` family."""
        return (
            scanner_name == _ME_SHORT_SCANNER and direction == _ME_SHORT_DIRECTION
        )

    # -- source-key dispatch ----------------------------------------------
    def _make_source_key(
        self, scanner_name: str, symbol: str, direction: str, signal_time: Any
    ) -> int:
        """Use the ME_SHORT candle identity when scoped; else legacy identity.

        The candle lineage is carried through the isolated ContextVar set by
        :meth:`observe` for the duration of one call (reset in ``finally``), so
        this override can never leak identity across calls.
        """
        lineage = _CANDLE_LINEAGE.get()
        if lineage is not None:
            key = self._make_candle_source_key(
                scanner_name,
                symbol,
                direction,
                lineage.entry_timeframe,
                lineage.signal_candle_open_time,
            )
            if key is not None:
                return key
        # Out of scope (or no lineage): preserve the frozen base identity.
        return super()._make_source_key(scanner_name, symbol, direction, signal_time)

    # -- capture entry point ----------------------------------------------
    def observe(
        self,
        scanner_name: str,
        direction: str,
        symbol: str,
        signal_time: Any,
        reference_price: float,
        invalidation_price: float | None,
        target_1: float | None,
        target_2: float | None,
        score: float,
        features: dict,
        parameters: dict,
        market_regime: str | None,
        detection_result: Any | None = None,
        entry_timeframe: str | None = None,
        signal_candle_open_time: int | None = None,
    ) -> list[int]:
        """Capture a candidate, adding ME_SHORT candle identity when in scope.

        For every family other than ``MOMENTUM_EXHAUSTION`` / ``SHORT`` this is a
        pure pass-through to the frozen base observer (no new identity, no added
        fail-closed).  For the ME_SHORT family a complete candle lineage is
        required; an incomplete lineage fails closed and captures nothing.
        """
        if not self._is_me_short_scope(scanner_name, direction):
            # Non-ME_SHORT families: unchanged frozen-base behaviour.
            return super().observe(
                scanner_name,
                direction,
                symbol,
                signal_time,
                reference_price,
                invalidation_price,
                target_1,
                target_2,
                score,
                features,
                parameters,
                market_regime,
                detection_result=detection_result,
            )

        # ME_SHORT scope: a known signal candle is mandatory (fail-closed).
        key = self._make_candle_source_key(
            scanner_name,
            symbol,
            direction,
            entry_timeframe,
            signal_candle_open_time,
        )
        if key is None:
            self._stats["me_short_fail_closed"] = (
                self._stats.get("me_short_fail_closed", 0) + 1
            )
            logger.info(
                "scanner signal integrity: ME_SHORT fail-closed (missing candle "
                "lineage) for %s %s", scanner_name, symbol,
            )
            return []

        token = _CANDLE_LINEAGE.set(
            _CandleLineage(entry_timeframe, signal_candle_open_time)
        )
        try:
            return super().observe(
                scanner_name,
                direction,
                symbol,
                signal_time,
                reference_price,
                invalidation_price,
                target_1,
                target_2,
                score,
                features,
                parameters,
                market_regime,
                detection_result=detection_result,
            )
        finally:
            # Mandatory cleanup: the lineage must never survive this call.
            _CANDLE_LINEAGE.reset(token)
