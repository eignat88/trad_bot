from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

import requests

OUT = Path(r"D:\py_pro\trad_bot\research_snapshot\htf_point_b_warmup_14d")

URL = "https://api.bybit.com/v5/market/kline"

START_MS = int(datetime(2026, 8, 22, tzinfo=timezone.utc).timestamp() * 1000)
END_MS   = int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp() * 1000)

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "SUIUSDT",
    "NEARUSDT",
    "ZECUSDT",
    "ENAUSDT",
    "UNIUSDT",
    "ARBUSDT",
]

EXPECTED_ROWS = 14 * 24 * 12   # 4032
EXPECTED_1H = 14 * 24          # 336


def fetch_page(symbol: str, end_ms: int):
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": "5",
        "limit": 1000,
        "end": end_ms,
    }

    r = requests.get(URL, params=params, timeout=30)
    r.raise_for_status()

    payload = r.json()

    if payload.get("retCode") != 0:
        raise RuntimeError(
            f"{symbol}: {payload.get('retCode')} {payload.get('retMsg')}"
        )

    rows = [
        (int(x[0]), *[float(v) for v in x[1:6]])
        for x in payload["result"]["list"]
    ]

    rows.sort(key=lambda x: x[0])
    return rows


def save_rows(symbol: str, rows: dict[int, tuple]):
    path = OUT / f"{symbol}_5m.csv"
    tmp = path.with_suffix(".tmp")

    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])

        for ts in sorted(rows):
            w.writerow(rows[ts])

    tmp.replace(path)


def validate(symbol: str, rows: dict[int, tuple]):
    ts = sorted(rows)

    expected = list(range(START_MS, END_MS, 5 * 60 * 1000))

    missing = sorted(set(expected) - set(ts))
    extra = sorted(set(ts) - set(expected))

    buckets = {}

    for t in ts:
        h = t // 3600000 * 3600000
        buckets.setdefault(h, []).append(t)

    complete_1h = 0
    incomplete_1h = 0

    for h, values in buckets.items():
        values.sort()

        if (
            len(values) == 12
            and len(set(values)) == 12
            and values[0] == h
            and values[-1] == h + 55 * 60 * 1000
        ):
            complete_1h += 1
        else:
            incomplete_1h += 1

    result = {
        "symbol": symbol,
        "rows": len(ts),
        "expected_rows": EXPECTED_ROWS,
        "missing": len(missing),
        "extra": len(extra),
        "duplicates": len(ts) - len(set(ts)),
        "complete_1h": complete_1h,
        "expected_1h": EXPECTED_1H,
        "incomplete_1h": incomplete_1h,
        "first": ts[0] if ts else None,
        "last": ts[-1] if ts else None,
    }

    result["valid"] = (
        result["rows"] == EXPECTED_ROWS
        and result["missing"] == 0
        and result["extra"] == 0
        and result["duplicates"] == 0
        and result["complete_1h"] == EXPECTED_1H
        and result["incomplete_1h"] == 0
    )

    return result


def process(symbol: str):
    print(f"\nFETCH {symbol}", flush=True)

    rows = {}
    end_ms = END_MS - 1
    pages = 0

    while True:
        page = fetch_page(symbol, end_ms)

        if not page:
            break

        pages += 1

        for row in page:
            ts = row[0]

            if START_MS <= ts < END_MS:
                rows[ts] = row

        oldest = page[0][0]

        print(
            f"  page={pages} oldest={oldest} collected={len(rows)}",
            flush=True,
        )

        if oldest <= START_MS:
            break

        end_ms = oldest - 1
        sleep(0.05)

    save_rows(symbol, rows)

    result = validate(symbol, rows)
    result["pages"] = pages

    print(json.dumps(result, indent=2), flush=True)

    if not result["valid"]:
        raise RuntimeError(f"WARMUP_INVALID {symbol}: {result}")

    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    summary = []

    for symbol in SYMBOLS:
        summary.append(process(symbol))

    (OUT / "summary.json").write_text(
        json.dumps(summary, indent=2)
    )

    print("\n========================================")
    print("WARMUP FETCH COMPLETE")
    print("========================================")

    print(
        json.dumps(
            {
                "symbols": len(summary),
                "valid": sum(1 for x in summary if x["valid"]),
                "rows_total": sum(x["rows"] for x in summary),
                "complete_1h_total": sum(x["complete_1h"] for x in summary),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
