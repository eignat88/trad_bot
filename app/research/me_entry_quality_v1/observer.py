"""Issue #296: isolated prospective source observer.

Research only. No trading, scanner or Bybit API calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from .contracts import (
    SourceValidationError,
    ValidatedSourceEvent,
    as_utc,
    validate_source_event,
)


class SourceRepository(Protocol):
    """Storage interface implemented by the PostgreSQL adapter."""

    def load_activation(self) -> Mapping[str, Any] | None:
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

    def record_health(
        self,
        status: str,
        source_rows_seen: int,
        events_inserted: int,
        diagnostics: Mapping[str, Any],
    ) -> None:
        ...


@dataclass(frozen=True)
class ObserverResult:
    status: str
    source_rows_seen: int = 0
    events_inserted: int = 0
    duplicates: int = 0
    invalid: int = 0


class EntryQualityObserver:
    """Independent prospective-only event consumer."""

    def __init__(
        self,
        repository: SourceRepository,
        *,
        batch_limit: int = 500,
    ):
        if batch_limit <= 0:
            raise ValueError("INVALID_BATCH_LIMIT")

        self.repository = repository
        self.batch_limit = batch_limit

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
            ):
                if rollback is not None:
                    rollback()
            return result
        except Exception:
            if rollback is not None:
                rollback()
            raise

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

        rows = self.repository.fetch_pending_events(
            freeze_ts,
            self.batch_limit,
        )

        inserted = 0
        duplicates = 0
        invalid = 0
        errors: dict[str, int] = {}

        for row in rows:
            try:
                event = validate_source_event(
                    row,
                    freeze_ts=freeze_ts,
                )
            except (SourceValidationError, KeyError, TypeError) as exc:
                invalid += 1
                reason = str(exc)
                errors[reason] = errors.get(reason, 0) + 1
                self.repository.mark_invalid(
                    str(row.get("event_key") or ""),
                    reason,
                )
                continue

            save_status = self.repository.save_event(event)

            if save_status == "INSERTED":
                inserted += 1
            elif save_status == "DUPLICATE":
                duplicates += 1
            else:
                raise RuntimeError(
                    f"UNEXPECTED_SAVE_STATUS:{save_status}"
                )

        status = (
            "SOURCE_INCOMPLETE"
            if invalid
            else "OK"
        )

        self.repository.record_health(
            status=status,
            source_rows_seen=len(rows),
            events_inserted=inserted,
            diagnostics={
                "duplicates": duplicates,
                "invalid": invalid,
                "validation_errors": errors,
                "batch_limit_reached": len(rows) >= self.batch_limit,
            },
        )

        return ObserverResult(
            status=status,
            source_rows_seen=len(rows),
            events_inserted=inserted,
            duplicates=duplicates,
            invalid=invalid,
        )
