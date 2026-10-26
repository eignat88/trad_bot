"""Issue #202 — fail-closed, observe-only SHORT fill feasibility capture.

Capture happens at signal time, never reuses old closed execution observations.
No order submission is possible in this module.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

LOG = logging.getLogger(__name__)
EXPERIMENT = "ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1"


def _datetime(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


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


def calculate_snapshot(reference, stop, book, notional=100, now_ms=None):
    """Pure, testable calculations; invalid/stale source is not tradable."""
    ref, stop = Decimal(str(reference)), Decimal(str(stop))
    if ref <= 0 or stop <= ref:
        raise ValueError("invalid frozen SHORT geometry")
    result = book.get("result") or {}
    bids, asks = result.get("b") or [], result.get("a") or []
    ts_raw = result.get("ts") or book.get("time")
    ts = int(ts_raw) if ts_raw is not None else None
    if not bids or not asks or ts is None:
        return {"status": "INVALID_BOOK", "reason": "missing bid/ask/book timestamp"}
    bid, ask = Decimal(str(bids[0][0])), Decimal(str(asks[0][0]))
    if bid <= 0 or ask < bid:
        return {"status": "INVALID_BOOK", "reason": "crossed/invalid top of book"}
    now = int(now_ms) if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    if ts > now + 1000 or now - ts > 10000:
        return {"status": "STALE_BOOK", "reason": "timestamp outside 10-second capture bound"}
    fill = _estimate_bid_fill(bids, notional)
    status = "OK" if fill is not None else "INSUFFICIENT_DEPTH"
    depth = sum(
        Decimal(str(px)) * Decimal(str(qty)) for px, qty in bids
        if Decimal(str(px)) >= bid * Decimal("0.999")
    )
    spread_bps = (ask - bid) / ((ask + bid) / 2) * 10000
    # SHORT taker entry at bid; adverse entry deviation relative to frozen reference.
    slip = ((ref - fill) / ref * 10000) if fill is not None else None
    # Quote-side estimate only: exit price/cost remain unmeasured.
    return {
        "status": status, "reason": None if fill is not None else "insufficient executable bid depth",
        "bid": bid, "ask": ask, "book_time_ms": ts,
        "spread_bps": spread_bps,
        "reference_to_bid_bps": (ref - bid) / ref * 10000,
        "depth_bid_10bps_usdt": depth,
        "estimated_fill": fill, "entry_slippage_bps": slip,
        "estimated_roundtrip_cost_r": None,
    }


def capture(conn, client, *, scanner_name, direction, symbol, signal_time,
            reference_price, invalidation_price, target_1, notional=100):
    """Persist one signal-time quote; DB gate and feature flag both required."""
    if os.environ.get("ME_SHORT_A_FILL_CAPTURE_ENABLED") != "1":
        return False
    if scanner_name != "MOMENTUM_EXHAUSTION" or direction != "SHORT":
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
        try:
            book = client._public_get(
                "/v5/market/orderbook", category="linear", symbol=symbol, limit=50)
            stats = calculate_snapshot(ref, stop, book, notional=notional)
        except Exception as exc:
            book = {}
            stats = {"status": "ERROR", "reason": type(exc).__name__}
        cur = conn.cursor()
        try:
            cur.execute(
                """INSERT INTO research.me_short_a_fill_feasibility_v1
                (event_key,symbol,signal_time,captured_at,freeze_ts,
                reference_price,stop_price,target_price,bid,ask,book_time_ms,
                spread_bps,reference_to_bid_bps,depth_bid_10bps_usdt,
                requested_notional_usdt,estimated_fill,entry_slippage_bps,
                estimated_roundtrip_cost_r,feasibility_status,reason,raw_snapshot)
                SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                WHERE EXISTS (SELECT 1 FROM research.me_short_a_fill_activation_v1
                  WHERE experiment_id=%s AND status='RUNNING' AND freeze_ts=%s)
                ON CONFLICT(event_key) DO NOTHING""",
                (event_key,symbol,event_time,captured_at,freeze,ref,stop,target,
                 stats.get("bid"),stats.get("ask"),stats.get("book_time_ms"),
                 stats.get("spread_bps"),stats.get("reference_to_bid_bps"),
                 stats.get("depth_bid_10bps_usdt"),Decimal(str(notional)),
                 stats.get("estimated_fill"),stats.get("entry_slippage_bps"),
                 stats.get("estimated_roundtrip_cost_r"),stats["status"],
                 stats.get("reason"),json.dumps(book),EXPERIMENT,freeze))
            conn.commit()
            return True
        finally:
            cur.close()
    except Exception:
        conn.rollback()
        LOG.exception("ME SHORT A fill capture failed closed; no orders")
        return False
