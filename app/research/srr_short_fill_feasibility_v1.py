"""Issue #259 — ``SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1``.

Independent, prospective-only, research-only feasibility capture for SRR
SHORT signals. It answers one question the frozen research result cannot:

    does ``Net E[R] = +0.064721R`` (computed on the MODEL entry
    ``reference_price``) survive when the entry must be filled at the
    actually available Bybit book price at signal time?

Design invariants
-----------------
* **Prospective only.** Capture requires the runtime flag
  ``SRR_SHORT_FILL_CAPTURE_ENABLED=1`` AND an explicit DB activation row
  (``research.srr_short_fill_activation_v1``) in ``RUNNING`` state with an
  immutable ``activation_ts``. Only ``signal_time > activation_ts`` events
  are eligible. Backfill is impossible by construction (runtime check plus
  a DB ``CHECK`` on the same boundary). The shipped default is DISABLED and
  nothing in this module can activate itself.
* **No Bybit call inside the prospective observer transaction.** The
  scanner wrapper invokes :func:`capture` only AFTER
  ``ProspectiveOOSObserver.observe()`` has committed, reusing the observer's
  dedicated connection for short research transactions of its own.
* **Deterministic event identity.** Every row is bound to EXACTLY ONE
  source SRR prospective ``observation_id`` (verified against
  ``research.prospective_observation`` — the id list returned by
  ``observe()`` may contain ids of other experiments and is never trusted
  wholesale). ``event_key`` is a sha256 over that identity.
* **Atomic claim before any API call.** A ``PENDING`` row is reserved with
  ``ON CONFLICT DO NOTHING`` and committed BEFORE the orderbook request, so
  a concurrent duplicate can never trigger a second Bybit call. Replayed
  events return ``False`` without side effects.
* **Bounded, observable API budget.** At most
  ``SRR_SHORT_FILL_MAX_PER_CYCLE`` orderbook captures per scanner cycle and
  ``SRR_SHORT_FILL_MAX_PER_RUN`` per process, through the existing
  budgeted/rate-limited ``BybitClient._public_get`` (no limiter bypass, no
  retry layer of our own). Every denial is counted for coverage accounting.
* **Fail-open for the scanner, fail-closed for the research verdict.** Any
  error here is swallowed by the wrapper: it can never stop the scanner,
  roll back an observation, block other OOS experiments or touch
  Paper/Live. A capture whose quote, timing or constraints are not
  verified is stored but marked NOT eligible for execution statistics.

Cost model (SRR-adapted — NOT copied from ME SHORT A)
-----------------------------------------------------
Frozen SRR geometry::

    structural_R = invalidation_price - reference_price
    execution_R  = 2.0 * structural_R
    variant_stop = reference_price + execution_R
    variant_target = reference_price - 1.5 * structural_R

Observed quote-side round-trip cost of the MODEL entry vs the executable
SHORT entry (single accounting pass — entry deviation, spread crossing and
taker fees each appear exactly once)::

    cost_pct = (reference - estimated_fill) / reference   # entry adverse dev
             + (ask - bid) / mid                          # exit crosses spread
             + 2 * FEE_PER_SIDE                           # taker fees both sides
    cost_r   = cost_pct * reference / execution_R         # frozen R units
    extra_r  = cost_r - 0.0021 * reference / execution_R  # vs frozen NORMAL

``observed_cost_r`` is a COST estimate in frozen ``execution_R`` units. It
is deliberately NOT a recomputed Net R: changing the entry changes the
TP/SL trajectory and the risk profile, so an execution-aligned outcome
requires a full trajectory recomputation with the executable entry — an
explicit next stage of this research, not part of this capture module.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.research.me_short_a_fill_feasibility_v1 import (
    BOOK_FUTURE_SKEW_MS,
    BOOK_STALENESS_LIMIT_MS,
    DEFAULT_NOTIONAL_SCENARIOS,
    ELEVATED_ROUND_TRIP,
    FEE_PER_SIDE,
    NORMAL_ROUND_TRIP,
    _scenario_result,
    instrument_constraints,
)

LOG = logging.getLogger(__name__)

# ── Experiment / event identity ──────────────────────────────────────────
EXPERIMENT_ID = "SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1"
SOURCE_EXPERIMENT_ID = (
    "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
)
SCANNER_NAME = "SUPPORT_RESISTANCE_REACTION"
DIRECTION = "SHORT"

# Frozen SRR execution geometry (prospective registry cost/normalization).
FROZEN_SL_R = Decimal("2.0")
FROZEN_TP_R = Decimal("1.5")

ORDERBOOK_ENDPOINT = "/v5/market/orderbook"

# ── Runtime activation / configuration flags (all fail-closed) ───────────
ENABLE_FLAG = "SRR_SHORT_FILL_CAPTURE_ENABLED"          # explicit "1" to arm
EXPECTED_ACTIVATION_ENV = "SRR_SHORT_FILL_EXPECTED_ACTIVATION_TS"  # optional
QUOTE_DELAY_LIMIT_ENV = "SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS"        # required
MAX_PER_CYCLE_ENV = "SRR_SHORT_FILL_MAX_PER_CYCLE"
MAX_PER_RUN_ENV = "SRR_SHORT_FILL_MAX_PER_RUN"
# F1: cumulative wall-clock protection for the synchronous scanner cycle.
CYCLE_LATENCY_ENV = "SRR_SHORT_FILL_MAX_CYCLE_LATENCY_MS"
CAPTURE_LATENCY_ENV = "SRR_SHORT_FILL_MAX_CAPTURE_MS"
# F2: bounded quote workers — at most this many capture worker threads may
# exist at any moment; beyond the ceiling new captures are DENIED before
# their claim (coverage API_BUDGET_DENIED / WORKER_CAPACITY_EXHAUSTED)
# instead of leaking unbounded daemon threads against the shared client.
WORKERS_ENV = "SRR_SHORT_FILL_MAX_QUOTE_WORKERS"

DEFAULT_MAX_PER_CYCLE = 5
DEFAULT_MAX_PER_RUN = 200
HARD_MAX_PER_CYCLE = 20      # configuration can never exceed these caps
HARD_MAX_PER_RUN = 2000
DEFAULT_MAX_CYCLE_LATENCY_MS = 5000   # per scanner cycle, all captures
DEFAULT_MAX_CAPTURE_MS = 2000         # per single capture (worker join bound)
HARD_MAX_CYCLE_LATENCY_MS = 60000
HARD_MAX_CAPTURE_MS = 60000
DEFAULT_MAX_QUOTE_WORKERS = 3         # concurrent quote workers (ceiling)
HARD_MAX_QUOTE_WORKERS = 10

# Exchange instrument constraints are static enough to cache per symbol.
_INSTRUMENT_CACHE_TTL_S = 3600.0
_INSTRUMENT_CACHE: dict[str, tuple[float, dict | None, str | None]] = {}


class SrrFillCaptureError(RuntimeError):
    """Base class for fail-closed capture refusals (diagnostic only)."""


class SrrFillConfigError(SrrFillCaptureError):
    """Runtime configuration is missing or inconsistent."""


class SrrFillActivationError(SrrFillCaptureError):
    """DB activation record is absent, not RUNNING or not usable."""


class SrrFillIdentityError(SrrFillCaptureError):
    """Event identity could not be confirmed as an SRR SHORT event."""


# ── Observability counters (coverage accounting for the API budget) ─────
_STATS: dict[str, int] = {
    "attempts": 0,
    "flag_denied": 0,
    "identity_denied": 0,
    "config_denied": 0,
    "activation_denied": 0,
    "pre_activation_denied": 0,
    "duplicate_skipped": 0,
    "budget_denied": 0,
    "latency_denied": 0,
    "worker_capacity_denied": 0,   # F2: worker ceiling reached (pre-claim)
    "deadline_refused": 0,         # F1: worker skipped a new Bybit request
    "timing_denied": 0,
    "claimed": 0,
    "captured": 0,
    "capture_errors": 0,
    "finalize_failed": 0,
    "coverage_write_failed": 0,
}


def stats() -> dict[str, int]:
    """Snapshot of the capture counters (for logging / audit queries)."""
    return dict(_STATS)


def reset_stats() -> None:
    """Reset counters (tests / explicit audit tooling only)."""
    for key in _STATS:
        _STATS[key] = 0


# ── API budget ───────────────────────────────────────────────────────────


class _Budget:
    """Process-local API + latency budget (single-threaded by contract:
    capture runs exclusively from the main scan loop, so plain ``+=`` is
    safe and no lock is taken).

    ``cycle_latency_ms`` is the F1 cumulative wall-clock protection: every
    completed capture adds its measured elapsed time; a new capture is
    refused while less than one full ``max_capture_ms`` window remains.
    """

    def __init__(self) -> None:
        self.cycle_used = 0
        self.run_used = 0
        self.cycle_latency_ms = 0
        self._denied_logged = False

    def begin_cycle(self) -> None:
        self.cycle_used = 0
        self.cycle_latency_ms = 0
        self._denied_logged = False

    def snapshot(self) -> dict:
        return {
            "cycle_used": self.cycle_used,
            "run_used": self.run_used,
            "cycle_latency_ms": self.cycle_latency_ms,
        }

    def latency_slot_available(self, max_capture_ms: int,
                               cycle_limit_ms: int) -> tuple[bool, int]:
        """Fail-closed pre-flight: a full capture window must still fit."""
        remaining = cycle_limit_ms - self.cycle_latency_ms
        return remaining >= max_capture_ms, remaining

    def add_cycle_latency(self, elapsed_ms: int) -> None:
        if elapsed_ms > 0:
            self.cycle_latency_ms += int(elapsed_ms)


_BUDGET = _Budget()


def begin_capture_cycle() -> None:
    """Reset the per-scanner-cycle portions of the budget (API slots AND
    the F1 cumulative latency window).

    Called once at the top of ``run_scan_cycle``. Pure in-memory operation:
    it never touches the database, the network, or the activation state.
    The per-run counter intentionally survives cycles (it bounds the whole
    process) and resets only on process restart.
    """
    _BUDGET.begin_cycle()


def reset_budget() -> None:
    """Reset BOTH budget counters. Tests / offline audit tooling only —
    production code only ever calls :func:`begin_capture_cycle`."""
    _BUDGET.begin_cycle()
    _BUDGET.run_used = 0


def _read_limit(env_name: str, default: int, hard_cap: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise SrrFillConfigError(
            f"{env_name} must be a non-negative integer, got {raw!r}"
        ) from exc
    if value < 0:
        raise SrrFillConfigError(
            f"{env_name} must be a non-negative integer, got {raw!r}"
        )
    return min(value, hard_cap)


def _try_consume_budget() -> tuple[bool, str | None]:
    """Atomically (in-process) consume one API-budget slot."""
    cycle_limit = _read_limit(MAX_PER_CYCLE_ENV, DEFAULT_MAX_PER_CYCLE,
                              HARD_MAX_PER_CYCLE)
    run_limit = _read_limit(MAX_PER_RUN_ENV, DEFAULT_MAX_PER_RUN,
                            HARD_MAX_PER_RUN)
    if _BUDGET.cycle_used >= cycle_limit:
        if not _BUDGET._denied_logged:
            _BUDGET._denied_logged = True
            LOG.info(
                "SRR fill capture skipped: per-cycle API budget exhausted "
                "(%d/%d); coverage loss recorded",
                _BUDGET.cycle_used, cycle_limit,
            )
        _STATS["budget_denied"] += 1
        return False, "API_BUDGET_CYCLE_EXHAUSTED"
    if _BUDGET.run_used >= run_limit:
        if not _BUDGET._denied_logged:
            _BUDGET._denied_logged = True
            LOG.info(
                "SRR fill capture skipped: per-run API budget exhausted "
                "(%d/%d); coverage loss recorded",
                _BUDGET.run_used, run_limit,
            )
        _STATS["budget_denied"] += 1
        return False, "API_BUDGET_RUN_EXHAUSTED"
    _BUDGET.cycle_used += 1
    _BUDGET.run_used += 1
    return True, None


# ── Helpers ──────────────────────────────────────────────────────────────


def _datetime(value) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None \
        else dt.astimezone(timezone.utc)


def _decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _jsonable(value):
    """JSON encoder helper: Decimal -> str (exact), nested structures kept."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def srr_geometry(reference_price, invalidation_price) -> dict:
    """Frozen SRR SHORT geometry; raises ``ValueError`` when invalid."""
    ref = Decimal(str(reference_price))
    invalidation = Decimal(str(invalidation_price))
    if ref <= 0:
        raise ValueError("reference_price must be positive")
    if invalidation <= ref:
        raise ValueError("SHORT invalidation_price must be above reference")
    structural_r = invalidation - ref
    execution_r = FROZEN_SL_R * structural_r
    return {
        "reference_price": ref,
        "invalidation_price": invalidation,
        "structural_r": structural_r,
        "execution_r": execution_r,
        "variant_stop": ref + execution_r,
        "variant_target": ref - FROZEN_TP_R * structural_r,
    }


def _geometry_matches_frozen(g: dict, variant_stop, variant_target) -> bool:
    """Cross-check the observation's frozen variant geometry.

    ``variant_stop`` / ``variant_target`` were frozen by the prospective
    observer at observation time; they must agree with the frozen SRR
    geometry derived from ``reference_price``/``invalidation_price``.
    A disagreement means the event identity cannot be confirmed -> fail
    closed (no capture).
    """
    stop = _decimal(variant_stop)
    target = _decimal(variant_target)
    if stop is None or target is None:
        return False
    tolerance = max(Decimal("1e-9"), abs(g["reference_price"]) * Decimal("1e-9"))
    return (
        abs(stop - g["variant_stop"]) <= tolerance
        and abs(target - g["variant_target"]) <= tolerance
    )


# ── Static identity: registry + adapter cross-check ──────────────────────
_STATIC_IDENTITY_CACHE: dict[str, bool] = {}


def _static_identity_ok() -> bool:
    """Confirm scanner/experiment identity against registry + adapter.

    Cached after the first successful verification. Any I/O problem makes
    the check fail closed (``False``), which blocks capture — identity must
    be positively confirmed, never assumed.
    """
    cached = _STATIC_IDENTITY_CACHE.get("ok")
    if cached is not None:
        return cached
    try:
        from app.research.adapters.support_resistance import SCANNER_NAME as ADAPTER_SCANNER
        if ADAPTER_SCANNER != SCANNER_NAME:
            _STATIC_IDENTITY_CACHE["ok"] = False
            return False

        import pathlib
        registry_path = pathlib.Path(__file__).with_name("prospective_registry.json")
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
        experiments = raw.get("experiments") or raw
        if isinstance(experiments, dict):
            experiments = list(experiments.values())
        entry = next(
            (item for item in experiments
             if isinstance(item, dict)
             and item.get("experiment_id") == SOURCE_EXPERIMENT_ID),
            None,
        )
        ok = bool(entry) and entry.get("scanner_name") == SCANNER_NAME \
            and entry.get("direction") == DIRECTION
    except Exception:
        LOG.debug("SRR fill static identity verification failed", exc_info=True)
        ok = False
    _STATIC_IDENTITY_CACHE["ok"] = ok
    return ok


def clear_static_identity_cache() -> None:
    """Testing/audit helper: drop the cached registry verification."""
    _STATIC_IDENTITY_CACHE.clear()


# ── Runtime configuration (fail-closed) ──────────────────────────────────


@dataclass(frozen=True)
class RuntimeConfig:
    quote_delay_limit_ms: int
    expected_activation_ts: datetime | None
    max_cycle_latency_ms: int
    max_capture_ms: int
    # F2: concurrency ceiling for the capture quote workers. A capture that
    # finds the ceiling reached is denied BEFORE its claim and accounted in
    # the coverage ledger — no queue, no unbounded thread accumulation.
    max_quote_workers: int


def _load_runtime_config() -> RuntimeConfig:
    """Parse and validate the runtime activation configuration.

    Every problem raises :class:`SrrFillConfigError` (fail-closed): a
    missing/invalid quote-delay budget, a non-integer API budget or an
    unparseable expected activation timestamp all block capture instead of
    silently weakening it.
    """
    raw_delay = os.environ.get(QUOTE_DELAY_LIMIT_ENV)
    if raw_delay is None or not str(raw_delay).strip():
        raise SrrFillConfigError(
            f"{QUOTE_DELAY_LIMIT_ENV} must be set explicitly when capture is "
            "enabled (signal-to-quote delay budget is a forward-protocol "
            "parameter, never a hardcoded production default)"
        )
    try:
        delay_limit = int(str(raw_delay).strip())
    except (TypeError, ValueError) as exc:
        raise SrrFillConfigError(
            f"{QUOTE_DELAY_LIMIT_ENV} must be a positive integer (ms)"
        ) from exc
    if delay_limit <= 0:
        raise SrrFillConfigError(
            f"{QUOTE_DELAY_LIMIT_ENV} must be a positive integer (ms)"
        )

    # Budgets are validated here too so a bad value fails closed early.
    _read_limit(MAX_PER_CYCLE_ENV, DEFAULT_MAX_PER_CYCLE, HARD_MAX_PER_CYCLE)
    _read_limit(MAX_PER_RUN_ENV, DEFAULT_MAX_PER_RUN, HARD_MAX_PER_RUN)

    # F1 latency protection: both windows are positive integers, the hard
    # caps clamp them, and a per-capture window larger than the per-cycle
    # window would make every capture impossible -> config error instead.
    cycle_latency_ms = _read_limit(CYCLE_LATENCY_ENV,
                                   DEFAULT_MAX_CYCLE_LATENCY_MS,
                                   HARD_MAX_CYCLE_LATENCY_MS)
    max_capture_ms = _read_limit(CAPTURE_LATENCY_ENV,
                                 DEFAULT_MAX_CAPTURE_MS,
                                 HARD_MAX_CAPTURE_MS)
    if cycle_latency_ms <= 0 or max_capture_ms <= 0:
        raise SrrFillConfigError(
            f"{CYCLE_LATENCY_ENV} and {CAPTURE_LATENCY_ENV} must be "
            f"positive integers (ms); got cycle={cycle_latency_ms} "
            f"capture={max_capture_ms}")
    if max_capture_ms > cycle_latency_ms:
        raise SrrFillConfigError(
            f"{CAPTURE_LATENCY_ENV} ({max_capture_ms} ms) must not exceed "
            f"{CYCLE_LATENCY_ENV} ({cycle_latency_ms} ms)")

    # F2 worker ceiling: a positive integer, hard-clamped. A capture that
    # cannot get a worker slot is denied BEFORE its claim and accounted in
    # the coverage ledger (API_BUDGET_DENIED / WORKER_CAPACITY_EXHAUSTED).
    max_quote_workers = _read_limit(WORKERS_ENV, DEFAULT_MAX_QUOTE_WORKERS,
                                    HARD_MAX_QUOTE_WORKERS)
    if max_quote_workers < 1:
        raise SrrFillConfigError(
            f"{WORKERS_ENV} must be a positive integer (>= 1), "
            f"got {max_quote_workers}")

    expected = None
    raw_expected = os.environ.get(EXPECTED_ACTIVATION_ENV)
    if raw_expected is not None and str(raw_expected).strip():
        try:
            expected = _datetime(str(raw_expected).strip())
        except (TypeError, ValueError) as exc:
            raise SrrFillConfigError(
                f"{EXPECTED_ACTIVATION_ENV} is not a valid ISO-8601 "
                f"timestamp: {raw_expected!r}"
            ) from exc
    return RuntimeConfig(
        quote_delay_limit_ms=delay_limit,
        expected_activation_ts=expected,
        max_cycle_latency_ms=cycle_latency_ms,
        max_capture_ms=max_capture_ms,
        max_quote_workers=max_quote_workers,
    )


def _load_activation(conn) -> datetime:
    """Read the authoritative DB activation record; fail closed on any doubt.

    The runtime NEVER writes this table: restarting the Scanner (or
    anything else) cannot move the activation boundary.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT status, activation_ts FROM "
            "research.srr_short_fill_activation_v1 WHERE experiment_id=%s",
            (EXPERIMENT_ID,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row:
        raise SrrFillActivationError("activation record is absent")
    status, activation_ts = row[0], row[1]
    if status != "RUNNING":
        raise SrrFillActivationError(
            f"activation status is {status!r}, not 'RUNNING'")
    if activation_ts is None:
        raise SrrFillActivationError("activation record has no activation_ts")
    try:
        boundary = _datetime(activation_ts)
    except (TypeError, ValueError) as exc:
        raise SrrFillActivationError(
            f"activation_ts is not a usable UTC timestamp: {activation_ts!r}"
        ) from exc
    return boundary


# ── Observation identity resolution ──────────────────────────────────────


def _resolve_observations(conn, observation_ids, *, symbol: str,
                          expected_signal_time=None,
                          expected_reference=None) -> list[dict]:
    """Confirm which returned ids really are SRR SHORT observations.

    ``ProspectiveOOSObserver.observe()`` returns ids for potentially SEVERAL
    experiments in one call. Only rows that are provably
    ``SOURCE_EXPERIMENT_ID`` + ``SHORT`` + the scanned symbol, matching the
    candidate's own ``signal_time``/``reference_price``, with a valid frozen
    geometry are eligible for capture; everything else is ignored.
    """
    resolved: list[dict] = []
    signal_ref = _datetime(expected_signal_time) \
        if expected_signal_time is not None else None
    reference_ref = _decimal(expected_reference)
    for obs_id in observation_ids:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT observation_id, experiment_id, direction, symbol, "
                "signal_time, reference_price, invalidation_price, "
                "variant_stop, variant_target "
                "FROM research.prospective_observation WHERE observation_id=%s",
                (obs_id,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        if not row:
            continue
        (db_obs_id, experiment_id, direction, db_symbol, signal_time,
         reference_price, invalidation_price, variant_stop,
         variant_target) = row[:9]
        if experiment_id != SOURCE_EXPERIMENT_ID:
            continue
        if direction != DIRECTION:
            continue
        if db_symbol != symbol:
            continue
        # Cross-bind to the candidate that produced this id: the very same
        # signal_time and (within float noise) reference_price the scanner
        # passed to observe(). A mismatch means the id does not belong to
        # THIS event -> fail closed for this candidate.
        if signal_ref is not None and _datetime(signal_time) != signal_ref:
            continue
        ref = _decimal(reference_price)
        invalidation = _decimal(invalidation_price)
        if ref is None or invalidation is None:
            continue
        if reference_ref is not None:
            tolerance = max(abs(reference_ref) * Decimal("1e-6"),
                            Decimal("1e-9"))
            if abs(ref - reference_ref) > tolerance:
                continue
        try:
            geometry = srr_geometry(ref, invalidation)
        except ValueError:
            continue
        if not _geometry_matches_frozen(geometry, variant_stop, variant_target):
            continue
        resolved.append({
            "observation_id": db_obs_id,
            "experiment_id": experiment_id,
            "direction": direction,
            "symbol": db_symbol,
            "signal_time": _datetime(signal_time),
            "reference_price": geometry["reference_price"],
            "invalidation_price": geometry["invalidation_price"],
            "structural_r": geometry["structural_r"],
            "execution_r": geometry["execution_r"],
            "variant_stop": geometry["variant_stop"],
            "variant_target": geometry["variant_target"],
        })
    return resolved


def _event_key(obs: dict) -> str:
    """Deterministic idempotent identity of one fill-capture event."""
    payload = "|".join([
        "SRR_SHORT_FILL_V1",
        str(obs["observation_id"]),
        obs["symbol"],
        DIRECTION,
        obs["signal_time"].isoformat(),
        str(obs["reference_price"]),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Pure snapshot calculation (SRR-adapted) ──────────────────────────────


def calculate_snapshot(reference_price, structural_r, book, *,
                       notional_scenarios=None, constraints=None,
                       constraints_error=None, now_ms=None,
                       fee_per_side=FEE_PER_SIDE,
                       normal_round_trip=NORMAL_ROUND_TRIP,
                       elevated_round_trip=ELEVATED_ROUND_TRIP,
                       stale_limit_ms=BOOK_STALENESS_LIMIT_MS):
    """Pure, testable SRR SHORT fill calculations from one orderbook.

    Returns ``status`` of the BASE (first) scenario plus the per-scenario
    results. Book-level failures (invalid / stale / future) mark every
    scenario infeasible. Costs are normalized by the FROZEN
    ``execution_R = 2.0 * structural_R`` and are single-counted: entry
    deviation, exit spread crossing and taker fees each appear exactly once
    (see module docstring).
    """
    ref = Decimal(str(reference_price))
    structural = Decimal(str(structural_r))
    if ref <= 0 or structural <= 0:
        raise ValueError("invalid SRR SHORT geometry")
    execution_r = FROZEN_SL_R * structural
    variant_stop = ref + execution_r
    variant_target = ref - FROZEN_TP_R * structural
    scenarios = tuple(
        Decimal(str(item)) for item in
        (notional_scenarios if notional_scenarios is not None
         else DEFAULT_NOTIONAL_SCENARIOS)
    )
    if not scenarios or any(item <= 0 for item in scenarios):
        raise ValueError("notional scenarios must be positive")

    def _book_failure(status: str, reason: str) -> dict:
        return {
            "status": status, "reason": reason,
            "structural_r": structural, "execution_r": execution_r,
            "variant_stop": variant_stop, "variant_target": variant_target,
            "scenario_results": [],
        }

    result = book.get("result") or {}
    bids, asks = result.get("b") or [], result.get("a") or []
    ts_raw = result.get("ts") or book.get("time")
    ts = int(ts_raw) if ts_raw is not None else None
    if not bids or not asks or ts is None:
        return _book_failure("INVALID_BOOK", "missing bid/ask/book timestamp")
    bid, ask = Decimal(str(bids[0][0])), Decimal(str(asks[0][0]))
    if bid <= 0 or ask < bid:
        return _book_failure("INVALID_BOOK", "crossed/invalid top of book")
    now = int(now_ms) if now_ms is not None else int(
        datetime.now(timezone.utc).timestamp() * 1000)
    if ts > now + BOOK_FUTURE_SKEW_MS:
        return _book_failure(
            "FUTURE_BOOK",
            f"book timestamp more than {BOOK_FUTURE_SKEW_MS}ms in the future")
    if now - ts > stale_limit_ms:
        return _book_failure(
            "STALE_BOOK",
            f"book timestamp outside the {stale_limit_ms // 1000}s bound")

    mid = (ask + bid) / 2
    spread_pct = (ask - bid) / mid
    spread_bps = spread_pct * 10000
    depth = sum(
        Decimal(str(px)) * Decimal(str(qty)) for px, qty in bids
        if Decimal(str(px)) >= bid * Decimal("0.999")
    )
    scenario_results = []
    for notional in scenarios:
        scenario = _scenario_result(notional, ref, bids, constraints)
        fill = scenario["estimated_fill"]
        if fill is not None:
            # Single accounting pass: entry adverse deviation + exit spread
            # crossing + both taker fees. Never add these again downstream.
            entry_dev_pct = (ref - fill) / ref
            cost_pct = entry_dev_pct + spread_pct + 2 * fee_per_side
            cost_r = cost_pct * ref / execution_r
            frozen_normal_r = normal_round_trip * ref / execution_r
            frozen_elevated_r = elevated_round_trip * ref / execution_r
            scenario["observed_roundtrip_cost_pct"] = cost_pct
            scenario["observed_cost_r"] = cost_r
            scenario["frozen_normal_cost_r"] = frozen_normal_r
            scenario["frozen_elevated_cost_r"] = frozen_elevated_r
            scenario["extra_cost_r_vs_frozen_normal"] = cost_r - frozen_normal_r
            scenario["entry_deviation_pct"] = entry_dev_pct
            if cost_pct <= normal_round_trip:
                scenario["cost_class"] = "NORMAL"
            elif cost_pct <= elevated_round_trip:
                scenario["cost_class"] = "ELEVATED"
            else:
                scenario["cost_class"] = "ABOVE_ELEVATED"
        else:
            scenario["observed_roundtrip_cost_pct"] = None
            scenario["observed_cost_r"] = None
            scenario["frozen_normal_cost_r"] = None
            scenario["frozen_elevated_cost_r"] = None
            scenario["extra_cost_r_vs_frozen_normal"] = None
            scenario["entry_deviation_pct"] = None
            scenario["cost_class"] = None
        scenario_results.append(scenario)

    base = scenario_results[0]
    return {
        "status": base["status"], "reason": base["reason"],
        "bid": bid, "ask": ask, "book_time_ms": ts, "book_age_ms": now - ts,
        "spread_bps": spread_bps,
        "reference_to_bid_bps": (ref - bid) / ref * 10000,
        "depth_bid_10bps_usdt": depth,
        "structural_r": structural,
        "execution_r": execution_r,
        "variant_stop": variant_stop,
        "variant_target": variant_target,
        "requested_notional_usdt": base["notional_usdt"],
        "requested_qty": base["requested_qty"],
        "executable_qty": base["executable_qty"],
        "estimated_fill": base["estimated_fill"],
        "entry_slippage_bps": base["entry_slippage_bps"],
        "observed_roundtrip_cost_pct": base["observed_roundtrip_cost_pct"],
        "observed_cost_r": base["observed_cost_r"],
        "frozen_normal_cost_r": base["frozen_normal_cost_r"],
        "frozen_elevated_cost_r": base["frozen_elevated_cost_r"],
        "extra_cost_r_vs_frozen_normal": base["extra_cost_r_vs_frozen_normal"],
        "cost_class": base["cost_class"],
        "scenario_results": scenario_results,
        "constraints": {
            "known": constraints is not None,
            "min_order_qty": (constraints or {}).get("min_order_qty"),
            "qty_step": (constraints or {}).get("qty_step"),
            "min_notional": (constraints or {}).get("min_notional"),
            "lookup_error": constraints_error,
        },
    }


# ── Capture ──────────────────────────────────────────────────────────────

_FINALIZE_COLUMNS = (
    "captured_at",
    "capture_finished_at",
    "book_time_ms",
    "book_age_ms",
    "signal_to_capture_ms",
    "signal_to_quote_ms",
    "api_request_ms",
    "bid",
    "ask",
    "spread_bps",
    "reference_to_bid_bps",
    "depth_bid_10bps_usdt",
    "requested_notional_usdt",
    "requested_qty",
    "executable_qty",
    "min_order_qty",
    "qty_step",
    "min_notional",
    "estimated_fill",
    "entry_slippage_bps",
    "observed_roundtrip_cost_pct",
    "observed_cost_r",
    "frozen_normal_cost_r",
    "frozen_elevated_cost_r",
    "extra_cost_r_vs_frozen_normal",
    "cost_class",
    "quote_quality_status",
    "feasibility_status",
    "eligible_for_execution_stats",
    "failure_reason",
    "exchange_constraints",
    "scenario_results",
    "source_diagnostics",
    "raw_snapshot",
)


def _finalize_row(conn, event_key: str, payload: dict) -> bool:
    """Store the final evidence into the reserved PENDING row (one shot)."""
    assignments = ", ".join(f"{col}=%s" for col in _FINALIZE_COLUMNS)
    params = []
    for col in _FINALIZE_COLUMNS:
        value = payload.get(col)
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(_jsonable(value))
        params.append(value)
    params.append(event_key)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE research.srr_short_fill_feasibility_v1 SET " + assignments
            + " WHERE event_key=%s AND feasibility_status='PENDING'",
            tuple(params),
        )
        updated = cursor.rowcount == 1
        conn.commit()
        return updated
    finally:
        cursor.close()


# ── F3: persistent coverage accounting ──────────────────────────────────
# One row per prospective source event whose attempt did NOT produce an
# evidence row. The coverage DENOMINATOR always comes from the independent
# prospective source (research.prospective_observation), never from these
# rows or from capture rows — a missing row can only DECREASE measured
# coverage, never inflate it.
COVERAGE_DISPOSITIONS = (
    "POLICY_DENIED",          # flag-adjacent policy/config/activation/boundary
    "API_BUDGET_DENIED",      # per-cycle or per-run API budget exhausted
    "LATENCY_BUDGET_DENIED",  # F1 cumulative/per-capture window exhausted
    "DUPLICATE",              # evidence row already exists (replay)
    "CLAIM_RACE_LOST",        # concurrent claimant owns this event
)


def _record_coverage(conn, observations, disposition: str,
                     reason: str | None) -> None:
    """Persist a non-capturing attempt (F3). Never raises.

    First disposition wins (``ON CONFLICT DO NOTHING``); a later successful
    capture is represented by the evidence row itself, which takes
    precedence in analysis. A failed coverage write only bumps a counter:
    it must never affect the scanner or the capture decision.
    """
    rows = list(observations or ())
    if not rows or disposition not in COVERAGE_DISPOSITIONS:
        return
    cursor = None
    try:
        cursor = conn.cursor()
        for obs in rows:
            cursor.execute(
                """INSERT INTO research.srr_short_fill_coverage_v1
                (observation_id, experiment_id, symbol, signal_time,
                 disposition, reason)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (observation_id) DO NOTHING""",
                (
                    obs["observation_id"], EXPERIMENT_ID, obs["symbol"],
                    obs["signal_time"], disposition, reason,
                ),
            )
        conn.commit()
    except Exception:
        _STATS["coverage_write_failed"] += 1
        LOG.warning(
            "SRR coverage record failed (capture decision unchanged)",
            exc_info=True)
        try:
            conn.rollback()
        except Exception:
            LOG.debug("SRR coverage rollback failed", exc_info=True)
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass


def capture(conn, client, *, scanner_name, direction, symbol, signal_time,
            reference_price, invalidation_price, observation_ids):
    """Capture one signal-time quote for each confirmed SRR SHORT event.

    Returns ``True`` only when this call reserved AND finalized a new row.
    Refusals that happen AFTER the observation ids were resolved (policy,
    activation, boundary, latency budget, API budget, duplicate, lost
    claim race) are persisted into ``research.srr_short_fill_coverage_v1``
    (F3 coverage accounting) before returning ``False``; flag/identity
    refusals and clock anomalies predate resolution and are only counted
    in-process. Every internal error is swallowed with rollback + logging
    so the scanner and the prospective observer are never affected.
    """
    _STATS["attempts"] += 1
    # 1. Explicit feature flag — cheapest gate first, no DB, no network.
    if os.environ.get(ENABLE_FLAG) != "1":
        _STATS["flag_denied"] += 1
        return False
    # 2. Static event identity (scanner / direction).
    if scanner_name != SCANNER_NAME or direction != DIRECTION:
        _STATS["identity_denied"] += 1
        return False
    if not observation_ids:
        _STATS["identity_denied"] += 1
        return False
    if conn is None or client is None:
        _STATS["identity_denied"] += 1
        return False
    if not _static_identity_ok():
        LOG.warning(
            "SRR fill capture refused: registry/adapter identity "
            "verification failed (scanner=%s)", scanner_name)
        _STATS["identity_denied"] += 1
        return False

    try:
        # 3. Confirm which observation ids really are SRR SHORT events —
        #    BEFORE any policy gate so a rejected source event can still be
        #    recorded in the coverage table (denominator stays honest).
        observations = _resolve_observations(
            conn, observation_ids, symbol=symbol,
            expected_signal_time=signal_time,
            expected_reference=reference_price)
        if not observations:
            _STATS["identity_denied"] += 1
            return False

        # 4. Runtime configuration (fail-closed on any inconsistency).
        try:
            config = _load_runtime_config()
        except SrrFillConfigError:
            LOG.warning("SRR fill capture refused: configuration invalid",
                        exc_info=True)
            _STATS["config_denied"] += 1
            _record_coverage(conn, observations, "POLICY_DENIED", "config")
            return False

        # 5. Authoritative DB activation (read-only; never written here).
        try:
            activation_ts = _load_activation(conn)
        except SrrFillActivationError:
            LOG.debug("SRR fill capture refused: activation not granted",
                      exc_info=True)
            _STATS["activation_denied"] += 1
            _record_coverage(conn, observations, "POLICY_DENIED",
                             "activation")
            return False
        if config.expected_activation_ts is not None \
                and config.expected_activation_ts != activation_ts:
            LOG.warning(
                "SRR fill capture refused: configured activation_ts does not "
                "match the DB activation record (config=%s db=%s)",
                config.expected_activation_ts.isoformat(),
                activation_ts.isoformat(),
            )
            _STATS["config_denied"] += 1
            _record_coverage(conn, observations, "POLICY_DENIED",
                             "activation_ts_mismatch")
            return False

        captured_any = False
        for obs in observations:
            # 6. Strict prospective boundary: signal_time > activation_ts.
            if obs["signal_time"] <= activation_ts:
                _STATS["pre_activation_denied"] += 1
                _record_coverage(conn, [obs], "POLICY_DENIED",
                                 "pre_activation_boundary")
                continue
            # F1: charge the FULL wall-clock of this capture — dup-check,
            # claim, quote worker AND the DB finalization — to the cycle's
            # cumulative latency budget, on EVERY path (return or
            # exception), so a slow finalize can never hide from the
            # pre-flight of the next capture.
            capture_started = time.monotonic()
            try:
                if _capture_one(conn, client, obs, activation_ts, config):
                    captured_any = True
            finally:
                _BUDGET.add_cycle_latency(
                    int((time.monotonic() - capture_started) * 1000))
        return captured_any
    except Exception:
        try:
            conn.rollback()
        except Exception:
            LOG.debug("SRR fill capture rollback failed", exc_info=True)
        _STATS["capture_errors"] += 1
        LOG.exception(
            "SRR fill capture failed closed; scanner and observations "
            "unaffected; no orders")
        return False


# ── F1/F2: single monotonic deadline + bounded quote workers ────────────
# ``thread.join(timeout)`` only bounds how long the scanner thread WAITS —
# it is NOT a cancellation: a worker that outlives its capture keeps
# running. Two mechanisms replace that assumption:
#
#   1. DEADLINE PROPAGATION — every live worker carries the capture's
#      single monotonic ``deadline_at``. The worker checks it before
#      EVERY potentially blocking step (instrument lookup, limiter wait
#      inside the client, orderbook send) and passes the remaining budget
#      into the client, which re-checks it after each wait and before each
#      HTTP attempt (per-attempt timeout clamped to the remainder). No new
#      Bybit request ever starts after the deadline; an already in-flight
#      request is bounded by the real network timeout.
#   2. BOUNDED CONCURRENCY — at most ``max_quote_workers`` worker threads
#      exist at any moment. The main scan thread is the ONLY adder
#      (single-threaded capture contract); workers remove themselves in
#      ``finally``. Past the ceiling a capture is denied BEFORE its claim
#      and accounted in the coverage ledger — there is no queue and no
#      unbounded accumulation, even while requests hang.
_WORKERS_LOCK = threading.Lock()
_ACTIVE_WORKERS: set[threading.Thread] = set()


def _prune_workers_locked() -> None:
    for worker in [w for w in _ACTIVE_WORKERS if not w.is_alive()]:
        _ACTIVE_WORKERS.discard(worker)


def active_worker_count() -> int:
    """Live quote workers (finished threads pruned first). Monitoring."""
    with _WORKERS_LOCK:
        _prune_workers_locked()
        return len(_ACTIVE_WORKERS)


def _quote_worker_capacity_available(max_workers: int) -> bool:
    with _WORKERS_LOCK:
        _prune_workers_locked()
        return len(_ACTIVE_WORKERS) < max_workers


def _failure_reason_for(exc: BaseException) -> str:
    """F3 refusal taxonomy: shared-limiter denial vs Bybit vs transport.

    The client attaches ``limiter_result`` to every fail-closed shared
    limiter refusal (``DENIED`` / ``LIMITER_UNAVAILABLE`` / ...); surfacing
    it here separates "the shared limiter said no" from a plain Bybit or
    transport error without changing the client contract.
    """
    limiter_result = getattr(exc, "limiter_result", None)
    if limiter_result:
        return f"limiter:{limiter_result}"
    return f"orderbook:{type(exc).__name__}"


def _fetch_quote_phase(
    client: Any,
    symbol: str,
    *,
    deadline_at: float,
    max_workers: int,
) -> dict:
    """Instrument lookup + orderbook GET inside a bounded worker (F1/F2).

    ``deadline_at`` is the capture's single monotonic deadline (capture
    start + ``max_capture_ms``); ``max_workers`` is the live-worker
    ceiling. The scanner thread waits at most until ``deadline_at`` (join
    bound), so a hung request can never stall the cycle beyond the
    configured per-capture latency budget. Evidence additionally requires
    the quote to have been RECORDED on or before ``deadline_at`` — a
    worker finishing a microsecond late (or still running) is a timeout,
    which closes the join/finish race deterministically. The worker NEVER
    touches the database — the research connection stays single-threaded
    — and it uses the same budgeted, rate-limited client as before: no
    limiter bypass, no extra retry layer, no separate rate limiter.
    A late orphan's result is discarded by the claim finalization and the
    worker exits without ever issuing a post-deadline request, so a late
    quote can never become evidence.
    """
    result: dict = {
        "done": False, "book": None, "constraints": None,
        "constraints_error": None, "received_at": None, "error": None,
        "waited_ms": 0, "timed_out": False,
        "received_mono": None, "finished_mono": None,
        "instrument_lookup_ms": None, "orderbook_http_ms": None,
        "deadline_refused": False, "worker_capacity_denied": False,
        "worker_active_count": 0,
    }
    started = time.monotonic()

    # F1: never spawn a worker that is already past its deadline.
    if time.monotonic() >= deadline_at:
        result["deadline_refused"] = True
        result["timed_out"] = True
        with _WORKERS_LOCK:
            _STATS["deadline_refused"] += 1
        return result

    def _run() -> None:
        try:
            # F1: no NEW Bybit request once the deadline has passed.
            if time.monotonic() >= deadline_at:
                result["deadline_refused"] = True
                with _WORKERS_LOCK:
                    _STATS["deadline_refused"] += 1
                return
            lookup_started = time.monotonic()
            constraints, constraints_error = instrument_constraints(
                client, symbol,
                deadline_s=max(0.0, deadline_at - lookup_started))
            result["constraints"] = constraints
            result["constraints_error"] = constraints_error
            result["instrument_lookup_ms"] = int(
                (time.monotonic() - lookup_started) * 1000)
            if time.monotonic() >= deadline_at:
                # The lookup outlived the capture window (slow limiter or
                # HTTP): the orderbook request is NOT issued.
                result["deadline_refused"] = True
                with _WORKERS_LOCK:
                    _STATS["deadline_refused"] += 1
                return
            request_started = time.monotonic()
            book = client._public_get(
                ORDERBOOK_ENDPOINT, category="linear", symbol=symbol,
                limit=50,
                deadline_s=max(0.0, deadline_at - request_started))
            result["orderbook_http_ms"] = int(
                (time.monotonic() - request_started) * 1000)
            result["book"] = book
            result["received_at"] = datetime.now(timezone.utc)
            # Monotonic stamp decides evidence: received AFTER the
            # deadline is a timeout even if the thread finishes later.
            result["received_mono"] = time.monotonic()
        except BaseException as exc:  # noqa: BLE001 — recorded, never raised
            result["error"] = exc
        finally:
            # Completion stamp (BEFORE done): a genuine worker error that
            # arrived in time is an API failure, not a latency timeout.
            result["finished_mono"] = time.monotonic()
            result["done"] = True
            with _WORKERS_LOCK:
                _ACTIVE_WORKERS.discard(thread)

    thread = threading.Thread(target=_run, name="srr-fill-quote",
                              daemon=True)
    with _WORKERS_LOCK:
        _prune_workers_locked()
        if len(_ACTIVE_WORKERS) >= max_workers:
            # F2: the ceiling is reached — deny BEFORE spawn (the caller
            # has already checked pre-claim; this is the fail-closed
            # defense for the post-claim spawn itself). No queue.
            result["worker_capacity_denied"] = True
            result["worker_active_count"] = len(_ACTIVE_WORKERS)
            result["timed_out"] = True
            return result
        _ACTIVE_WORKERS.add(thread)
        result["worker_active_count"] = len(_ACTIVE_WORKERS)
    thread.start()
    thread.join(max(0.0, deadline_at - time.monotonic()))
    result["waited_ms"] = int((time.monotonic() - started) * 1000)
    # Race-free classification:
    #   * evidence requires the quote RECORDED on or before the deadline
    #     (never "the thread happened to finish");
    #   * a worker that refused past the deadline is a timeout;
    #   * a worker error that FINISHED in time is a genuine API failure
    #     (orderbook/limiter taxonomy), never reclassified as latency.
    if not result["done"]:
        on_time = False
    elif result["received_mono"] is not None:
        on_time = result["received_mono"] <= deadline_at
    elif result["deadline_refused"]:
        on_time = False
    else:
        on_time = (result.get("finished_mono") is not None
                   and result["finished_mono"] <= deadline_at)
    result["timed_out"] = not on_time
    return result


def _capture_one(conn, client, obs: dict, activation_ts: datetime,
                 config: RuntimeConfig) -> bool:
    """Reserve + quote + finalize exactly one confirmed SRR SHORT event."""
    event_key = _event_key(obs)

    # 6a. Fast-path duplicate check: replays never reach the API budget.
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM research.srr_short_fill_feasibility_v1 "
            "WHERE event_key=%s OR observation_id=%s",
            (event_key, obs["observation_id"]),
        )
        if cursor.fetchone():
            _STATS["duplicate_skipped"] += 1
            _record_coverage(conn, [obs], "DUPLICATE", "evidence_exists")
            return False
    finally:
        cursor.close()

    # 6b. Timing sanity: a signal_time in the FUTURE of the capture start is
    #     a clock/identity anomaly, never a valid signal-time quote. Fail
    #     closed BEFORE the budget and before any network request.
    capture_started_at = datetime.now(timezone.utc)
    # F1: the capture's single monotonic deadline covers the WHOLE
    # sequence — dup-check, claim, instrument lookup, limiter wait,
    # orderbook, retry/backoff — and is propagated into the quote worker.
    capture_started_mono = time.monotonic()
    deadline_at = capture_started_mono + config.max_capture_ms / 1000.0
    signal_to_capture_ms = int(
        (capture_started_at - obs["signal_time"]).total_seconds() * 1000)
    if signal_to_capture_ms < 0:
        _STATS["timing_denied"] += 1
        LOG.warning(
            "SRR fill capture refused: signal_time is ahead of capture "
            "start: symbol=%s observation_id=%s signal_to_capture_ms=%d",
            obs["symbol"], obs["observation_id"], signal_to_capture_ms)
        return False

    # 6c. F1 latency pre-flight: one full per-capture window must still fit
    #     into the cycle's cumulative wall-clock budget. Fail-closed BEFORE
    #     the API budget and before any claim or network request.
    fits, remaining_ms = _BUDGET.latency_slot_available(
        config.max_capture_ms, config.max_cycle_latency_ms)
    if not fits:
        _STATS["latency_denied"] += 1
        LOG.info(
            "SRR fill capture skipped: per-cycle latency budget exhausted "
            "(used=%dms remaining=%dms < capture max %dms); coverage loss "
            "recorded", _BUDGET.cycle_latency_ms, remaining_ms,
            config.max_capture_ms)
        _record_coverage(conn, [obs], "LATENCY_BUDGET_DENIED",
                         f"remaining_ms={remaining_ms}")
        return False

    # 6c2. F2 worker ceiling: refuse BEFORE the claim when no quote-worker
    #      slot is free (orphans from earlier timeouts still hold theirs).
    #      The event stays accounted in the coverage ledger, no thread and
    #      no HTTP call is started, and the API budget slot is NOT spent.
    if not _quote_worker_capacity_available(config.max_quote_workers):
        _STATS["worker_capacity_denied"] += 1
        active = active_worker_count()
        LOG.info(
            "SRR fill capture skipped: quote worker capacity exhausted "
            "(%d/%d active); coverage loss recorded",
            active, config.max_quote_workers)
        _record_coverage(conn, [obs], "API_BUDGET_DENIED",
                         "WORKER_CAPACITY_EXHAUSTED")
        return False

    # 6d. Explicit API budget: predictable refusal, counted for coverage.
    allowed, budget_reason = _try_consume_budget()
    if not allowed:
        _record_coverage(conn, [obs], "API_BUDGET_DENIED", budget_reason)
        return False

    # 6e. Atomic claim/reservation, committed BEFORE any Bybit request so
    #     a concurrent duplicate can never produce a second orderbook call.
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO research.srr_short_fill_feasibility_v1
            (event_key, observation_id, experiment_id, source_experiment_id,
             scanner_name, symbol, direction, signal_time, activation_ts,
             captured_at, capture_started_at, capture_finished_at,
             reference_price, invalidation_price, structural_r, execution_r,
             variant_stop, variant_target, signal_to_capture_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING""",
            (
                event_key, obs["observation_id"], EXPERIMENT_ID,
                SOURCE_EXPERIMENT_ID, SCANNER_NAME, obs["symbol"], DIRECTION,
                obs["signal_time"], activation_ts,
                capture_started_at, capture_started_at, capture_started_at,
                obs["reference_price"], obs["invalidation_price"],
                obs["structural_r"], obs["execution_r"],
                obs["variant_stop"], obs["variant_target"],
                signal_to_capture_ms,
            ),
        )
        claimed = cursor.rowcount == 1
        conn.commit()
    finally:
        cursor.close()
    if not claimed:
        # Concurrent claimant owns this event; it will (or did) fetch the
        # book. No Bybit request is made here.
        _STATS["duplicate_skipped"] += 1
        _record_coverage(conn, [obs], "CLAIM_RACE_LOST", "claim_conflict")
        return False
    _STATS["claimed"] += 1

    # 6f. Bounded quote fetch (F1/F2): the worker carries the single
    #     monotonic deadline — instrument lookup, limiter wait, retries
    #     and HTTP are all deadline-bounded inside it — and the number of
    #     live workers is capped; this thread waits at most until the
    #     deadline. The FULL wall-clock of the capture — including the DB
    #     finalization below — is charged to the cycle budget by
    #     capture() when this function returns (every path).
    quote = _fetch_quote_phase(client, obs["symbol"],
                               deadline_at=deadline_at,
                               max_workers=config.max_quote_workers)

    capture_finished_at = datetime.now(timezone.utc)
    api_request_ms = int(
        (capture_finished_at - capture_started_at).total_seconds() * 1000)
    # P1: full signal->quote latency (claim + instrument lookup + limiter
    # wait + HTTP); None only when no book reached us at all. This is the
    # value that gates FRESH/DELAYED - a late quote is never eligible.
    quote_received_at = quote["received_at"]
    signal_to_quote_ms = (
        int((quote_received_at - obs["signal_time"]).total_seconds() * 1000)
        if quote_received_at is not None else None)
    constraints = quote["constraints"]
    constraints_error = quote["constraints_error"]

    snapshot = None
    book_error: BaseException | None = None
    if quote["timed_out"]:
        # Deadline/worker-ceiling miss: fail closed. The orphaned worker's
        # result is discarded - a late quote can never be stored as
        # evidence, and the worker itself never starts a NEW request past
        # the deadline (it exits instead).
        book_error = TimeoutError(
            f"capture latency budget {config.max_capture_ms}ms exceeded")
        LOG.warning(
            "SRR fill capture hit the per-capture latency bound: symbol=%s "
            "observation_id=%s waited_ms=%d max_capture_ms=%d "
            "deadline_refused=%s worker_capacity_denied=%s",
            obs["symbol"], obs["observation_id"], quote["waited_ms"],
            config.max_capture_ms, quote.get("deadline_refused"),
            quote.get("worker_capacity_denied"))
    elif quote["error"] is not None:
        book_error = quote["error"]
    else:
        try:
            now_ms = (int(quote_received_at.timestamp() * 1000)
                      if quote_received_at is not None
                      else int(time.time() * 1000))
            snapshot = calculate_snapshot(
                obs["reference_price"], obs["structural_r"], quote["book"],
                constraints=constraints, constraints_error=constraints_error,
                now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001 — recorded, never raised
            book_error = exc

    if snapshot is None:
        if quote.get("worker_capacity_denied"):
            # Unreachable in practice (checked pre-claim at 6c2); the
            # claim exists, so the row is finalized with an explicit
            # reason instead of vanishing (F4).
            reason = "worker:CAPACITY_DENIED"
        elif quote.get("deadline_refused"):
            # The worker refused to START a new Bybit request past the
            # deadline — stricter than a plain join timeout.
            reason = "deadline:EXCEEDED"
        elif quote["timed_out"]:
            reason = "latency:TIMEOUT"
        elif book_error is not None:
            # F3 taxonomy: a shared-limiter refusal carries its own
            # limiter_result and never masquerades as a Bybit error.
            reason = _failure_reason_for(book_error)
        else:
            reason = "orderbook:UNKNOWN"
        payload = _error_payload(
            obs, capture_finished_at, signal_to_capture_ms,
            signal_to_quote_ms, api_request_ms, reason, constraints,
            constraints_error, config)
        status = "ERROR"
        quality = "NO_QUOTE"
    else:
        payload = _snapshot_payload(
            obs, snapshot, capture_finished_at, signal_to_capture_ms,
            signal_to_quote_ms, api_request_ms, config, constraints,
            constraints_error, quote["book"])
        status = snapshot["status"]
        quality = payload["quote_quality_status"]

    # F1/F3 evidence: what this capture cost, how much room was left and
    # how the quote phase was bounded (separate telemetry per component).
    now_mono = time.monotonic()
    payload["source_diagnostics"]["latency"] = {
        "signal_to_capture_ms": signal_to_capture_ms,
        "quote_phase_ms": quote["waited_ms"],
        "capture_total_ms": int((now_mono - capture_started_mono) * 1000),
        # Negative once the deadline passed: the budget LEFT when this row
        # was built (the DB finalization is charged to the cycle budget).
        "deadline_remaining_ms": int((deadline_at - now_mono) * 1000),
        "worker_timed_out": bool(quote["timed_out"]),
        "instrument_lookup_ms": quote["instrument_lookup_ms"],
        "orderbook_http_ms": quote["orderbook_http_ms"],
        # F3: the shared limiter exposes no per-acquire wait on the success
        # path — recorded as NULL/UNKNOWN, never a guessed value.
        "limiter_wait_ms": None,
        "limiter_wait_source": "unknown",
        "worker_active_count": quote["worker_active_count"],
        "worker_capacity_denied_count": _STATS["worker_capacity_denied"],
        # Budget state including THIS capture through the quote phase; the
        # cycle charge (capture()) additionally includes DB finalization.
        "cycle_latency_ms": _BUDGET.cycle_latency_ms + int(
            (datetime.now(timezone.utc) - capture_started_at)
            .total_seconds() * 1000),
        "max_capture_ms": config.max_capture_ms,
        "max_cycle_latency_ms": config.max_cycle_latency_ms,
        "max_quote_workers": config.max_quote_workers,
    }

    if not _finalize_row(conn, event_key, payload):
        _STATS["finalize_failed"] += 1
        LOG.warning(
            "SRR fill capture finalization lost the claim race: "
            "event_key=%s observation_id=%s", event_key,
            obs["observation_id"])
        try:
            conn.rollback()
        except Exception:
            LOG.debug("SRR fill capture rollback failed", exc_info=True)
        return False

    _STATS["captured"] += 1
    if status != "OK" or quality != "FRESH":
        LOG.info(
            "SRR fill capture stored with non-eligible status: symbol=%s "
            "observation_id=%s feasibility=%s quote_quality=%s "
            "signal_to_capture_ms=%d signal_to_quote_ms=%s",
            obs["symbol"], obs["observation_id"], status, quality,
            signal_to_capture_ms, signal_to_quote_ms)
    return True


def _common_diagnostics(obs, config, constraints_known) -> dict:
    return {
        "experiment_id": EXPERIMENT_ID,
        "source_experiment_id": SOURCE_EXPERIMENT_ID,
        "scanner_name": SCANNER_NAME,
        "observation_id": obs["observation_id"],
        "quote_delay_limit_ms": config.quote_delay_limit_ms,
        "book_staleness_limit_ms": BOOK_STALENESS_LIMIT_MS,
        "book_future_skew_ms": BOOK_FUTURE_SKEW_MS,
        "notional_scenarios_usdt": [str(item)
                                    for item in DEFAULT_NOTIONAL_SCENARIOS],
        "budget": _BUDGET.snapshot(),
        "budget_limits": {
            "per_cycle": _read_limit(MAX_PER_CYCLE_ENV, DEFAULT_MAX_PER_CYCLE,
                                     HARD_MAX_PER_CYCLE),
            "per_run": _read_limit(MAX_PER_RUN_ENV, DEFAULT_MAX_PER_RUN,
                                   HARD_MAX_PER_RUN),
        },
        "execution_r_formula": "execution_R = 2.00 * structural_R",
        "cost_normalization": "execution_R",
        "constraints_known": bool(constraints_known),
        "executable_net_r_computed": False,
        "backfill": "FORBIDDEN",
    }


def _quote_quality(snapshot: dict, signal_to_quote_ms: int | None,
                   config: RuntimeConfig) -> str:
    """Classify quote quality; book failures outrank the delay verdict.

    ``signal_to_quote_ms`` is the FULL latency from the SRR signal to the
    actual orderbook receipt (DB claim commit + instrument lookup + limiter
    wait + HTTP round trip), not merely the capture start. A quote that
    arrives outside ``SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS`` is DELAYED even
    when the capture started promptly, and DELAYED is never eligible for
    execution statistics. A missing latency stamp fails closed to DELAYED.
    """
    status = snapshot.get("status")
    if status in ("INVALID_BOOK",):
        return "INVALID_BOOK"
    if status == "STALE_BOOK":
        return "STALE_BOOK"
    if status == "FUTURE_BOOK":
        return "FUTURE_BOOK"
    if (signal_to_quote_ms is None
            or not 0 <= signal_to_quote_ms <= config.quote_delay_limit_ms):
        return "DELAYED"
    return "FRESH"


def _error_payload(obs, capture_finished_at, signal_to_capture_ms,
                   signal_to_quote_ms, api_request_ms, reason, constraints,
                   constraints_error, config) -> dict:
    constraints_block = {
        "known": constraints is not None,
        "min_order_qty": (constraints or {}).get("min_order_qty"),
        "qty_step": (constraints or {}).get("qty_step"),
        "min_notional": (constraints or {}).get("min_notional"),
        "lookup_error": constraints_error,
    }
    diagnostics = _common_diagnostics(obs, config, constraints is not None)
    diagnostics["api"] = {"error": reason, "api_request_ms": api_request_ms}
    return {
        "captured_at": capture_finished_at,
        "capture_finished_at": capture_finished_at,
        "book_time_ms": None,
        "book_age_ms": None,
        "signal_to_capture_ms": signal_to_capture_ms,
        "signal_to_quote_ms": signal_to_quote_ms,
        "api_request_ms": api_request_ms,
        "bid": None, "ask": None, "spread_bps": None,
        "reference_to_bid_bps": None, "depth_bid_10bps_usdt": None,
        "requested_notional_usdt": None, "requested_qty": None,
        "executable_qty": None, "min_order_qty": None, "qty_step": None,
        "min_notional": None, "estimated_fill": None,
        "entry_slippage_bps": None,
        "observed_roundtrip_cost_pct": None, "observed_cost_r": None,
        "frozen_normal_cost_r": None, "frozen_elevated_cost_r": None,
        "extra_cost_r_vs_frozen_normal": None, "cost_class": None,
        "quote_quality_status": "NO_QUOTE",
        "feasibility_status": "ERROR",
        "eligible_for_execution_stats": False,
        "failure_reason": reason,
        "exchange_constraints": _jsonable(constraints_block),
        "scenario_results": {},
        "source_diagnostics": _jsonable(diagnostics),
        "raw_snapshot": {"error": reason},
    }


def _snapshot_payload(obs, snapshot, capture_finished_at,
                      signal_to_capture_ms, signal_to_quote_ms,
                      api_request_ms, config,
                      constraints, constraints_error, book) -> dict:
    quality = _quote_quality(snapshot, signal_to_quote_ms, config)
    constraints_block = snapshot.get("constraints") or {
        "known": constraints is not None,
        "min_order_qty": (constraints or {}).get("min_order_qty"),
        "qty_step": (constraints or {}).get("qty_step"),
        "min_notional": (constraints or {}).get("min_notional"),
        "lookup_error": constraints_error,
    }
    feasibility = snapshot["status"]
    constraints_known = bool(constraints_block.get("known"))
    # Eligible only for a fresh, valid, depth-feasible snapshot with known
    # exchange constraints; unknown constraints are never "confirmed
    # executable". Base (first, 100 USDT) scenario gates the verdict.
    eligible = (
        quality == "FRESH"
        and feasibility == "OK"
        and constraints_known
    )
    diagnostics = _common_diagnostics(obs, config, constraints_known)
    diagnostics["api"] = {"api_request_ms": api_request_ms,
                          "orderbook_calls": 1}
    diagnostics["delay_verdict"] = (
        "WITHIN_LIMIT"
        if (signal_to_quote_ms is not None
            and signal_to_quote_ms <= config.quote_delay_limit_ms)
        else "EXCEEDED_LIMIT")
    diagnostics["constraints_lookup_error"] = constraints_error
    return {
        "captured_at": capture_finished_at,
        "capture_finished_at": capture_finished_at,
        "book_time_ms": snapshot.get("book_time_ms"),
        "book_age_ms": snapshot.get("book_age_ms"),
        "signal_to_capture_ms": signal_to_capture_ms,
        "signal_to_quote_ms": signal_to_quote_ms,
        "api_request_ms": api_request_ms,
        "bid": snapshot.get("bid"),
        "ask": snapshot.get("ask"),
        "spread_bps": snapshot.get("spread_bps"),
        "reference_to_bid_bps": snapshot.get("reference_to_bid_bps"),
        "depth_bid_10bps_usdt": snapshot.get("depth_bid_10bps_usdt"),
        "requested_notional_usdt": snapshot.get("requested_notional_usdt"),
        "requested_qty": snapshot.get("requested_qty"),
        "executable_qty": snapshot.get("executable_qty"),
        "min_order_qty": (constraints_block or {}).get("min_order_qty"),
        "qty_step": (constraints_block or {}).get("qty_step"),
        "min_notional": (constraints_block or {}).get("min_notional"),
        "estimated_fill": snapshot.get("estimated_fill"),
        "entry_slippage_bps": snapshot.get("entry_slippage_bps"),
        "observed_roundtrip_cost_pct": snapshot.get(
            "observed_roundtrip_cost_pct"),
        "observed_cost_r": snapshot.get("observed_cost_r"),
        "frozen_normal_cost_r": snapshot.get("frozen_normal_cost_r"),
        "frozen_elevated_cost_r": snapshot.get("frozen_elevated_cost_r"),
        "extra_cost_r_vs_frozen_normal": snapshot.get(
            "extra_cost_r_vs_frozen_normal"),
        "cost_class": snapshot.get("cost_class"),
        "quote_quality_status": quality,
        "feasibility_status": feasibility,
        "eligible_for_execution_stats": eligible,
        "failure_reason": snapshot.get("reason"),
        "exchange_constraints": _jsonable(constraints_block),
        "scenario_results": _jsonable(
            {"scenarios": snapshot.get("scenario_results", []),
             "base_scenario": "SMALL (100 USDT)"}),
        "source_diagnostics": _jsonable(diagnostics),
        "raw_snapshot": _jsonable(book),
    }
