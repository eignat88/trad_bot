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

# Activation record lifecycle states.
ACTIVATION_STATUS_PENDING = "PENDING"
ACTIVATION_STATUS_ACTIVE = "ACTIVE"
ACTIVATION_STATUS_REVOKED = "REVOKED"
ACTIVATION_VALID_STATUSES = (
    ACTIVATION_STATUS_PENDING,
    ACTIVATION_STATUS_ACTIVE,
    ACTIVATION_STATUS_REVOKED,
)

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


class SrrActivationRecordNotActive(SrrActivationBoundaryError):
    """Persisted activation record exists but is not in ``ACTIVE`` status."""


class SrrActivationRecordImmutableViolation(SrrActivationBoundaryError):
    """An immutable activation record field was changed."""


class SrrActivationInvalidTransition(SrrActivationBoundaryError):
    """Requested activation record status transition is not permitted."""


class SrrPreActivationObservation(SrrActivationBoundaryError):
    """Observation is strictly before the activation boundary."""


def _parse_utc_ms(value: Any, *, field: str) -> int:
    """Parse an explicit timezone-aware timestamp into UTC milliseconds.

    Delegates to ``_parse_utc_ns`` and truncates toward the past. A
    timestamp 1µs after the boundary stays strictly after it; a timestamp
    1µs before stays strictly before it. No rounding can flip the side.
    """
    return _parse_utc_ns(value, field=field) // 1_000_000


def normalize_activation_ts(value: Any) -> tuple[int, str]:
    """Return the normalized ``(utc_ns, canonical_text)`` pair for a boundary.

    The canonical text is the single storage format used for activation
    records and SQL parameters. Nanosecond precision is preserved so that
    PostgreSQL ``timestamptz`` (6-digit fractional seconds) round-trips
    without loss. When the value has only microsecond precision the
    canonical text is emitted with 6 fractional digits (PostgreSQL-native).
    """
    ns = _parse_utc_ns(value, field="ACTIVATION_TS")
    whole_seconds = ns // 1_000_000_000
    ns_remainder = ns % 1_000_000_000

    base_dt = datetime.fromtimestamp(whole_seconds, tz=timezone.utc)

    if ns_remainder == 0:
        # No fractional part: emit with zero microseconds for uniformity.
        canonical = base_dt.strftime(ACTIVATION_TS_STORAGE_FORMAT)
    elif ns_remainder % 1000 == 0:
        # Microsecond precision: emit exactly 6 fractional digits (PG-native).
        micro = ns_remainder // 1000
        canonical = base_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{micro:06d}Z"
    else:
        # Sub-microsecond precision: emit 9 fractional digits (full ns).
        canonical = base_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ns_remainder:09d}Z"

    return ns, canonical


def _parse_utc_ns(value: Any, *, field: str) -> int:
    """Parse an explicit timezone-aware timestamp into UTC nanoseconds.

    Preserves full nanosecond precision (PostgreSQL ``timestamptz`` stores
    6-digit fractional seconds = microsecond; Python ``datetime`` stores
    microsecond). Sub-microsecond values in ISO-8601 strings (e.g. 9 digits
    after the decimal point) are truncated toward the past, never rounded up.
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
        raw_text: str | None = None
    elif isinstance(value, str):
        raw_text = value.strip()
        if not raw_text:
            raise SrrActivationConfigError(f"{field} must not be empty")
        try:
            parsed = datetime.fromisoformat(raw_text.replace("Z", "+00:00"))
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

    # Capture nanosecond precision from the raw string if available.
    # Python's fromisoformat truncates sub-microsecond digits; re-extract
    # them from the original text so the boundary comparison is exact.
    frac_ns: int | None = None
    if raw_text is not None:
        frac_ns = _extract_fraction_ns(raw_text)

    utc = parsed.astimezone(timezone.utc)
    seconds = int(utc.timestamp())
    micro = utc.microsecond

    if frac_ns is not None:
        # Replace the microsecond-derived sub-nanosecond digits with the
        # raw string's exact fraction. Truncation toward the past already
        # happened in ``_extract_fraction_ns``.
        ns_total = seconds * 1_000_000_000 + frac_ns
    else:
        ns_total = seconds * 1_000_000_000 + micro * 1000

    return ns_total


def _extract_fraction_ns(text: str) -> int | None:
    """Extract the fractional-second portion of an ISO-8601 string as ns.

    Returns nanoseconds after the whole second, or ``None`` if the string
    has no fractional part. Truncates (does not round) digits beyond
    nanosecond precision.
    """
    import re

    # Strip the timezone suffix for fraction extraction.
    tz_match = re.search(r"[+-]\d{2}:?\d{2}$|Z$", text)
    base = text[: tz_match.start()] if tz_match else text
    frac_match = re.search(r"\.(\d+)", base)
    if not frac_match:
        return None
    frac_digits = frac_match.group(1)
    # Truncate to 9 digits (nanoseconds); do not round up.
    frac_padded = (frac_digits + "0" * 9)[:9]
    return int(frac_padded)


# ── Activation record state machine ─────────────────────────────
# The only permitted transitions are PENDING → ACTIVE → REVOKED.
# A direct PENDING → REVOKED transition (cancel-before-activation) is
# explicitly permitted; see module docstring for the rationale.
ACTIVATION_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    ACTIVATION_STATUS_PENDING: (ACTIVATION_STATUS_ACTIVE, ACTIVATION_STATUS_REVOKED),
    ACTIVATION_STATUS_ACTIVE: (ACTIVATION_STATUS_REVOKED,),
    ACTIVATION_STATUS_REVOKED: (),  # terminal; no reactivation
}

# Immutable fields that may never change after the initial INSERT.
ACTIVATION_IMMUTABLE_FIELDS = (
    "experiment_id",
    "direction",
    "boundary_version",
    "activation_ts",
)


def validate_activation_transition(current: str, target: str) -> None:
    """Raise ``SrrActivationInvalidTransition`` for a disallowed transition.

    Self-transitions (``current == target``) are permitted as a no-op so
    that idempotent status updates do not fail.
    """
    if current == target:
        return
    allowed = ACTIVATION_ALLOWED_TRANSITIONS.get(current)
    if allowed is None:
        raise SrrActivationInvalidTransition(
            f"SRR activation status {current!r} is not a known state"
        )
    if target not in allowed:
        raise SrrActivationInvalidTransition(
            f"SRR activation transition {current!r} → {target!r} is not permitted; "
            f"allowed from {current!r}: {list(allowed)!r}"
        )


def validate_activation_record_immutability(
    original: Mapping[str, Any],
    updated: Mapping[str, Any],
) -> None:
    """Raise ``SrrActivationRecordImmutableViolation`` if any immutable field changed.

    ``activation_ts`` may never change after INSERT, regardless of status.
    ``experiment_id``, ``direction``, and ``boundary_version`` may never change.
    Only ``status``, ``notes``, ``updated_at``, and lifecycle timestamps may
    be modified.
    """
    for field in ACTIVATION_IMMUTABLE_FIELDS:
        old_value = original.get(field)
        new_value = updated.get(field)
        if field == "activation_ts":
            # Compare as normalized UTC ms to tolerate format differences.
            try:
                old_ms, _ = normalize_activation_ts(old_value)
            except SrrActivationConfigError as exc:
                raise SrrActivationRecordImmutableViolation(
                    f"original activation_ts is invalid: {old_value!r}"
                ) from exc
            try:
                new_ms, _ = normalize_activation_ts(new_value)
            except SrrActivationConfigError as exc:
                raise SrrActivationRecordImmutableViolation(
                    f"updated activation_ts is invalid: {new_value!r}"
                ) from exc
            if old_ms != new_ms:
                raise SrrActivationRecordImmutableViolation(
                    f"activation_ts is immutable and cannot change: "
                    f"original={old_value!r} updated={new_value!r}"
                )
        else:
            if str(old_value or "") != str(new_value or ""):
                raise SrrActivationRecordImmutableViolation(
                    f"{field} is immutable and cannot change: "
                    f"original={old_value!r} updated={new_value!r}"
                )


@dataclass(frozen=True)
class SrrShortWriterActivationBoundary:
    """Immutable runtime activation boundary for SRR SHORT outcome writes."""

    activation_ts_ns: int
    activation_ts_text: str

    @classmethod
    def create(cls, activation_ts: Any) -> "SrrShortWriterActivationBoundary":
        ns, text = normalize_activation_ts(activation_ts)
        return cls(activation_ts_ns=ns, activation_ts_text=text)

    @property
    def activation_ts_ms(self) -> int:
        """UTC milliseconds since the epoch (truncated toward the past)."""
        return self.activation_ts_ns // 1_000_000

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
        """Return True only for observations strictly after the boundary.

        The comparison is performed at nanosecond precision so that a
        timestamp exactly at the boundary is rejected and a timestamp 1µs
        after the boundary is accepted. No rounding can flip the side.
        """
        if experiment_id != SRR_ACTIVATION_EXPERIMENT_ID:
            return False
        if direction != SRR_ACTIVATION_DIRECTION:
            return False
        try:
            signal_ns = _parse_utc_ns(signal_time, field="signal_time")
        except SrrActivationConfigError:
            return False
        return signal_ns > self.activation_ts_ns

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
        # Status of the record as last verified; ``assert_writer_allowed``
        # re-checks this at call time so a revoked record blocks writes even
        # if the in-memory copy has not been refreshed.
        self._record_status: str | None = None

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

        A missing record, a non-ACTIVE status, or any mismatch raises; only
        an exact match on ``ACTIVE`` clears the gate for writer-mode
        operation.
        """
        if self._boundary is None:
            raise SrrActivationConfigError(
                "SRR activation boundary is not configured"
            )
        if not record:
            raise SrrActivationRecordUnavailable(
                "SRR activation record is absent; writer mode is blocked"
            )

        status = str(record.get("status", "") or "")
        if status != ACTIVATION_STATUS_ACTIVE:
            raise SrrActivationRecordNotActive(
                "SRR activation record is not ACTIVE; writer mode is blocked: "
                f"status={status!r}"
            )

        record_text = str(record.get("activation_ts", "") or "")
        if not record_text:
            raise SrrActivationRecordUnavailable(
                "SRR activation record has no activation_ts; writer mode is blocked"
            )
        try:
            record_ns, _ = normalize_activation_ts(record_text)
        except SrrActivationConfigError as exc:
            raise SrrActivationRecordUnavailable(
                f"SRR activation record activation_ts is invalid: {record_text!r}"
            ) from exc
        if record_ns != self._boundary.activation_ts_ns:
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
        direction = str(record.get("direction", "") or "")
        if direction != SRR_ACTIVATION_DIRECTION:
            raise SrrActivationRecordMismatch(
                "SRR activation record direction mismatch: "
                f"record={direction!r}"
            )
        version = str(record.get("boundary_version", "") or "")
        if version != SRR_ACTIVATION_BOUNDARY_V1:
            raise SrrActivationRecordMismatch(
                "SRR activation record version mismatch: "
                f"record={version!r} expected={SRR_ACTIVATION_BOUNDARY_V1!r}"
            )
        self._record = dict(record)
        self._record_status = status

    @property
    def verified_record_status(self) -> str | None:
        """Status of the last verified record, or ``None`` if not verified."""
        return self._record_status

    def load_authoritative_record(self, conn: Any) -> Mapping[str, Any] | None:
        """Load the activation record from PostgreSQL and verify it.

        This is the ONLY trusted source for the writer's authorization.
        A caller-supplied record dict can be forged; a row read inside the
        writer's own transaction cannot. Returns ``None`` when no row exists
        or the read fails; raises on any mismatch with the runtime boundary.

        The row is locked with ``FOR SHARE`` inside the caller's
        transaction. This holds a shared row lock on the activation record
        until the writer's transaction commits or rolls back. A concurrent
        ``ACTIVE → REVOKED`` UPDATE must therefore wait for the writer to
        finish, guaranteeing that the writer either sees the record before
        the revoke or after it — never a torn read. The lock is released on
        every commit and rollback path of the outcome write.
        """
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT experiment_id, direction, boundary_version,
                           activation_ts, status
                    FROM research.srr_short_writer_activation
                    WHERE experiment_id = %s
                    FOR SHARE
                    """,
                    (SRR_ACTIVATION_EXPERIMENT_ID,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        except Exception as exc:
            raise SrrActivationRecordUnavailable(
                "SRR activation record read failed; writer mode is blocked"
            ) from exc

        if row is None:
            return None

        record = {
            "experiment_id": row[0],
            "direction": row[1],
            "boundary_version": row[2],
            "activation_ts": row[3],
            "status": row[4],
        }
        self.verify_persisted_record(record)
        return record

    def assert_writer_allowed(self) -> None:
        """Raise unless writer mode is explicitly enabled and verified.

        Re-checks the record status at call time: a record that has been
        revoked or has fallen out of ACTIVE state since verification blocks
        the write.
        """
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
        current_status = self._record_status
        if current_status != ACTIVATION_STATUS_ACTIVE:
            raise SrrActivationRecordNotActive(
                "SRR activation record is no longer ACTIVE; writer mode is blocked: "
                f"status={current_status!r}"
            )


@dataclass(frozen=True)
class SrrShortWriterActivationMode:
    """Explicit writer-mode flag. Default is disabled (observe-only)."""

    enabled: bool

    @property
    def allows_writes(self) -> bool:
        return bool(self.enabled)


OBSERVE_ONLY_MODE = SrrShortWriterActivationMode(enabled=False)
