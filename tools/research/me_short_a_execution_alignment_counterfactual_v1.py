"""Issue #206: offline, read-only, signal-aligned ME SHORT A counterfactual.

Inputs are externally exported CSV files. Never queries or modifies production DB.
Only fully CLOSED 5-minute bars whose open is >= signal time are used.
This deliberately excludes the signal's partially observed candle: incomplete
intrabar sequencing cannot be made reliable from OHLC alone.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

D = Decimal
NORMAL_COST = D("0.0021")
ELEVATED_COST = D("0.0031")
HOLD_MINUTES = 240


def ts(value: str) -> datetime:
    result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include timezone")
    return result.astimezone(timezone.utc)


def evaluate(signal: dict, candles: list[dict], *, entry_key: str, cost_rate: Decimal = NORMAL_COST) -> dict:
    symbol = signal["symbol"]
    start = ts(signal["signal_time"])
    end = start + timedelta(minutes=HOLD_MINUTES)
    entry = D(signal[entry_key])
    stop = D(signal["stop_price"])
    target = D(signal["target_price"])
    if not (entry > target > 0 and stop > entry):
        return {"status": "INVALID_GEOMETRY", "model": entry_key}
    risk = stop - entry
    # Exclude incomplete signal candle. Also exclude candles crossing timeout.
    eligible = sorted(
        (c for c in candles if c["symbol"] == symbol
         and c.get("is_closed", "true").lower() in ("true", "t", "1")
         and ts(c["open_time"]) >= start
         and ts(c["open_time"]) + timedelta(minutes=5) <= end),
        key=lambda c: ts(c["open_time"]),
    )
    # Require contiguous 5m coverage through horizon, including timeout candle.
    # Early exits can be finalized with contiguous coverage only through exit bar.
    # First complete 5-minute candle opening strictly after the signal:
    epoch = int(start.timestamp())
    first_open = datetime.fromtimestamp(((epoch // 300) + 1) * 300, timezone.utc)
    cursor = first_open
    for candle in eligible:
        opened = ts(candle["open_time"])
        if opened != cursor:
            return {"status": "MISSING_CANDLE", "model": entry_key, "expected_open": cursor.isoformat()}
        high, low = D(candle["high"]), D(candle["low"])
        hit_stop, hit_tp = high >= stop, low <= target
        if hit_stop or hit_tp:
            path = "AMBIGUOUS_STOP_FIRST" if hit_stop and hit_tp else ("SL" if hit_stop else "TP")
            exit_price = stop if hit_stop else target
            gross = (entry - exit_price) / risk
            net = gross - cost_rate * entry / risk
            return {"status": "FINAL", "model": entry_key, "path": path,
                    "exit_candle": opened.isoformat(), "entry": str(entry),
                    "exit": str(exit_price), "gross_r": str(gross),
                    "net_r": str(net), "net_pnl_pct": str((entry-exit_price)/entry*100-cost_rate*100)}
        cursor += timedelta(minutes=5)
    # A signal at hh:37 has 240m cutoff at hh:37; the final fully closed 5m
    # candle ends at hh:35. A precise timeout close is NOT available from 5m OHLC.
    return {"status": "TIMEOUT_CLOSE_UNAVAILABLE", "model": entry_key,
            "covered_until": cursor.isoformat(), "deadline": end.isoformat()}


def audit(signals: list[dict], candles: list[dict]) -> list[dict]:
    rows = []
    for sig in signals:
        row = {"symbol": sig["symbol"], "signal_time": sig["signal_time"]}
        for label, key in (("frozen", "reference_price"), ("executable", "estimated_fill")):
            if not sig.get(key):
                row[label] = {"status": "MISSING_ENTRY", "model": key}
            else:
                row[label] = evaluate(sig, candles, entry_key=key)
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--candles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.signals.open(newline="") as f:
        signals = list(csv.DictReader(f))
    with args.candles.open(newline="") as f:
        candles = list(csv.DictReader(f))
    results = audit(signals, candles)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"protocol": "ISSUE_206_ME_SHORT_A_V1",
        "assumptions": {"hold_minutes": HOLD_MINUTES, "cost_rate": str(NORMAL_COST),
                        "first_partial_candle": "EXCLUDED",
                        "exit_slippage": "NOT_MODELED", "overlap_portfolio": "NOT_MODELED"},
        "results": results}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"n": len(results),
        "executable_final": sum(r["executable"]["status"] == "FINAL" for r in results),
        "inconclusive": sum(r["executable"]["status"] != "FINAL" for r in results)}))


if __name__ == "__main__":
    main()
