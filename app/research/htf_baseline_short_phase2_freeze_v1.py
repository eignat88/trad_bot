"""Issue #176 Phase-2 independent freeze boundary (Blocker B2).

A Phase-2 experiment has its own freeze boundary, independent of every existing
frozen experiment (in particular the Point-B prospective studies, whose freeze
timestamps must never be reused). The freeze
timestamp is chosen by the operator at a future authorized activation; this
module never invents, reserves or writes one.

Enforced rules (all fail-closed):

* the freeze must exist and the protocol, registry and database copies must
  agree exactly — a missing or mismatched freeze rejects everything;
* ``signal_time > freeze_ts`` STRICT — ``<=`` is rejected;
* ``first_touch_time > freeze_ts`` STRICT — an episode whose first touch is at
  or before the freeze is a pre-freeze (potentially historically known) event
  and is rejected even if its confirmation lands after the freeze;
* warmup-origin episodes are never eligible;
* episodes restored from a checkpoint created before the freeze are rejected
  (``resumed_pre_freeze``).

Pure logic only: no I/O. The runner supplies the three freeze sources.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

REASON_OK = "OK"
REASON_MISSING_FREEZE = "MISSING_FREEZE"
REASON_FREEZE_MISMATCH = "FREEZE_MISMATCH"
REASON_SIGNAL_NOT_AFTER_FREEZE = "SIGNAL_NOT_AFTER_FREEZE"
REASON_FIRST_TOUCH_NOT_AFTER_FREEZE = "FIRST_TOUCH_NOT_AFTER_FREEZE"
REASON_WARMUP_ORIGIN_EPISODE = "WARMUP_ORIGIN_EPISODE"
REASON_RESUMED_PRE_FREEZE_EPISODE = "RESUMED_PRE_FREEZE_EPISODE"


class FreezeMismatchError(ValueError):
    """Raised when freeze sources are missing or disagree. Fail-closed."""


def parse_ts_ms(value) -> int:
    """Parse an ISO-8601 UTC timestamp (or int ms) into epoch milliseconds."""
    if isinstance(value, bool):
        raise ValueError("timestamp must not be boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return int(value.timestamp() * 1000)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    raise ValueError(f"unsupported timestamp type {type(value).__name__}")


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str

    def __bool__(self) -> bool:  # fail-closed ergonomics
        return self.eligible


@dataclass(frozen=True)
class Phase2FreezeBoundary:
    experiment_id: str
    freeze_ts_ms: int
    protocol_fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        experiment_id: str,
        protocol_freeze_ts,
        registry_freeze_ts,
        database_freeze_ts,
        protocol_fingerprint: str,
    ) -> "Phase2FreezeBoundary":
        """Build a boundary only when all three sources agree exactly."""
        if not experiment_id:
            raise FreezeMismatchError("experiment_id required")
        if not protocol_fingerprint or len(protocol_fingerprint) != 64:
            raise FreezeMismatchError("protocol fingerprint required")
        sources = {
            "protocol": protocol_freeze_ts,
            "registry": registry_freeze_ts,
            "database": database_freeze_ts,
        }
        missing = [name for name, value in sources.items() if value in (None, "")]
        if missing:
            raise FreezeMismatchError(
                f"missing freeze source(s): {sorted(missing)} — fail closed"
            )
        parsed = {name: parse_ts_ms(value) for name, value in sources.items()}
        if len(set(parsed.values())) != 1:
            raise FreezeMismatchError(
                f"freeze source mismatch {sorted(parsed.items())} — fail closed"
            )
        return cls(
            experiment_id=experiment_id,
            freeze_ts_ms=parsed["protocol"],
            protocol_fingerprint=protocol_fingerprint,
        )

    def check_signal_time(self, signal_time) -> EligibilityDecision:
        try:
            signal_ms = parse_ts_ms(signal_time)
        except ValueError:
            return EligibilityDecision(False, REASON_SIGNAL_NOT_AFTER_FREEZE)
        if signal_ms <= self.freeze_ts_ms:
            return EligibilityDecision(False, REASON_SIGNAL_NOT_AFTER_FREEZE)
        return EligibilityDecision(True, REASON_OK)

    def check_first_touch(self, first_touch_time) -> EligibilityDecision:
        try:
            touch_ms = parse_ts_ms(first_touch_time)
        except ValueError:
            return EligibilityDecision(False, REASON_FIRST_TOUCH_NOT_AFTER_FREEZE)
        if touch_ms <= self.freeze_ts_ms:
            return EligibilityDecision(False, REASON_FIRST_TOUCH_NOT_AFTER_FREEZE)
        return EligibilityDecision(True, REASON_OK)

    def check_event(
        self,
        *,
        signal_time,
        first_touch_time,
        warmup_origin: bool = False,
        resumed_pre_freeze: bool = False,
    ) -> EligibilityDecision:
        """Full eligibility gate for one emitted event."""
        if warmup_origin:
            return EligibilityDecision(False, REASON_WARMUP_ORIGIN_EPISODE)
        if resumed_pre_freeze:
            return EligibilityDecision(False, REASON_RESUMED_PRE_FREEZE_EPISODE)
        decision = self.check_first_touch(first_touch_time)
        if not decision:
            return decision
        return self.check_signal_time(signal_time)


def reject_all(reason: str = REASON_MISSING_FREEZE) -> EligibilityDecision:
    """Fail-closed decision used when no valid boundary exists."""
    return EligibilityDecision(False, reason)
