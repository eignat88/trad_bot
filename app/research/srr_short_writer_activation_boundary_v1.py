"""SRR SHORT writer activation boundary V1.

This module defines the immutable activation boundary for persisting
prospective outcomes of exactly one experiment:

    ``SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1``

Design invariants:

- ``ACTIVATION_TS`` must be supplied explicitly, must carry an explicit
  timezone, and is normalized to UTC with millisecond precision. It can never
  be derived from ``datetime.now()`` or any other implicit clock.
- An observation is eligible for persistence only when all of the following
  hold: experiment identity matches, direction is ``SHORT``, and
  ``signal_time > ACTIVATION_TS``. Equality with the boundary is rejected.
- The boundary is evaluated *before* historical candle retrieval. A
  pre-activation observation must not trigger Bybit requests, frozen policy
  calls, or writer calls.
- A persisted activation record (from migration 062) is the authoritative
  source of the boundary once writer mode is enabled. A mismatch between the
  runtime value and the persisted record blocks writes.
- Without an explicitly authorized writer mode the boundary is inert: the
  production runner remains in ``dry_run=True`` / ``enable_writes=False`` and
  no database connection is opened by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

SRR_ACTIVATION_BOUNDARY_V1 = "SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1"
SRR_ACTIVATION_EXPERIMENT_ID = (
    "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
)
SRR_ACTIVATION_DIRECTION = "SHORT"

# Canonical storage format for ACTIVATION_TS. All comparisons use the
# normalized UTC millisecond representation derived from this format.
ACTIVATION_TS_STORAGE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class SrrActivationBoundaryError(ValueError):
    """Base error for activation boundary violations."""


class SrrActivationConfigError(SrrActivationBoundaryError):
    """ACTIVATION_TS is missing, malformed, or otherwise unusable."""


class SrrActivationRecordMismatch(SrrActivationBoundaryError):
    """Runtime ACTIVATION_TS disagrees with the persisted activation record."""


class SrrActivationRecordUnavailable(SrrActivationBoundaryError):
    """Persisted activation record lookup failed or is absent."""


class SrrPreActivationObservation(SrrActivationBoundaryError):
    """Observation is strictly before the activation boundary."""


def _parse_utc_ms(value: Any, *, field: str) -> int:
    """Parse an explicit timezone-aware timestamp into UTC milliseconds.

    Naive datetimes, booleans, and non-timestamp scalars are rejected. A
    timezone offset other than UTC is converted to UTC; the result carries
    exactly the input's calendar and clock time interpreted in UTC.
    """
    if isinstance(value, bool):
        raise SrrActivationConfigError(f"{field} must be a timestamp, not a boolean")
    if isinstance(value, int):
        raise SrrActivationConfigError(
            f"{field} must be an explicit ISO-8601 timestamp with timezone, "
            f"not a raw integer"
        )
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise SrrActivationConfigError(f"{field} must not be empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SrrActivationConfigError(f"{field} is not a valid ISO-8601 timestamp: {value!r}") from exc
    else:
        raise SrrActivationConfigError(
            f"{field} must be an explicit ISO-8601 timestamp with timezone, "
            f"got {type(value).__name__}"
        )

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SrrActivationConfigError(
            f"{field} must include an explicit timezone; naive datetimes are "
            f"rejected: {value!r}"
        )

    utc = parsed.astimezone(timezone.utc)
    return int(round(utc.timestamp() * 1000))


def normalize_activation_ts(value: Any) -> tuple[int, str]:
    """Return the normalized ``(utc_ms, canonical_text)`` pair for a boundary.

    The canonical text is the single storage format used for activation
    records and SQL parameters. Milliseconds are always present.
    """
    ms = _parse_utc_ms(value, field="ACTIVATION_TS")
    canonical = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        ACTIVATION_TS_STORAGE_FORMAT
    )
    return ms, canonical


@dataclass(frozen=True)
class SrrShortWriterActivationBoundary:
    """Immutable runtime activation boundary for SRR SHORT outcome writes."""

    activation_ts_ms: int
    activation_ts_text: str

    @classmethod
    def create(cls, activation_ts: Any) -> "SrrShortWriterActivationBoundary":
        ms, text = normalize_activation_ts(activation_ts)
        return cls(activation_ts_ms=ms, activation_ts_text=text)

    @property
    def boundary_version(self) -> str:
        return SRR_ACTIVATION_BOUNDARY_V1

    @property
    def experiment_id(self) -> str:
        return SRR_ACTIVATION_EXPERIMENT_ID

    @property
    def direction(self) -> str:
        return SRR_ACTIVATION_DIRECTION

    def is_eligible(
        self,
        *,
        experiment_id: Any,
        direction: Any,
        signal_time: Any,
    ) -> bool:
        """Return True only for observations strictly after the boundary."""
        if experiment_id != SRR_ACTIVATION_EXPERIMENT_ID:
            return False
        if direction != SRR_ACTIVATION_DIRECTION:
            return False
        try:
            signal_ms = _parse_utc_ms(signal_time, field="signal_time")
        except SrrActivationConfigError:
            return False
        return signal_ms > self.activation_ts_ms

    def require_eligible(
        self,
        *,
        experiment_id: Any,
        direction: Any,
        signal_time: Any,
        observation_id: Any = None,
    ) -> None:
        """Raise ``SrrPreActivationObservation`` when ineligible."""
        if not self.is_eligible(
            experiment_id=experiment_id,
            direction=direction,
            signal_time=signal_time,
        ):
            raise SrrPreActivationObservation(
                "SRR_PRE_ACTIVATION_OBSERVATION: "
                f"observation_id={observation_id!r} experiment_id={experiment_id!r} "
                f"direction={direction!r} signal_time={signal_time!r} "
                f"activation_ts={self.activation_ts_text!r}"
            )


def build_activation_sql_filters(boundary: SrrShortWriterActivationBoundary) -> tuple[str, tuple[Any, ...]]:
    """Return the SQL fragment and parameters for the strict eligibility filter.

    The fragment is intentionally parameterized on the normalized UTC text so
    that the database performs the comparison. Equality with the boundary is
    excluded by construction (strict ``>``).
    """
    fragment = (
        "o.experiment_id = %s "
        "AND o.direction = %s "
        "AND o.signal_time > %s::timestamptz"
    )
    params = (
        SRR_ACTIVATION_EXPERIMENT_ID,
        SRR_ACTIVATION_DIRECTION,
        boundary.activation_ts_text,
    )
    return fragment, params


class SrrShortWriterActivationGate:
    """Gate that protects every SRR SHORT persistence entry point.

    The gate is fail-closed by construction:

    - writer mode requires an explicit ``SRRShortWriterActivationMode``;
    - an unset boundary always blocks writes;
    - once a persisted activation record has been read, any mismatch with the
      runtime boundary blocks writes with an explicit diagnostic.
    """

    def __init__(
        self,
        *,
        boundary: SrrShortWriterActivationBoundary | None,
        mode: "SrrShortWriterActivationMode | None" = None,
    ) -> None:
        self._boundary = boundary
        self._mode = mode
        self._record: Mapping[str, Any] | None = None

    @property
    def boundary(self) -> SrrShortWriterActivationBoundary | None:
        return self._boundary

    @property
    def writer_enabled(self) -> bool:
        return (
            self._boundary is not None
            and self._mode is not None
            and self._mode.allows_writes
        )

    def verify_persisted_record(self, record: Mapping[str, Any] | None) -> None:
        """Compare the runtime boundary against a persisted activation record.

        A missing record or any mismatch raises; only an exact match clears
        the gate for writer-mode operation.
        """
        if self._boundary is None:
            raise SrrActivationConfigError(
                "SRR activation boundary is not configured"
            )
        if not record:
            raise SrrActivationRecordUnavailable(
                "SRR activation record is absent; writer mode is blocked"
            )
        record_text = str(record.get("activation_ts", "") or "")
        if not record_text:
            raise SrrActivationRecordUnavailable(
                "SRR activation record has no activation_ts; writer mode is blocked"
            )
        try:
            record_ms, _ = normalize_activation_ts(record_text)
        except SrrActivationConfigError as exc:
            raise SrrActivationRecordUnavailable(
                f"SRR activation record activation_ts is invalid: {record_text!r}"
            ) from exc
        if record_ms != self._boundary.activation_ts_ms:
            raise SrrActivationRecordMismatch(
                "SRR activation record mismatch: "
                f"record={record_text!r} runtime={self._boundary.activation_ts_text!r}"
            )
        experiment = str(record.get("experiment_id", "") or "")
        if experiment != SRR_ACTIVATION_EXPERIMENT_ID:
            raise SrrActivationRecordMismatch(
                "SRR activation record experiment mismatch: "
                f"record={experiment!r}"
            )
        version = str(record.get("boundary_version", "") or "")
        if version != SRR_ACTIVATION_BOUNDARY_V1:
            raise SrrActivationRecordMismatch(
                "SRR activation record version mismatch: "
                f"record={version!r} expected={SRR_ACTIVATION_BOUNDARY_V1!r}"
            )
        self._record = dict(record)

    def assert_writer_allowed(self) -> None:
        """Raise unless writer mode is explicitly enabled and verified."""
        if self._mode is None or not self._mode.allows_writes:
            raise SrrActivationBoundaryError(
                "SRR_SHORT_WRITER_DISABLED: explicit activation authorization required"
            )
        if self._boundary is None:
            raise SrrActivationConfigError(
                "SRR activation boundary is not configured"
            )
        if self._record is None:
            raise SrrActivationRecordUnavailable(
                "SRR activation record has not been verified; writer mode is blocked"
            )


@dataclass(frozen=True)
class SrrShortWriterActivationMode:
    """Explicit writer-mode flag. Default is disabled (observe-only)."""

    enabled: bool

    @property
    def allows_writes(self) -> bool:
        return bool(self.enabled)


OBSERVE_ONLY_MODE = SrrShortWriterActivationMode(enabled=False)
