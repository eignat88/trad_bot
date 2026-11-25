"""Issue #312 / FR-08 — H2 prospective runtime telemetry (research only).

Counters + logging for the research runtime.  Every cycle records what was
received, accepted/rejected, evaluated, confirmed/expired and what the candle
source did (requests / retries / rate-limit skips / budget skips).  Lineage
and immutable-data conflicts are counted explicitly.

``SOURCE_EMPTY_WAITING`` (logged verbatim) is the EXPECTED state when the
#305 cohort has no events: it is NEVER an RSI-algorithm error and NEVER
evidence that source coverage is proven (FR-08).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    RUNTIME_BLOCKED_CANDLE_SOURCE,
    RUNTIME_RUNNING,
    RUNTIME_SOURCE_EMPTY_WAITING,
    SOURCE_EMPTY_WAITING_LOG,
)

H2P_TELEMETRY_VERSION = "H2P_TELEMETRY_1.0.0"


@dataclass
class TelemetryCounters:
    """Per-cycle research counters (persisted + logged, never traded on)."""

    # ── source ingest ──
    source_events_received: int = 0
    source_events_accepted: int = 0
    source_events_rejected: int = 0
    source_events_pre_freeze: int = 0
    source_events_invalid: int = 0
    idempotent_duplicates: int = 0
    lineage_conflicts: int = 0
    # ── lifecycle ──
    variant_states_evaluated: int = 0
    variants_pending: int = 0
    variants_confirmed_delayed: int = 0
    variants_expired: int = 0
    variants_incomplete_coverage: int = 0
    variants_invalid_source: int = 0
    variants_not_ready: int = 0
    #: wall time from signal_time to RSI confirmation (ms), per confirmation
    confirmation_delays_ms: list[int] = field(default_factory=list)
    #: event counts per H2 variant id (FR-08): {variant_id: {state: count}}
    per_variant: dict[str, dict[str, int]] = field(default_factory=dict)
    # ── candle source ──
    candle_fetch_errors: int = 0
    api_requests: int = 0
    api_retries: int = 0
    rate_limit_skips: int = 0
    budget_skips: int = 0
    # ── integrity ──
    immutable_result_conflicts: int = 0

    def note_variant(self, variant_id: str, state: str) -> None:
        """Count ONE (event, variant) outcome toward its H2 variant."""
        bucket = self.per_variant.setdefault(variant_id, {})
        bucket[state] = bucket.get(state, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        data = {
            "source_events_received": self.source_events_received,
            "source_events_accepted": self.source_events_accepted,
            "source_events_rejected": self.source_events_rejected,
            "source_events_pre_freeze": self.source_events_pre_freeze,
            "source_events_invalid": self.source_events_invalid,
            "idempotent_duplicates": self.idempotent_duplicates,
            "lineage_conflicts": self.lineage_conflicts,
            "variant_states_evaluated": self.variant_states_evaluated,
            "variants_pending": self.variants_pending,
            "variants_confirmed_delayed": self.variants_confirmed_delayed,
            "variants_expired": self.variants_expired,
            "variants_incomplete_coverage": self.variants_incomplete_coverage,
            "variants_invalid_source": self.variants_invalid_source,
            "variants_not_ready": self.variants_not_ready,
            "confirmation_delays_ms": list(self.confirmation_delays_ms),
            "per_variant": {
                key: dict(value) for key, value in self.per_variant.items()
            },
            "candle_fetch_errors": self.candle_fetch_errors,
            "api_requests": self.api_requests,
            "api_retries": self.api_retries,
            "rate_limit_skips": self.rate_limit_skips,
            "budget_skips": self.budget_skips,
            "immutable_result_conflicts": self.immutable_result_conflicts,
        }
        if self.confirmation_delays_ms:
            delays = sorted(self.confirmation_delays_ms)
            data["confirmation_delay_avg_ms"] = sum(delays) / len(delays)
            data["confirmation_delay_max_ms"] = delays[-1]
        return data


def cycle_status(
    *,
    counters: TelemetryCounters,
    source_rows_seen: int,
    candle_provider_enabled: bool,
    source_cohort_empty: bool = False,
) -> str:
    """The honest contour status of ONE cycle (never a market mode)."""
    if not candle_provider_enabled:
        return RUNTIME_BLOCKED_CANDLE_SOURCE
    if source_rows_seen == 0 and source_cohort_empty:
        return RUNTIME_SOURCE_EMPTY_WAITING
    return RUNTIME_RUNNING


def log_cycle(
    logger: logging.Logger,
    *,
    status: str,
    counters: TelemetryCounters,
    details: dict[str, Any] | None = None,
) -> None:
    """Log one cycle: the FR-08 metrics, plus ``SOURCE_EMPTY_WAITING``."""
    if status == RUNTIME_SOURCE_EMPTY_WAITING:
        logger.info(SOURCE_EMPTY_WAITING_LOG)
    logger.info(
        "H2_PROSPECTIVE_CYCLE status=%s counters=%s details=%s",
        status,
        counters.to_dict(),
        details or {},
    )


def log_source_empty_waiting(logger: logging.Logger) -> None:
    """Verbatim ``SOURCE_EMPTY_WAITING`` marker (criterion 4)."""
    logger.info(SOURCE_EMPTY_WAITING_LOG)
