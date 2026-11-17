"""ME_LONG_V2_INVERSE_SHORT_OOS2_V1 — frozen prospective paper protocol.

Independent SHORT paper contour derived from the RAW detector events of
``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` (source events are LONG setups;
this contour trades the inverse side) — the OOS-2 successor experiment of
``ME_LONG_V2_INVERSE_SHORT_V2`` (Issue #276).

Why this is a NEW experiment and not a parameter edit of V1/V2:
the three-stage PARTIAL take-profit ladder and the OOS-2-local 12-position
capacity are substantive research-protocol changes, so they ship with their
own experiment ID, their own activation row, their own clean prospective
holdout and their own fingerprint.  V1 and V2 are never modified by this
package.

Frozen execution parameters (requirement: fixed, never configurable):

  * Direction              : SHORT
  * Source scanner         : MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 (LONG side)
  * Capacity               : 12 concurrent OPEN positions — OOS-2 LOCAL
                             (never ``Settings.max_open_positions``)
  * Entry signal TTL       : 120 minutes from ``signal_time``
                             (database-enforced post-lock admission)
  * Stop loss              : -10% price movement vs entry (``entry * 1.10``),
                             on ALL remaining quantity
  * Take profit ladder     : TP1 +5%  -> close 50% of the ORIGINAL quantity
                             TP2 +7%  -> close 75% of the THEN-REMAINING
                                         quantity (= 37.5% original)
                             TP3 +10% -> close ALL remaining (= 12.5% original)
  * Max hold               : 7200 minutes (5 days)
  * DCA / trailing / BE    : OFF
  * Mode                   : prospective-only, paper-only
  * Historical backfill    : forbidden

Price semantics: TP/SL percentages are ASSET price movements relative to the
ACTUAL entry price (no leverage, NOT ROE).  For a SHORT:
``tp1 = entry * 0.95``, ``tp2 = entry * 0.93``, ``tp3 = entry * 0.90``,
``stop = entry * 1.10``.

Partial-exit contract (Issue #276):

  * every TP stage fires AT MOST ONCE per position (durable leg identity);
  * a partial exit NEVER marks the position CLOSED, NEVER frees a capacity
    slot and NEVER changes ``entered_at`` / ``deadline_at`` / entry price;
  * ``STOP_FIRST`` wins an ambiguous bar: if the stop is touched in the same
    bar as any TP, the remaining quantity closes at the stop;
  * a bar may skip several TP levels — the levels then fire in ladder order
    (TP1 → TP2 → TP3), each at its own level price, each exactly once;
  * TIMEOUT closes the remaining quantity and PRESERVES the realized PnL of
    earlier TP legs;
  * funding accrues on the REMAINING quantity only.

TP2 interpretation (explicit, Issue #276): **75% of the quantity remaining
after TP1** (→ 37.5% of the original).  If the owner instead intends 75%
cumulative of the original position, that is a protocol change: it changes
this spec, therefore the fingerprint, therefore requires a NEW experiment
version — it must be decided BEFORE activation.

The whole protocol is expressed once in :func:`execution_spec`.  The
``protocol_fingerprint`` (SHA-256 of that canonical spec) must be stored in
the activation row before the contour may open a single position — a
parameter change necessarily changes the fingerprint and therefore
invalidates the existing activation (requirement: separate fixation of
protocol fingerprint / ``freeze_ts`` before launch).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
STRATEGY_ID = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1"
PROTOCOL_VERSION = "1.0.0"
EXPERIMENT_ID = STRATEGY_ID  # activation-row identity

SOURCE_SCANNER = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
SOURCE_DIRECTION = "LONG"          # direction of the ORIGINAL detector event
TRADE_DIRECTION = "SHORT"          # direction traded by this contour

# Namespace for every piece of contour state (positions, keys, rows).
POSITION_NAMESPACE = STRATEGY_ID

# ---------------------------------------------------------------------------
# Frozen execution parameters
# ---------------------------------------------------------------------------

# OOS-2 LOCAL capacity (Issue #276): 12 concurrent OPEN logical positions.
# Deliberately a protocol constant — the contour NEVER reads the global
# Settings.max_open_positions, so V1/V2/global capacity is untouched.
MAX_OPEN_POSITIONS = 12

# Maximum admissible age of a source event at the instant of a NEW entry.
# age >= TTL => never enter; the event is terminally disposed
# (EXPIRED / CAPACITY_REJECTED) with reason + timestamp.
ENTRY_MAX_AGE_MINUTES = 120

MAX_HOLD_MINUTES = 7200            # 5 days
STOP_LOSS_PCT = 0.10               # -10% favorable-move stop: entry * 1.10

# Three-stage partial take-profit ladder (frozen):
#   stage, price factor vs entry, close basis, close fraction
#   TP1: entry * 0.95 — close 50% of the ORIGINAL quantity
#   TP2: entry * 0.93 — close 75% of the THEN-REMAINING quantity
#   TP3: entry * 0.90 — close ALL remaining quantity
TAKE_PROFIT_STAGES: tuple[dict, ...] = (
    {
        "stage": "TAKE_PROFIT_1",
        "price_factor": 0.95,
        "close_basis": "ORIGINAL",
        "close_fraction": 0.50,
    },
    {
        "stage": "TAKE_PROFIT_2",
        "price_factor": 0.93,
        "close_basis": "REMAINING",
        "close_fraction": 0.75,
    },
    {
        "stage": "TAKE_PROFIT_3",
        "price_factor": 0.90,
        "close_basis": "REMAINING",
        "close_fraction": 1.00,   # all that is left (12.5% of original)
    },
)

# Deterministic quantity rounding / dust policy for partial legs.
# Paper quantities are continuous floats (no exchange lot grid), so:
#   * every close quantity is rounded HALF-EVEN to QTY_DECIMALS decimals;
#   * the result is clamped to [0, remaining] — NEVER overclose;
#   * when the leftover after a leg would be dust (< remaining *
#     QTY_DUST_RELATIVE), the leg absorbs it and closes the full remainder.
QTY_DECIMALS = 12
QTY_DUST_RELATIVE = 1e-9

DCA_ENABLED = False
TRAILING_ENABLED = False
BREAKEVEN_ENABLED = False

PROSPECTIVE_ONLY = True
PAPER_ONLY = True
HISTORICAL_BACKFILL = False        # forbidden by construction

# Entry model: the first observed market price AFTER the source event was
# captured (never a reconstructed/historical fill), and only within the
# frozen entry TTL above.
ENTRY_MODEL = "NEXT_OBSERVED_MARKET_PRICE"

# Exit priority.  Protective stops always win over targets and over the
# administrative time exit (the frozen repo-wide convention is STOP_FIRST).
# Within the TP ladder the PRICE order for a SHORT is TP1 -> TP2 -> TP3
# (falling price): a single bar may fire several stages, always in that
# order, each at its own level price.
EXIT_PRIORITY = ("STOP_LOSS", "TAKE_PROFIT_1", "TAKE_PROFIT_2",
                 "TAKE_PROFIT_3", "TIMEOUT")
INTRABAR_POLICY = "STOP_FIRST"

# Terminal exit reasons emitted by this contour (CHECK constraint,
# migration 088).  TP1/TP2 partial legs are recorded in the close-leg
# ledger; they are NOT terminal position states.
EXIT_REASONS = ("STOP_LOSS", "STOP_LOSS_GAP", "TAKE_PROFIT_1",
                "TAKE_PROFIT_2", "TAKE_PROFIT_3", "TIMEOUT")

# Terminal reasons that close the WHOLE remaining quantity (position flips
# to CLOSED and the capacity slot is released).
TERMINAL_EXIT_REASONS = ("STOP_LOSS", "STOP_LOSS_GAP", "TAKE_PROFIT_3",
                         "TIMEOUT")

# Partial legs: stages that close PART of the remaining quantity only.
PARTIAL_EXIT_STAGES = ("TAKE_PROFIT_1", "TAKE_PROFIT_2")

# Fee / slippage assumptions recorded in the frozen protocol (the engine
# still takes the live values from Settings — these document the contract).
FEE_PER_SIDE = 0.00055
SLIPPAGE_PER_SIDE = 0.0005

# Funding settlement model: periodic interval, notional-rate, charged on the
# REMAINING quantity only (funding on outstanding size, Issue #276).
FUNDING_SETTLEMENT = "PERIODIC_INTERVAL_NOTIONAL_RATE_REMAINING_QTY"

# Capture routes (how the source event reached this contour).
ROUTE_PRE_FILTER_SOURCE = "PRE_FILTER_SOURCE"
CAPTURE_ROUTES = (ROUTE_PRE_FILTER_SOURCE,)


def short_geometry(entry_price: float) -> tuple[float, float, float, float]:
    """Return ``(stop, tp1, tp2, tp3)`` for a SHORT at ``entry_price``.

    All levels are derived from the ACTUAL (slippaged) entry price:
    stop +10% above, TP1/TP2/TP3 at -5% / -7% / -10% below it.
    """
    if entry_price <= 0:
        raise ValueError("INVALID_ENTRY_PRICE")
    stop = entry_price * (1.0 + STOP_LOSS_PCT)
    tp1 = entry_price * TAKE_PROFIT_STAGES[0]["price_factor"]
    tp2 = entry_price * TAKE_PROFIT_STAGES[1]["price_factor"]
    tp3 = entry_price * TAKE_PROFIT_STAGES[2]["price_factor"]
    return stop, tp1, tp2, tp3


def take_profit_levels(entry_price: float) -> dict[str, float]:
    """Mapping ``stage -> level price`` for the frozen ladder."""
    _stop, tp1, tp2, tp3 = short_geometry(entry_price)
    return {"TAKE_PROFIT_1": tp1, "TAKE_PROFIT_2": tp2, "TAKE_PROFIT_3": tp3}


def close_quantity(stage: str, original_qty: float,
                   remaining_qty: float) -> float:
    """Deterministic quantity to close for ``stage`` (Issue #276 ladder).

    * ``TAKE_PROFIT_1``: 50% of the ORIGINAL quantity.
    * ``TAKE_PROFIT_2``: 75% of the quantity REMAINING after TP1.
    * ``TAKE_PROFIT_3``: ALL remaining quantity.

    Never returns more than ``remaining_qty`` (clamp), always rounded
    HALF-EVEN to ``QTY_DECIMALS``; a dust leftover below
    ``remaining * QTY_DUST_RELATIVE`` is absorbed by the leg (the leg then
    closes the full remainder — the position completes).
    """
    if original_qty <= 0 or remaining_qty <= 0:
        return 0.0
    spec = next(
        (s for s in TAKE_PROFIT_STAGES if s["stage"] == stage), None,
    )
    if spec is None:
        raise ValueError(f"UNKNOWN_TP_STAGE:{stage}")
    if spec["close_basis"] == "ORIGINAL":
        raw = original_qty * spec["close_fraction"]
    else:
        raw = remaining_qty * spec["close_fraction"]
    qty = round(raw, QTY_DECIMALS)
    qty = min(qty, remaining_qty)
    if qty < 0:
        qty = 0.0
    # Dust absorption: never leave a sub-dust remainder behind a leg.
    leftover = remaining_qty - qty
    if qty > 0 and 0 < leftover < remaining_qty * QTY_DUST_RELATIVE:
        qty = remaining_qty
    return qty


def deadline_for(entered_at: datetime) -> datetime:
    """Return the immutable TIMEOUT deadline for a position."""
    if entered_at.tzinfo is None or entered_at.utcoffset() is None:
        raise ValueError("NAIVE_ENTERED_AT")
    return entered_at + timedelta(minutes=MAX_HOLD_MINUTES)


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
        # OOS-2 LOCAL capacity — never the global Settings value.
        "max_open_positions": MAX_OPEN_POSITIONS,
        "entry_max_age_minutes": ENTRY_MAX_AGE_MINUTES,
        "max_hold_minutes": MAX_HOLD_MINUTES,
        "stop_loss_pct": STOP_LOSS_PCT,
        # The frozen three-stage partial ladder (TP2 = 75% of REMAINING).
        "take_profit_stages": [dict(s) for s in TAKE_PROFIT_STAGES],
        "qty_decimals": QTY_DECIMALS,
        "qty_dust_relative": QTY_DUST_RELATIVE,
        "dca_enabled": DCA_ENABLED,
        "trailing_enabled": TRAILING_ENABLED,
        "breakeven_enabled": BREAKEVEN_ENABLED,
        "historical_backfill": HISTORICAL_BACKFILL,
        "entry_model": ENTRY_MODEL,
        "exit_priority": list(EXIT_PRIORITY),
        "intrabar_policy": INTRABAR_POLICY,
        "exit_reasons": list(EXIT_REASONS),
        "terminal_exit_reasons": list(TERMINAL_EXIT_REASONS),
        "partial_exit_stages": list(PARTIAL_EXIT_STAGES),
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
