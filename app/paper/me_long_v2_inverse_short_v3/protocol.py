"""ME_LONG_V2_INVERSE_SHORT_V3 — frozen prospective paper protocol (Issue #285).

The V3 successor contour of ``ME_LONG_V2_INVERSE_SHORT_V2`` (forked from V2;
V2 is never modified by this package).  Source events are the RAW detector
events of ``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` (LONG setups; this contour
trades the inverse side) — exactly the same source, detector and frozen
execution geometry as V2.

**The one protocol change V3 makes over V2 — entry guards and capacity
(``ME_INVERSE_SHORT_V3_ENTRY_GUARDS_AND_CAPACITY_V1``, Issue #285):**

  * ``max_open_positions_total = 12`` — the cap counts OPEN **V2 + OPEN V3**
    positions (never 12 new on top of the old ones); a slot frees only after
    a confirmed durable close;
  * ``single_active_symbol = true`` — at most ONE OPEN position per symbol
    across BOTH contours (legacy V2 duplicate rows are only ever allowed to
    run out, never re-created);
  * ``symbol_entry_cooldown_hours = 12`` — a symbol may enter again only
    when ``decision_time >= last committed entered_at + 12 hours`` (cooldown
    runs from the ACCEPTED ENTRY, never from a close; refused/blocked signals
    never extend it; after expiry a NEW eligible signal is required — the old
    event is never replayed).

Everything else is frozen and IDENTICAL to V2 (requirement: no change to
detector, source/regime/gates/scoring, SHORT geometry TP/SL, MAX_HOLD /
TIMEOUT, ENTRY_TTL = 120 min, paper sizing, fees, slippage, funding,
STOP_FIRST, protective exits, idempotency and recovery — asserted by the
parity tests):

  * Direction              : SHORT
  * Take profit            : 10% of the ACTUAL entry price
  * Stop loss              : 10% of the ACTUAL entry price
  * Maximum holding period : 7200 minutes (120 hours)
  * Entry signal TTL       : 120 minutes from ``signal_time``
  * DCA / trailing / BE    : OFF
  * Mode                   : prospective-only, paper-only
  * Historical backfill    : forbidden

The whole protocol is expressed once in :func:`execution_spec`.  The
``protocol_fingerprint`` (SHA-256 of that canonical spec) must be stored in
the V3 activation row before the contour may open a single position — the
guard parameters are PART of the frozen spec, so changing any of them
necessarily changes the fingerprint and invalidates the existing activation
(requirement: separate fixation of protocol fingerprint / ``freeze_ts``).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Identity (V3 — separate experiment, separate fingerprint namespace)
# ---------------------------------------------------------------------------
STRATEGY_ID = "ME_LONG_V2_INVERSE_SHORT_V3"
PROTOCOL_VERSION = "3.0.0"
EXPERIMENT_ID = STRATEGY_ID  # activation-row identity

# Owner-facing Issue #285 name (documentation / telemetry only).
ISSUE_285_NAME = "ME_INVERSE_SHORT_V3_ENTRY_GUARDS_AND_CAPACITY_V1"

SOURCE_SCANNER = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
SOURCE_DIRECTION = "LONG"          # direction of the ORIGINAL detector event
TRADE_DIRECTION = "SHORT"          # direction traded by this contour

# Namespace for every piece of contour state (positions, keys, rows).
POSITION_NAMESPACE = STRATEGY_ID

# ---------------------------------------------------------------------------
# Frozen execution parameters (IDENTICAL to V2 — parity asserted by tests)
# ---------------------------------------------------------------------------
TAKE_PROFIT_PCT = 0.10             # 10% from actual entry price
STOP_LOSS_PCT = 0.10               # 10% from actual entry price
MAX_HOLD_MINUTES = 7200            # 120 hours

# Frozen entry-admissibility TTL (unchanged V2 rule): age >= TTL => never
# enter; the event is terminally disposed (EXPIRED / CAPACITY_REJECTED).
ENTRY_MAX_AGE_MINUTES = 120

DCA_ENABLED = False
TRAILING_ENABLED = False
BREAKEVEN_ENABLED = False

PROSPECTIVE_ONLY = True
PAPER_ONLY = True
HISTORICAL_BACKFILL = False        # forbidden by construction

ENTRY_MODEL = "NEXT_OBSERVED_MARKET_PRICE"

EXIT_PRIORITY = ("STOP_LOSS", "TAKE_PROFIT_1", "TIMEOUT")
INTRABAR_POLICY = "STOP_FIRST"

EXIT_REASONS = ("STOP_LOSS", "STOP_LOSS_GAP", "TAKE_PROFIT_1", "TIMEOUT")

FEE_PER_SIDE = 0.00055
SLIPPAGE_PER_SIDE = 0.0005

FUNDING_SETTLEMENT = "PERIODIC_INTERVAL_NOTIONAL_RATE"

ROUTE_PRE_FILTER_SOURCE = "PRE_FILTER_SOURCE"
CAPTURE_ROUTES = (ROUTE_PRE_FILTER_SOURCE,)

# ---------------------------------------------------------------------------
# V3 NEW — entry guards and capacity (Issue #285, frozen)
# ---------------------------------------------------------------------------
# 1) Global capacity: OPEN V2 + OPEN V3 <= 12 at admission time.
MAX_OPEN_POSITIONS_TOTAL = 12
# 2) At most one OPEN position per symbol across BOTH contours.
SINGLE_ACTIVE_SYMBOL = True
# 3) Per-symbol entry cooldown measured from the last COMMITTED entered_at
#    of either contour (accepted entries only).
SYMBOL_ENTRY_COOLDOWN_HOURS = 12

# Cross-contour scope of the guards: the admission transaction reads the V2
# ledger READ-ONLY (dds.me_long_v2_inverse_short_v2_position) plus its own.
CROSS_CONTOUR_SCOPE = "V2+V3"

# Terminal admission-refusal reasons (event.status = SYMBOL_REJECTED).
REFUSAL_SYMBOL_ALREADY_OPEN = "SYMBOL_ALREADY_OPEN"
REFUSAL_SYMBOL_COOLDOWN = "SYMBOL_COOLDOWN_12H"
SYMBOL_REFUSAL_REASONS = (REFUSAL_SYMBOL_ALREADY_OPEN, REFUSAL_SYMBOL_COOLDOWN)

# Deferred (non-terminal) refusal reasons — frozen V2 entry-TTL contract:
# a cap-bound event stays CAPTURED until the TTL sweep disposes of it.
REFUSAL_ENTRY_CAP = "ENTRY_CAP"
REFUSAL_ENTRY_TTL = "ENTRY_TTL"

# Cross-contour admission lock: ONE deterministic serialized admission
# critical section for the whole V3 contour (pg_advisory_xact_lock on this
# key), acquired BEFORE the event and activation row locks.  Every V3
# admission, across processes and restarts, enters the same critical section,
# so the V2+V3 count / symbol / cooldown re-checks are race-safe.  Closes and
# the TTL sweep never take this lock, so exits can never be blocked by it.
ADMISSION_LOCK_NAMESPACE = "ME_LONG_V2_INVERSE_SHORT_V3_ADMISSION"
# Deterministic 64-bit key for pg_advisory_xact_lock (derived once, pinned).
ADMISSION_LOCK_KEY = int(
    hashlib.sha256(ADMISSION_LOCK_NAMESPACE.encode("utf-8")).hexdigest()[:16], 16
)


def short_geometry(entry_price: float) -> tuple[float, float]:
    """Return ``(stop_price, target_price)`` for a SHORT at ``entry_price``.

    Both levels are derived from the ACTUAL (slippaged) entry price, exactly
    10% away from it (frozen, identical to V2).
    """
    if entry_price <= 0:
        raise ValueError("INVALID_ENTRY_PRICE")
    stop = entry_price * (1.0 + STOP_LOSS_PCT)
    target = entry_price * (1.0 - TAKE_PROFIT_PCT)
    return stop, target


def deadline_for(entered_at: datetime) -> datetime:
    """Return the immutable TIMEOUT deadline for a position."""
    if entered_at.tzinfo is None or entered_at.utcoffset() is None:
        raise ValueError("NAIVE_ENTERED_AT")
    return entered_at + timedelta(minutes=MAX_HOLD_MINUTES)


def cooldown_boundary(last_entered_at: datetime) -> datetime:
    """Return the first instant a new entry for the symbol is admissible."""
    if last_entered_at.tzinfo is None or last_entered_at.utcoffset() is None:
        raise ValueError("NAIVE_ENTERED_AT")
    return last_entered_at + timedelta(hours=SYMBOL_ENTRY_COOLDOWN_HOURS)


def execution_spec() -> dict:
    """Canonical, order-independent description of the frozen protocol."""
    return {
        "strategy_id": STRATEGY_ID,
        "protocol_version": PROTOCOL_VERSION,
        "mode": "paper",
        "execution_mode": "prospective_only",
        "source_scanner": SOURCE_SCANNER,
        "source_direction": SOURCE_DIRECTION,
        "direction": TRADE_DIRECTION,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "max_hold_minutes": MAX_HOLD_MINUTES,
        "entry_max_age_minutes": ENTRY_MAX_AGE_MINUTES,
        # V3 NEW (Issue #285) — frozen entry guards and capacity.
        "max_open_positions_total": MAX_OPEN_POSITIONS_TOTAL,
        "single_active_symbol": SINGLE_ACTIVE_SYMBOL,
        "symbol_entry_cooldown_hours": SYMBOL_ENTRY_COOLDOWN_HOURS,
        "cross_contour_scope": CROSS_CONTOUR_SCOPE,
        "dca_enabled": DCA_ENABLED,
        "trailing_enabled": TRAILING_ENABLED,
        "breakeven_enabled": BREAKEVEN_ENABLED,
        "historical_backfill": HISTORICAL_BACKFILL,
        "entry_model": ENTRY_MODEL,
        "exit_priority": list(EXIT_PRIORITY),
        "intrabar_policy": INTRABAR_POLICY,
        "exit_reasons": list(EXIT_REASONS),
        "fee_per_side": FEE_PER_SIDE,
        "slippage_per_side": SLIPPAGE_PER_SIDE,
        "funding_settlement": FUNDING_SETTLEMENT,
        "namespace": POSITION_NAMESPACE,
    }


def protocol_fingerprint() -> str:
    """SHA-256 of the canonical protocol spec — the fixation fingerprint."""
    canonical = json.dumps(
        execution_spec(), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def describe_protocol() -> dict:
    """Human/audit view: frozen parameters plus the fingerprint."""
    spec = execution_spec()
    spec["protocol_fingerprint"] = protocol_fingerprint()
    return spec
