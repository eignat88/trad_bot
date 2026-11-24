"""Issue #296: isolated prospective source observer.

Research only. No trading, scanner or Bybit API calls.

Review remediation (PR #303):

* the source integrity audit runs periodically inside the regular runtime
  contour (not only as a standalone tool);
* an invalid source event is fail-closed: it is recorded in the rejection
  ledger with a deterministic ``reason_code``, never enters the eligible
  cohort, and the research contour transitions to ``BLOCKED`` — strictly
  limited to this new observer (activation is untouched, its write path
  belongs to a separate administrative contour);
* ``SOURCE_VALIDATED`` and a final OOS verdict are never issued while
  source integrity is violated or the upstream capture completeness is
  unproven (:func:`app.research.me_entry_quality_v1.integrity.verdict_gate`);
* eventual consistency is honoured: unprocessed events younger than the
  coverage grace window are ``COVERAGE_LAG``, not ``COVERAGE_GAP``.

Escalation rules (explicit, deterministic):

=========================================  =============================
Condition                                Action
=========================================  =============================
audit ``INTEGRITY_CONFLICT``               BLOCK contour immediately
audit ``COVERAGE_GAP`` (stale unprocessed) BLOCK contour immediately
audit ``COVERAGE_LAG`` (fresh unprocessed) log + continue capture
audit ``SOURCE_COVERAGE_UNKNOWN``          log + continue capture
any invalid source event in a batch        record rejections, admit
                                           nothing, BLOCK contour
``SourceIntegrityError`` on save           BLOCK contour (conflict code)
=========================================  =============================

The ``BLOCKED`` state is sticky and recorded in the append-only
``observer_state_event`` ledger. Recovery is manual and administrative
(``deploy/sql/296_observer_recovery.sql``) and never resets ``freeze_ts``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from .contracts import (
    SourceIntegrityError,
    SourceValidationError,
    ValidatedSourceEvent,
    as_utc,
    normalize_reason_code,
    validate_source_event,
)
from .integrity import (
    AUDIT_COVERAGE_GAP,
    AUDIT_INTEGRITY_CONFLICT,
    DEFAULT_GAP_GRACE_SECONDS,
    verdict_gate,
)

LOG = logging.getLogger("me_eq296.observer")

# Contour state values (append-only state ledger).
STATE_BLOCKED = "BLOCKED"
STATE_RESUMED = "RESUMED"

# Deterministic block reason codes.
BLOCK_INVALID_SOURCE_EVENT = "INVALID_SOURCE_EVENT"


class SourceRepository(Protocol):
    """Storage interface implemented by the PostgreSQL adapter."""

    def load_activation(self) -> Mapping[str, Any] | None:
        ...

    def load_contour_state(self) -> Mapping[str, Any] | None:
        ...

    def fetch_pending_events(
        self,
        freeze_ts: datetime,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        ...

    def save_event(
        self,
        event: ValidatedSourceEvent,
    ) -> str:
        ...

    def mark_invalid(
        self,
        event_key: str,
        reason_code: str,
    ) -> None:
        ...

    def record_state(
        self,
        state: str,
        reason_code: str,
        details: Mapping[str, Any],
    ) -> None:
        ...

    def record_health(
        self,
        status: str,
        source_rows_seen: int,
        events_inserted: int,
        diagnostics: Mapping[str, Any],
    ) -> None:
        ...

    def audit_source_integrity(
        self,
        *,
        gap_grace_seconds: int,
        as_of: Any = None,
    ) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class ObserverResult:
    status: str
    source_rows_seen: int = 0
    events_inserted: int = 0
    duplicates: int = 0
    invalid: int = 0
    blocked_reason: str | None = None
    audit_status: str | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


class EntryQualityObserver:
    """Independent prospective-only event consumer."""

    def __init__(
        self,
        repository: SourceRepository,
        *,
        batch_limit: int = 500,
        audit_interval_seconds: float = 300.0,
        coverage_gap_grace_seconds: int = DEFAULT_GAP_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if type(batch_limit) is not int or batch_limit <= 0:
            raise ValueError("INVALID_BATCH_LIMIT")

        if audit_interval_seconds <= 0:
            raise ValueError("INVALID_AUDIT_INTERVAL")

        if (
            type(coverage_gap_grace_seconds) is not int
            or coverage_gap_grace_seconds < 0
        ):
            raise ValueError("INVALID_GAP_GRACE")

        self.repository = repository
        self.batch_limit = batch_limit
        self.audit_interval_seconds = float(audit_interval_seconds)
        self.coverage_gap_grace_seconds = coverage_gap_grace_seconds
        self._clock = clock
        self._last_audit_at: float | None = None
        self._last_audit: Mapping[str, Any] | None = None

    # -- transaction / lock plumbing ---------------------------------

    def run_once(self) -> ObserverResult:
        acquire = getattr(
            self.repository, "acquire_processing_lock", None
        )
        rollback = getattr(
            self.repository, "rollback", None
        )

        if acquire is not None:
            try:
                if not acquire():
                    if rollback is not None:
                        rollback()
                    return ObserverResult(status="ALREADY_RUNNING")
            except Exception:
                if rollback is not None:
                    rollback()
                raise

        try:
            result = self._run_once_locked()
            if result.status in (
                "NOT_ACTIVATED",
                "NOT_RUNNING",
                "MISSING_FREEZE_TS",
                "BLOCKED",
            ):
                if rollback is not None:
                    rollback()
            return result
        except Exception:
            if rollback is not None:
                rollback()
            raise

    # -- main cycle --------------------------------------------------

    def _run_once_locked(self) -> ObserverResult:
        activation = self.repository.load_activation()

        if not activation:
            return ObserverResult(status="NOT_ACTIVATED")

        if activation.get("status") != "RUNNING":
            return ObserverResult(status="NOT_RUNNING")

        freeze_raw = activation.get("freeze_ts")

        if freeze_raw is None:
            return ObserverResult(status="MISSING_FREEZE_TS")

        freeze_ts = as_utc(freeze_raw)

        # A previously blocked contour never captures again before an
        # explicit administrative recovery (fail-closed, sticky).
        state = self.repository.load_contour_state()

        if state is not None and state.get("state") == STATE_BLOCKED:
            return ObserverResult(
                status="BLOCKED",
                blocked_reason=str(state.get("reason_code") or ""),
                audit_status=(
                    str(self._last_audit.get("status"))
                    if self._last_audit
                    else None
                ),
                diagnostics={
                    "contour_state": dict(state),
                },
            )

        # Periodic source integrity / coverage audit (read-only).
        audit = self._maybe_audit()

        if audit is not None:
            audit_status = str(audit.get("status") or "")

            if audit_status in (AUDIT_INTEGRITY_CONFLICT, AUDIT_COVERAGE_GAP):
                return self._block(
                    reason=audit_status,
                    details={
                        "trigger": "PERIODIC_AUDIT",
                        "audit": dict(audit),
                    },
                    audit=audit,
                )

        rows = self.repository.fetch_pending_events(
            freeze_ts,
            self.batch_limit,
        )

        # Validation pass. A batch containing invalid source events is
        # tainted: rejections are recorded, nothing is admitted.
        valid_events: list[ValidatedSourceEvent] = []
        rejections: list[tuple[str, str]] = []
        errors: dict[str, int] = {}

        for row in rows:
            try:
                event = validate_source_event(
                    row,
                    freeze_ts=freeze_ts,
                )
            except (SourceValidationError, KeyError, TypeError) as exc:
                reason = normalize_reason_code(exc)
                key = str(row.get("event_key") or "")
                rejections.append((key, reason))
                errors[reason] = errors.get(reason, 0) + 1
            else:
                valid_events.append(event)

        if rejections:
            return self._reject_and_block(
                rows_seen=len(rows),
                rejections=rejections,
                errors=errors,
                audit=audit,
            )

        inserted = 0
        duplicates = 0

        for event in valid_events:
            try:
                save_status = self.repository.save_event(event)
            except SourceIntegrityError as exc:
                # Conflicting duplicate payload / processing ledger
                # conflict — deterministic integrity violation.
                return self._block(
                    reason=str(exc) or "SOURCE_INTEGRITY_ERROR",
                    details={
                        "trigger": "SAVE_CONFLICT",
                        "event_key": event.event_key,
                        "audit": dict(audit) if audit else None,
                    },
                    audit=audit,
                    source_rows_seen=len(rows),
                    events_inserted=inserted,
                    duplicates=duplicates,
                )

            if save_status == "INSERTED":
                inserted += 1
            elif save_status == "DUPLICATE":
                duplicates += 1
            else:
                raise RuntimeError(
                    f"UNEXPECTED_SAVE_STATUS:{save_status}"
                )

        diagnostics: dict[str, Any] = {
            "duplicates": duplicates,
            "invalid": 0,
            "validation_errors": {},
            "batch_limit_reached": len(rows) >= self.batch_limit,
            "audit": dict(audit) if audit is not None else dict(
                self._last_audit or {}
            ),
            "verdict_gate": self._verdict_gate("ACTIVE", audit),
        }

        self.repository.record_health(
            status="OK",
            source_rows_seen=len(rows),
            events_inserted=inserted,
            diagnostics=diagnostics,
        )

        return ObserverResult(
            status="OK",
            source_rows_seen=len(rows),
            events_inserted=inserted,
            duplicates=duplicates,
            invalid=0,
            audit_status=(
                str(audit.get("status")) if audit is not None else None
            ),
            diagnostics=diagnostics,
        )

    # -- fail-closed paths ------------------------------------------

    def _reject_and_block(
        self,
        *,
        rows_seen: int,
        rejections: list[tuple[str, str]],
        errors: Mapping[str, int],
        audit: Mapping[str, Any] | None,
    ) -> ObserverResult:
        """Invalid source events: durable rejection + fail-closed block.

        Rejection diagnostics are committed BEFORE the block is
        effective; nothing from a tainted batch is admitted to the
        eligible cohort.
        """

        for key, reason in rejections:
            if key:
                # Durable rejection ledger entry with the concrete
                # reason_code. REJECTED rows never enter the eligible
                # OOS cohort.
                self.repository.mark_invalid(key, reason)

        details: dict[str, Any] = {
            "trigger": BLOCK_INVALID_SOURCE_EVENT,
            "rejections": [
                {"event_key": key, "reason_code": reason}
                for key, reason in rejections
            ],
            "validation_errors": dict(errors),
            "events_admitted": 0,
            "audit": dict(audit) if audit else None,
        }

        return self._block(
            reason=BLOCK_INVALID_SOURCE_EVENT,
            details=details,
            audit=audit,
            source_rows_seen=rows_seen,
            events_inserted=0,
            invalid=len(rejections),
            diagnostics_extra={
                "invalid": len(rejections),
                "validation_errors": dict(errors),
                "rejections": details["rejections"],
            },
            health_status="SOURCE_INCOMPLETE",
        )

    def _block(
        self,
        *,
        reason: str,
        details: Mapping[str, Any],
        audit: Mapping[str, Any] | None,
        source_rows_seen: int = 0,
        events_inserted: int = 0,
        duplicates: int = 0,
        invalid: int = 0,
        diagnostics_extra: Mapping[str, Any] | None = None,
        health_status: str = "BLOCKED",
    ) -> ObserverResult:
        """Transition the research contour to BLOCKED (fail-closed).

        Strictly limited to this observer: no activation change, no
        source change, no backfill, no freeze_ts change.
        """

        self.repository.record_state(STATE_BLOCKED, reason, dict(details))

        diagnostics: dict[str, Any] = {
            "blocked_reason": reason,
            "block_details": dict(details),
            "duplicates": duplicates,
            "invalid": invalid,
            "batch_limit_reached": False,
            "audit": dict(audit) if audit is not None else dict(
                self._last_audit or {}
            ),
            "verdict_gate": self._verdict_gate("BLOCKED", audit),
        }
        if diagnostics_extra:
            diagnostics.update(diagnostics_extra)

        self.repository.record_health(
            status=health_status,
            source_rows_seen=source_rows_seen,
            events_inserted=events_inserted,
            diagnostics=diagnostics,
        )

        LOG.error(
            "OBSERVER_BLOCKED reason=%s rows_seen=%d invalid=%d",
            reason,
            source_rows_seen,
            invalid,
        )

        return ObserverResult(
            status="BLOCKED",
            source_rows_seen=source_rows_seen,
            events_inserted=events_inserted,
            duplicates=duplicates,
            invalid=invalid,
            blocked_reason=reason,
            audit_status=(
                str(audit.get("status")) if audit is not None else None
            ),
            diagnostics=diagnostics,
        )

    # -- periodic audit ----------------------------------------------

    def _maybe_audit(self) -> Mapping[str, Any] | None:
        """Run the read-only integrity audit when the interval elapsed.

        Returns ``None`` when the audit is not due yet. Audit failures
        propagate: the transaction is aborted and the whole cycle fails
        atomically (no partial state is ever committed).
        """

        now = self._clock()

        if (
            self._last_audit_at is not None
            and now - self._last_audit_at < self.audit_interval_seconds
        ):
            return None

        self._last_audit_at = now

        audit = self.repository.audit_source_integrity(
            gap_grace_seconds=self.coverage_gap_grace_seconds,
        )
        self._last_audit = audit

        status = str(audit.get("status") or "")
        LOG.info(
            "SOURCE_INTEGRITY_AUDIT status=%s ledger=%s upstream=%s "
            "eligible=%s unprocessed=%s stale=%s conflicts=%s",
            status,
            audit.get("ledger_coverage"),
            audit.get("upstream_source_coverage"),
            audit.get("eligible_source"),
            audit.get("unprocessed"),
            audit.get("unprocessed_stale"),
            audit.get("integrity_conflicts"),
        )

        return audit

    def _verdict_gate(
        self,
        contour_state: str,
        audit: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        effective = audit if audit is not None else self._last_audit
        return verdict_gate(
            contour_state=contour_state,
            audit=effective,
        )
