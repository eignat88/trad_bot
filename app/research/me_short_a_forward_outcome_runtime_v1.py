"""ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1.

Independent, strictly forward-only, **observe-only** runtime that drives the
Issue #208 collector
:mod:`app.research.me_short_a_forward_outcome_lineage_v1` from live market
data.

Contract
--------
* This runtime **observes**. It never places, simulates or records an order,
  and it has no paper/live surface at all.
* It is the **only** writer of the four ``me_short_a_*`` capture tables, under
  its own least-privilege role ``trad_bot_me_short_a_collector``. It has no
  privilege on ``research.me_short_a_fill_feasibility_v1`` (Phase 1), on the
  closed frozen OOS tables, or on any scanner / paper / live object.
* The deterministic ``event_key`` is shared with Phase 1
  (:func:`app.research.me_short_a_fill_feasibility_v1.capture`), so
  ``source signal -> Phase 1 snapshot -> outcome`` joins on one key.
* Strictly forward-only: nothing is written before the activation row's
  immutable ``freeze_ts``. ``CHECK (signal_time > freeze_ts)`` makes a
  backfill impossible even if this code were bypassed.
* Candles enter the pipeline **only when the 5m candle is closed**
  (``candle_close_time <= observed_at``), and the 240m ``STOP_FIRST`` outcome
  is finalized on closed candles only.

Nothing in this module is imported by the scanner, the paper engine or the
live engine. Activation requires all of

* ``ME_SHORT_A_LINEAGE_CAPTURE_ENABLED=1`` (runtime feature flag),
* ``ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1`` configured in the
  runtime registry,
* an activation row ``status='RUNNING'`` with a non-NULL ``freeze_ts``,

and is fail-closed otherwise. Deployment and activation are separate,
explicitly approved changes; see
``docs/research/ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1.md``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence

try:  # pragma: no cover - psycopg is optional in unit tests
    import psycopg  # type: ignore
except Exception:  # pragma: no cover
    psycopg = None  # type: ignore

from app.research.me_short_a_forward_outcome_lineage_v1 import (
    EXPERIMENT as LINEAGE_EXPERIMENT,
    INTRABAR_POLICY,
    MAX_HOLD_MINUTES,
    DIRECTION as LINEAGE_DIRECTION,
    capture_post_signal_candle,
    capture_source_signal,
    event_key as compute_event_key,
    expected_window_opens,
    finalize_outcome,
    record_event_lineage,
    window_bounds,
)

__all__ = [
    "PROTOCOL_VERSION",
    "RUNTIME_PROTOCOL_VERSION",
    "EXPERIMENT",
    "RUNTIME_ROLE",
    "COLLECTOR_ROLE",
    "AUDIT_ROLE",
    "RUNTIME_AUDIT_ROLE",
    "FEATURE_FLAG",
    "DEFAULT_REGISTRY_PATH",
    "STATE_READY_TO_START",
    "STATE_RUNNING",
    "STATE_COMPLETED",
    "STATE_CANCELLED",
    "RUNNER_STATE_IDLE",
    "RUNNER_STATE_RUNNING",
    "RUNNER_STATE_STOPPING",
    "RUNNER_STATE_STOPPED",
    "RUNNER_STATE_FAILED",
    "Candle",
    "Candidate",
    "CandidatePage",
    "CandidateSource",
    "ListCandidateSource",
    "PAGE_SIZE_DEFAULT",
    "HANDLED",
    "HANDLED_SKIPPED",
    "HANDLER_RETRY",
    "HANDLER_OUTCOMES",
    "DatabaseAccessError",
    "Phase1Link",
    "CandleSource",
    "RuntimeConfig",
    "RuntimeMetrics",
    "RuntimeDiagnostics",
    "ActivationStateError",
    "COUNTER_NAMES",
    "InMemoryCandleSource",
    "ForwardOutcomeRuntime",
    "utcnow",
    "canonical_price",
    "load_registry",
    "runtime_protocol_payload",
    "runtime_protocol_hash",
    "verify_runtime_identity",
    "resolve_config",
    "replace_activation",
    "event_key_for",
]

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1"
RUNTIME_PROTOCOL_VERSION = PROTOCOL_VERSION
EXPERIMENT = "ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1"
RUNTIME_ROLE = "trad_bot_me_short_a_runtime"
COLLECTOR_ROLE = "trad_bot_me_short_a_collector"
AUDIT_ROLE = "trad_bot_me_short_a_audit"
RUNTIME_AUDIT_ROLE = "trad_bot_me_short_a_runtime_audit"
FEATURE_FLAG = "ME_SHORT_A_LINEAGE_CAPTURE_ENABLED"
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parent
    / "me_short_a_forward_outcome_runtime_registry_v1.json"
)

# Activation lifecycle (mirrors research.me_short_a_lineage_activation_v1).
STATE_READY_TO_START = "READY_TO_START"
STATE_RUNNING = "RUNNING"
STATE_COMPLETED = "COMPLETED"
STATE_CANCELLED = "CANCELLED"
ACTIVATION_STATES = (STATE_READY_TO_START, STATE_RUNNING, STATE_COMPLETED,
                     STATE_CANCELLED)

# Runner lifecycle.
RUNNER_STATE_IDLE = "IDLE"
RUNNER_STATE_RUNNING = "RUNNING"
RUNNER_STATE_STOPPING = "STOPPING"
RUNNER_STATE_STOPPED = "STOPPED"
RUNNER_STATE_FAILED = "FAILED"
RUNNER_STATES = (RUNNER_STATE_IDLE, RUNNER_STATE_RUNNING, RUNNER_STATE_STOPPING,
                 RUNNER_STATE_STOPPED, RUNNER_STATE_FAILED)

# How many already-handled ids are remembered for idempotency.
SEEN_IDS_LIMIT = 4096
# Keyset page size for candidate intake. Pages are traversed to exhaustion
# inside one cycle, so this is a batching parameter, never a limit on how many
# candidates a cycle may observe.
PAGE_SIZE_DEFAULT = 500

# 5m candle granularity used everywhere in this protocol.
CANDLE_MINUTES = 5


# ── clock ──────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    """Injectable wall clock (UTC). Tests monkeypatch this."""
    return datetime.now(timezone.utc)


def canonical_price(value: Any) -> Decimal | None:
    """Normalize a price to the representation the frozen identity uses.

    Phase 1 builds its ``event_key`` from
    ``Decimal(str(<float produced by the scanner>))`` (see
    :func:`app.research.me_short_a_fill_feasibility_v1.capture`). Reproducing
    that shared key therefore requires the SAME normalization: a numeric
    column read back as ``Decimal('100.00000000')`` must become
    ``Decimal('100.0')``, which is exactly what the scanner's in-memory
    ``float`` produces. Without this step the two layers would hash different
    strings and the ``source signal -> Phase 1 snapshot -> outcome`` chain
    would never join.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(float(value)))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _parse_source_ids(value: Any) -> list[int]:
    """Normalize Phase 1 ``source_observation_ids`` whatever the driver gave.

    ``jsonb`` columns are decoded to a list by psycopg and delivered as text
    by pg8000 (the documented fallback in the runner), so both shapes — plus
    a tuple and a bare scalar — must parse to the same list of ints.
    Unparseable elements are dropped rather than guessed at.
    """
    if value is None:
        return []
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except Exception:
            value = [text]
    if isinstance(value, Mapping):
        return []
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    out: list[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            out.append(int(item))
            continue
        if isinstance(item, float) and item.is_integer():
            out.append(int(item))
            continue
        if isinstance(item, Mapping):
            for field in ("observation_id", "id", "source_observation_id"):
                if field in item:
                    try:
                        out.append(int(item[field]))
                    except (TypeError, ValueError):
                        pass
                    break
            continue
        try:
            out.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return out


# ── frozen identity of the runtime protocol ────────────────────────────

def runtime_protocol_payload() -> dict[str, Any]:
    """Canonical payload describing this runtime integration protocol."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "FORWARD_OUTCOME_RUNTIME_INTEGRATION",
        "mode": "OBSERVE_ONLY_NO_ORDERS",
        "collector_protocol_version": LINEAGE_EXPERIMENT,
        "event_key_contract": (
            "sha256(symbol|SHORT|<utc isoformat signal_time>|"
            "<str(Decimal(str(reference_price)))>|"
            "<str(Decimal(str(invalidation_price)))>|"
            "<str(Decimal(str(target_1)))>)"
        ),
        "event_key_shared_with_phase1": True,
        "symbol_scope": "MOMENTUM_EXHAUSTION_SHORT_CANDIDATES",
        "direction": LINEAGE_DIRECTION,
        "max_hold_minutes": MAX_HOLD_MINUTES,
        "candle_granularity_minutes": CANDLE_MINUTES,
        "intrabar_policy": INTRABAR_POLICY,
        "closed_candles_only": True,
        "closed_5m_candles_only": True,
        "closed_candle_rule": "candle_close_time <= observed_at",
        "candle_provenance_required": True,
        "forwards_only_after_freeze_ts": True,
        "lookahead_free": True,
        "no_backfill": True,
        "append_only": True,
        "order_pathway": None,
        "order_pathway_forbidden": True,
        "paper_surface": None,
        "live_surface": None,
        "scanner_gate_surface": None,
        "collector_role": COLLECTOR_ROLE,
        "runtime_role": RUNTIME_ROLE,
        "audit_role": AUDIT_ROLE,
        "runtime_audit_role": RUNTIME_AUDIT_ROLE,
        "read_only_toward": [
            "research.me_short_a_fill_feasibility_v1",
            "research.prospective_observation",
            "research.prospective_outcome",
        ],
        "sources": {
            "candidate_source": "MOMENTUM_EXHAUSTION_SHORT_CANDIDATES",
            "candle_source": "CLOSED_5M_KLINES",
        },
        "heartbeat_interval_seconds": 60,
        "diagnostics_retention_rows": 2000,
    }


def runtime_protocol_payload_hash_source() -> dict[str, Any]:
    """The subset of the payload that is hashed into ``protocol_hash``."""
    return runtime_protocol_payload()


def runtime_protocol_hash() -> str:
    """Deterministic ``sha256`` over the canonical runtime protocol payload."""
    import hashlib

    canonical = json.dumps(
        runtime_protocol_payload_hash_source(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_runtime_identity(
    *, protocol_version: str, protocol_hash: str,
) -> tuple[bool, list[str]]:
    """Verify the runtime identity against this code's frozen identity."""
    problems: list[str] = []
    if protocol_version != PROTOCOL_VERSION:
        problems.append(
            f"protocol_version mismatch: {protocol_version!r} != {PROTOCOL_VERSION!r}")
    if protocol_hash != runtime_protocol_hash():
        problems.append("protocol_hash mismatch")
    return (not problems, problems)


# ── registry ───────────────────────────────────────────────────────────

def load_registry(path: os.PathLike[str] | str | None = None) -> dict[str, Any]:
    """Load the frozen runtime registry document."""
    target = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    with open(target, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("runtime registry must be a JSON object")
    return data


# ── data shapes ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candle:
    """One closed 5m OHLC candle plus its retrieval provenance."""

    symbol: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    retrieved_at: datetime
    source: str = "CLOSED_5M_KLINES"

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None or self.close_time.tzinfo is None:
            raise ValueError("candle times must be timezone-aware")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        if self.retrieved_at < self.close_time:
            raise ValueError(
                "lookahead: retrieved_at must be >= candle_close_time")
        if self.high < self.low:
            raise ValueError("high must be >= low")

    @property
    def is_closed(self) -> bool:
        return True

    @property
    def open_time_iso(self) -> str:
        return self.open_time.astimezone(timezone.utc).isoformat()


# ── candidate shape ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """A MOMENTUM_EXHAUSTION SHORT candidate observed at ``signal_time``.

    Prices are already the frozen A geometry: ``reference_price`` is the max
    of the last five 5m highs, ``invalidation_price`` is the absolute stop and
    ``target_1`` is the absolute target.
    """

    symbol: str
    signal_time: datetime
    reference_price: Decimal
    invalidation_price: Decimal
    target_1: Decimal
    trigger_5m_close: Decimal | None = None
    entry_zone_low: Decimal | None = None
    entry_zone_high: Decimal | None = None
    recent_high_window: Sequence[Candle] = ()
    market_context: str = ""
    signal_time_quote: str = ""
    source_observation_id: int | None = None
    source: str = "MOMENTUM_EXHAUSTION_SHORT_CANDIDATES"
    observed_at: datetime | None = None
    event_id: str | None = None
    # Honest reporting: anything the source could not supply is named here and
    # is NEVER imputed.
    missing_fields: tuple[str, ...] = ()
    source_quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.signal_time.tzinfo is None:
            raise ValueError("signal_time must be timezone-aware")
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if self.invalidation_price <= self.reference_price:
            raise ValueError("invalidation_price must be above reference_price")
        if self.target_1 >= self.reference_price:
            raise ValueError("target_1 must be below reference_price")
        if self.signal_time.utcoffset() != timedelta(0):
            raise ValueError("signal_time must be UTC")
        if self.signal_time.second or self.signal_time.microsecond:
            raise ValueError("signal_time must be whole seconds")
        if self.recent_high_window and len(self.recent_high_window) > 5:
            raise ValueError("recent_high_window holds at most five candles")
        if self.recent_high_window:
            latest_close = max(c.close_time for c in self.recent_high_window)
            if latest_close > self.signal_time:
                raise ValueError(
                    "lookahead: recent_high_window must close at or before "
                    "signal_time")
        if self.recent_high_window:
            reference = max(c.high for c in self.recent_high_window)
            if reference != self.reference_price:
                raise ValueError(
                    "reference_price must equal max(last five 5m highs)")

    @property
    def structural_r(self) -> Decimal:
        return self.invalidation_price - self.reference_price


# ── candidate source (keyset-paginated) ───────────────────────────────

@dataclass(frozen=True)
class CandidatePage:
    """One keyset page of candidate rows, plus everything needed to resume.

    ``cursor`` is the composite keyset ``(detected_at, setup_id)`` of the LAST
    row returned by this page — or ``None`` for an empty page. It is only ever
    advanced by the caller after the whole page has been handled, so a failure
    in the middle of a page can never lose the rows behind it.

    ``rejected`` names the rows that could not be represented as a
    :class:`Candidate` at all. They are counted and surfaced, never silently
    dropped, because silently dropping a row is how candidates get lost.
    """

    candidates: tuple[Candidate, ...] = ()
    rejected: tuple[str, ...] = ()
    rows_read: int = 0
    page_size: int = 0
    cursor: tuple[datetime, Any] | None = None
    exhausted: bool = True

    @property
    def next_cursor(self) -> tuple[datetime, Any] | None:
        """Resume point: strictly AFTER this page's last row."""
        return self.cursor


class CandidateSource:
    """Interface the runtime needs from a candidate provider.

    Implementations MUST be keyset-paginated on ``(detected_at, setup_id)`` and
    MUST apply the immutable ``freeze_ts`` as a strict lower bound in the
    backend query, so a pre-freeze backlog can never be re-read, block the
    selection, or push post-freeze rows out of a page.
    """

    def fetch_page(
        self,
        cursor: tuple[datetime, Any] | None,
        *,
        freeze_ts: datetime,
        page_size: int,
    ) -> CandidatePage:
        raise NotImplementedError


class ListCandidateSource(CandidateSource):
    """In-memory keyset-paginated source (tests and simple providers).

    Applies exactly the same ordering, freeze bound and cursor semantics as
    :class:`DbScannerSetupCandidateSource`, so pagination tests written
    against it describe the real contract.
    """

    def __init__(self, rows: Iterable[tuple[Any, Candidate]]) -> None:
        self._rows = sorted(
            list(rows), key=lambda item: (item[0], str(item[1])))

    @classmethod
    def from_candidates(
        cls, candidates: Iterable[Candidate],
    ) -> "ListCandidateSource":
        return cls([(c.signal_time, c) for c in candidates])

    def fetch_page(
        self,
        cursor: tuple[datetime, Any] | None,
        *,
        freeze_ts: datetime,
        page_size: int,
    ) -> CandidatePage:
        if freeze_ts is None:
            raise ValueError("fetch_page requires an immutable freeze_ts")
        limit = max(1, int(page_size))
        selected: list[Candidate] = []
        rejected: list[str] = []
        last: tuple[datetime, Any] | None = None
        read = 0
        for key, candidate in self._rows:
            if key[0] is None or key[0] <= freeze_ts:
                continue                      # pre-freeze never blocks a page
            if cursor is not None and (key[0], str(key[1])) <= (
                    cursor[0], str(cursor[1])):
                continue
            read += 1
            selected.append(candidate)
            last = (key[0], str(key[1]))
            if read >= limit:
                break
        return CandidatePage(
            candidates=tuple(selected),
            rejected=tuple(rejected),
            rows_read=read,
            page_size=limit,
            cursor=last,
            exhausted=read < limit,
        )


# ── candle source (interface + default in-memory implementation) ──────

class CandleSource:
    """Minimal interface the runtime needs from a candle provider."""

    def closed_candles(
        self, symbol: str, *, start: datetime, end: datetime,
    ) -> list[Candle]:
        raise NotImplementedError


class InMemoryCandleSource(CandleSource):
    """Deterministic in-memory candle source used by tests and by dry-runs."""

    def __init__(self, candles: Iterable[Candle] = ()) -> None:
        self._by_symbol: dict[str, list[Candle]] = {}
        self.add(candles)

    def add(self, candles: Iterable[Candle]) -> None:
        for candle in candles:
            self._by_symbol.setdefault(candle.symbol, []).append(candle)
        for rows in self._by_symbol.values():
            rows.sort(key=lambda c: c.open_time)

    def closed_candles(
        self, symbol: str, *, start: datetime, end: datetime,
    ) -> list[Candle]:
        rows = self._by_symbol.get(symbol, [])
        return [
            c for c in rows
            if start <= c.open_time < end and c.close_time <= self._asof()
        ]

    def _asof(self) -> datetime:
        return utcnow()


# ── runtime configuration / lifecycle ──────────────────────────────────

@dataclass(frozen=True)
class RuntimeConfig:
    """Immutable runtime configuration resolved from registry + environment."""

    experiment: str
    protocol_version: str
    protocol_hash: str
    enabled: bool
    freeze_ts: datetime | None
    activation_status: str | None
    heartbeat_interval_seconds: int
    diagnostics_retention_rows: int
    idle_sleep_seconds: float
    max_events_per_cycle: int
    registry_path: str

    @property
    def activated(self) -> bool:
        return (
            self.enabled
            and self.activation_status == STATE_RUNNING
            and self.freeze_ts is not None
        )

    @property
    def fail_closed_reason(self) -> str | None:
        if not self.enabled:
            return f"{FEATURE_FLAG} is not '1'"
        if self.activation_status != STATE_RUNNING:
            return (f"activation status is {self.activation_status!r}, "
                    f"expected 'RUNNING'")
        if self.freeze_ts is None:
            return "activation freeze_ts is NULL"
        return None


class ActivationStateError(RuntimeError):
    """Raised when the runtime is not allowed to write."""


class DatabaseAccessError(RuntimeError):
    """A READ-ONLY confirmation query failed.

    Raised instead of being silently converted into "the row is absent",
    because the capture tables are append-only: a confirmation that is
    mistaken for a negative is written once and can never be repaired.
    """


class Phase1Link(NamedTuple):
    """Result of the READ-ONLY Phase 1 linkage lookup."""

    event_key: str | None
    observation_id: int | None
    source_observation_ids: tuple[int, ...] = ()


# Per-candidate handling outcomes. The first two let the keyset cursor move
# on; the third keeps the cursor where it is so the candidate is retried and
# is therefore never lost.
HANDLED = "HANDLED"
HANDLED_SKIPPED = "HANDLED_SKIPPED"
HANDLER_RETRY = "HANDLER_RETRY"
HANDLER_OUTCOMES = (HANDLED, HANDLED_SKIPPED, HANDLER_RETRY)


# ── metrics / diagnostics ──────────────────────────────────────────────

COUNTER_NAMES = (
    "cycles_completed",
    "candidates_seen",
    "signals_captured",
    "lineages_recorded",
    "candles_captured",
    "outcomes_finalized",
    "outcomes_matched",
    "outcomes_incomplete",
    "skipped_not_activated",
    "skipped_pre_freeze",
    "skipped_idempotent",
    "skipped_invalid_candidate",
    "skipped_lookahead",
    "skipped_unmatured",
    "errors",
)


@dataclass
class RuntimeMetrics:
    """Monotonic counters for one runtime instance."""

    cycles_completed: int = 0
    candidates_seen: int = 0
    signals_captured: int = 0
    lineages_recorded: int = 0
    candles_captured: int = 0
    outcomes_finalized: int = 0
    outcomes_matched: int = 0
    outcomes_incomplete: int = 0
    skipped_not_activated: int = 0
    skipped_pre_freeze: int = 0
    skipped_idempotent: int = 0
    skipped_invalid_candidate: int = 0
    skipped_lookahead: int = 0
    skipped_unmatured: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in COUNTER_NAMES}

    def add(self, name: str, amount: int = 1) -> None:
        if name not in COUNTER_NAMES:
            raise KeyError(f"unknown counter {name!r}")
        setattr(self, name, int(getattr(self, name)) + int(amount))


@dataclass(frozen=True)
class RuntimeDiagnostics:
    """A point-in-time diagnostic snapshot of one runtime instance."""

    protocol_version: str
    runtime_protocol_hash: str
    runner_id: str
    generation: int
    runner_state: str
    lifecycle_phase: str
    activation_status: str | None
    freeze_ts: datetime | None
    enabled: bool
    activated: bool
    fail_closed_reason: str | None
    started_at: datetime
    observed_at: datetime
    last_event_time: datetime | None
    last_error: str | None
    open_events: int
    metrics: dict[str, int]
    config_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        for key in ("freeze_ts", "started_at", "observed_at", "last_event_time"):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.astimezone(timezone.utc).isoformat()
        return payload


# ── the observe-only runtime ───────────────────────────────────────────

class ForwardOutcomeRuntime:
    """Independent observe-only runtime for the forward outcome lineage.

    One instance drives exactly one cycle set:

    1. refresh the (read-only) activation state of the lineage collector;
    2. observe new ``MOMENTUM_EXHAUSTION`` SHORT candidates;
    3. for each one, append the source signal detail and the deterministic
       lineage record (shared ``event_key`` with Phase 1);
    4. append every CLOSED post-signal 5m candle of the 240m window;
    5. once the horizon is settled on closed candles, finalize the STOP_FIRST
       outcome exactly once;
    6. append one heartbeat/diagnostics row.

    Concurrency is made safe by **idempotency**, not by a lock: every writer is
    ``INSERT ... ON CONFLICT DO NOTHING`` on an append-only table, so two
    runners racing produce the same evidence exactly once.
    """

    def __init__(
        self,
        conn,
        *,
        config: RuntimeConfig,
        candle_source: CandleSource,
        candidate_source: CandidateSource
        | Callable[[datetime | None], Iterable[Candidate]]
        | None = None,
        clock: Callable[[], datetime] = utcnow,
        runner_id: str | None = None,
        generation: int = 0,
        sleep: Callable[[float], None] = time.sleep,
        page_size: int = PAGE_SIZE_DEFAULT,
    ) -> None:
        self._conn = conn
        self._config = config
        self._candles = candle_source
        self._candidates = self._normalize_source(candidate_source)
        self._clock = clock
        self._sleep = sleep
        self._runner_id = runner_id or f"me-short-a-forward-runtime-{os.getpid()}"
        self._generation = int(generation)
        self._metrics = RuntimeMetrics()
        self._state = RUNNER_STATE_IDLE
        self._phase = "INITIALIZED"
        self._started_at = clock()
        self._last_event_time: datetime | None = None
        self._last_error: str | None = None
        self._seen_candidate_ids: set[str] = set()
        self._seen_order: list[str] = []
        self._cycle_id = 0
        self._page_size = max(1, int(page_size))
        # Keyset cursor over (detected_at, setup_id). It is advanced ONLY after
        # a whole page has been handled, so a failure can never push the
        # cursor past rows that were not written. On restart it resets to
        # None, i.e. to the immutable freeze_ts bound, and every write below
        # is INSERT ... ON CONFLICT DO NOTHING — so a restart neither loses
        # nor duplicates a candidate.
        self._candidate_cursor: tuple[datetime, Any] | None = None

    @staticmethod
    def _normalize_source(candidate_source):
        """Accept either a keyset-paginated source or a plain callable.

        A plain ``f(freeze) -> Iterable[Candidate]`` is wrapped into a single
        page, which keeps simple providers (and tests) usable while the real
        ``DbScannerSetupCandidateSource`` drives full keyset pagination.
        """
        if candidate_source is None:
            return ListCandidateSource(())
        if hasattr(candidate_source, "fetch_page"):
            return candidate_source

        class _CallableSource(CandidateSource):
            def __init__(self, fn):
                self._fn = fn

            def fetch_page(self, cursor, *, freeze_ts, page_size):
                if cursor is not None:
                    return CandidatePage(
                        rows_read=0, page_size=page_size,
                        cursor=cursor, exhausted=True)
                rows = list(self._fn(freeze_ts))
                return CandidatePage(
                    candidates=tuple(rows),
                    rows_read=len(rows),
                    page_size=max(len(rows), 1),
                    cursor=None,
                    exhausted=True,
                )

        return _CallableSource(candidate_source)

    # ── introspection ───────────────────────────────────────────────
    @property
    def config(self) -> RuntimeConfig:
        return self._config

    @property
    def state(self) -> str:
        return self._state

    @property
    def lifecycle_phase(self) -> str:
        return self._phase

    @property
    def metrics(self) -> RuntimeMetrics:
        return self._metrics

    @property
    def runner_id(self) -> str:
        return self._runner_id

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def candidate_cursor(self) -> tuple[datetime, Any] | None:
        """The keyset ``(detected_at, setup_id)`` already fully handled.

        ``None`` means intake has not advanced past the immutable freeze yet.
        """
        return self._candidate_cursor

    def config_fingerprint(self) -> str:
        import hashlib

        canonical = json.dumps(
            {
                "experiment": self._config.experiment,
                "protocol_version": self._config.protocol_version,
                "protocol_hash": self._config.protocol_hash,
                "enabled": self._config.enabled,
                "registry_path": self._config.registry_path,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def diagnostics(self) -> RuntimeDiagnostics:
        return RuntimeDiagnostics(
            protocol_version=self._config.protocol_version,
            runtime_protocol_hash=self._config.protocol_hash,
            runner_id=self._runner_id,
            generation=self._generation,
            runner_state=self._state,
            lifecycle_phase=self._phase,
            activation_status=self._config.activation_status,
            freeze_ts=self._config.freeze_ts,
            enabled=self._config.enabled,
            activated=self._config.activated,
            fail_closed_reason=self._config.fail_closed_reason,
            started_at=self._started_at,
            observed_at=self._clock(),
            last_event_time=self._last_event_time,
            last_error=self._last_error,
            open_events=self._count_open_events(),
            metrics=self._metrics.as_dict(),
            config_fingerprint=self.config_fingerprint(),
        )

    # ── lifecycle ───────────────────────────────────────────────────
    def _transition(self, state: str, phase: str) -> None:
        if state not in RUNNER_STATES:
            raise ValueError(f"unknown runner state {state!r}")
        if state == self._state and phase == self._phase:
            return
        self._state = state
        self._phase = phase
        log.info(
            "ME SHORT A forward runtime %s -> %s (%s)",
            self._runner_id, state, phase)

    def start(self) -> None:
        """Enter the running lifecycle. Does not write anything by itself."""
        self._started_at = self._clock()
        self._transition(RUNNER_STATE_RUNNING, "STARTED")

    def request_stop(self) -> None:
        if self._state == RUNNER_STATE_RUNNING:
            self._transition(RUNNER_STATE_STOPPING, "STOP_REQUESTED")

    def stop(self, reason: str = "STOPPED") -> None:
        self._transition(RUNNER_STATE_STOPPED, reason)

    def fail(self, message: str) -> None:
        self._last_error = message
        self._metrics.add("errors")
        self._transition(RUNNER_STATE_FAILED, "FAILED")

    # ── idempotency ─────────────────────────────────────────────────
    def _already_seen(self, token: str) -> bool:
        return token in self._seen_candidate_ids

    def _remember(self, token: str) -> None:
        if token in self._seen_candidate_ids:
            return
        self._seen_candidate_ids.add(token)
        self._seen_order.append(token)
        while len(self._seen_order) > SEEN_IDS_LIMIT:
            stale = self._seen_order.pop(0)
            self._seen_candidate_ids.discard(stale)

    # ── read-only DB helpers ────────────────────────────────────────
    def _activation_snapshot(self) -> tuple[str | None, datetime | None]:
        """Read the lineage activation row (READ-ONLY)."""
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT status,freeze_ts FROM "
                "research.me_short_a_lineage_activation_v1 WHERE protocol_version=%s",
                (LINEAGE_EXPERIMENT,))
            row = cursor.fetchone()
        finally:
            cursor.close()
        if not row:
            return None, None
        freeze = row[1]
        if isinstance(freeze, datetime):
            freeze = freeze.replace(tzinfo=timezone.utc) if freeze.tzinfo is None \
                else freeze.astimezone(timezone.utc)
        return row[0], freeze

    def _phase1_lookup(self, key: str) -> Phase1Link:
        """READ-ONLY check whether a Phase 1 snapshot shares this event_key.

        Never inserts, updates or retro-fits anything in
        ``research.me_short_a_fill_feasibility_v1``.

        Correctness contract:

        * no row  -> :class:`Phase1Link` with ``event_key=None`` (honest
          "no Phase 1 evidence for this key");
        * a row   -> the observed ``event_key`` plus the ACTUAL
          ``source_observation_ids``, parsed whatever the driver returned
          (``jsonb`` comes back as a list from psycopg and as text from
          pg8000, which this runtime may fall back to);
        * a DB error (for example the least-privilege role has no ``SELECT``
          on this table) -> :class:`DatabaseAccessError`.

        A database error must never be flattened into "no Phase 1 evidence":
        the capture tables are append-only, so a false ``phase1_linked =
        FALSE`` would be written once and could never be repaired — it is
        exactly what turns a healthy event into a permanent
        ``MISSING_LINEAGE``.
        """
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT event_key,source_observation_ids FROM "
                "research.me_short_a_fill_feasibility_v1 WHERE event_key=%s",
                (key,))
            row = cursor.fetchone()
        except Exception as exc:
            raise DatabaseAccessError(
                f"Phase 1 linkage lookup failed for event_key={key[:16]}…: "
                f"{type(exc).__name__}: {exc}") from exc
        finally:
            cursor.close()
        if not row:
            return Phase1Link(None, None, ())
        source_ids = _parse_source_ids(row[1])
        observation_id = source_ids[0] if source_ids else None
        return Phase1Link(str(row[0]), observation_id, tuple(source_ids))

    def _source_signal_present(self, key: str) -> bool:
        """READ-ONLY confirmation that a source_signal row exists for ``key``.

        Returns the TRUE state of the append-only
        ``research.me_short_a_source_signal_v1`` table rather than the return
        value of the current ``INSERT ... ON CONFLICT DO NOTHING``:

        * a re-run inserts nothing and must still report the row as present;
        * a partial recovery (source signal written, process died before the
          lineage row) must not be recorded as ``source_signal_missing``;
        * a failed insert reports the row as absent, which the caller turns
          into a retry instead of a permanent false record.

        Raises :class:`DatabaseAccessError` when the confirmation itself
        cannot be read — the least-privilege runtime role holds ``SELECT`` on
        this table, but a role that only has ``INSERT`` must fail loudly here
        rather than let a permission error masquerade as "absent".
        """
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT event_key FROM research.me_short_a_source_signal_v1 "
                "WHERE event_key=%s",
                (key,))
            row = cursor.fetchone()
        except Exception as exc:
            raise DatabaseAccessError(
                f"source_signal confirmation failed for event_key={key[:16]}…: "
                f"{type(exc).__name__}: {exc}") from exc
        finally:
            cursor.close()
        return row is not None

    def _count_open_events(self) -> int:
        try:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    "SELECT COUNT(*) FROM research.me_short_a_event_lineage_v1 l "
                    "LEFT JOIN research.me_short_a_outcome_observation_v1 o "
                    "ON o.event_key=l.event_key WHERE o.event_key IS NULL")
                row = cursor.fetchone()
            finally:
                cursor.close()
        except Exception:  # pragma: no cover - diagnostics must never raise
            return 0
        return int(row[0]) if row else 0

    def open_events(self) -> list[dict[str, Any]]:
        """Events whose 240m outcome is not settled yet (READ-ONLY).

        Derived from the append-only tables, so a restart resumes tracking
        without any mutable state.
        """
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT l.symbol,l.signal_time,l.reference_price,l.stop_price,"
                "l.target_price,l.event_key FROM "
                "research.me_short_a_event_lineage_v1 l "
                "LEFT JOIN research.me_short_a_outcome_observation_v1 o "
                "ON o.event_key=l.event_key WHERE o.event_key IS NULL "
                "ORDER BY l.signal_time")
            rows = cursor.fetchall()
        finally:
            cursor.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            signal_time = row[1]
            if isinstance(signal_time, datetime):
                signal_time = signal_time.replace(tzinfo=timezone.utc) \
                    if signal_time.tzinfo is None else signal_time.astimezone(timezone.utc)
            out.append({
                "symbol": row[0],
                "signal_time": signal_time,
                "reference_price": canonical_price(row[2]),
                "stop_price": canonical_price(row[3]),
                "target_price": canonical_price(row[4]),
                "event_key": row[5],
            })
        return out

    # ── heartbeat / diagnostics persistence ─────────────────────────
    def heartbeat(self) -> bool:
        """Append one heartbeat/diagnostics row. Operational metadata only.

        The heartbeat table holds no prices, no outcomes and no evidence; it
        exists so operators can see the runner's lifecycle and counters. It is
        still append-only (UPDATE/DELETE are blocked in the database).
        """
        snapshot = self.diagnostics()
        sql = (
            "INSERT INTO research.me_short_a_forward_outcome_runtime_heartbeat_v1 "
            "(experiment,runtime_protocol_version,runtime_protocol_hash,runner_id,"
            "generation,cycle_id,runner_state,lifecycle_phase,activation_status,"
            "activation_freeze_ts,started_at,observed_at,cycles_completed,"
            "candidates_seen,signals_captured,lineages_recorded,candles_captured,"
            "outcomes_finalized,outcomes_matched,outcomes_incomplete,"
            "skipped_not_activated,skipped_pre_freeze,skipped_idempotent,"
            "skipped_invalid_candidate,skipped_lookahead,skipped_unmatured,errors,"
            "last_event_time,last_error,open_events,config_fingerprint) "
            "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s "
            "ON CONFLICT DO NOTHING"
        )
        m = snapshot.metrics
        params = (
            EXPERIMENT, snapshot.protocol_version,
            snapshot.runtime_protocol_hash, snapshot.runner_id,
            snapshot.generation, self._cycle_id, snapshot.runner_state,
            snapshot.lifecycle_phase, snapshot.activation_status,
            snapshot.freeze_ts, snapshot.started_at, snapshot.observed_at,
            m["cycles_completed"], m["candidates_seen"], m["signals_captured"],
            m["lineages_recorded"], m["candles_captured"],
            m["outcomes_finalized"], m["outcomes_matched"],
            m["outcomes_incomplete"], m["skipped_not_activated"],
            m["skipped_pre_freeze"], m["skipped_idempotent"],
            m["skipped_invalid_candidate"], m["skipped_lookahead"],
            m["skipped_unmatured"], m["errors"],
            snapshot.last_event_time, snapshot.last_error,
            snapshot.open_events, snapshot.config_fingerprint,
        )
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, params)
            self._conn.commit()
            return cursor.rowcount == 1
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            log.exception("ME SHORT A runtime heartbeat write failed")
            return False
        finally:
            cursor.close()

    # ── one observation cycle ───────────────────────────────────────
    def run_cycle(self) -> RuntimeMetrics:
        """Run exactly one observe-only cycle and return the counters."""
        self._cycle_id += 1
        self._transition(RUNNER_STATE_RUNNING, "CYCLE_STARTED")
        try:
            status, freeze = self._activation_snapshot()
            self._config = replace_activation(self._config, status, freeze)
            if not self._config.activated:
                self._metrics.add("skipped_not_activated")
                self._transition(RUNNER_STATE_RUNNING, "BLOCKED_NOT_ACTIVATED")
                self._metrics.add("cycles_completed")
                self.heartbeat()
                return self._metrics

            self._transition(RUNNER_STATE_RUNNING, "OBSERVING")
            self._observe_candidates(freeze)
            self._advance_open_events(freeze)
            self._metrics.add("cycles_completed")
            self._transition(RUNNER_STATE_RUNNING, "CYCLE_FINISHED")
        except Exception as exc:  # pragma: no cover - defensive
            self.fail(f"{type(exc).__name__}: {exc}")
            log.exception("ME SHORT A forward runtime cycle failed")
        try:
            self.heartbeat()
        except Exception:  # pragma: no cover
            log.exception("ME SHORT A runtime heartbeat failed")
        return self._metrics

    def run_forever(self, *, max_cycles: int | None = None) -> RuntimeMetrics:
        """Run cycles until stop is requested. Used by the systemd unit."""
        self.start()
        cycles = 0
        while self._state in (RUNNER_STATE_RUNNING, RUNNER_STATE_STOPPING):
            self.run_cycle()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            if self._state == RUNNER_STATE_STOPPING:
                break
            self._sleep(self._config.idle_sleep_seconds)
        self.stop("RUN_COMPLETED" if self._state != RUNNER_STATE_FAILED else "FAILED")
        return self._metrics

    # ── candidate intake (keyset pagination) ──────────────────────
    def _observe_candidates(self, freeze: datetime) -> None:
        """Consume every post-freeze candidate page, without losing any.

        Contract:

        * the immutable ``freeze_ts`` is the strict lower bound, applied by the
          source itself, so a pre-freeze backlog is never re-read and can
          never occupy a page or push a post-freeze row out of one;
        * the keyset cursor advances only over pages that were handled in
          full — a surfaced database error stops intake at that exact page and
          leaves the cursor where it was, so the same rows are retried next
          cycle and are never lost;
        * pages are traversed to exhaustion inside one cycle, so a batch
          larger than ``page_size`` is fully observed.
        """
        while True:
            try:
                page = self._candidates.fetch_page(
                    self._candidate_cursor,
                    freeze_ts=freeze,
                    page_size=self._page_size,
                )
            except Exception as exc:
                self._metrics.add("errors")
                self._last_error = f"candidate page read failed: {type(exc).__name__}: {exc}"
                log.exception("ME SHORT A candidate page read failed")
                return

            for rejected_id in page.rejected:
                self._metrics.add("skipped_invalid_candidate")
                self._last_error = f"candidate row rejected: {rejected_id}"
                log.warning(
                    "ME SHORT A runtime rejected candidate row %s", rejected_id)

            if page.rows_read <= 0:
                return

            stalled = False
            for candidate in page.candidates:
                self._metrics.add("candidates_seen")
                try:
                    outcome = self._handle_candidate(candidate, freeze)
                except Exception as exc:
                    self._metrics.add("errors")
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("ME SHORT A candidate handling failed")
                    outcome = HANDLER_RETRY
                if outcome not in HANDLER_OUTCOMES:
                    self._metrics.add("errors")
                    self._last_error = f"unknown handler outcome {outcome!r}"
                    outcome = HANDLER_RETRY
                if outcome == HANDLER_RETRY:
                    stalled = True
                    break

            if stalled:
                # Do NOT advance the keyset cursor: the rest of this page and
                # everything behind it stays reachable and is retried. A page
                # is never skipped, because skipping it would silently lose
                # candidates.
                self._last_error = (
                    self._last_error or "candidate page stalled; retrying"
                )
                log.error(
                    "ME SHORT A forward runtime stalled at cursor=%s; "
                    "retrying the same page next cycle",
                    self._candidate_cursor)
                return

            self._candidate_cursor = page.next_cursor
            self._last_event_time = max(
                filter(None, [
                    self._last_event_time,
                    *(c.signal_time for c in page.candidates),
                ]), default=self._last_event_time)
            if page.exhausted or page.rows_read < self._page_size:
                return

    def _handle_candidate(
        self, candidate: Candidate, freeze: datetime,
    ) -> str:
        """Capture one candidate. Returns a HANDLER_* outcome.

        ``HANDLER_RETRY`` means the evidence chain could NOT be confirmed and
        the keyset cursor must not move past this candidate. It is returned
        instead of writing a record that would be permanently wrong: the
        capture tables are append-only, so a lineage row written with a false
        ``source_signal_present`` or a false ``phase1_linked`` can never be
        repaired and would show up as a bogus ``MISSING_LINEAGE`` /
        ``QUARANTINED`` forever.
        """
        token = candidate.event_id or (
            f"{candidate.symbol}|{candidate.signal_time.isoformat()}|"
            f"{candidate.reference_price}|{candidate.invalidation_price}|"
            f"{candidate.target_1}")
        if self._already_seen(token):
            self._metrics.add("skipped_idempotent")
            return HANDLED_SKIPPED

        if candidate.signal_time <= freeze:
            self._metrics.add("skipped_pre_freeze")
            self._remember(token)
            return HANDLED_SKIPPED

        if candidate.signal_time.utcoffset() != timedelta(0):
            self._metrics.add("skipped_invalid_candidate")
            self._remember(token)
            return HANDLED_SKIPPED

        missing = [
            name for name, value in (
                ("trigger_5m_close", candidate.trigger_5m_close),
                ("entry_zone_low", candidate.entry_zone_low),
                ("entry_zone_high", candidate.entry_zone_high),
            ) if value is None
        ]
        missing.extend(
            name for name in candidate.missing_fields if name not in missing)
        source_quality_flags = list(candidate.source_quality_flags)
        window = list(candidate.recent_high_window or [])
        no_lookahead_flags: list[str] = []
        now = self._clock()
        for candle in window:
            if candle.close_time > candidate.signal_time:
                self._metrics.add("skipped_lookahead")
                self._remember(token)
                return HANDLED_SKIPPED
            if candle.retrieved_at > now:
                self._metrics.add("skipped_lookahead")
                self._remember(token)
                return HANDLED_SKIPPED
        if window:
            no_lookahead_flags.append("recent_high_window_closed_before_signal")
        else:
            if "recent_high_window_candles" not in missing:
                missing.append("recent_high_window_candles")
            if "post_signal_candle_highs_240m" not in missing:
                missing.append("post_signal_candle_highs_240m")
            if "source_window_unavailable" not in source_quality_flags:
                source_quality_flags.append("source_window_unavailable")

        signal_candle_close = None
        if window:
            signal_candle_close = max(c.close_time for c in window)

        key = event_key_for(candidate)
        captured = capture_source_signal(
            self._conn,
            symbol=candidate.symbol,
            signal_time=candidate.signal_time,
            reference_price=candidate.reference_price,
            stop_price=candidate.invalidation_price,
            target_price=candidate.target_1,
            trigger_5m_close=candidate.trigger_5m_close,
            entry_zone_low=candidate.entry_zone_low,
            entry_zone_high=candidate.entry_zone_high,
            signal_candle_close_time=signal_candle_close,
            recent_high_window=[
                {
                    "symbol": c.symbol,
                    "open_time": c.open_time,
                    "close_time": c.close_time,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "source": c.source,
                } for c in window
            ],
            market_context=candidate.market_context or None,
            signal_time_quote=candidate.signal_time_quote or None,
            missing_fields=missing,
            source_quality_flags=source_quality_flags,
            no_lookahead_flags=no_lookahead_flags,
            source_observation_ids=(
                [candidate.source_observation_id]
                if candidate.source_observation_id is not None else None),
        )

        # `captured` only tells us whether THIS INSERT created a row. On a
        # re-run, on ON CONFLICT DO NOTHING and during partial recovery it is
        # False even though the append-only row exists. The lineage record
        # must report the table's real state, so confirm it (READ-ONLY).
        try:
            present = self._source_signal_present(key)
        except DatabaseAccessError as exc:
            self._metrics.add("errors")
            self._last_error = str(exc)
            log.error(
                "ME SHORT A runtime cannot confirm the source signal; "
                "retrying without writing lineage: %s", exc)
            return HANDLER_RETRY
        if not present:
            # The source signal row genuinely does not exist: the capture did
            # not land. Never write a lineage row claiming it did, and never
            # write one claiming `source_signal_missing` either — both would
            # be permanent, unrepairable lies on an append-only table.
            self._metrics.add("errors")
            self._last_error = (
                f"source_signal row not confirmed for event_key={key[:16]}…")
            log.error(
                "ME SHORT A runtime source signal missing after capture; "
                "retrying the same candidate")
            return HANDLER_RETRY

        if captured:
            self._metrics.add("signals_captured")
        else:
            self._metrics.add("skipped_idempotent")

        try:
            phase1 = self._phase1_lookup(key)
        except DatabaseAccessError as exc:
            self._metrics.add("errors")
            self._last_error = str(exc)
            log.error(
                "ME SHORT A runtime Phase 1 linkage lookup failed; "
                "retrying without writing lineage: %s", exc)
            return HANDLER_RETRY

        linked = record_event_lineage(
            self._conn,
            symbol=candidate.symbol,
            signal_time=candidate.signal_time,
            reference_price=candidate.reference_price,
            stop_price=candidate.invalidation_price,
            target_price=candidate.target_1,
            event_key_stored=key,
            source_observation_id=candidate.source_observation_id,
            phase1_event_key=phase1.event_key,
            phase1_observation_id=phase1.observation_id,
            phase1_source_observation_ids=(
                list(phase1.source_observation_ids) or None),
            source_signal_present=present,
        )
        if linked:
            self._metrics.add("lineages_recorded")
        else:
            self._metrics.add("skipped_idempotent")

        self._last_event_time = max(
            filter(None, [self._last_event_time, candidate.signal_time]))
        self._remember(token)
        return HANDLED

    # ── candle + outcome advancement ────────────────────────────────
    def _advance_open_events(self, freeze: datetime) -> None:
        now = self._clock()
        for event in self.open_events():
            try:
                self._advance_event(event, freeze, now)
            except Exception as exc:  # pragma: no cover - defensive
                self._metrics.add("errors")
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("ME SHORT A event advancement failed")

    def _advance_event(
        self, event: dict[str, Any], freeze: datetime, now: datetime,
    ) -> None:
        symbol = event["symbol"]
        signal_time = event["signal_time"]
        ref = event["reference_price"]
        stop = event["stop_price"]
        target = event["target_price"]

        window_start, last_open, horizon_end = window_bounds(signal_time)
        rows = self._candles.closed_candles(
            symbol, start=window_start,
            end=last_open + timedelta(minutes=CANDLE_MINUTES))

        for candle in rows:
            if candle.close_time > now:
                continue
            inserted = capture_post_signal_candle(
                self._conn,
                event_key_value=event["event_key"],
                symbol=symbol,
                signal_time=signal_time,
                reference_price=ref,
                stop_price=stop,
                target_price=target,
                candle={
                    "symbol": candle.symbol,
                    "open_time": candle.open_time,
                    "close_time": candle.close_time,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "source": candle.source,
                    "is_closed": True,
                },
            )
            if inserted:
                self._metrics.add("candles_captured")

        # Finalize ONLY once the horizon is settled on CLOSED 5m candles.
        settled = [c for c in rows if c.close_time <= now]
        if not settled:
            return
        if max(c.close_time for c in settled) < horizon_end:
            self._metrics.add("skipped_unmatured")
            return

        missing_opens = [
            op for op in expected_window_opens(signal_time)
            if not any(c.open_time == op for c in settled)
        ]
        source_quality_flags: list[str] = []
        missing_fields: list[str] = [f"candle_open_{op.isoformat()}"
                                     for op in missing_opens]
        if missing_opens:
            source_quality_flags.append("post_signal_candles_incomplete")

        inserted = finalize_outcome(
            self._conn,
            symbol=symbol,
            signal_time=signal_time,
            reference_price=ref,
            stop_price=stop,
            target_price=target,
            asof_time=now,
            candles=[
                {
                    "symbol": c.symbol,
                    "open_time": c.open_time,
                    "close_time": c.close_time,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "source": c.source,
                    "is_closed": True,
                } for c in settled
            ],
            source_quality_flags=source_quality_flags,
            missing_fields=missing_fields,
        )
        if inserted:
            self._metrics.add("outcomes_finalized")
        else:
            self._metrics.add("skipped_idempotent")


def replace_activation(config: RuntimeConfig, status: str | None,
                       freeze_ts: datetime | None) -> RuntimeConfig:
    """Return a copy of ``config`` with a refreshed activation snapshot."""
    return RuntimeConfig(
        experiment=config.experiment,
        protocol_version=config.protocol_version,
        protocol_hash=config.protocol_hash,
        enabled=config.enabled,
        freeze_ts=freeze_ts,
        activation_status=status,
        heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        diagnostics_retention_rows=config.diagnostics_retention_rows,
        idle_sleep_seconds=config.idle_sleep_seconds,
        max_events_per_cycle=config.max_events_per_cycle,
        registry_path=config.registry_path,
    )


def event_key_for(candidate: Candidate) -> str:
    """The deterministic ``event_key`` shared with Phase 1 for a candidate."""
    return compute_event_key(
        candidate.symbol, LINEAGE_DIRECTION, candidate.signal_time,
        candidate.reference_price, candidate.invalidation_price,
        candidate.target_1)


# ── configuration resolution ──────────────────────────────────────────

def resolve_config(
    *,
    registry: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    registry_path: os.PathLike[str] | str | None = None,
) -> RuntimeConfig:
    """Resolve the immutable runtime configuration. Fail-closed by design."""
    env = os.environ if environ is None else environ
    document = dict(registry) if registry is not None else load_registry(registry_path)
    runtime = dict(document.get("runtime") or {})
    enabled_raw = str(env.get(FEATURE_FLAG, "")).strip()
    enabled = enabled_raw == "1"
    return RuntimeConfig(
        experiment=str(document.get("experiment") or EXPERIMENT),
        protocol_version=str(document.get("protocol_version") or PROTOCOL_VERSION),
        protocol_hash=str(document.get("runtime_protocol_hash")
                          or runtime_protocol_hash()),
        enabled=enabled,
        freeze_ts=None,
        activation_status=None,
        heartbeat_interval_seconds=int(
            runtime.get("heartbeat_interval_seconds") or 60),
        diagnostics_retention_rows=int(
            runtime.get("diagnostics_retention_rows") or 2000),
        idle_sleep_seconds=float(runtime.get("idle_sleep_seconds") or 5.0),
        max_events_per_cycle=int(runtime.get("max_events_per_cycle") or 500),
        registry_path=str(registry_path or DEFAULT_REGISTRY_PATH),
    )
