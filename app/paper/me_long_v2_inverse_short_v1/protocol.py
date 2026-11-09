"""ME_LONG_V2_INVERSE_SHORT_V1 — frozen prospective paper protocol.

Independent SHORT paper contour derived from the RAW detector events of
``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` (source events are LONG setups;
this contour trades the inverse side).

Frozen execution parameters (requirement: fixed, never configurable):

  * Direction              : SHORT
  * Take profit            : 10% of the ACTUAL entry price
  * Stop loss              : 10% of the ACTUAL entry price
  * Maximum holding period : 7200 minutes (120 hours)
  * DCA / trailing / BE    : OFF
  * Mode                   : prospective-only, paper-only
  * Historical backfill    : forbidden

The whole protocol is expressed once in :func:`execution_spec`.  The
``protocol_fingerprint`` (SHA-256 of that canonical spec) must be stored in
the activation row before the contour may open a single position — a parameter
change necessarily changes the fingerprint and therefore invalidates the
existing activation (requirement: separate fixation of protocol fingerprint /
freeze_ts before launch).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
STRATEGY_ID = "ME_LONG_V2_INVERSE_SHORT_V1"
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
TAKE_PROFIT_PCT = 0.10             # 10% from actual entry price
STOP_LOSS_PCT = 0.10               # 10% from actual entry price
MAX_HOLD_MINUTES = 7200            # 120 hours

DCA_ENABLED = False
TRAILING_ENABLED = False
BREAKEVEN_ENABLED = False

PROSPECTIVE_ONLY = True
PAPER_ONLY = True
HISTORICAL_BACKFILL = False        # forbidden by construction

# Entry model: the first observed market price AFTER the source event was
# captured (never a reconstructed/historical fill).
ENTRY_MODEL = "NEXT_OBSERVED_MARKET_PRICE"

# Exit priority.  Protective stops always win over targets and over the
# administrative time exit (the frozen repo-wide convention is STOP_FIRST,
# see ``_frozen_intrabar_policy`` used by the ME V3 prospective protocol).
EXIT_PRIORITY = ("STOP_LOSS", "TAKE_PROFIT_1", "TIMEOUT")
INTRABAR_POLICY = "STOP_FIRST"

# Exit reasons emitted by this contour (CHECK constraint in migration 085).
EXIT_REASONS = ("STOP_LOSS", "STOP_LOSS_GAP", "TAKE_PROFIT_1", "TIMEOUT")

# Fee / slippage assumptions recorded in the frozen protocol (the engine
# still takes the live values from Settings — these document the contract).
FEE_PER_SIDE = 0.00055
SLIPPAGE_PER_SIDE = 0.0005

# Funding settlement model.
FUNDING_SETTLEMENT = "PERIODIC_INTERVAL_NOTIONAL_RATE"

# Capture routes (how the source event reached this contour).
ROUTE_PRE_FILTER_SOURCE = "PRE_FILTER_SOURCE"
CAPTURE_ROUTES = (ROUTE_PRE_FILTER_SOURCE,)


def short_geometry(entry_price: float) -> tuple[float, float]:
    """Return ``(stop_price, target_price)`` for a SHORT at ``entry_price``.

    Both levels are derived from the ACTUAL (slippaged) entry price, exactly
    10% away from it.
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
