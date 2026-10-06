from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

sys.path.insert(0, r"D:\py_pro\trad_bot")
import requests

OUT = Path(r"D:\py_pro\trad_bot\research_snapshot\htf_point_b_30d")
URL = "https://api.bybit.com/v5/market/kline"
START_MS = int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp() * 1000)
END_MS = int(datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp() * 1000)
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT", "NEARUSDT", "ZECUSDT",
    "ENAUSDT", "UNIUSDT", "ARBUSDT", "1000PEPEUSDT", "PUMPFUNUSDT", "FARTCOINUSDT",
    "HYPEUSDT", "USELESSUSDT", "SNDKUSDT", "SPCXUSDT", "DOGEUSDT", "ONDOUSDT", "LINKUSDT",
]
MAX_PAGES = 3


def state_path(symbol: str) -> Path:
    return OUT / f"{symbol}_state.json"


def data_path(symbol: str) -> Path:
    return OUT / f"{symbol}_5m.csv"


def load_state(symbol: str) -> dict:
    path = state_path(symbol)
    if not path.exists():
        return {"symbol": symbol, "next_end_ms": END_MS, "pages_completed": 0, "completed": False}
    return json.loads(path.read_text())


def save_state(symbol: str, state: dict) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    path = state_path(symbol)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def load_rows(symbol: str) -> dict[int, tuple]:
    path = data_path(symbol)
    if not path.exists():
        return {}
    rows = {}
    with path.open(newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for raw in reader:
            if len(raw) == 6:
                rows[int(raw[0])] = (int(raw[0]), *[float(x) for x in raw[1:6]])
    return rows


def save_rows(symbol: str, rows: dict[int, tuple]) -> None:
    path = data_path(symbol)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts in sorted(rows):
            writer.writerow([ts, *rows[ts][1:]])
    tmp.replace(path)


def fetch_page(symbol: str, end_ms: int) -> list[tuple[int, float, float, float, float, float]]:
    params = {"category": "linear", "symbol": symbol, "interval": "5", "limit": 1000, "end": end_ms}
    response = requests.get(URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(payload.get("retMsg"))
    rows = [(int(r[0]), *[float(x) for x in r[1:6]]) for r in payload["result"]["list"]]
    rows.sort(key=lambda row: row[0])
    return rows


def process_symbol(symbol: str) -> None:
    state = load_state(symbol)
    if state["completed"]:
        return
    rows = load_rows(symbol)
    pages = 0
    while pages < MAX_PAGES and not state["completed"]:
        page_rows = fetch_page(symbol, state["next_end_ms"])
        pages += 1
        state["pages_completed"] += 1
        if not page_rows:
            state["completed"] = True
            break
        for row in page_rows:
            if START_MS <= row[0] < END_MS:
                rows[row[0]] = row
        oldest = page_rows[0][0]
        next_end = oldest - 1
        if next_end >= state["next_end_ms"]:
            raise RuntimeError("CHECKPOINT_INCONSISTENT")
        state["next_end_ms"] = next_end
        state["oldest_collected_ms"] = min(rows)
        state["newest_collected_ms"] = max(rows)
        save_rows(symbol, rows)
        save_state(symbol, state)
        if oldest <= START_MS:
            state["completed"] = True
            save_state(symbol, state)
            break
        sleep(0.03)


def coverage(rows: dict[int, tuple]) -> dict:
    timestamps = sorted(rows)
    duplicates = 0
    missing = 8640 - len(rows)
    gaps = [(timestamps[i] - timestamps[i - 1]) // 60000 for i in range(1, len(timestamps))]
    buckets: dict[int, list[int]] = defaultdict(list)
    for ts in timestamps:
        buckets[ts // 3600000 * 3600000].append(ts)
    complete = incomplete = 0
    for group in buckets.values():
        if len(group) == 12 and len(set(group)) == 12 and group[-1] == group[0] + 55 * 60000:
            complete += 1
        else:
            incomplete += 1
    return {
        "rows": len(rows),
        "missing": missing,
        "duplicates": duplicates,
        "largest_gap_min": max(gaps) if gaps else 0,
        "first": timestamps[0] if timestamps else None,
        "last": timestamps[-1] if timestamps else None,
        "complete_1h": complete,
        "incomplete_1h": incomplete,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    summary = []
    for symbol in SYMBOLS:
        try:
            process_symbol(symbol)
            state = load_state(symbol)
            rows = load_rows(symbol)
            cov = coverage(rows)
            status = "VALID" if state["completed"] and cov["rows"] >= 8208 and cov["duplicates"] == 0 else "GAPPY"
            summary.append({"symbol": symbol, "pages": state["pages_completed"], "completed": state["completed"], **cov, "status": status})
        except Exception as exc:
            summary.append({"symbol": symbol, "status": "FAILED", "error": str(exc)})
    combined = OUT / "htf_point_b_30d.csv"
    with combined.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "timestamp", "open", "high", "low", "close", "volume"])
        for symbol in sorted(SYMBOLS):
            for row in load_rows(symbol).values():
                writer.writerow([symbol, *row])
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
