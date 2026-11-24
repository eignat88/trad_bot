"""Issue #308 — H1-C: historical HTF source + event adapter (research-only).

Bridges the pure Issue #297 4H/1D classifier to source events of the
independent prospective source #305 (``ME_UPSTREAM_SOURCE_V1``) so that every
event can be classified at its ORIGINAL ``signal_time`` for the later OOS
analysis of hypothesis H1 (parent #295).

Scope guarantees (Issue #308):

* ``READ_ONLY_SOURCE`` — the package never fetches, writes, caches or
  mutates anything: raw candle rows and source-event rows are supplied by
  the caller (read-only SQL SELECT / bounded offline fetch);
* pure and deterministic — no network, no database, no clock, no I/O
  (enforced by ``tests/test_me_entry_quality_h1c_purity_v1.py``);
* not wired into any scanner, orchestrator, trading gate, systemd unit,
  migration, observer or OOS registry — nothing here runs in production;
* classification is performed at ``signal_time`` (never ``captured_at``);
* missing / broken data NEVER becomes a market mode: every failure is an
  explicit ``INVALID_SOURCE`` reason code;
* lineage (``source_event_key``, ``source_identity``, ``signal_time``,
  ``activation_freeze_ts``, ``lineage_hash``) is preserved verbatim and
  immutably.

Modules:

* ``htf_provider``   — H1-C.2 read-only HTF candle provider (source
  identity, coverage, reason codes);
* ``event_adapter``  — H1-C.3 #305 ``source_event`` normalization (duplicates,
  invalid rows, immutable lineage, ``signal_time`` ASOF);
* ``h1_result``      — H1-C.4 per-event result contract (4H/1D regime + BOS +
  coverage, one of nine HTF combinations, explicit eligibility, protocol
  version, full lineage);
* ``pipeline``       — orchestration of the three stages per event.
"""
from __future__ import annotations

from .event_adapter import (
    H1C_EVENT_ADAPTER_VERSION,
    EventBatch,
    EventRejection,
    SourceEventRecord,
    adapt_source_events,
    classification_asof_ms,
    parse_utc_ms,
)
from .h1_result import (
    H1C_PROTOCOL_VERSION,
    H1Result,
    HtfStateSummary,
    build_h1_result,
)
from .htf_provider import (
    COVERAGE_COMPLETE,
    COVERAGE_NONE,
    COVERAGE_PARTIAL,
    H1C_PROVIDER_VERSION,
    KNOWN_SOURCE_KINDS,
    SOURCE_KIND_BYBIT_KLINE_V5,
    SOURCE_KIND_MARKET_CANDLE_TABLE,
    SOURCE_KIND_OFFLINE_SNAPSHOT,
    CoverageReport,
    HtfCandleSet,
    HtfSourceIdentity,
    build_source_identity,
    provide_htf_candles,
    source_payload_hash,
)
from .pipeline import (
    H1C_PIPELINE_VERSION,
    H1EventOutcome,
    run_h1_event,
)

__all__ = [
    "COVERAGE_COMPLETE",
    "COVERAGE_NONE",
    "COVERAGE_PARTIAL",
    "CoverageReport",
    "EventBatch",
    "EventRejection",
    "H1C_EVENT_ADAPTER_VERSION",
    "H1C_PIPELINE_VERSION",
    "H1C_PROTOCOL_VERSION",
    "H1C_PROVIDER_VERSION",
    "H1EventOutcome",
    "H1Result",
    "HtfCandleSet",
    "HtfSourceIdentity",
    "HtfStateSummary",
    "KNOWN_SOURCE_KINDS",
    "SOURCE_KIND_BYBIT_KLINE_V5",
    "SOURCE_KIND_MARKET_CANDLE_TABLE",
    "SOURCE_KIND_OFFLINE_SNAPSHOT",
    "SourceEventRecord",
    "adapt_source_events",
    "build_h1_result",
    "build_source_identity",
    "classification_asof_ms",
    "parse_utc_ms",
    "provide_htf_candles",
    "run_h1_event",
    "source_payload_hash",
]
