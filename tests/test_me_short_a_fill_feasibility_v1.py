"""Phase 1: quote math + fail-closed activation. No exchange calls."""
import os
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.research.me_short_a_fill_feasibility_v1 import (
    _estimate_bid_fill, calculate_snapshot, capture,
)


def book(ts=100000):
    return {"result": {
        "ts": ts,
        "b": [["99.9", "2"], ["99.8", "3"]],
        "a": [["100.1", "3"]]}}


def test_weighted_short_fill_and_spread():
    out = calculate_snapshot(100, 100.2, book(), notional=300, now_ms=100100)
    assert out["status"] == "OK"
    assert Decimal("99.8") <= out["estimated_fill"] <= Decimal("99.9")
    assert out["spread_bps"] > 0
    assert out["reference_to_bid_bps"] > 0
    assert out["estimated_roundtrip_cost_r"] is None  # exit not observed


def test_insufficient_depth_and_stale_quote():
    out = calculate_snapshot(100, 100.2, book(), notional=10000, now_ms=100100)
    assert out["status"] == "INSUFFICIENT_DEPTH"
    out = calculate_snapshot(100, 100.2, book(), now_ms=120000)
    assert out["status"] == "STALE_BOOK"


def test_invalid_crossed_book_and_geometry():
    bad = book()
    bad["result"]["a"] = [["99.0", "1"]]
    assert calculate_snapshot(100, 100.2, bad, now_ms=100100)["status"] == "INVALID_BOOK"
    try:
        calculate_snapshot(100, 99.9, book(), now_ms=100100)
        assert False
    except ValueError:
        pass


def test_no_capture_when_disabled_or_wrong_direction(monkeypatch):
    monkeypatch.delenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", raising=False)
    db, client = MagicMock(), MagicMock()
    base = dict(scanner_name="MOMENTUM_EXHAUSTION",direction="SHORT",
                symbol="BTCUSDT",signal_time=datetime.now(timezone.utc),
                reference_price=100,invalidation_price=100.2,target_1=99)
    assert capture(db, client, **base) is False
    db.cursor.assert_not_called()
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED", "1")
    base["direction"] = "LONG"
    assert capture(db, client, **base) is False
    client._public_get.assert_not_called()


def test_activation_missing_fails_closed(monkeypatch):
    monkeypatch.setenv("ME_SHORT_A_FILL_CAPTURE_ENABLED","1")
    db,client=MagicMock(),MagicMock()
    db.cursor.return_value.fetchone.return_value=None
    assert capture(db,client,scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT",symbol="BTCUSDT",
        signal_time=datetime.now(timezone.utc),reference_price=100,
        invalidation_price=100.2,target_1=99) is False
    client._public_get.assert_not_called()
