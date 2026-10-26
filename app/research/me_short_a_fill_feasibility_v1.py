"""Issue #202 Phase 1 — fail-closed, observe-only SHORT fill feasibility capture.

``ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1`` records one signal-time
market-microstructure snapshot per eligible new MOMENTUM_EXHAUSTION SHORT
candidate. Capture happens at signal time only; historical backfill is
impossible by construction (strict ``signal_time > freeze_ts`` check plus a DB
``CHECK`` on the same boundary).

Safety invariants:
- No order submission is possible in this module (read-only market endpoints).
- Capture requires BOTH ``ME_SHORT_A_FILL_CAPTURE_ENABLED=1`` and a RUNNING DB
  activation row with an explicit immutable ``freeze_ts``.
- Every write is idempotent on ``event_key`` (sha256 identity of the signal).
- The frozen A signal/exit geometry is never modified here: entry is the
  frozen ``reference_price``, stop is ``invalidation_price`` (``> reference``),
  target is ``target_1`` (``< reference``).

Cost model (frozen, from ``ME_SHORT_GEOM_A_EXECUTION_V1`` registry):
- ``FEE_PER_SIDE`` taker fee per side,
- ``NORMAL_ROUND_TRIP`` / ``ELEVATED_ROUND_TRIP`` frozen round-trip cost
  assumptions used only to CLASSIFY the observed quote-side estimate,
- ``structural_R = invalidation_price - reference_price``; costs in R are
  normalized by ``structural_R`` exactly like the research net-R formulas.

Observed quote-side round-trip cost estimate (documented in the registry):

    cost_pct = (reference - estimated_fill) / reference   # entry adverse dev.
             + (ask - bid) / mid                          # exit crosses spread
             + 2 * FEE_PER_SIDE                          # taker fees
    cost_r   = cost_pct * reference / structural_R
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

LOG = logging.getLogger(__name__)
EXPERIMENT = "ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1"

# Frozen cost constants (ME_SHORT_GEOM_A_EXECUTION_V1). Never retune here.
FEE_PER_SIDE = Decimal("0.00055")
NORMAL_ROUND_TRIP = Decimal("0.0021")
ELEVATED_ROUND_TRIP = Decimal("0.0031")

# Predefined notional/risk-size scenarios (registry: notional_scenarios_usdt).
DEFAULT_NOTIONAL_SCENARIOS = (Decimal("100"), Decimal("500"), Decimal("1000"))

BOOK_STALENESS_LIMIT_MS = 10000
BOOK_FUTURE_SKEW_MS = 1000

# Exchange instrument constraints are static enough to cache per symbol.
_INSTRUMENT_CACHE_TTL_S = 3600.0
_INSTRUMENT_CACHE: dict[str, tuple[float, dict | None, str | None]] = {}

ORDERBOOK_ENDPOINT = "/v5/market/orderbook"
INSTRUMENTS_ENDPOINT = "/v5/market/instruments-info"


def _datetime(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


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
    return value


def _estimate_bid_fill(levels, notional):
    """Weighted sale price into bids for a requested USDT short notional."""
    remaining = Decimal(str(notional))
    qty_total = Decimal("0")
    proceeds = Decimal("0")
    for price_raw, qty_raw in levels:
        price, qty = Decimal(str(price_raw)), Decimal(str(qty_raw))
        if price <= 0 or qty <= 0:
            continue
        take = min(qty, remaining / price)
        proceeds += take * price
        qty_total += take
        remaining -= take * price
        if remaining <= Decimal("0.00000001"):
            break
    if remaining > Decimal("0.00000001") or qty_total <= 0:
        return None
    return proceeds / qty_total


def _round_down_to_step(value, step):
    if step is None or step <= 0:
        return value
    return (value / step).to_integral_value(rounding="ROUND_DOWN") * step


def instrument_constraints(client, symbol, *, now=None):
    """Return ``(constraints_dict | None, error | None)`` for a linear symbol.

    Fail-open on the lookup itself (quote capture must survive a flaky
    instruments endpoint) but fail-closed in reporting: when constraints are
    unknown the per-scenario results are flagged instead of assumed feasible.
    """
    now_s = time.time() if now is None else float(now)
    cached = _INSTRUMENT_CACHE.get(symbol)
    if cached and now_s - cached[0] < _INSTRUMENT_CACHE_TTL_S:
        return cached[1], cached[2]
    constraints = None
    error = None
    try:
        payload = client._public_get(
            INSTRUMENTS_ENDPOINT, category="linear", symbol=symbol)
        rows = (payload.get("result") or {}).get("list") or []
        lot = (rows[0].get("lotSizeFilter") or {}) if rows else {}
        constraints = {
            "min_order_qty": _decimal(lot.get("minOrderQty")),
            "qty_step": _decimal(lot.get("qtyStep")),
            # Bybit field name varies by market type; accept both spellings.
            "min_notional": _decimal(lot.get("minNotional") or lot.get("minOrderAmt")),
        }
    except Exception as exc:
        error = f"instruments_info:{type(exc).__name__}"
    _INSTRUMENT_CACHE[symbol] = (now_s, constraints, error)
    return constraints, error


def _scenario_result(notional, ref, fill, constraints):
    """Per-notional-scenario feasibility, slippage and cost estimate.

    Exchange minimum checks are applied only when constraints are known;
    ``constraints_known=False`` marks rows whose min-qty/min-notional
    feasibility could not be assessed and must be excluded at analysis time.
    """
    notional = Decimal(str(notional))
    requested_qty = notional / ref
    min_qty = (constraints or {}).get("min_order_qty")
    qty_step = (constraints or {}).get("qty_step")
    min_notional = (constraints or {}).get("min_notional")
    executable_qty = _round_down_to_step(requested_qty, qty_step)
    below_min_qty = bool(min_qty is not None and executable_qty < min_qty)
    below_min_notional = bool(
        min_notional is not None and executable_qty * ref < min_notional)
    if below_min_qty or below_min_notional:
        status = "BELOW_EXCHANGE_MIN"
        broken = []
        if below_min_qty:
            broken.append("minOrderQty")
        if below_min_notional:
            broken.append("minNotional")
        reason = "below exchange minimum: " + "+".join(broken)
    elif fill is None:
        status, reason = "INSUFFICIENT_DEPTH", "insufficient executable bid depth"
    else:
        status, reason = "OK", None
    entry_slippage = (ref - fill) / ref * 10000 if fill is not None else None
    return {
        "notional_usdt": notional,
        "requested_qty": requested_qty,
        "executable_qty": executable_qty,
        "constraints_known": constraints is not None,
        "below_min_qty": below_min_qty,
        "below_min_notional": below_min_notional,
        "status": status,
        "reason": reason,
        "estimated_fill": fill,
        "entry_slippage_bps": entry_slippage,
    }


def calculate_snapshot(reference, stop, book, *, notional_scenarios=None,
                       constraints=None, constraints_error=None, now_ms=None,
                       fee_per_side=FEE_PER_SIDE,
                       normal_round_trip=NORMAL_ROUND_TRIP,
                       elevated_round_trip=ELEVATED_ROUND_TRIP,
                       stale_limit_ms=BOOK_STALENESS_LIMIT_MS):
    """Pure, testable calculations; invalid/stale source is not tradable.

    ``notional_scenarios`` is the predefined notional grid; the FIRST scenario
    is the base scenario mirrored on the top-level result keys. Book-level
    failures (invalid/stale) mark every scenario infeasible.
    """
    ref, stop = Decimal(str(reference)), Decimal(str(stop))
    if ref <= 0 or stop <= ref:
        raise ValueError("invalid frozen SHORT geometry")
    structural_r = stop - ref
    scenarios = tuple(
        Decimal(str(item)) for item in
        (notional_scenarios if notional_scenarios is not None
         else DEFAULT_NOTIONAL_SCENARIOS)
    )
    if not scenarios or any(item <= 0 for item in scenarios):
        raise ValueError("notional scenarios must be positive")
    result = book.get("result") or {}
    bids, asks = result.get("b") or [], result.get("a") or []
    ts_raw = result.get("ts") or book.get("time")
    ts = int(ts_raw) if ts_raw is not None else None
    if not bids or not asks or ts is None:
        return {"status": "INVALID_BOOK", "reason": "missing bid/ask/book timestamp",
                "structural_r": structural_r,
                "structural_r_pct": structural_r / ref * 100,
                "scenario_results": []}
    bid, ask = Decimal(str(bids[0][0])), Decimal(str(asks[0][0]))
    if bid <= 0 or ask < bid:
        return {"status": "INVALID_BOOK", "reason": "crossed/invalid top of book",
                "structural_r": structural_r,
                "structural_r_pct": structural_r / ref * 100,
                "scenario_results": []}
    now = int(now_ms) if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    if ts > now + BOOK_FUTURE_SKEW_MS or now - ts > stale_limit_ms:
        return {"status": "STALE_BOOK",
                "reason": f"timestamp outside {stale_limit_ms // 1000}-second capture bound",
                "structural_r": structural_r,
                "structural_r_pct": structural_r / ref * 100,
                "scenario_results": []}
    mid = (ask + bid) / 2
    spread_pct = (ask - bid) / mid
    spread_bps = spread_pct * 10000
    depth = sum(
        Decimal(str(px)) * Decimal(str(qty)) for px, qty in bids
        if Decimal(str(px)) >= bid * Decimal("0.999")
    )
    scenario_results = []
    for notional in scenarios:
        fill = _estimate_bid_fill(bids, notional)
        scenario = _scenario_result(notional, ref, fill, constraints)
        if fill is not None:
            # Quote-side round-trip cost estimate (module docstring / registry).
            cost_pct = (ref - fill) / ref + spread_pct + 2 * fee_per_side
            scenario["estimated_roundtrip_cost_pct"] = cost_pct
            scenario["estimated_roundtrip_cost_r"] = cost_pct * ref / structural_r
            if cost_pct <= normal_round_trip:
                scenario["cost_class"] = "NORMAL"
            elif cost_pct <= elevated_round_trip:
                scenario["cost_class"] = "ELEVATED"
            else:
                scenario["cost_class"] = "ABOVE_ELEVATED"
        else:
            scenario["estimated_roundtrip_cost_pct"] = None
            scenario["estimated_roundtrip_cost_r"] = None
            scenario["cost_class"] = None
        scenario_results.append(scenario)
    base = scenario_results[0]
    # SHORT taker entry at bid; adverse entry deviation relative to frozen reference.
    return {
        "status": base["status"], "reason": base["reason"],
        "bid": bid, "ask": ask, "book_time_ms": ts, "book_age_ms": now - ts,
        "spread_bps": spread_bps,
        "reference_to_bid_bps": (ref - bid) / ref * 10000,
        "depth_bid_10bps_usdt": depth,
        "structural_r": structural_r,
        "structural_r_pct": structural_r / ref * 100,
        "requested_notional_usdt": base["notional_usdt"],
        "requested_qty": base["requested_qty"],
        "executable_qty": base["executable_qty"],
        "estimated_fill": base["estimated_fill"],
        "entry_slippage_bps": base["entry_slippage_bps"],
        "estimated_roundtrip_cost_pct": base["estimated_roundtrip_cost_pct"],
        "estimated_roundtrip_cost_r": base["estimated_roundtrip_cost_r"],
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


def capture(conn, client, *, scanner_name, direction, symbol, signal_time,
            reference_price, invalidation_price, target_1,
            notional_scenarios=None, observation_ids=None):
    """Persist one signal-time quote; DB gate and feature flag both required.

    Returns True only when a new row was inserted. Duplicate ``event_key``
    (idempotent replay) and any gate miss return False without side effects.
    """
    if os.environ.get("ME_SHORT_A_FILL_CAPTURE_ENABLED") != "1":
        return False
    if scanner_name != "MOMENTUM_EXHAUSTION" or direction != "SHORT":
        return False
    if conn is None or client is None:
        return False
    try:
        ref = Decimal(str(reference_price))
        stop = Decimal(str(invalidation_price))
        target = Decimal(str(target_1))
        if not (ref > target > 0 and stop > ref):
            return False
        event_time = _datetime(signal_time)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT status,freeze_ts FROM research.me_short_a_fill_activation_v1 "
                "WHERE experiment_id=%s", (EXPERIMENT,))
            row = cursor.fetchone()
            if not row or row[0] != "RUNNING" or row[1] is None:
                return False
            freeze = _datetime(row[1])
            # Strict post-freeze boundary: eligible iff signal_time > freeze_ts.
            if event_time <= freeze:
                return False
            event_key = hashlib.sha256(
                f"{symbol}|SHORT|{event_time.isoformat()}|{ref}|{stop}|{target}".encode()
            ).hexdigest()
            cursor.execute(
                "SELECT 1 FROM research.me_short_a_fill_feasibility_v1 WHERE event_key=%s",
                (event_key,))
            if cursor.fetchone():
                return False
        finally:
            cursor.close()
        captured_at = datetime.now(timezone.utc)
        constraints, constraints_error = instrument_constraints(client, symbol)
        try:
            book = client._public_get(
                ORDERBOOK_ENDPOINT, category="linear", symbol=symbol, limit=50)
            stats = calculate_snapshot(
                ref, stop, book,
                notional_scenarios=notional_scenarios,
                constraints=constraints, constraints_error=constraints_error)
        except Exception as exc:
            book = {}
            stats = {"status": "ERROR",
                     "reason": f"orderbook:{type(exc).__name__}",
                     "structural_r": stop - ref,
                     "structural_r_pct": (stop - ref) / ref * 100,
                     "scenario_results": [],
                     "constraints": {
                         "known": constraints is not None,
                         "min_order_qty": (constraints or {}).get("min_order_qty"),
                         "qty_step": (constraints or {}).get("qty_step"),
                         "min_notional": (constraints or {}).get("min_notional"),
                         "lookup_error": constraints_error,
                     }}
        constraints_block = stats.get("constraints") or {
            "known": constraints is not None,
            "min_order_qty": (constraints or {}).get("min_order_qty"),
            "qty_step": (constraints or {}).get("qty_step"),
            "min_notional": (constraints or {}).get("min_notional"),
            "lookup_error": constraints_error,
        }
        scenario_json = json.dumps(_jsonable({
            "scenarios": stats.get("scenario_results", []),
            "exchange_constraints": constraints_block,
            "cost_model": {
                "fee_per_side": str(FEE_PER_SIDE),
                "normal_round_trip": str(NORMAL_ROUND_TRIP),
                "elevated_round_trip": str(ELEVATED_ROUND_TRIP),
                "formula": "cost_pct=(ref-fill)/ref + spread + 2*fee; cost_r=cost_pct*ref/structural_r",
            },
        }))
        cur = conn.cursor()
        try:
            cur.execute(
                """INSERT INTO research.me_short_a_fill_feasibility_v1
                (event_key,symbol,signal_time,captured_at,freeze_ts,
                reference_price,stop_price,target_price,structural_r,structural_r_pct,
                bid,ask,book_time_ms,book_age_ms,
                spread_bps,reference_to_bid_bps,depth_bid_10bps_usdt,
                requested_notional_usdt,requested_qty,executable_qty,
                min_order_qty,qty_step,min_notional,
                estimated_fill,entry_slippage_bps,
                estimated_roundtrip_cost_pct,estimated_roundtrip_cost_r,cost_class,
                feasibility_status,reason,scenario_results,source_observation_ids,
                raw_snapshot)
                SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb
                WHERE EXISTS (SELECT 1 FROM research.me_short_a_fill_activation_v1
                  WHERE experiment_id=%s AND status='RUNNING' AND freeze_ts=%s)
                ON CONFLICT(event_key) DO NOTHING""",
                (event_key, symbol, event_time, captured_at, freeze,
                 ref, stop, target,
                 stats.get("structural_r"), stats.get("structural_r_pct"),
                 stats.get("bid"), stats.get("ask"), stats.get("book_time_ms"),
                 stats.get("book_age_ms"),
                 stats.get("spread_bps"), stats.get("reference_to_bid_bps"),
                 stats.get("depth_bid_10bps_usdt"),
                 stats.get("requested_notional_usdt"), stats.get("requested_qty"),
                 stats.get("executable_qty"),
                 constraints_block.get("min_order_qty"),
                 constraints_block.get("qty_step"),
                 constraints_block.get("min_notional"),
                 stats.get("estimated_fill"), stats.get("entry_slippage_bps"),
                 stats.get("estimated_roundtrip_cost_pct"),
                 stats.get("estimated_roundtrip_cost_r"), stats.get("cost_class"),
                 stats["status"], stats.get("reason"), scenario_json,
                 json.dumps(_jsonable(observation_ids)) if observation_ids else None,
                 json.dumps(book),
                 EXPERIMENT, freeze))
            conn.commit()
            # True only for a real insert: idempotent replays and activation
            # races (WHERE EXISTS guard) never claim a capture.
            return cur.rowcount == 1
        finally:
            cur.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            LOG.debug("ME SHORT A fill capture rollback failed", exc_info=True)
        LOG.exception("ME SHORT A fill capture failed closed; no orders")
        return False
